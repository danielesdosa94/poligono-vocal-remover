/**
 * Polígono AI Hub - Main Process
 * ==============================
 *
 * Electron main process that orchestrates:
 * - Window management
 * - The persistent Python motor (one process per session, JSON over stdio)
 * - Job lifecycle (run, cancel, shutdown, crash recovery)
 * - IPC with the renderer
 *
 * Cancellation is cooperative: the motor receives {"type":"cancel"} on stdin
 * and aborts from inside the Demucs callback. process.kill() is never used
 * on Windows because every signal there is TerminateProcess; the only forced
 * path is `taskkill /PID <pid> /T /F`, as a last resort after a timeout.
 */

const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const readline = require('readline');

// =============================================================================
// CONFIGURATION
// =============================================================================

const CONFIG = {
    window: {
        width: 1280,
        height: 800,
        minWidth: 1000,
        minHeight: 700,
        backgroundColor: '#141414',
    },
    supportedAudio: ['mp3', 'wav', 'flac', 'm4a', 'ogg', 'wma', 'aac'],
    supportedVideo: ['mp4', 'mov', 'avi', 'mkv', 'webm', 'wmv', 'flv'],
    // Importing torch + demucs, plus a cold antivirus scan of motor.exe,
    // can take a while on first launch.
    readyTimeoutMs: 120000,
    // A cooperative cancel is honoured at the next Demucs callback; if it
    // has not arrived by then the motor is stuck and gets killed.
    cancelTimeoutMs: 15000,
    // Grace period between the "shutdown" command and taskkill on app exit.
    shutdownGraceMs: 3000,
    // How long to wait for 'close' after a forced kill.
    killWaitMs: 2000,
};

// Motor events forwarded to the renderer, by channel.
const EVENT_CHANNELS = {
    ready: 'motor:ready',
    start: 'motor:start',
    progress: 'motor:progress',
    step_change: 'motor:step',
    log: 'motor:log',
    warning: 'motor:warning',
    error: 'motor:error',
    success: 'motor:success',
    cancelled: 'motor:cancelled',
};

// =============================================================================
// GLOBAL STATE
// =============================================================================

let mainWindow = null;

/**
 * ProcessManager - owns the single motor process for the whole session.
 */
class ProcessManager {
    constructor() {
        this.child = null;          // ChildProcess while the motor is alive
        this.readyPromise = null;   // Resolves with the "ready" event payload
        this.readyInfo = null;
        this.exitPromise = null;    // Resolves when the child emits 'close'
        this.stopping = false;      // A shutdown was requested by us
        this.jobCounter = 0;
        this.currentJob = null;     // { jobId, resolve, cancelling, cancelTimer, onSettled }
    }

    // ------------------------------------------------------------ paths

    /**
     * Command line for the motor, per environment.
     * Both variants speak the same protocol; only the executable differs.
     */
    getMotorCommand() {
        const parentArgs = ['--parent-pid', String(process.pid)];

        if (app.isPackaged) {
            const motorPath = path.join(process.resourcesPath, 'motor', 'motor.exe');
            const ffmpegPath = path.join(process.resourcesPath, 'bin', 'ffmpeg', 'ffmpeg.exe');
            return {
                command: motorPath,
                args: [...parentArgs, '--ffmpeg-path', ffmpegPath],
                cwd: path.dirname(motorPath),
            };
        }

        const repoRoot = path.join(__dirname, '..');
        const venvPython = path.join(repoRoot, 'python', 'venv', 'Scripts', 'python.exe');
        const scriptPath = path.join(repoRoot, 'python', 'motor.py');
        const ffmpegPath = path.join(repoRoot, 'resources', 'bin', 'ffmpeg', 'ffmpeg.exe');
        const command = fs.existsSync(venvPython) ? venvPython : 'python';
        return {
            command,
            args: ['-u', scriptPath, ...parentArgs, '--ffmpeg-path', ffmpegPath],
            cwd: repoRoot,
        };
    }

    // ------------------------------------------------------------ state

    isBusy() {
        return this.currentJob !== null;
    }

    hasChild() {
        return this.child !== null;
    }

    // ------------------------------------------------------------ lifecycle

    /**
     * Start the motor if it is not running, and wait until it reports ready.
     * Cheap when already running: returns the cached ready promise.
     */
    ensureStarted() {
        if (this.child && this.readyPromise) {
            return this.readyPromise;
        }
        return this.spawnMotor();
    }

