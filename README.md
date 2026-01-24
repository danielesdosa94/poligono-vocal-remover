# Polígono Vocal Remover

AI-powered vocal removal tool for audio and video files.

## Phase 1: Communication Protocol Testing

This skeleton implements the robust communication layer between Electron and Python.

### Features Implemented

- ✅ JSON-based communication protocol
- ✅ Graceful process cancellation (SIGTERM/SIGINT handling)
- ✅ Line-by-line stdout parsing
- ✅ Video support (FFmpeg audio extraction)
- ✅ Multiple quality presets
- ✅ GPU/CPU device selection
- ✅ Structured error handling

### Project Structure

```
poligono-vocal-remover/
├── src/
│   ├── main.js              # Electron main process
│   └── renderer/
│       └── index.html       # Testing UI
├── python/
│   ├── motor.py             # AI processing engine
│   ├── requirements.txt
│   └── utils/
│       ├── protocol.py      # JSON protocol
│       └── signal_handler.py # Cancellation handling
├── package.json
└── PROJECT_STRUCTURE.md     # Full architecture docs
```

## Setup Instructions

### Prerequisites

- Node.js 18+ and npm
- Python 3.10+ 
- FFmpeg (for video support)
- CUDA Toolkit 11.8+ (optional, for GPU acceleration)

### 1. Install Node Dependencies

```bash
npm install
```

### 2. Setup Python Environment

```bash
# Create virtual environment
cd python
python -m venv venv

# Activate it
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# For GPU support (choose your CUDA version):
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
```

### 3. Download FFmpeg (for video support)

1. Download from: https://github.com/BtbN/FFmpeg-Builds/releases
2. Extract `ffmpeg.exe` and `ffprobe.exe` to `resources/bin/ffmpeg/`

### 4. Run Development Mode

```bash
npm run dev
```

## Protocol Reference

### Event Types (Python → Electron)

| Event | Description | Key Fields |
|-------|-------------|------------|
| `start` | Processing began | `file`, `model`, `device` |
| `step_change` | New processing phase | `step`, `stepNumber`, `totalSteps` |
| `progress` | Progress update | `stepPercent`, `globalPercent`, `eta` |
| `log` | Debug message | `message`, `level` |
| `warning` | Non-fatal issue | `message`, `code` |
| `error` | Error occurred | `message`, `code`, `fatal` |
| `success` | Processing complete | `outputs`, `elapsedSeconds` |
| `cancelled` | User cancelled | `reason`, `lastStep` |

### Example Protocol Message

```json
{
  "event": "progress",
  "timestamp": "2024-01-15T10:30:45.123456",
  "stepPercent": 45.5,
  "globalPercent": 38.2,
  "currentStep": "separating",
  "detail": "Processing chunk 3/10",
  "etaSeconds": 120
}
```

## Testing the Protocol

1. Run `npm run dev`
2. Select an audio file
3. Click "Start Processing"
4. Watch the Protocol Log panel for JSON messages
5. Test cancellation with "Cancel" button

## Next Steps (Phase 2)

- [ ] Waveform visualization
- [ ] File queue system
- [ ] Polished UI design
- [ ] Fun facts during processing
- [ ] ETA calculation
- [ ] PyInstaller compilation spec
