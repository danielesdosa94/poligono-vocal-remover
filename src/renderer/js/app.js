/**
 * Polígono AI Hub - App bootstrap
 * ===============================
 *
 * Wires DOM events and motor events to the queue, then starts the UI.
 * Everything that runs at load time lives here, at the end of the script
 * chain, so no module executes before the others have declared their state.
 */

// =====================================================================
// Drop Zone
// =====================================================================

elements.dropZone.addEventListener('click', async () => {
    const filePaths = await bridge.openFiles();
    if (filePaths) {
        // openFiles always returns an array
        await addFiles(filePaths);
    }
});

elements.dropZone.addEventListener('dragenter', (e) => {
    e.preventDefault();
    e.stopPropagation();
    console.log('🎯 Drag Enter detected');
    elements.dropZone.classList.add('drag-over');
});

elements.dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.stopPropagation();
    console.log('🔄 Hovering over drop zone...');
    elements.dropZone.classList.add('drag-over');
});

elements.dropZone.addEventListener('dragleave', (e) => {
    e.preventDefault();
    e.stopPropagation();
    // Only remove if leaving the dropZone itself, not child elements
    if (e.target === elements.dropZone) {
        console.log('🚪 Drag Leave detected');
        elements.dropZone.classList.remove('drag-over');
    }
});

elements.dropZone.addEventListener('drop', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    console.log('💥 DROP DETECTED!');

    elements.dropZone.classList.remove('drag-over');

    const files = e.dataTransfer.files;
    console.log('📦 Files dropped:', files.length);

    if (!files || files.length === 0) {
        console.warn('⚠️ No files in dataTransfer');
        return;
    }

    const filePaths = [];
    for (let i = 0; i < files.length; i++) {
        const file = files[i];

        // Resolved in the preload: webUtils is not reachable from here.
        const filePath = bridge.getPathForFile(file);

        console.log(`📄 File ${i + 1}:`, {
            name: file.name,
            path: filePath,
            type: file.type,
            size: file.size
        });

        if (filePath) {
            filePaths.push(filePath);
        } else {
            console.error(`❌ File ${i + 1} has no valid path!`);
        }
    }

    console.log('🚀 Sending paths to addFiles:', filePaths);

    if (filePaths.length > 0) {
        await addFiles(filePaths);
    } else {
        console.error('❌ No valid file paths extracted');
    }
});

// =====================================================================
// Queue Buttons
// =====================================================================

elements.btnStartQueue.addEventListener('click', () => {
    startQueue();
});

elements.btnStopQueue.addEventListener('click', () => {
    stopQueue();
});

elements.btnClearCompleted.addEventListener('click', () => {
    clearCompleted();
});

// =====================================================================
// Job Actions (Remove, Open Folder)
// =====================================================================

elements.jobList.addEventListener('click', async (e) => {
    const button = e.target.closest('.job-btn');
    if (!button) return;

    const jobId = parseInt(button.dataset.jobId);
    const action = button.dataset.action;
    const job = state.queue.find(j => j.id === jobId);

    if (!job) return;

    if (action === 'remove') {
        removeJob(job);
    } else if (action === 'open' && job.outputPath) {
        await bridge.openPath(job.outputPath);
    }
});

// =====================================================================
// Motor Event Listeners
// =====================================================================

bridge.onMotorEvent('ready', (data) => {
    const gpu = data.cuda && data.cuda.available
        ? `${data.cuda.name} (${data.cuda.vramGb} GB)`
        : 'CPU only';
    logConsole(`Separation engine ready: ${gpu}, torch ${data.torch}`);
    // The motor is the authority on presets and formats; adopt its tables so
    // the labels and the bit depth choices cannot drift from the engine.
    adoptMotorCapabilities(data);
});

// Model weights being fetched on first use. Without this the bar would sit
// frozen on "Loading model" for a 1 GB download.
bridge.onMotorEvent('download', (data) => {
    if (!state.currentJobId) return;
    const dict = getDict();
    updateJob(state.currentJobId, {
        progress: Math.round(data.percent ?? 0),
        detail: interpolate(dict.job_downloading_model, {
            index: data.fileIndex,
            count: data.fileCount,
            done: formatBytes(data.bytesDone),
            total: formatBytes(data.bytesTotal),
        }),
    });
});

