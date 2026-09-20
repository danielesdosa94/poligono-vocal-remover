# Polígono AI Hub

Local AI stem separation for audio and video. Built by **Polígono Studio**
for post-production work.

Two modes, both running entirely on your own machine — nothing is uploaded,
nothing needs an account:

- **Vocal Remover (2 tracks)** — `vocals` from the model, and `instrumental`
  as the source minus the vocals. The two files null against the original.
- **Stem Splitter (4 tracks)** — `vocals`, `bass`, `drums`, `other`.

Separation is done by [Demucs v4](https://github.com/adefossez/demucs)
(Hybrid Transformer). The audio chain is float32 end to end, with a single
final resample → dither → encode step, so the delivered stems have exactly
the sample rate and frame count of the source.

---

## Requirements

**Windows 10 or 11, 64-bit.** This is the only supported platform: the motor
ships as `motor.exe` and process management is Windows-specific.

| | |
|---|---|
| **GPU acceleration** | NVIDIA GPU, **Turing (GTX 16xx / RTX 20xx) or newer**, with an up-to-date driver. No CUDA Toolkit needed — the runtime is bundled. |
| **Without a compatible GPU** | Works on CPU. Expect roughly 10–20× longer. The app detects this and says so in the log; it never fails over to a GPU it cannot use. |
| **RAM** | 8 GB minimum, 16 GB recommended. Files over 12 minutes are separated in overlapping blocks to keep the ceiling around 6 GB. |
| **Disk** | ~5 GB for the app, plus up to 1 GB of model weights downloaded on first use. |
| **Internet** | Only for that first model download. Everything else is offline. |

Turing is the **supported** baseline, not a hard floor: the PyTorch build
currently shipped also carries kernels for Pascal (GTX 10xx) and Volta, so
those cards do run. The app decides by actually running something on the GPU
before it commits to it, and falls back to the CPU with a line in the log if
that fails — so an unsupported card is slow, never broken.

Model weights are cached per user in
`C:\Users\<you>\.cache\torch\hub\checkpoints\` and survive reinstalling.

---

## Features

- **Batch queue** — drop a folder's worth of files and walk away. One job at
  a time, cancellable mid-run.
- **Audio and video in** — WAV, MP3, FLAC, M4A, OGG, WMA, AAC, MP4, MOV,
  AVI, MKV, WEBM, WMV, FLV.
- **WAV / FLAC / MP3 out**, at 32-bit float, 24-bit or 16-bit. Integer
  targets are dithered; 32-bit float is not quantised at all.
- **Sample-accurate output** — every stem comes back at the source rate with
  the source frame count.
- **Quality presets** — *Fast* (1×), *High Quality* (4×), *Ultra* (16×),
  where the multiplier is processing time relative to Fast.
- **Long-file handling** — past 12 minutes, blocks with a 2 second overlap
  joined by a linear crossfade. Sample-accurate at every block length.
- **Source warnings on the row** — a 5.1 file is flagged as soon as it is
  queued, before you spend GPU time on a downmix.
- **Persistent settings** and a configurable output folder.
- **English and Spanish** interface.

---

## Project structure

```text
poligono-ai-hub/
├── src/                          Electron
│   ├── background.js             Main process: window, motor lifecycle, IPC
│   ├── preload.js                contextBridge: the renderer's only API
│   ├── settings.js               Persisted settings (userData/settings.json)
│   ├── probe.js                  ffprobe metadata for queued files
│   └── renderer/
│       ├── index.html
│       ├── css/styles.css
│       └── js/                   bridge, ui, settings, queue, app,
│                                 translations (plain JS, no bundler)
├── python/
│   ├── motor.py                  Daemon: JSON over stdin/stdout, one job at
│   │                             a time, cooperative cancel, parent watchdog
│   ├── motor.spec                PyInstaller build
│   ├── engine/
│   │   ├── separator.py          demucs.api wrapper, device probe, pipeline
│   │   ├── audio_io.py           float32 I/O (soundfile + one ffmpeg call)
│   │   ├── chunking.py           Long-file blocks and crossfade
│   │   ├── models.py             Checkpoint discovery and download
│   │   └── presets.py            Presets, modes, formats, models
│   ├── tools/separate_cli.py     Headless harness, no Electron
│   └── utils/protocol.py         JSON event protocol
├── resources/bin/ffmpeg/         ffmpeg.exe + ffprobe.exe (not in git)
├── scripts/build.ps1             Full build: PyInstaller + electron-builder
├── tests/                        pytest (engine) + node --test (settings)
└── docs/
    ├── REFACTOR_PLAN.md          Phased work plan
    └── DISTRIBUTION.md           Building, sizes, SmartScreen, signing
```

---

## Development setup

```powershell
# Node side
npm install

# Python side
python -m venv python\venv
.\python\venv\Scripts\Activate.ps1
pip install -r python\requirements.txt
pip install -r python\requirements-dev.txt
```

PyTorch must be the **cu128** build (Turing through Blackwell):

```powershell
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
```

Then put `ffmpeg.exe` and `ffprobe.exe` in `resources\bin\ffmpeg\`. They are
not in git.

```powershell
npm start
```

If that fails with *"Cannot read properties of undefined"*:
`Remove-Item Env:\ELECTRON_RUN_AS_NODE`.

---

## Testing

```powershell
# Engine: CPU only, synthetic fixtures, no GPU needed
python -m pytest tests -q

# Settings module
npm run test:js

# Headless separation, no Electron
python -m python.tools.separate_cli <input> <outdir> --mode vocal_remover
```

The harness prints input and output sample rate, bit depth, frame counts per
stem, and the peak of the null-test residual.

---

## Building the installer

```powershell
.\scripts\build.ps1
```

See **[docs/DISTRIBUTION.md](docs/DISTRIBUTION.md)** for build options,
installer size, the SmartScreen notice that belongs on the download page,
and how to turn on code signing.

---

## License

Proprietary. See `LICENSE.txt`. Demucs is MIT-licensed; its model weights
carry their own terms.
