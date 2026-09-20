/**
 * Tests for src/settings.js
 * =========================
 *
 * Run with: npm run test:js
 *
 * The module is reloaded per test because it memoises both the settings cache
 * and the resolved file path; a fresh require is the cheapest way to get a
 * clean process-like state. POLIGONO_SETTINGS_DIR keeps every write inside a
 * temp directory, so nothing here can touch the real settings file.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const MODULE_PATH = require.resolve('../src/settings.js');

/**
 * A temp directory that disappears when the test process exits.
 */
function tempDir(label) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), `poligono-${label}-`));
    test.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    return dir;
}

/**
 * Load settings.js with its file pointed at `dir`, with no cached state.
 */
function freshSettings(dir) {
    process.env.POLIGONO_SETTINGS_DIR = dir;
    delete require.cache[MODULE_PATH];
    return require(MODULE_PATH);
}

/**
 * Write a settings.json by hand, as a previous build would have left it.
 */
function seed(dir, contents) {
    fs.writeFileSync(
        path.join(dir, 'settings.json'),
        typeof contents === 'string' ? contents : JSON.stringify(contents),
        'utf8',
    );
}

// =====================================================================
// sanitize
// =====================================================================

test('sanitize fills in every field from the defaults', () => {
    const settings = freshSettings(tempDir('sanitize'));
    assert.deepStrictEqual(settings.sanitize({}), settings.defaults());
    assert.deepStrictEqual(settings.sanitize(null), settings.defaults());
    assert.deepStrictEqual(settings.sanitize('nonsense'), settings.defaults());
});

test('sanitize drops unknown keys and replaces invalid values', () => {
    const settings = freshSettings(tempDir('sanitize-invalid'));
    const result = settings.sanitize({
        mode: 'splitter',
        preset: 'nonsense',
        wavBitDepth: 12,
        chunkMinutes: 500,
        monoOutput: 'yes',
        somethingElse: 1,
    });

    assert.strictEqual(result.mode, 'splitter', 'a valid value survives');
    assert.strictEqual(result.preset, 'hq');
    assert.strictEqual(result.wavBitDepth, 32);
    assert.strictEqual(result.chunkMinutes, 3);
    assert.strictEqual(result.monoOutput, false);
    assert.ok(!('somethingElse' in result), 'unknown keys are dropped');
});

test('sanitize keeps folder mode when the folder is there', () => {
    const dir = tempDir('sanitize-ok');
    const settings = freshSettings(dir);
    const result = settings.sanitize({ outputMode: 'folder', outputDir: dir });

    assert.strictEqual(result.outputMode, 'folder');
    assert.strictEqual(result.outputDir, dir);
});

test('sanitize clears both fields when the folder has gone', () => {
    const dir = tempDir('sanitize-gone');
    const settings = freshSettings(dir);
    const result = settings.sanitize({
        outputMode: 'folder',
        outputDir: path.join(dir, 'renamed-away'),
    });

    assert.strictEqual(result.outputMode, 'beside');
    assert.strictEqual(result.outputDir, null, 'the dead path must not linger');
});

test('sanitize clears a dead path even when the mode already reads beside', () => {
    // The exact shape the bug left on disk: mode reset, path kept. Read back
    // as-is it makes "a folder is set" true while folder mode is unreachable.
    const dir = tempDir('sanitize-mixed');
    const settings = freshSettings(dir);
    const result = settings.sanitize({
        outputMode: 'beside',
        outputDir: path.join(dir, 'renamed-away'),
    });

    assert.strictEqual(result.outputMode, 'beside');
    assert.strictEqual(result.outputDir, null);
});

test('sanitize refuses folder mode with no folder at all', () => {
    const settings = freshSettings(tempDir('sanitize-null'));
    const result = settings.sanitize({ outputMode: 'folder', outputDir: null });

    assert.strictEqual(result.outputMode, 'beside');
    assert.strictEqual(result.outputDir, null);
});

test('sanitize rejects a file posing as an output folder', () => {
    const dir = tempDir('sanitize-file');
    const asFile = path.join(dir, 'not-a-folder.wav');
    fs.writeFileSync(asFile, '', 'utf8');

    const settings = freshSettings(dir);
    const result = settings.sanitize({ outputMode: 'folder', outputDir: asFile });

    assert.strictEqual(result.outputMode, 'beside');
    assert.strictEqual(result.outputDir, null);
});

// =====================================================================
// load
// =====================================================================

test('load returns defaults when there is no file', () => {
    const settings = freshSettings(tempDir('load-missing'));
    assert.deepStrictEqual(settings.load(), settings.defaults());
});

test('load returns defaults when the file is corrupted', () => {
    const dir = tempDir('load-corrupt');
    seed(dir, '{ this is not json');

    const settings = freshSettings(dir);
    assert.deepStrictEqual(settings.load(), settings.defaults());
});

