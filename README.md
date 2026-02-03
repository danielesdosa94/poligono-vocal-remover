# Polígono AI Hub

Professional AI Audio Processing Suite for Sound Designers and Musicians.
Developed by **Polígono Studio**.

![Polígono AI Hub](assets/logo.png)

## Overview

Polígono AI Hub is a local, desktop-based application that leverages state-of-the-art AI models (Demucs v4) to separate audio tracks. Unlike cloud services, it runs entirely offline, utilizing your GPU for maximum privacy and speed.

It features a robust **Batch Processing Queue**, allowing users to drag and drop multiple files and let the AI work in the background without freezing the UI.

## Key Features

### 🎧 Processing Modes
* **Vocal Remover (2 Tracks):** Extracts *Vocals* and creates a mixed *Instrumental* track. Ideal for karaoke, ADR, and lip-sync analysis.
* **Stem Splitter (4 Tracks):** Separates audio into *Vocals, Bass, Drums, and Other*. Perfect for remixing and sampling.

### 🚀 Core Functionality
* **Batch Queue System:** Process hundreds of files sequentially.
* **Smart State Management:** Distinct "Ready" vs "Waiting" states with realistic progress bars.
* **Format Agnostic:** Accepts Audio (WAV, MP3, FLAC) and Video (MP4, MOV, MKV) inputs.
* **Output Flexibility:** Exports to WAV (Lossless), FLAC, or MP3.
* **Hardware Acceleration:** Auto-detects CUDA (NVIDIA GPU) for 10x faster inference compared to CPU.

### 🎨 UI/UX
* **Hub Architecture:** Single-view interface with tabbed navigation.
* **Polígono Design System:** Dark mode optimized for studio environments.
* **Internationalization:** Native support for English (EN) and Spanish (ES) [Coming Soon].

---

## Project Structure

```text
poligono-ai-hub/
├── src/
│   ├── main.js              # Electron Orchestrator (Window & Process Mgmt)
│   └── renderer/
│       ├── index.html       # Single Page Application (Hub UI)
│       ├── css/
│       │   └── styles.css   # Polígono Design System
│       ├── js/
│       │   ├── app.js       # UI Logic & Queue Manager
│       │   └── translations.js # i18n Dictionaries (EN/ES)
│       └── assets/
│           └── logo.png     # Branding
├── python/
│   ├── motor.py             # Python AI Engine (Demucs Wrapper)
│   └── utils/
│       └── protocol.py      # JSON Communication Layer
├── package.json             # Node Dependencies & Build Scripts
└── README.md                # Documentation

## Setup Instructions

### Prerequisites
* **Node.js:** v18 or higher.
* **Python:** v3.10 (Recommended).
* **FFmpeg:** Required for video file processing.
* **CUDA Toolkit 11.8+:** Highly recommended for NVIDIA GPU users.

### 1. Installation

```bash
# Clone repository
git clone [repo-url]

# Install Node dependencies
npm install

# Setup Python Environment
cd python
python -m venv venv
# Activate: .\venv\Scripts\activate (Windows) or source venv/bin/activate (Mac/Linux)
pip install -r requirements.txt

2. External Dependencies
FFmpeg: Download ffmpeg.exe and ffprobe.exe and place them in resources/bin/ffmpeg/.

PyTorch (GPU): Ensure you install the CUDA-enabled version of PyTorch: pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118

3. Development Run
Bash
npm run dev

Roadmap
[x] Phase 1: Core AI Implementation & Protocol (Completed)

[x] Phase 2: Queue System & Video Support (Completed)

[x] Phase 3: UI/UX Overhaul & Branding (Completed)

[ ] Phase 4: Internationalization (In Progress)

[ ] Phase 5: Compilation (.exe) & Installer

License: Proprietary - Polígono Studio.

---