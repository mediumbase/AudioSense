# AudioSense

## Description
AudioSense is a sophisticated web application for real-time audio monitoring and analysis. It features a responsive sound meter with dynamic visualizations, auto-recording triggered by customizable decibel thresholds, and audio file management with playback controls. Built with FastAPI and WebSocket for seamless audio processing and a sleek, dark-themed UI for scientific precision.

## Setup Instructions
# Fix "Host is down" error for arecord
export XDG_RUNTIME_DIR=/run/user/$(id -u)

# Test recording with ALSA directly
arecord -D hw:0,0 -d 5 -f cd test.wav

# List available audio devices
python3 -c "import sounddevice as sd; print(sd.query_devices())"

# Update FastAPI code: change DEVICE to 'default'
# In your Python script, replace:
# DEVICE = "hw:0,0"
# with:
# DEVICE = "default"

# Audio recording works with 'hw:0,0' and 'default'
# Update FastAPI code to use DEVICE='default' for reliability
sed -i 's/DEVICE = "hw:0,0"/DEVICE = "default"/' your_script.py

1. **Clone the repository**: `git clone <repository-url>`
2. **Install dependencies**: `pip install fastapi uvicorn sounddevice numpy librosa`
3. **Create recordings directory**: `mkdir recordings`
4. **Run the server**: `python main.py`
5. **Access the app**: Open `http://localhost:8000` in a browser

# Restart FastAPI server
pkill uvicorn
uvicorn your_script:app --host 0.0.0.0 --port 8000 &

## Current Features
1. Real-time audio amplitude monitoring with dynamic wave visualization
2. Auto-recording based on customizable decibel thresholds
3. Recording management (play, pause, delete, download, share)
4. Spectrogram visualization for audio frequency analysis
5. Adjustable parameters for threshold, duration, and silence timeout

## Potential Features
1. Advanced audio feature extraction (e.g., pitch, tempo)
2. Cloud storage integration for recordings
3. Multi-device audio input support
4. Real-time noise suppression or filtering
5. Exportable analysis reports in multiple formats