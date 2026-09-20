/**
 * Polígono AI Hub - Settings (renderer side)
 * ==========================================
 *
 * Mirrors the persisted settings (src/settings.js, held by the main process)
 * into the controls, and writes every change straight back. The main process
 * stays the authority: it validates what it receives and it is what the motor
 * reads, so nothing here has to know the defaults or the valid ranges.
 *
 * The hidden `opt-*` selects are kept in sync as well, because queue.js reads
 * the current mode from them.
 */

// Last known settings, as the main process returned them.
let appSettings = null;

// Whether the configured output folder was still reachable last time we
// asked. Kept so a retranslation can redraw the path without re-checking.
let outputDirUsable = true;

// What each format can carry. Replaced by the motor's own table on 'ready',
// so the UI never drifts from engine/presets.py.
let formatCapabilities = {
    wav: { bitDepths: [16, 24, 32], defaultBitDepth: 32 },
    flac: { bitDepths: [16, 24], defaultBitDepth: 24 },
    mp3: { bitDepths: [], defaultBitDepth: null },
};

// Relative processing cost per preset, for the "4×" labels. Also replaced on
// 'ready'.
let presetCosts = { fast: 1, hq: 4, ultra: 16 };

function getSettings() {
    return appSettings;
}

/**
 * Bit depth currently selected for a format (null for the lossy ones).
 */
function bitDepthForFormat(format) {
    if (!appSettings) return null;
    if (format === 'wav') return appSettings.wavBitDepth;
    if (format === 'flac') return appSettings.flacBitDepth;
    return null;
}

/**
 * Rebuild the bit depth options for the selected format, and hide the whole
 * group for formats that have no word length to choose.
 */
function renderBitDepthOptions() {
    const format = elements.optFormat.value;
    const caps = formatCapabilities[format] || { bitDepths: [] };
    const depths = caps.bitDepths || [];
    const select = elements.optBitDepth;

    select.replaceChildren();
    elements.groupBitDepth.classList.toggle('hidden', depths.length === 0);
    if (depths.length === 0) return;

    const dict = getDict();
    // Deepest first: 32-bit float is the default for WAV and the only choice
    // that adds no quantisation at all.
    for (const depth of [...depths].sort((a, b) => b - a)) {
        const option = document.createElement('option');
        option.value = String(depth);
        option.textContent = depth === 32
            ? dict.bit_depth_32
            : interpolate(dict.bit_depth_int, { depth });
        select.appendChild(option);
    }

    const current = bitDepthForFormat(format);
    select.value = String(depths.includes(current) ? current : caps.defaultBitDepth);
}

/**
 * Label the quality options with their cost relative to "fast".
 */
function renderPresetLabels() {
    const dict = getDict();
    for (const select of [elements.settingsQuality, elements.optQuality]) {
        for (const option of select.options) {
            const cost = presetCosts[option.value];
            if (!cost) continue;
            const base = dict[`preset_name_${option.value}`] || option.value;
            option.textContent = interpolate(dict.preset_label, { name: base, cost });
        }
    }
}

/**
 * Whether a custom output folder is both chosen and reachable. The picker
 * opens on its own when it is not, so a path that went stale can never leave
 * the dropdown stuck on an option the settings would refuse to save.
 */
function hasUsableOutputDir() {
    return Boolean(appSettings && appSettings.outputDir) && outputDirUsable;
}

/**
 * Show where the output is going.
 */
function renderOutputDir(usable = outputDirUsable) {
    outputDirUsable = usable !== false;
    const dict = getDict();
    const custom = appSettings && appSettings.outputMode === 'folder';

    elements.optOutputMode.value = custom ? 'folder' : 'beside';
    elements.btnChooseOutput.classList.toggle('hidden', !custom);

    if (!custom) {
        elements.outputDirPath.textContent = dict.settings_output_beside_short;
        elements.outputDirPath.classList.remove('output-dir-path--missing');
        elements.outputDirPath.removeAttribute('title');
        return;
    }

    const dir = appSettings.outputDir || '';
    elements.outputDirPath.textContent = dir || dict.settings_output_not_set;
    elements.outputDirPath.setAttribute('title', dir);
    // A folder that has gone away (unplugged drive, renamed path) must say so
    // rather than let jobs quietly land somewhere else.
    elements.outputDirPath.classList.toggle('output-dir-path--missing', !outputDirUsable);
}

/**
 * Push the current settings into every control.
 */
function applySettingsToControls(usable) {
    if (!appSettings) return;

    elements.optMode.value = appSettings.mode;
    elements.optFormat.value = appSettings.format;
    elements.optModel.value = appSettings.model;
    elements.optQuality.value = appSettings.preset;
    elements.optDevice.value = appSettings.device;

    elements.settingsModel.value = appSettings.model;
    elements.settingsQuality.value = appSettings.preset;
    elements.settingsDevice.value = appSettings.device;
    elements.settingsLanguage.value = appSettings.language;
    elements.settingsMono.value = String(appSettings.monoOutput);
    elements.settingsChunk.value = String(appSettings.chunkMinutes);

    renderPresetLabels();
    renderBitDepthOptions();
    renderOutputDir(usable);
}

/**
 * Persist a patch and refresh the controls from what was accepted.
 */
async function updateSettings(patch) {
    const result = await bridge.saveSettings(patch);
    appSettings = result.settings;
    applySettingsToControls(result.outputDirUsable);
    return appSettings;
}

/**
 * Read the settings at boot and apply them. Migrates the language preference
 * that older builds kept in localStorage.
 */
async function initSettings() {
    const result = await bridge.getSettings();
    appSettings = result.settings;

    const legacyLanguage = localStorage.getItem('language');
    if (legacyLanguage && legacyLanguage !== appSettings.language) {
        appSettings = await updateSettings({ language: legacyLanguage });
    }
    localStorage.removeItem('language');

    applySettingsToControls(result.outputDirUsable);
    updateLanguage(appSettings.language);
    return appSettings;
}

/**
 * Adopt the tables the motor reports on 'ready', so the labels and the bit
 * depth choices come from engine/presets.py rather than from this file.
 */
function adoptMotorCapabilities(ready) {
    if (ready && ready.formats) {
        formatCapabilities = {};
        for (const [name, spec] of Object.entries(ready.formats)) {
            formatCapabilities[name] = {
                bitDepths: spec.bitDepths || [],
                defaultBitDepth: spec.defaultBitDepth,
            };
        }
    }
    if (ready && ready.presets) {
        presetCosts = {};
        for (const [name, spec] of Object.entries(ready.presets)) {
            presetCosts[name] = spec.relativeCost;
        }
    }
    renderPresetLabels();
    renderBitDepthOptions();
}