    spawnMotor() {
        const { command, args, cwd } = this.getMotorCommand();
        console.log('[Motor] Starting:', command, args.join(' '));

        const child = spawn(command, args, {
            cwd,
            windowsHide: true,
            stdio: ['pipe', 'pipe', 'pipe'],
            env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
        });

        this.child = child;
        this.stopping = false;
        this.readyInfo = null;

        readline.createInterface({ input: child.stdout, crlfDelay: Infinity })
            .on('line', (line) => this.handleMotorMessage(child, line));

        readline.createInterface({ input: child.stderr, crlfDelay: Infinity })
            .on('line', (line) => console.error('[Motor stderr]', line));

        this.exitPromise = new Promise((resolve) => {
            child.once('close', (code, signal) => {
                this.handleExit(child, code, signal);
                resolve();
            });
        });

        this.readyPromise = new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                settle(new Error(`Motor did not report ready within ${CONFIG.readyTimeoutMs / 1000}s`));
                this.killTree();
            }, CONFIG.readyTimeoutMs);

            const settle = (err, info) => {
                clearTimeout(timer);
                child.removeListener('error', onSpawnError);
                if (err) reject(err); else resolve(info);
            };

            const onSpawnError = (err) => {
                console.error('[Motor] Spawn error:', err);
                settle(err);
                // 'close' does not fire when spawn itself failed.
                this.handleExit(child, null, null);
            };

            child.once('error', onSpawnError);
            child.once('close', () => settle(new Error('Motor exited before reporting ready')));
            this._onReady = (info) => settle(null, info);
        });
        // A failed start is reported through runJob(); avoid an unhandled rejection.
        this.readyPromise.catch(() => {});

        this.sendToRenderer('motor:log', { message: 'Starting separation engine...', level: 'info' });
        return this.readyPromise;
    }

    /**
     * Ask the motor to exit, wait a little, then force it. Used on app quit.
     */
    async shutdown() {
        const child = this.child;
        if (!child) return;

        this.stopping = true;
        console.log('[Motor] Shutdown requested');
        this.send({ type: 'shutdown' });
        try {
            child.stdin.end();
        } catch (err) {
            // Already closed
        }

        const exited = await this.waitForExit(CONFIG.shutdownGraceMs);
        if (!exited) {
            console.warn('[Motor] No exit after grace period, killing process tree');
            await this.killTree();
        }
    }

    /**
     * Last resort: kill the motor and everything it spawned (ffmpeg).
     */
    async killTree() {
        const child = this.child;
        if (!child || !child.pid) return;

        console.warn('[Motor] Force killing pid', child.pid);
        if (process.platform === 'win32') {
            await new Promise((resolve) => {
                const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], {
                    windowsHide: true,
                    stdio: 'ignore',
                });
                killer.once('close', resolve);
                killer.once('error', resolve);
            });
        } else {
            try {
                child.kill('SIGKILL');
            } catch (err) {
                // Already dead
            }
        }
        await this.waitForExit(CONFIG.killWaitMs);
    }

    /**
     * Resolve true when the child has closed, false on timeout.
     */
    waitForExit(timeoutMs) {
        if (!this.child || !this.exitPromise) return Promise.resolve(true);
        return Promise.race([
            this.exitPromise.then(() => true),
            new Promise((resolve) => setTimeout(() => resolve(false), timeoutMs)),
        ]);
    }

    /**
     * The motor is gone (clean exit, crash, or our kill). Settle whatever
     * job was in flight; the next runJob() relaunches the motor.
     */
    handleExit(child, code, signal) {
        if (this.child !== child) return;
        console.log(`[Motor] Exited with code ${code}, signal ${signal}`);

        this.child = null;
        this.readyPromise = null;
        this.readyInfo = null;
        this.exitPromise = null;

        const job = this.currentJob;
        const wasStopping = this.stopping;
        this.stopping = false;

        if (job) {
            if (job.cancelling || wasStopping) {
                this.settleJob({
                    event: 'cancelled',
                    jobId: job.jobId,
                    reason: 'Motor process terminated',
                    forced: true,
                });
            } else {
                const message = `Motor process exited unexpectedly (code ${code})`;
                this.sendToRenderer('motor:error', { jobId: job.jobId, message, code: 'MOTOR_EXITED', fatal: true });
                this.settleJob({ event: 'error', jobId: job.jobId, message, code: 'MOTOR_EXITED', fatal: true });
            }
        } else if (!wasStopping && code !== 0) {
            this.sendToRenderer('motor:log', {
                message: `Separation engine exited (code ${code}); it will restart on the next job`,
                level: 'warn',
            });
        }
    }

    // ------------------------------------------------------------ messages

    /**
     * Write one JSON command line to the motor's stdin.
     */
    send(command) {
        const child = this.child;
        if (!child || !child.stdin || !child.stdin.writable) return false;
        try {
            child.stdin.write(JSON.stringify(command) + '\n');
            return true;
        } catch (err) {
            console.error('[Motor] stdin write failed:', err);
            return false;
        }
    }

    /**
     * One line of motor stdout: forward to the renderer and settle jobs.
     */
    handleMotorMessage(child, line) {
        if (this.child !== child) return;
        const trimmed = line.trim();
        if (!trimmed) return;

        let message;
        try {
            message = JSON.parse(trimmed);
        } catch (err) {
            console.log('[Motor raw]', trimmed);
            return;
        }

        if (message.event === 'ready') {
            this.readyInfo = message;
            console.log('[Motor] Ready:', JSON.stringify(message.cuda), 'torch', message.torch);
            if (this._onReady) this._onReady(message);
        }

        if (message.event === 'pong') {
            return;
        }

        const channel = EVENT_CHANNELS[message.event];
        if (channel) {
            this.sendToRenderer(channel, message);
        } else {
            console.warn('[Motor] Unknown event:', message.event);
        }

        const job = this.currentJob;
        if (!job || message.jobId !== job.jobId) return;

        const isTerminal = message.event === 'success'
            || message.event === 'cancelled'
            || (message.event === 'error' && message.fatal !== false);
        if (isTerminal) {
            this.settleJob(message);
        }
    }

    settleJob(result) {
        const job = this.currentJob;
        if (!job) return;
        this.currentJob = null;
        if (job.cancelTimer) clearTimeout(job.cancelTimer);

        const payload = { status: result.event, ...result };
        job.resolve(payload);
        if (job.onSettled) job.onSettled(payload);
    }

    sendToRenderer(channel, data) {
        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send(channel, data);
        }
    }

    // ------------------------------------------------------------ jobs

    /**
     * Run one separation job. Resolves (never rejects) with the terminal
     * event: { status: 'success' | 'error' | 'cancelled', ...event }.
     */
    async runJob({ inputPath, outputDir, options = {} }) {
        if (this.currentJob) {
            return { status: 'error', message: 'A job is already running', code: 'BUSY', fatal: true };
        }

        try {
            await this.ensureStarted();
        } catch (err) {
            return {
                status: 'error',
                message: `Failed to start the separation engine: ${err.message}`,
                code: 'SPAWN_ERROR',
                fatal: true,
            };
        }

        const jobId = `job-${++this.jobCounter}-${Date.now().toString(36)}`;
        return new Promise((resolve) => {
            this.currentJob = { jobId, resolve, cancelling: false, cancelTimer: null, onSettled: null };

            const command = {
                type: 'separate',
                jobId,
                input: inputPath,
                outputDir: outputDir || null,
                mode: options.mode || 'vocal_remover',
                preset: options.preset || 'hq',
                format: options.format || 'wav',
                device: options.device || 'auto',
            };
            if (options.model) command.model = options.model;

            if (!this.send(command)) {
                this.settleJob({
                    event: 'error',
                    jobId,
                    message: 'Separation engine is not accepting commands',
                    code: 'MOTOR_UNAVAILABLE',
                    fatal: true,
                });
            }
        });
    }

    /**
     * Cooperative cancel of the running job. Falls back to taskkill if the
     * motor does not report "cancelled" within cancelTimeoutMs.
     */
    cancel() {
        const job = this.currentJob;
        if (!job) return Promise.resolve({ success: true, reason: 'No job running' });
        if (job.cancelling) return Promise.resolve({ success: false, reason: 'Already cancelling' });

        job.cancelling = true;
        console.log('[Motor] Cancel requested for', job.jobId);
        const sent = this.send({ type: 'cancel', jobId: job.jobId });

        return new Promise((resolve) => {
            job.onSettled = (result) => resolve({ success: true, forced: Boolean(result.forced), status: result.status });
            job.cancelTimer = setTimeout(async () => {
                console.warn('[Motor] Cancel not acknowledged, killing process tree');
                await this.killTree();
                // handleExit settles the job as cancelled (forced). If the
                // process somehow survived, settle here so the UI moves on.
                if (this.currentJob === job) {
                    this.settleJob({ event: 'cancelled', jobId: job.jobId, reason: 'Forced', forced: true });
                }
            }, sent ? CONFIG.cancelTimeoutMs : 0);
        });
    }
}