test('load recovers from the state the bug left behind', () => {
    // Verbatim from a reproduction: folder renamed in Explorer, app reopened.
    const dir = tempDir('load-stale');
    seed(dir, {
        mode: 'splitter',
        preset: 'hq',
        outputMode: 'beside',
        outputDir: path.join(dir, 'test2448'),
        language: 'en',
    });

    const settings = freshSettings(dir);
    const loaded = settings.load();

    assert.strictEqual(loaded.outputMode, 'beside');
    assert.strictEqual(loaded.outputDir, null, 'boot must leave a usable state');
    assert.strictEqual(loaded.mode, 'splitter', 'unrelated preferences survive');
});

// =====================================================================
// save
// =====================================================================

test('save persists a chosen folder and it survives a reload', () => {
    const dir = tempDir('save-folder');
    const chosen = path.join(dir, 'stems out');
    fs.mkdirSync(chosen);

    const settings = freshSettings(dir);
    const saved = settings.save({ outputDir: chosen, outputMode: 'folder' });

    assert.strictEqual(saved.outputMode, 'folder');
    assert.strictEqual(saved.outputDir, chosen);

    const reloaded = freshSettings(dir).load();
    assert.strictEqual(reloaded.outputMode, 'folder');
    assert.strictEqual(reloaded.outputDir, chosen);
});

test('choosing a valid folder works from the stuck state', () => {
    // The regression: a dead path on disk used to make every later attempt at
    // folder mode bounce straight back to 'beside'.
    const dir = tempDir('save-recover');
    seed(dir, {
        outputMode: 'beside',
        outputDir: path.join(dir, 'gone-for-good'),
    });

    const settings = freshSettings(dir);
    assert.strictEqual(settings.load().outputDir, null);

    const chosen = path.join(dir, 'a new folder');
    fs.mkdirSync(chosen);
    const saved = settings.save({ outputDir: chosen, outputMode: 'folder' });

    assert.strictEqual(saved.outputMode, 'folder');
    assert.strictEqual(saved.outputDir, chosen);
});

test('save keeps folder mode across an unrelated change', () => {
    const dir = tempDir('save-unrelated');
    const chosen = path.join(dir, 'keep me');
    fs.mkdirSync(chosen);

    const settings = freshSettings(dir);
    settings.save({ outputDir: chosen, outputMode: 'folder' });
    const saved = settings.save({ format: 'flac' });

    assert.strictEqual(saved.format, 'flac');
    assert.strictEqual(saved.outputMode, 'folder');
    assert.strictEqual(saved.outputDir, chosen);
});

test('save drops folder mode once the folder disappears mid-session', () => {
    const dir = tempDir('save-vanish');
    const chosen = path.join(dir, 'doomed');
    fs.mkdirSync(chosen);

    const settings = freshSettings(dir);
    settings.save({ outputDir: chosen, outputMode: 'folder' });
    fs.rmSync(chosen, { recursive: true });

    const saved = settings.save({ preset: 'fast' });
    assert.strictEqual(saved.outputMode, 'beside');
    assert.strictEqual(saved.outputDir, null);
});

test('save leaves no temp file behind', () => {
    const dir = tempDir('save-temp');
    const settings = freshSettings(dir);
    settings.save({ mode: 'splitter' });

    assert.deepStrictEqual(fs.readdirSync(dir), ['settings.json']);
});

// =====================================================================
// definedOnly / resolveOutputDir / bitDepthFor
// =====================================================================

test('definedOnly drops undefined so a patch cannot wipe a good value', () => {
    const settings = freshSettings(tempDir('defined'));
    assert.deepStrictEqual(
        settings.definedOnly({ mode: undefined, preset: 'fast', model: null }),
        { preset: 'fast', model: null },
    );
});

test('resolveOutputDir uses the folder only while it is usable', () => {
    const dir = tempDir('resolve');
    const settings = freshSettings(dir);
    const input = path.join(dir, 'songs', 'track.wav');

    assert.strictEqual(
        settings.resolveOutputDir({ outputMode: 'folder', outputDir: dir }, input),
        dir,
    );
    assert.strictEqual(
        settings.resolveOutputDir({ outputMode: 'beside', outputDir: dir }, input),
        path.join(dir, 'songs'),
    );
    assert.strictEqual(
        settings.resolveOutputDir(
            { outputMode: 'folder', outputDir: path.join(dir, 'gone') },
            input,
        ),
        path.join(dir, 'songs'),
        'a job never fails because of a stale setting',
    );
});

test('bitDepthFor reports per format, and null for the lossy ones', () => {
    const settings = freshSettings(tempDir('depth'));
    const current = { wavBitDepth: 24, flacBitDepth: 16 };

    assert.strictEqual(settings.bitDepthFor(current, 'wav'), 24);
    assert.strictEqual(settings.bitDepthFor(current, 'flac'), 16);
    assert.strictEqual(settings.bitDepthFor(current, 'mp3'), null);
});
