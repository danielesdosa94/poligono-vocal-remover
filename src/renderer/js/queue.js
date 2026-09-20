/**
 * Polígono AI Hub - Queue
 * =======================
 *
 * Owns the job list and its state machine. One job runs at a time: each
 * bridge.runJob() call settles with the motor's terminal event, so there is
 * no polling here. Progress arrives through the motor listeners in app.js.
 */

const state = {
    queue: [], // Array of job objects
    isProcessing: false,
    currentJobId: null,
    nextJobId: 1,
};

// Job states: 'pending', 'processing', 'completed', 'error'
function createJob(fileInfo) {
    // If queue is running, new files enter as 'Waiting...'
    // If queue is stopped, they enter as 'Ready'
    const dict = getDict();
    const detail = state.isProcessing ? dict.job_waiting : dict.job_ready;

    return {
        id: state.nextJobId++,
        file: fileInfo,
        status: 'pending',
        progress: 0,
        detail: detail,
        error: null,
        outputPath: null,
        // Warnings the motor raises mid-run, already worded. Reset per run.
        warnings: [],
        // Notices about the source itself, found by probing the file as it is
        // queued. Stored as { key, values } so they follow the language, and
        // kept apart from `warnings` because they describe the file, not the
        // run: starting the job must not wipe them.
        notices: [],
    };
}

function updateJob(jobId, updates) {
    const job = state.queue.find(j => j.id === jobId);
    if (job) {
        Object.assign(job, updates);
        renderJob(job);
        updateQueueStats();
    }
}

// =====================================================================
// File Handling
// =====================================================================

async function addFiles(filePaths) {
    if (!Array.isArray(filePaths) || filePaths.length === 0) {
        logConsole('No files to add', 'error');
        return;
    }

    logConsole(`Adding ${filePaths.length} file(s) to queue...`);

    const added = [];
    for (const filePath of filePaths) {
        const info = await bridge.getFileInfo(filePath);

        if (info.valid) {
            const job = createJob(info);
            state.queue.push(job);
            renderJob(job);
            added.push(job);
            logConsole(`Added: ${info.name}`);
        } else {
            logConsole(`Invalid file: ${basename(filePath)} - ${info.reason}`, 'error');
        }
    }

    updateQueueStats();

    // The rows are already on screen; the notices fill in behind them. All
    // the probes go out at once, so dropping forty files does not turn into
    // forty sequential ffprobe calls before the last row can say anything.
    await Promise.all(added.map(job => attachSourceNotices(job)));
}

/**
 * Motor warnings a notice on the row already covers, by notice key.
 *
 * The motor raises its own version of these mid-run, in English, and without
 * it the row would say the same thing twice in two languages. Matching the
 * shape of the motor's message is the coupling we accept for that; if its
 * wording ever drifts the worst case is the duplicate line coming back, not
 * a warning going missing.
 */
const NOTICE_SUPERSEDES = {
    warning_multichannel: /channel source downmixed to stereo/i,
};

/**
 * Drop the motor warnings that a notice on this row already states.
 */
function withoutSupersededWarnings(job, messages) {
    const patterns = (job.notices || [])
        .map(notice => NOTICE_SUPERSEDES[notice.key])
        .filter(Boolean);
    if (patterns.length === 0) return messages;
    return messages.filter(message => !patterns.some(pattern => pattern.test(message)));
}

/**
 * Flag anything about the source that changes what the client gets back.
 *
 * Runs at queue time, not at process time: a 5.1 source is downmixed to
 * stereo, and whoever is after dialogue wants to know that while they can
 * still go and extract the centre channel instead.
 */
async function attachSourceNotices(job) {
    let info;
    try {
        info = await bridge.probeAudio(job.file.path);
    } catch (error) {
        // A file that cannot be probed is still perfectly queueable.
        return;
    }
    if (!info || !info.ok) return;

    const notices = [];
    if (info.channels > 2) {
        notices.push({ key: 'warning_multichannel', values: { channels: info.channels } });
    }
    if (notices.length > 0) {
        updateJob(job.id, { notices });
    }
}

