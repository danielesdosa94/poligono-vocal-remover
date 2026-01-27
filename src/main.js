/**
 * Polígono Vocal Remover - Main Process
 * =====================================
 * 
 * Electron main process that orchestrates:
 * - Window management
 * - Python motor communication via JSON protocol
 * - Process lifecycle (start, cancel, cleanup)
 * - IPC with renderer process
 */

const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const readline = require('readline');

// =============================================================================
// CONFIGURATION
// =============================================================================

const CONFIG = {
    window: {
        width: 1000,
        height: 750,
        minWidth: 800,
        minHeight: 600,
        backgroundColor: '#141414',
    },
    supportedAudio: ['mp3', 'wav', 'flac', 'm4a', 'ogg', 'wma', 'aac'],
    supportedVideo: ['mp4', 'mov', 'avi', 'mkv', 'webm', 'wmv', 'flv'],
    // Timeout for graceful shutdown (ms)
    shutdownTimeout: 10000,
    // Interval to check if parent is still responsive
    healthCheckInterval: 5000,
};

// =============================================================================
// GLOBAL STATE
// =============================================================================

let mainWindow = null;

/**
 * ProcessManager - Handles Python motor subprocess lifecycle
 */
class ProcessManager {
    constructor() {
        this.currentProcess = null;
        this.isShuttingDown = false;
        this.currentJobId = null;
    }

    /**
     * Get paths for motor executable based on environment
     */
    getMotorPaths() {
        if (app.isPackaged) {
            // Production: Use compiled motor.exe from resources
            const motorPath = path.join(process.resourcesPath, 'motor', 'motor.exe');
            const ffmpegPath = path.join(process.resourcesPath, 'bin', 'ffmpeg', 'ffmpeg.exe');
            return {
                command: motorPath,
                ffmpegPath,
                isExe: true
            };
        } else {
            // Development: Use Python directly
            const pythonPath = path.join(__dirname, '..', 'python', 'venv', 'Scripts', 'python.exe');
            const scriptPath = path.join(__dirname, '..', 'python', 'motor.py');
            const ffmpegPath = path.join(__dirname, '..', 'resources', 'bin', 'ffmpeg', 'ffmpeg.exe');
            
            // Fallback to system Python if venv doesn't exist
            const fs = require('fs');
            const command = fs.existsSync(pythonPath) ? pythonPath : 'python';
            
            return {
                command,
                scriptPath,
                ffmpegPath,
                isExe: false
            };
        }
    }

    /**
     * Build command arguments for the motor
     */
    buildArguments(inputPath, outputDir, options = {}) {
        const paths = this.getMotorPaths();
        const args = [];

        // If using Python script (not exe), add script path and unbuffered flag
        if (!paths.isExe && paths.scriptPath) {
            args.push('-u', paths.scriptPath);
        }

        // Required positional arguments
        args.push(inputPath, outputDir);

        // Optional arguments
        if (options.mode) {
            args.push('--mode', options.mode);
        }
        if (options.model) {
            args.push('--model', options.model);
        }
        if (options.device) {
            args.push('--device', options.device);
        }
        if (options.quality) {
            args.push('--quality', options.quality);
        }
        if (options.shifts !== undefined) {
            args.push('--shifts', String(options.shifts));
        }
        if (options.outputFormat) {
            args.push('--output-format', options.outputFormat);
        }
        if (paths.ffmpegPath) {
            args.push('--ffmpeg-path', paths.ffmpegPath);
        }

        return { command: paths.command, args };
    }