const processManager = new ProcessManager();

// =============================================================================
// WINDOW MANAGEMENT
// =============================================================================

function createWindow() {
    mainWindow = new BrowserWindow({
        width: CONFIG.window.width,
        height: CONFIG.window.height,
        minWidth: CONFIG.window.minWidth,
        minHeight: CONFIG.window.minHeight,
        title: 'Polígono AI Hub',
        backgroundColor: CONFIG.window.backgroundColor,
        icon: path.join(__dirname, 'assets', 'icon.png'),
        webPreferences: {
            nodeIntegration: true,
            contextIsolation: false,
            // Phase 3: contextIsolation + preload
        },
        show: false,
    });

    mainWindow.setMenuBarVisibility(false);
    mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

    mainWindow.once('ready-to-show', () => {
        mainWindow.maximize();
        mainWindow.show();
    });

    // Closing with a job in flight: ask, then cancel cooperatively before
    // the window goes away. The motor itself is shut down in before-quit.
    mainWindow.on('close', async (event) => {
        if (!processManager.isBusy()) return;
        event.preventDefault();

        const { response } = await dialog.showMessageBox(mainWindow, {
            type: 'question',
            buttons: ['Cancel Processing', 'Keep Running'],
            defaultId: 1,
            title: 'Processing in Progress',
            message: 'Audio is currently being processed.',
            detail: 'Do you want to cancel the processing and close?',
        });

        if (response === 0) {
            await processManager.cancel();
            if (mainWindow && !mainWindow.isDestroyed()) mainWindow.destroy();
        }
    });

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}

