/**
 * Polígono AI Hub - UI
 * ====================
 *
 * DOM lookups, rendering and text. No IPC and no queue logic here: this
 * module only knows how to draw the state that queue.js owns.
 */

// =====================================================================
// DOM Elements
// =====================================================================

const elements = {
    dropZone: document.getElementById('drop-zone'),
    jobList: document.getElementById('job-list'),
    queueStats: document.getElementById('queue-stats'),
    btnStartQueue: document.getElementById('btn-start-queue'),
    btnStopQueue: document.getElementById('btn-stop-queue'),
    btnClearCompleted: document.getElementById('btn-clear-completed'),
    optMode: document.getElementById('opt-mode'),
    optDevice: document.getElementById('opt-device'),
    optFormat: document.getElementById('opt-format'),
    optModel: document.getElementById('opt-model'),
    optQuality: document.getElementById('opt-quality'),
    optBitDepth: document.getElementById('opt-bit-depth'),
    optOutputMode: document.getElementById('opt-output-mode'),
    groupBitDepth: document.getElementById('group-bit-depth'),
    outputDirPath: document.getElementById('output-dir-path'),
    btnChooseOutput: document.getElementById('btn-choose-output'),
    consoleLog: document.getElementById('console-log'),
    navTabs: document.querySelectorAll('.nav-tab'),
    // Views
    viewQueue: document.getElementById('view-queue'),
    viewSettings: document.getElementById('view-settings'),
    // Settings panel controls
    settingsModel: document.getElementById('settings-model'),
    settingsQuality: document.getElementById('settings-quality'),
    settingsDevice: document.getElementById('settings-device'),
    settingsLanguage: document.getElementById('settings-language'),
    settingsMono: document.getElementById('settings-mono'),
    settingsChunk: document.getElementById('settings-chunk'),
    // Brand
    brandLogo: document.querySelector('.brand__logo'),
    brandLogoFallback: document.querySelector('.brand__logo-fallback'),
    modeDescription: document.getElementById('mode-description'),
};

// Current mode state
let currentMode = 'vocal_remover';

// Current language state. The persisted value arrives from settings.js at
// boot; this is only the value used until then.
let currentLanguage = 'en';

function getDict() {
    return translations[currentLanguage] || translations['en'];
}

// =====================================================================
// UI Helpers
// =====================================================================

function updateQueueStats() {
    const pending = state.queue.filter(j => j.status === 'pending').length;
    const processing = state.queue.filter(j => j.status === 'processing').length;
    const completed = state.queue.filter(j => j.status === 'completed').length;

    const dict = getDict();
    elements.queueStats.textContent = interpolate(dict.queue_stats, {
        pending,
        processing,
        completed
    });

    // Update button states
    elements.btnStartQueue.disabled = state.isProcessing || state.queue.length === 0;
    elements.btnStopQueue.disabled = !state.isProcessing;
}

function formatFileSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function getStatusIcon(status, detail) {
    switch (status) {
        case 'pending':
            // Show circle for 'Ready', hourglass for 'Waiting...'
            return detail === 'Ready' ? '○' : '⏳';
        case 'processing': return '⚙️';
        case 'completed': return '✅';
        case 'error': return '❌';
        default: return '●';
    }
}

function logConsole(message, level = 'info') {
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    const time = new Date().toLocaleTimeString();
    entry.innerHTML = `<span class="log-level">[${escapeHtml(level)}]</span> `
        + `${time} - ${escapeHtml(message)}`;
    elements.consoleLog.appendChild(entry);
    elements.consoleLog.scrollTop = elements.consoleLog.scrollHeight;
}

// =====================================================================
// Job Rendering
// =====================================================================

/**
 * Warnings that belong to this job, on the job row itself.
 *
 * A 5.1 downmix or a mono source changes what the client gets, so it cannot
 * be left buried in the debug log where nobody reads it.
 */
function renderJobWarnings(job) {
    if (!job.warnings || job.warnings.length === 0) return '';
    const lines = job.warnings
        .map(message => `<div class="job-warning">⚠️ ${escapeHtml(message)}</div>`)
        .join('');
    return `<div class="job-warnings">${lines}</div>`;
}

