/**
 * Polígono AI Hub - Media probe
 * =============================
 *
 * Header-level metadata for a media file, read with ffprobe.
 *
 * This exists so a multichannel source can be flagged on its job row the
 * moment the file is queued. The motor reports the same downmix when the job
 * starts, which is too late: by then the client has already committed the
 * GPU time, and someone after dialogue would rather go and extract the centre
 * channel instead.
 *
 * Nothing here decodes audio; it is one ffprobe call over the container
 * header. Arguments are always an array, never a shell string, because the
 * paths this sees routinely carry spaces and accents.
 */

const { execFile } = require('child_process');
const path = require('path');
const fs = require('fs');

// A header read on a local file takes milliseconds. Anything past this is a
// dead network share or a container ffprobe cannot make sense of.
const PROBE_TIMEOUT_MS = 15000;
const PROBE_MAX_BUFFER = 1024 * 1024;

/**
 * Locate the bundled ffprobe, which sits next to the ffmpeg the motor uses.
 * Returns null when it is not there; the caller degrades to no notices.
 */
function findFfprobe({ isPackaged, resourcesPath, repoRoot }) {
    const candidate = isPackaged
        ? path.join(resourcesPath, 'bin', 'ffmpeg', 'ffprobe.exe')
        : path.join(repoRoot, 'resources', 'bin', 'ffmpeg', 'ffprobe.exe');
    return fs.existsSync(candidate) ? candidate : null;
}

/**
 * Read the first audio stream's header.
 *
 * Resolves with { ok: true, channels, sampleRate, durationSeconds, codec,
 * channelLayout } or { ok: false, reason }. Never rejects: a file that cannot
 * be probed still belongs in the queue, it just carries no notice.
 */
function probeAudio(ffprobePath, filePath) {
    const args = [
        '-v', 'error',
        '-select_streams', 'a:0',
        '-show_entries', 'stream=channels,sample_rate,codec_name,channel_layout',
        '-show_entries', 'format=duration',
        '-of', 'json',
        filePath,
    ];

    return new Promise((resolve) => {
        execFile(
            ffprobePath,
            args,
            { timeout: PROBE_TIMEOUT_MS, maxBuffer: PROBE_MAX_BUFFER, windowsHide: true },
            (error, stdout) => {
                if (error) {
                    resolve({ ok: false, reason: error.message });
                    return;
                }
                try {
                    const payload = JSON.parse(stdout);
                    const stream = (payload.streams || [])[0];
                    if (!stream) {
                        resolve({ ok: false, reason: 'No audio stream' });
                        return;
                    }
                    resolve({
                        ok: true,
                        channels: Number(stream.channels) || 0,
                        sampleRate: Number(stream.sample_rate) || 0,
                        channelLayout: stream.channel_layout || null,
                        codec: stream.codec_name || null,
                        durationSeconds: Number((payload.format || {}).duration) || 0,
                    });
                } catch (err) {
                    resolve({ ok: false, reason: `Unreadable ffprobe output: ${err.message}` });
                }
            }
        );
    });
}

module.exports = { findFfprobe, probeAudio };
