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
            elements.modeDescription.textContent = dict.mode_vocal_remover_subtitle;
            elements.modeDescription.setAttribute('data-i18n', 'mode_vocal_remover_subtitle');
            logConsole(dict.console_mode_switched_vocal);
        } else if (index === 1) {
            // Stem Splitter - Show Queue View
            currentMode = 'splitter';
            showView('queue');
            elements.optMode.value = 'splitter';
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

// Sync settings panel controls with hidden controls
elements.settingsModel.addEventListener('change', () => {
    elements.optModel.value = elements.settingsModel.value;
    logConsole(`AI Model changed to: ${elements.settingsModel.value}`);
});

elements.settingsQuality.addEventListener('change', () => {
    elements.optQuality.value = elements.settingsQuality.value;
    logConsole(`Quality changed to: ${elements.settingsQuality.value}`);
});

elements.settingsDevice.addEventListener('change', () => {
    elements.optDevice.value = elements.settingsDevice.value;
    logConsole(`Device changed to: ${elements.settingsDevice.value}`);
});

elements.settingsLanguage.addEventListener('change', () => {
    updateLanguage(elements.settingsLanguage.value);
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

// Initialize settings panel with current values
elements.settingsModel.value = elements.optModel.value;
elements.settingsQuality.value = elements.optQuality.value;
elements.settingsDevice.value = elements.optDevice.value;
elements.settingsLanguage.value = currentLanguage;

// Apply saved language on startup
updateLanguage(currentLanguage);

logConsole(getDict().console_queue_initialized);
updateQueueStats();