    /**
     * Start a new processing job
     */
    startProcess(inputPath, outputDir, options = {}) {
        return new Promise((resolve, reject) => {
            // Ensure no existing process
            if (this.currentProcess) {
                reject(new Error('A process is already running'));
                return;
            }

            this.isShuttingDown = false;
            this.currentJobId = Date.now().toString(36);

            const { command, args } = this.buildArguments(inputPath, outputDir, options);
            
            console.log('[Motor] Starting:', command);
            console.log('[Motor] Arguments:', args.join(' '));

            // Spawn the process
            this.currentProcess = spawn(command, args, {
                windowsHide: true,
                stdio: ['ignore', 'pipe', 'pipe'], // stdin, stdout, stderr
            });

            // Create readline interface for line-by-line JSON parsing
            const stdoutReader = readline.createInterface({
                input: this.currentProcess.stdout,
                crlfDelay: Infinity
            });

            const stderrReader = readline.createInterface({
                input: this.currentProcess.stderr,
                crlfDelay: Infinity
            });

            // Handle stdout (JSON protocol messages)
            stdoutReader.on('line', (line) => {
                this.handleMotorMessage(line);
            });

            // Handle stderr (errors and warnings)
            stderrReader.on('line', (line) => {
                console.error('[Motor stderr]:', line);
                // Only emit critical errors to renderer
                if (line.toLowerCase().includes('error') && !line.includes('%')) {
                    this.sendToRenderer('motor:stderr', line);
                }
            });

            // Handle process exit
            this.currentProcess.on('close', (code, signal) => {
                console.log(`[Motor] Process exited with code ${code}, signal ${signal}`);

                const wasShuttingDown = this.isShuttingDown;

                // CRITICAL: Cleanup BEFORE sending events to prevent race condition
                this.cleanup();

                if (wasShuttingDown) {
                    this.sendToRenderer('motor:cancelled', {
                        reason: 'User cancelled',
                        code
                    });
                } else if (code !== 0 && code !== null) {
                    this.sendToRenderer('motor:error', {
                        message: `Process exited with code ${code}`,
                        code: 'PROCESS_EXIT',
                        fatal: true
                    });
                }
            });

            // Handle spawn errors
            this.currentProcess.on('error', (err) => {
                console.error('[Motor] Spawn error:', err);
                this.cleanup();
                this.sendToRenderer('motor:error', {
                    message: `Failed to start motor: ${err.message}`,
                    code: 'SPAWN_ERROR',
                    fatal: true
                });
                reject(err);
            });

            // Process started successfully
            resolve({ jobId: this.currentJobId });
        });
    }

    /**
     * Parse and handle a JSON message from the motor
     */
    handleMotorMessage(line) {
        const trimmed = line.trim();
        if (!trimmed) return;

        try {
            const message = JSON.parse(trimmed);
            
            // Map event types to IPC channels
            const eventMap = {
                'start': 'motor:start',
                'progress': 'motor:progress',
                'step_change': 'motor:step',
                'log': 'motor:log',
                'warning': 'motor:warning',
                'error': 'motor:error',
                'success': 'motor:success',
                'cancelled': 'motor:cancelled',
            };

            const channel = eventMap[message.event];
            if (channel) {
                this.sendToRenderer(channel, message);
            } else {
                console.warn('[Motor] Unknown event type:', message.event);
            }

        } catch (err) {
            // Not JSON - might be raw output from dependencies
            console.log('[Motor raw]:', trimmed);
            
            // Try to extract progress from raw Demucs output
            const progressMatch = trimmed.match(/(\d{1,3})%/);
            if (progressMatch) {
                this.sendToRenderer('motor:raw-progress', {
                    percent: parseInt(progressMatch[1], 10),
                    raw: trimmed
                });
            }
        }
    }

    /**
     * Send message to renderer process
     */
    sendToRenderer(channel, data) {
        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send(channel, data);
        }
    }

    /**
     * Cancel the current process gracefully
     */
    async cancelProcess() {
        if (!this.currentProcess) {
            return { success: true, reason: 'No process running' };
        }

        if (this.isShuttingDown) {
            return { success: false, reason: 'Already shutting down' };
        }

        this.isShuttingDown = true;
        console.log('[Motor] Cancellation requested');

        return new Promise((resolve) => {
            const process = this.currentProcess;
            
            // Set a timeout for forceful termination
            const forceKillTimeout = setTimeout(() => {
                console.log('[Motor] Force killing after timeout');
                try {
                    process.kill('SIGKILL');
                } catch (e) {
                    // Already dead
                }
                resolve({ success: true, forced: true });
            }, CONFIG.shutdownTimeout);

            // Listen for graceful exit
            const onExit = () => {
                clearTimeout(forceKillTimeout);
                resolve({ success: true, forced: false });
            };

            process.once('close', onExit);
            process.once('exit', onExit);

            // Send termination signal
            try {
                // On Windows, SIGTERM might not work well, so we use SIGINT
                // which Python can catch for graceful shutdown
                if (process.platform === 'win32') {
                    // Windows: Send CTRL+C event
                    process.kill('SIGINT');
                } else {
                    // Unix: Send SIGTERM first (graceful)
                    process.kill('SIGTERM');
                }
            } catch (err) {
                console.error('[Motor] Error sending signal:', err);
                clearTimeout(forceKillTimeout);
                resolve({ success: false, error: err.message });
            }
        });
    }

    /**
     * Clean up process references
     */
    cleanup() {
        this.currentProcess = null;
        this.isShuttingDown = false;
        this.currentJobId = null;
    }

    /**
     * Check if a process is currently running
     */
    isRunning() {
        return this.currentProcess !== null && !this.isShuttingDown;
    }
}