// =====================================================================
// Queue Processing
// =====================================================================

async function processQueue() {
    if (state.isProcessing) return;

    state.isProcessing = true;
    updateQueueStats();
    logConsole('Queue processing started');

    while (true) {
        // Find next pending job
        const job = state.queue.find(j => j.status === 'pending');

        if (!job) {
            // No more pending jobs
            break;
        }

        // Process this job
        state.currentJobId = job.id;
        updateJob(job.id, {
            status: 'processing',
            detail: getDict().job_initializing,
            progress: 0,
            warnings: [],
        });

        logConsole(`Processing: ${job.file.name}`);

        try {
            // Only the mode is decided here (it follows the active tab);
            // everything else comes from the persisted settings, which the
            // main process merges in and validates.
            const options = { mode: elements.optMode.value };

            // One call per job: the promise settles with the motor's
            // terminal event (success / error / cancelled).
            const result = await bridge.runJob({
                inputPath: job.file.path,
                outputDir: null, // Resolved from the settings by the main process
                options
            });

            const dict = getDict();

            if (result.status === 'success') {
                logConsole(interpolate(dict.console_job_completed, { id: job.id }));
                updateJob(job.id, {
                    status: 'completed',
                    progress: 100,
                    detail: `✅ ${interpolate(dict.job_completed, { time: result.elapsedSeconds?.toFixed(1) })}`,
                    outputPath: result.outputDir || null,
                    warnings: withoutSupersededWarnings(
                        job,
                        (result.stats && result.stats.warnings) || job.warnings
                    ),
                });
            } else if (result.status === 'cancelled') {
                logConsole(interpolate(dict.console_job_cancelled, { id: job.id }));
                updateJob(job.id, {
                    status: 'error',
                    detail: dict.job_cancelled,
                    error: 'Cancelled',
                });
            } else {
                throw new Error(result.message || 'Unknown error');
            }

        } catch (error) {
            logConsole(`Error processing ${job.file.name}: ${error.message}`, 'error');
            updateJob(job.id, {
                status: 'error',
                detail: `Error: ${error.message}`,
                error: error.message,
            });
        }

        state.currentJobId = null;

        // Check if we should stop (user clicked stop button)
        if (!state.isProcessing) {
            break;
        }
    }

    state.isProcessing = false;
    updateQueueStats();
    logConsole('Queue processing finished');
}

/**
 * Start: flip every 'Ready' job to 'Waiting...' and run the queue.
 */
function startQueue() {
    const dict = getDict();

    state.queue.forEach(job => {
        if (job.status === 'pending' && (job.detail === dict.job_ready || job.detail === translations.en.job_ready || job.detail === translations.es.job_ready)) {
            job.detail = dict.job_waiting;
            renderJob(job);
        }
    });

    processQueue();
}

/**
 * Stop: cancel the running job cooperatively and put the rest back to
 * 'Ready'. processQueue() breaks out on the next iteration.
 */
async function stopQueue() {
    const dict = getDict();
    logConsole(dict.console_stopping_queue);
    state.isProcessing = false;

    // Cancel current job
    if (state.currentJobId) {
        await bridge.cancelJob();
    }

    // Change all pending jobs from 'Waiting...' back to 'Ready'
    state.queue.forEach(job => {
        if (job.status === 'pending' && (job.detail === dict.job_waiting || job.detail === translations.en.job_waiting || job.detail === translations.es.job_waiting)) {
            job.detail = dict.job_ready;
            renderJob(job);
        }
    });

    updateQueueStats();
}

function clearCompleted() {
    const before = state.queue.length;
    state.queue = state.queue.filter(j => j.status !== 'completed');
    const removed = before - state.queue.length;

    if (removed > 0) {
        logConsole(`Removed ${removed} completed job(s)`);
        renderAllJobs();
    }
}

function removeJob(job) {
    const index = state.queue.indexOf(job);
    if (index === -1) return;
    state.queue.splice(index, 1);
    logConsole(`Removed job: ${job.file.name}`);
    renderAllJobs();
}
