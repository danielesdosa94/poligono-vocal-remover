/**
 * Polígono AI Hub - Renderer bridge
 * =================================
 *
 * Thin wrapper over `window.api` (see src/preload.js) plus the few helpers
 * that used to come from Node's `path` when the renderer had nodeIntegration.
 *
 * Nothing else in the renderer talks to `window.api` directly.
 */

// contextBridge defines `window.api` as a non-configurable global property,
// so a top-level `const api` here would be a redeclaration SyntaxError.
const preloadApi = window.api;

/**
 * Last path component, for both Windows and POSIX separators.
 * Replaces path.basename(), which is no longer reachable from the renderer.
 */
function basename(filePath) {
    const parts = String(filePath ?? '').split(/[\\/]/);
    return parts[parts.length - 1] || String(filePath ?? '');
}

/**
 * All external text (file names, motor messages) goes through this
 * before it touches an innerHTML template.
 */
function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;',
        '"': '&quot;', "'": '&#39;'
    })[ch]);
}

const bridge = {
    openFiles: () => preloadApi.openFiles(),
    getFileInfo: (filePath) => preloadApi.getFileInfo(filePath),
    runJob: (payload) => preloadApi.runJob(payload),
    cancelJob: () => preloadApi.cancelJob(),
    openPath: (targetPath) => preloadApi.openPath(targetPath),
    showItemInFolder: (targetPath) => preloadApi.showItemInFolder(targetPath),
    getPathForFile: (file) => preloadApi.getPathForFile(file),
    onMotorEvent: (name, callback) => preloadApi.onMotorEvent(name, callback),
};