// =============================================================================
// IPC HANDLERS
// =============================================================================

ipcMain.handle('dialog:openFile', async () => {
    const allExtensions = [...CONFIG.supportedAudio, ...CONFIG.supportedVideo];

    const result = await dialog.showOpenDialog(mainWindow, {
        title: 'Select Audio or Video Files',
        properties: ['openFile', 'multiSelections'],
        filters: [
            { name: 'Media Files', extensions: allExtensions },
            { name: 'Audio Files', extensions: CONFIG.supportedAudio },
            { name: 'Video Files', extensions: CONFIG.supportedVideo },
        ],
    });

    if (result.canceled || result.filePaths.length === 0) {
        return null;
    }
    return result.filePaths;
});

/**
 * Run a job to completion. Resolves with the terminal event.
 */
ipcMain.handle('motor:run', (event, payload) => {
    return processManager.runJob(payload || {});
});

ipcMain.handle('motor:cancel', () => {
    return processManager.cancel();
});

ipcMain.handle('motor:status', () => {
    return {
        isRunning: processManager.isBusy(),
        jobId: processManager.currentJob ? processManager.currentJob.jobId : null,
        motorAlive: processManager.hasChild(),
        ready: processManager.readyInfo,
    };
});

ipcMain.handle('shell:openPath', async (event, targetPath) => {
    if (targetPath) {
        return shell.openPath(targetPath);
    }
    return 'No path provided';
});

ipcMain.handle('shell:showItemInFolder', (event, targetPath) => {
    if (targetPath) {
        shell.showItemInFolder(targetPath);
    }
});

ipcMain.handle('file:getInfo', (event, filePath) => {
    const ext = path.extname(filePath).toLowerCase().slice(1);

    const isAudio = CONFIG.supportedAudio.includes(ext);
    const isVideo = CONFIG.supportedVideo.includes(ext);

    if (!isAudio && !isVideo) {
        return { valid: false, reason: `Unsupported format: .${ext}` };
    }

    try {
        const stats = fs.statSync(filePath);
        return {
            valid: true,
            path: filePath,
            name: path.basename(filePath),
            size: stats.size,
            type: isVideo ? 'video' : 'audio',
            extension: ext,
        };
    } catch (err) {
        return { valid: false, reason: err.message };
    }
});

// =============================================================================
// APP LIFECYCLE
// =============================================================================

app.whenReady().then(() => {
    createWindow();

    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) {
            createWindow();
        }
    });
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
        app.quit();
    }
});

// Shut the motor down before the app exits: "shutdown" on stdin, a grace
// period, then taskkill. before-quit does not await, so we prevent the first
// quit, finish the shutdown, and quit again.
let motorShutdownDone = false;
app.on('before-quit', (event) => {
    if (motorShutdownDone || !processManager.hasChild()) return;
    event.preventDefault();
    processManager.shutdown()
        .catch((err) => console.error('[Motor] Shutdown error:', err))
        .finally(() => {
            motorShutdownDone = true;
            app.quit();
        });
});

process.on('uncaughtException', (error) => {
    console.error('Uncaught exception:', error);
    if (mainWindow && !mainWindow.isDestroyed()) {
        dialog.showErrorBox('Unexpected Error', error.message);
    }
});
