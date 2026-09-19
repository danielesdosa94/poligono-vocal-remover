/**
 * Polígono AI Hub - Preload
 * =========================
 *
 * The only place in the renderer side that touches Electron internals.
 * Runs with contextIsolation + sandbox, so it can require 'electron' but
 * not local CommonJS files; everything it needs lives in this one file.
 *
 * The renderer gets `window.api`: a fixed set of functions over a fixed set
 * of IPC channels. `ipcRenderer` itself is never exposed, and motor events
 * are delivered as plain payloads so the renderer can never reach
 * `event.sender`.
 */

const { contextBridge, ipcRenderer, webUtils } = require('electron');

// Motor events the renderer may subscribe to, by short name.
const MOTOR_EVENT_CHANNELS = {
    ready: 'motor:ready',
    start: 'motor:start',
    progress: 'motor:progress',
    step: 'motor:step',
    download: 'motor:download',
    log: 'motor:log',
    warning: 'motor:warning',
    error: 'motor:error',
};

contextBridge.exposeInMainWorld('api', {
    /**
     * Open the file picker. Resolves with an array of paths, or null.
     */
    openFiles: () => ipcRenderer.invoke('dialog:openFile'),

    /**
     * Validate a path and read its metadata (name, size, type, extension).
     */
    getFileInfo: (filePath) => ipcRenderer.invoke('file:getInfo', filePath),

    /**
     * Persisted settings. getSettings resolves with
     * { settings, defaults, outputDirUsable }; saveSettings takes a partial
     * patch and resolves with { settings, outputDirUsable }.
     */
    getSettings: () => ipcRenderer.invoke('settings:get'),

    saveSettings: (patch) => ipcRenderer.invoke('settings:set', patch),

    /**
     * Folder picker for the output directory. Resolves with
     * { canceled, settings }; on success the choice is already persisted.
     */
    chooseOutputDir: () => ipcRenderer.invoke('dialog:chooseOutputDir'),

    /**
     * Run one separation job to completion. Resolves with the motor's
     * terminal event: { status: 'success' | 'error' | 'cancelled', ... }.
     */
    runJob: (payload) => ipcRenderer.invoke('motor:run', payload),

    /**
     * Cooperative cancel of the running job.
     */
    cancelJob: () => ipcRenderer.invoke('motor:cancel'),

    openPath: (targetPath) => ipcRenderer.invoke('shell:openPath', targetPath),

    showItemInFolder: (targetPath) => ipcRenderer.invoke('shell:showItemInFolder', targetPath),

    /**
     * Resolve the filesystem path of a dropped File. Must happen here:
     * webUtils is not available to the renderer, and File objects do not
     * survive being cloned through the bridge as a list, so the renderer
     * calls this once per file.
     */
    getPathForFile: (file) => {
        try {
            return webUtils.getPathForFile(file);
        } catch (err) {
            return '';
        }
    },

    /**
     * Subscribe to a motor event by short name. The callback receives only
     * the payload. Returns an unsubscribe function.
     */
    onMotorEvent: (name, callback) => {
        const channel = MOTOR_EVENT_CHANNELS[name];
        if (!channel || typeof callback !== 'function') return () => {};

        const listener = (event, data) => callback(data);
        ipcRenderer.on(channel, listener);
        return () => ipcRenderer.removeListener(channel, listener);
    },
});
