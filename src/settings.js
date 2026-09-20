/**
 * Polígono AI Hub - Persistent settings
 * =====================================
 *
 * A small JSON file in the user's data directory. No dependency, no schema
 * library: a table of fields, each with a default and a validator, and
 * anything that does not pass falls back to the default rather than reaching
 * the motor.
 *
 * Reads are cached; writes are atomic (temp file + rename) so a crash or a
 * power cut can never leave a half-written settings file that would wipe the
 * user's preferences on the next launch.
 */

const { app } = require('electron');
const fs = require('fs');
const path = require('path');

const FILE_NAME = 'settings.json';

const oneOf = (...values) => (value) => (values.includes(value) ? value : undefined);

const integerIn = (...values) => (value) => {
    const parsed = Number(value);
    return values.includes(parsed) ? parsed : undefined;
};

const numberBetween = (min, max) => (value) => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed) || parsed < min || parsed > max) return undefined;
    return parsed;
};

const boolean = (value) => (typeof value === 'boolean' ? value : undefined);

const nonEmptyString = (value) => {
    if (typeof value !== 'string') return undefined;
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : undefined;
};

const stringOrNull = (value) => {
    if (value === null) return null;
    return nonEmptyString(value);
};

/**
 * Every persisted field. `validate` returns the accepted value, or undefined
 * to fall back to the default.
 *
 * Bit depth is stored per format: WAV defaults to 32-bit float (no
 * quantisation at all) and FLAC to 24-bit, which is the deepest it carries.
 */
const FIELDS = {
    mode: { fallback: 'vocal_remover', validate: oneOf('vocal_remover', 'splitter') },
    preset: { fallback: 'hq', validate: oneOf('fast', 'hq', 'ultra') },
    model: { fallback: 'htdemucs_ft', validate: oneOf('htdemucs_ft', 'htdemucs', 'mdx_extra') },
    device: { fallback: 'auto', validate: oneOf('auto', 'cuda', 'cpu') },
    format: { fallback: 'wav', validate: oneOf('wav', 'flac', 'mp3') },
    wavBitDepth: { fallback: 32, validate: integerIn(16, 24, 32) },
    flacBitDepth: { fallback: 24, validate: integerIn(16, 24) },
    // 'beside' writes next to the source file, 'folder' uses outputDir.
    outputMode: { fallback: 'beside', validate: oneOf('beside', 'folder') },
    outputDir: { fallback: null, validate: stringOrNull },
    monoOutput: { fallback: false, validate: boolean },
    // 0 turns chunking off. Keep in step with CHUNK_MINUTES in
    // engine/presets.py, where the RAM measurements behind it are recorded.
    chunkMinutes: { fallback: 3, validate: numberBetween(0, 180) },
    language: { fallback: 'en', validate: oneOf('en', 'es') },
};

let cache = null;
let filePath = null;

function settingsPath() {
    if (!filePath) {
        // POLIGONO_SETTINGS_DIR points the file somewhere else for the tests.
        // The app itself never sets it.
        const dir = process.env.POLIGONO_SETTINGS_DIR || app.getPath('userData');
        filePath = path.join(dir, FILE_NAME);
    }
    return filePath;
}

function defaults() {
    const result = {};
    for (const [key, field] of Object.entries(FIELDS)) {
        result[key] = field.fallback;
    }
    return result;
}

/**
 * Coerce an arbitrary object into a valid settings object.
 * Unknown keys are dropped; invalid values fall back to their default.
 */
function sanitize(raw) {
    const result = defaults();
    if (!raw || typeof raw !== 'object') return result;

    for (const [key, field] of Object.entries(FIELDS)) {
        if (!(key in raw)) continue;
        const accepted = field.validate(raw[key]);
        if (accepted !== undefined) {
            result[key] = accepted;
        }
    }

    // A folder that no longer exists (an unplugged drive, a renamed path)
    // must not send every job to an unwritable location -- and it must not
    // linger either. A stale path that fails every later validation is what
    // jams the picker: "a folder is set" and "that folder can be used" stop
    // meaning the same thing, so the UI reads the first, tries to switch to
    // folder mode, and this function keeps bouncing it back to 'beside'.
    // Clearing both together keeps the two in step: folder mode is only ever
    // persisted next to a directory that is actually there.
    if (!isUsableDirectory(result.outputDir)) {
        result.outputDir = null;
        result.outputMode = 'beside';
    }
    return result;
}

function isUsableDirectory(dir) {
    if (typeof dir !== 'string' || !dir) return false;
    try {
        return fs.statSync(dir).isDirectory();
    } catch (err) {
        return false;
    }
}

/**
 * Drop keys whose value is undefined.
 *
 * Spreading a patch over the current settings would otherwise let an absent
 * field (`{ mode: undefined }`) overwrite a good value, and since undefined
 * fails every validator the field would silently drop back to its default
 * instead of keeping what the user had chosen.
 */
function definedOnly(patch) {
    const result = {};
    for (const [key, value] of Object.entries(patch || {})) {
        if (value !== undefined) result[key] = value;
    }
    return result;
}

/**
 * Current settings, read from disk once and cached afterwards.
 */
function load() {
    if (cache) return { ...cache };

    let raw = null;
    try {
        raw = JSON.parse(fs.readFileSync(settingsPath(), 'utf8'));
    } catch (err) {
        // Missing on first launch, or corrupted: defaults either way.
        if (err.code !== 'ENOENT') {
            console.warn('[Settings] Could not read settings, using defaults:', err.message);
        }
    }

    cache = sanitize(raw);
    return { ...cache };
}

/**
 * Merge a patch into the settings and persist them.
 * Returns the full settings object as it now stands.
 */
function save(patch) {
    const merged = sanitize({ ...load(), ...definedOnly(patch) });
    cache = merged;

    const target = settingsPath();
    const temp = `${target}.tmp`;
    try {
        fs.mkdirSync(path.dirname(target), { recursive: true });
        fs.writeFileSync(temp, JSON.stringify(merged, null, 2), 'utf8');
        fs.renameSync(temp, target);
    } catch (err) {
        console.error('[Settings] Could not save settings:', err.message);
        try {
            fs.rmSync(temp, { force: true });
        } catch (cleanupError) {
            // Nothing else to do; the previous file is still intact.
        }
    }
    return { ...merged };
}

/**
 * Bit depth for a format, or null for the lossy ones.
 */
function bitDepthFor(settings, format) {
    if (format === 'wav') return settings.wavBitDepth;
    if (format === 'flac') return settings.flacBitDepth;
    return null;
}

/**
 * Where a job's output folder should go, given its input file.
 * Falls back to the input's own directory whenever the configured folder is
 * unusable, so a job never fails because of a stale setting.
 */
function resolveOutputDir(settings, inputPath) {
    if (settings.outputMode === 'folder' && isUsableDirectory(settings.outputDir)) {
        return settings.outputDir;
    }
    return path.dirname(inputPath);
}

module.exports = {
    FIELDS,
    defaults,
    definedOnly,
    load,
    save,
    sanitize,
    bitDepthFor,
    resolveOutputDir,
    isUsableDirectory,
};