function renderJob(job) {
    const existingItem = document.getElementById(`job-${job.id}`);

    const jobHtml = `
        <div class="job-status">
            ${getStatusIcon(job.status, job.detail)}
        </div>
        <div class="job-info">
            <div class="job-name" title="${escapeHtml(job.file.name)}">${escapeHtml(job.file.name)}</div>
            <div class="job-meta">
                ${escapeHtml(String(job.file.type).toUpperCase())} · ${formatFileSize(job.file.size)} · .${escapeHtml(job.file.extension)}
            </div>
            <div class="job-progress">
                <div class="job-progress-bar" style="width: ${job.progress}%"></div>
            </div>
            <div class="job-detail">${escapeHtml(job.detail)}</div>
            ${renderJobWarnings(job)}
        </div>
        <div class="job-actions">
            ${job.status === 'completed' ?
                `<button class="job-btn open" title="Open folder" data-job-id="${job.id}" data-action="open">📂</button>` :
                ''}
            ${job.status === 'pending' || job.status === 'error' || job.status === 'completed' ?
                `<button class="job-btn remove" title="Remove" data-job-id="${job.id}" data-action="remove">✕</button>` :
                ''}
        </div>
    `;

    if (existingItem) {
        existingItem.className = `job-item ${job.status}`;
        existingItem.innerHTML = jobHtml;
    } else {
        const item = document.createElement('div');
        item.id = `job-${job.id}`;
        item.className = `job-item ${job.status}`;
        item.innerHTML = jobHtml;
        elements.jobList.appendChild(item);
    }
}

function renderAllJobs() {
    elements.jobList.innerHTML = '';
    state.queue.forEach(job => renderJob(job));
    updateQueueStats();
}

// =====================================================================
// View Management
// =====================================================================

function showView(viewName) {
    // Hide all views
    elements.viewQueue.classList.remove('active');
    elements.viewSettings.classList.remove('active');

    // Show selected view
    if (viewName === 'queue') {
        elements.viewQueue.classList.add('active');
    } else if (viewName === 'settings') {
        elements.viewSettings.classList.add('active');
    }
}

// =====================================================================
// Internationalization (i18n)
// =====================================================================

/**
 * Retranslate the whole UI. Purely visual: persisting the choice is
 * settings.js's job, so that calling this from the boot path cannot loop.
 */
function updateLanguage(lang) {
    if (!translations[lang]) {
        console.error(`Language '${lang}' not found in translations`);
        return;
    }

    currentLanguage = lang;
    const dict = translations[lang];

    // Update all elements with data-i18n attribute
    document.querySelectorAll('[data-i18n]').forEach(element => {
        const key = element.getAttribute('data-i18n');
        if (dict[key]) {
            element.textContent = dict[key];
        }
    });

    // Update queue stats manually (has dynamic content)
    updateQueueStats();

    // Update mode subtitle based on current mode
    if (currentMode === 'vocal_remover') {
        elements.modeDescription.textContent = dict.mode_vocal_remover_subtitle;
    } else if (currentMode === 'splitter') {
        elements.modeDescription.textContent = dict.mode_splitter_subtitle;
    }

    // Update job details for pending jobs (Ready/Waiting)
    state.queue.forEach(job => {
        if (job.status === 'pending') {
            if (job.detail === 'Ready' || job.detail === 'Listo') {
                job.detail = dict.job_ready;
            } else if (job.detail === 'Waiting...' || job.detail === 'Esperando...') {
                job.detail = dict.job_waiting;
            }
            renderJob(job);
        }
    });

    // Options built at runtime carry translated text too.
    renderPresetLabels();
    renderBitDepthOptions();
    renderOutputDir();

    logConsole(interpolate(dict.console_language_changed, { value: lang }));
}

// =====================================================================
// Brand logo fallback
// =====================================================================

/**
 * Replaces the inline onerror attribute the markup used to carry, so the
 * page needs no inline script and the CSP can stay at script-src 'self'.
 */
function setupLogoFallback() {
    const { brandLogo, brandLogoFallback } = elements;
    if (!brandLogo || !brandLogoFallback) return;

    const showFallback = () => {
        brandLogo.style.display = 'none';
        brandLogoFallback.style.display = 'block';
    };

    brandLogo.addEventListener('error', showFallback);
    // The image may have failed before this script ran.
    if (brandLogo.complete && brandLogo.naturalWidth === 0) {
        showFallback();
    }
}