bridge.onMotorEvent('start', (data) => {
    if (state.currentJobId) {
        logConsole(`Motor started for job ${state.currentJobId}: ${data.model} on ${data.device}`);
    }
});

bridge.onMotorEvent('step', (data) => {
    if (state.currentJobId) {
        updateJob(state.currentJobId, {
            detail: `Step ${data.stepNumber}/${data.totalSteps}: ${data.step}`,
        });
    }
});

bridge.onMotorEvent('progress', (data) => {
    if (state.currentJobId) {
        // The motor reports whole-job progress (0-100); it goes
        // straight to the bar.
        const percent = Math.round(data.globalPercent ?? data.stepPercent ?? 0);

        const dict = getDict();
        updateJob(state.currentJobId, {
            progress: percent,
            detail: interpolate(dict.job_processing_ai, { progress: percent }),
        });
    }
});

bridge.onMotorEvent('log', (data) => {
    logConsole(data.message, data.level || 'info');
});

bridge.onMotorEvent('warning', (data) => {
    logConsole(`⚠️ ${data.message}`, 'warn');
    // Anything that changes what the client actually receives (a 5.1 downmix,
    // a mono source) belongs on the job row too.
    const job = state.queue.find(j => j.id === state.currentJobId);
    if (job && !job.warnings.includes(data.message)) {
        updateJob(job.id, { warnings: [...job.warnings, data.message] });
    }
});

// Job outcome (success / error / cancelled) is handled by the result of
// bridge.runJob() in processQueue(); this listener only logs.
bridge.onMotorEvent('error', (data) => {
    logConsole(`Error: ${data.message}`, 'error');
});

// =====================================================================
// Navigation Tabs - View Switcher
// =====================================================================

elements.navTabs.forEach((tab, index) => {
    tab.addEventListener('click', () => {
        const dict = getDict();

        // Remove active from all tabs
        elements.navTabs.forEach(t => t.classList.remove('active'));

        // Add active to clicked tab
        tab.classList.add('active');

        // Switch view and mode based on tab
        if (index === 0) {
            // Vocal Remover - Show Queue View
            currentMode = 'vocal_remover';
            showView('queue');
            elements.optMode.value = 'vocal_remover';
            updateSettings({ mode: 'vocal_remover' });
            elements.modeDescription.textContent = dict.mode_vocal_remover_subtitle;
            elements.modeDescription.setAttribute('data-i18n', 'mode_vocal_remover_subtitle');
            logConsole(dict.console_mode_switched_vocal);
        } else if (index === 1) {
            // Stem Splitter - Show Queue View
            currentMode = 'splitter';
            showView('queue');
            elements.optMode.value = 'splitter';
            updateSettings({ mode: 'splitter' });
            elements.modeDescription.textContent = dict.mode_splitter_subtitle;
            elements.modeDescription.setAttribute('data-i18n', 'mode_splitter_subtitle');
            logConsole(dict.console_mode_switched_splitter);
        } else if (index === 2) {
            // Settings - Show Settings View
            showView('settings');
            elements.modeDescription.textContent = dict.mode_settings_subtitle;
            elements.modeDescription.setAttribute('data-i18n', 'mode_settings_subtitle');
            logConsole(dict.console_settings_opened);
        }
    });
});

// =====================================================================
// Settings Synchronization
// =====================================================================

// Every control writes straight back to the persisted settings, and the
// mirrored hidden select is refreshed by applySettingsToControls().

elements.settingsModel.addEventListener('change', async () => {
    await updateSettings({ model: elements.settingsModel.value });
    logConsole(`AI Model changed to: ${elements.settingsModel.value}`);
});

elements.settingsQuality.addEventListener('change', async () => {
    await updateSettings({ preset: elements.settingsQuality.value });
    logConsole(`Quality changed to: ${elements.settingsQuality.value}`);
});