// Global process manager instance
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
        title: 'Polígono Vocal Remover',
        backgroundColor: CONFIG.window.backgroundColor,
        icon: path.join(__dirname, 'assets', 'logo.png'),
        webPreferences: {
            nodeIntegration: true,
            contextIsolation: false,
            // In production, consider using preload script for security
            // preload: path.join(__dirname, 'preload.js')
        },
        show: false, // Show when ready to avoid flash
    });

    // Remove menu bar
    mainWindow.setMenuBarVisibility(false);

    // Load the UI
    mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

    // Show window when ready
    mainWindow.once('ready-to-show', () => {
        mainWindow.show();
    });

    // Handle window close
    mainWindow.on('close', async (event) => {
        if (processManager.isRunning()) {
            event.preventDefault();
            
            // Ask user if they want to cancel
            const { response } = await dialog.showMessageBox(mainWindow, {
                type: 'question',
                buttons: ['Cancel Processing', 'Keep Running'],
                defaultId: 1,
                title: 'Processing in Progress',
                message: 'Audio is currently being processed.',
                detail: 'Do you want to cancel the processing and close?'
            });

            if (response === 0) {
                await processManager.cancelProcess();
                mainWindow.destroy();
            }
        }
    });

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}

// =============================================================================
// IPC HANDLERS
// =============================================================================

/**
 * Open file selector dialog
 */
ipcMain.handle('dialog:openFile', async () => {
    const allExtensions = [...CONFIG.supportedAudio, ...CONFIG.supportedVideo];

    const result = await dialog.showOpenDialog(mainWindow, {
        title: 'Select Audio or Video Files',
        properties: ['openFile', 'multiSelections'], // Enable multiple file selection
        filters: [
            {
                name: 'Media Files',
                extensions: allExtensions
            },
            {
                name: 'Audio Files',
                extensions: CONFIG.supportedAudio
            },
            {
                name: 'Video Files',
                extensions: CONFIG.supportedVideo
            },
        ]
    });

    if (result.canceled || result.filePaths.length === 0) {
        return null;
    }

    // Return array of file paths for batch processing
    return result.filePaths;
});

/**
 * Start processing a file
 */
ipcMain.handle('motor:start', async (event, { inputPath, outputDir, options }) => {
    try {
        // Default output directory to same as input if not provided
        const finalOutputDir = outputDir || path.dirname(inputPath);
        
        const result = await processManager.startProcess(inputPath, finalOutputDir, options);
        return { success: true, ...result };
    } catch (err) {
        return { success: false, error: err.message };
    }
});

/**
 * Cancel current processing
 */
ipcMain.handle('motor:cancel', async () => {
    const result = await processManager.cancelProcess();
    return result;
});

/**
 * Check if motor is running
 */
ipcMain.handle('motor:status', () => {
    return {
        isRunning: processManager.isRunning(),
        jobId: processManager.currentJobId
    };
});

/**
 * Open a folder in file explorer
 */
ipcMain.handle('shell:openPath', async (event, targetPath) => {
    if (targetPath) {
        return shell.openPath(targetPath);
    }
    return 'No path provided';
});

/**
 * Show item in folder
 */
ipcMain.handle('shell:showItemInFolder', (event, targetPath) => {
    if (targetPath) {
        shell.showItemInFolder(targetPath);
    }
});

/**
 * Get file info (for drag and drop validation)
 */
ipcMain.handle('file:getInfo', (event, filePath) => {
    const fs = require('fs');
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
            extension: ext
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

    // macOS: Re-create window when dock icon is clicked
    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) {
            createWindow();
        }
    });
});

// Quit when all windows are closed (except on macOS)
app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
        app.quit();
    }
});

// Clean up before quit
app.on('before-quit', async (event) => {
    if (processManager.isRunning()) {
        event.preventDefault();
        await processManager.cancelProcess();
        app.quit();
    }
});

// Handle uncaught exceptions
process.on('uncaughtException', (error) => {
    console.error('Uncaught exception:', error);
    // Try to show error to user
    if (mainWindow && !mainWindow.isDestroyed()) {
        dialog.showErrorBox('Unexpected Error', error.message);
    }
});