elements.settingsDevice.addEventListener('change', async () => {
    await updateSettings({ device: elements.settingsDevice.value });
    logConsole(`Device changed to: ${elements.settingsDevice.value}`);
});

elements.settingsLanguage.addEventListener('change', async () => {
    const language = elements.settingsLanguage.value;
    await updateSettings({ language });
    updateLanguage(language);
});

elements.settingsMono.addEventListener('change', async () => {
    const monoOutput = elements.settingsMono.value === 'true';
    await updateSettings({ monoOutput });
    logConsole(`Mono sources: ${monoOutput ? 'mono output' : 'dual-mono stereo'}`);
});

elements.settingsChunk.addEventListener('change', async () => {
    const chunkMinutes = Number(elements.settingsChunk.value);
    await updateSettings({ chunkMinutes });
    logConsole(chunkMinutes > 0
        ? `Long files: ${chunkMinutes} minute blocks`
        : 'Long files: chunking disabled');
});

// =====================================================================
// Output Settings (left panel)
// =====================================================================

elements.optFormat.addEventListener('change', async () => {
    await updateSettings({ format: elements.optFormat.value });
    logConsole(`Output format: ${elements.optFormat.value}`);
});

elements.optBitDepth.addEventListener('change', async () => {
    const depth = Number(elements.optBitDepth.value);
    const key = elements.optFormat.value === 'flac' ? 'flacBitDepth' : 'wavBitDepth';
    await updateSettings({ [key]: depth });
    logConsole(`Bit depth: ${depth === 32 ? '32-bit float' : `${depth}-bit`}`);
});

elements.optOutputMode.addEventListener('change', async () => {
    const mode = elements.optOutputMode.value;
    if (mode === 'folder' && !(getSettings() || {}).outputDir) {
        // Nothing chosen yet: ask straight away instead of leaving the app in
        // a state where "custom folder" means "no folder".
        await chooseOutputFolder();
        return;
    }
    await updateSettings({ outputMode: mode });
});

async function chooseOutputFolder() {
    const result = await bridge.chooseOutputDir();
    if (result.canceled) {
        // Put the control back to whatever is actually in effect.
        applySettingsToControls();
        return;
    }
    await updateSettings({});
    logConsole(`Output folder: ${result.settings.outputDir}`);
}

elements.btnChooseOutput.addEventListener('click', () => {
    chooseOutputFolder();
});

// =====================================================================
// Prevent default drag & drop behavior on entire window
// =====================================================================

// CRITICAL: Prevent browser from opening dropped files outside the drop zone
document.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.stopPropagation();
}, false);

document.addEventListener('drop', (e) => {
    // Only prevent default if NOT dropping on the drop zone
    // The drop zone handler will handle its own preventDefault
    if (!e.target.closest('#drop-zone')) {
        e.preventDefault();
        e.stopPropagation();
        console.log('🚫 Dropped outside drop zone - prevented default behavior');
    }
}, false);

// Additional safety: prevent dragenter on document
document.addEventListener('dragenter', (e) => {
    e.preventDefault();
}, false);

// =====================================================================
// Initialization
// =====================================================================

setupLogoFallback();
updateQueueStats();

// Settings come from disk (src/settings.js in the main process), so the app
// opens exactly as it was closed. Everything the controls need is applied in
// there, including the language.
initSettings()
    .then(() => {
        // The mode is persisted, so the matching tab has to be the active one.
        const tabIndex = getSettings().mode === 'splitter' ? 1 : 0;
        currentMode = getSettings().mode;
        elements.navTabs.forEach((tab, index) => {
            tab.classList.toggle('active', index === tabIndex);
        });
        const dict = getDict();
        elements.modeDescription.textContent = currentMode === 'splitter'
            ? dict.mode_splitter_subtitle
            : dict.mode_vocal_remover_subtitle;
        logConsole(dict.console_settings_loaded);
    })
    .catch((error) => {
        logConsole(`Could not load settings, using defaults: ${error.message}`, 'error');
        updateLanguage(currentLanguage);
    })
    .finally(() => {
        logConsole(getDict().console_queue_initialized);
        updateQueueStats();
    });
