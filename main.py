from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.requests import Request
from fastapi.staticfiles import StaticFiles
import sounddevice as sd
import numpy as np
import asyncio
import logging
import os
import wave
import datetime
from asyncio import Lock
from typing import Optional
from starlette.websockets import WebSocketState
import librosa

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/recordings", StaticFiles(directory="recordings"), name="recordings")

DEVICE = os.getenv("AUDIO_DEVICE", "default")
SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", 44100))
BLOCK_SIZE = int(os.getenv("BLOCK_SIZE", 1024))
auto_record_threshold = float(os.getenv("AUTO_RECORD_THRESHOLD", -40.0))
min_recording_duration = float(os.getenv("MIN_RECORDING_DURATION", 5.0))
silence_timeout = float(os.getenv("SILENCE_TIMEOUT", 2.0))
max_recording_duration = float(os.getenv("MAX_RECORDING_DURATION", 30.0))

class RecordingState:
    def __init__(self):
        self.is_recording = False
        self.recorder: Optional[wave.Wave_write] = None
        self.current_filename: Optional[str] = None
        self.last_loud_time: float = 0
        self.recording_start_time: float = 0
        self.lock = Lock()

recording_state = RecordingState()

@app.get("/", response_class=HTMLResponse)
async def get(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

async def check_auto_recording(db_level: float, websocket: WebSocket):
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    current_time = datetime.datetime.now().timestamp()
    logger.debug(f"Current dB: {db_level:.2f}, Threshold: {auto_record_threshold}")
    if not recording_state.is_recording and db_level > auto_record_threshold:
        logger.info(f"Threshold exceeded ({db_level:.2f} > {auto_record_threshold}) - starting recording")
        async with recording_state.lock:
            if not recording_state.is_recording:
                await start_auto_recording(websocket)
                recording_state.last_loud_time = current_time
                recording_state.recording_start_time = current_time
                return
    if recording_state.is_recording and db_level > auto_record_threshold:
        recording_state.last_loud_time = current_time
    if recording_state.is_recording:
        silence_duration = current_time - recording_state.last_loud_time
        recording_duration = current_time - recording_state.recording_start_time
        if recording_duration > max_recording_duration:
            logger.info(f"Max duration exceeded ({recording_duration:.2f} > {max_recording_duration}) - stopping recording")
            async with recording_state.lock:
                if recording_state.is_recording:
                    await stop_auto_recording(websocket)
        elif silence_duration > silence_timeout and recording_duration > min_recording_duration:
            logger.info(f"Silence timeout ({silence_duration:.2f} > {silence_timeout}) and min duration met - stopping recording")
            async with recording_state.lock:
                if recording_state.is_recording:
                    await stop_auto_recording(websocket)

async def start_auto_recording(websocket: WebSocket):
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    recording_state.current_filename = datetime.datetime.now().strftime("%Y%m%d_%H%M%S.wav")
    path = f"recordings/{recording_state.current_filename}"
    recording_state.recorder = wave.open(path, 'wb')
    recording_state.recorder.setnchannels(1)
    recording_state.recorder.setsampwidth(2)
    recording_state.recorder.setframerate(SAMPLE_RATE)
    recording_state.is_recording = True
    logger.info("Auto-recording started")
    if websocket.client_state == WebSocketState.CONNECTED:
        try:
            await websocket.send_json({
                "event": "auto_recording_started",
                "filename": recording_state.current_filename,
                "threshold": auto_record_threshold
            })
        except RuntimeError:
            logger.debug("Ignored send after close")

async def stop_auto_recording(websocket: WebSocket):
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    duration = datetime.datetime.now().timestamp() - recording_state.recording_start_time
    filename = recording_state.current_filename
    path = f"recordings/{filename}" if filename else None
    if recording_state.recorder:
        recording_state.recorder.close()
        recording_state.recorder = None
    recording_state.is_recording = False
    if duration < min_recording_duration and path and os.path.exists(path):
        os.remove(path)
        logger.info(f"Recording discarded: too short ({duration:.2f}s)")
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_json({
                    "event": "auto_recording_discarded",
                    "filename": filename,
                    "duration": duration
                })
            except RuntimeError:
                logger.debug("Ignored send after close")
    else:
        logger.info(f"Auto-recording stopped, duration: {duration:.2f}s")
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_json({
                    "event": "auto_recording_stopped",
                    "filename": filename,
                    "duration": duration
                })
            except RuntimeError:
                logger.debug("Ignored send after close")

async def audio_callback(indata, frames, time, status, websocket):
    try:
        indata = np.clip(indata, -1.0, 1.0)
        if status:
            logger.warning(str(status))
            if websocket.client_state == WebSocketState.CONNECTED:
                try:
                    await websocket.send_json({"warning": str(status), "log": str(status)})
                except RuntimeError:
                    logger.debug("Ignored send after close")
        rms = np.sqrt(np.mean(indata**2))
        db = 20 * np.log10(rms) if rms > 0 else -100
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_json({"amplitude_db": float(db)})
            except RuntimeError:
                logger.debug("Ignored send after close")
        await check_auto_recording(db, websocket)
        if recording_state.is_recording and recording_state.recorder:
            int_data = (indata * 32767).astype(np.int16)
            recording_state.recorder.writeframes(int_data.tobytes())
    except Exception as e:
        logger.error(f"Callback error: {e}")
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_json({"error": f"Callback error: {str(e)}"})
            except RuntimeError:
                logger.debug("Ignored send after close")

@app.websocket("/ws/sound-meter")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    loop = asyncio.get_running_loop()
    def callback(indata, frames, time, status):
        loop.create_task(audio_callback(indata, frames, time, status, websocket))
    try:
        devices = sd.query_devices()
        if DEVICE not in [d['name'] for d in devices if d['max_input_channels'] > 0]:
            if websocket.client_state == WebSocketState.CONNECTED:
                try:
                    await websocket.send_json({"error": "Device not available or no input channels"})
                except RuntimeError:
                    logger.debug("Ignored send after close")
            return
        with sd.InputStream(
            device=DEVICE,
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            channels=1,
            dtype='float32',
            callback=callback
        ) as stream:
            logger.info(f"Started audio stream with device: {DEVICE}")
            if websocket.client_state == WebSocketState.CONNECTED:
                try:
                    await websocket.send_json({
                        "log": "Audio stream started",
                        "auto_recording_config": {
                            "threshold": auto_record_threshold,
                            "min_duration": min_recording_duration,
                            "max_duration": max_recording_duration,
                            "silence_timeout": silence_timeout
                        }
                    })
                except RuntimeError:
                    logger.debug("Ignored send after close")
            while True:
                await asyncio.sleep(1)
    except sd.PortAudioError as e:
        logger.error(f"PortAudio error: {str(e)}")
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_json({"error": f"Audio device error: {str(e)}", "log": f"Error: {str(e)}"})
            except RuntimeError:
                logger.debug("Ignored send after close")
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_json({"error": f"Unexpected error: {str(e)}", "log": f"Error: {str(e)}"})
            except RuntimeError:
                logger.debug("Ignored send after close")
    finally:
        logger.info("Closing WebSocket connection")
        await websocket.close()

@app.post("/start_record")
async def start_record():
    async with recording_state.lock:
        if recording_state.is_recording:
            return JSONResponse({"status": "already recording"})
        recording_state.current_filename = datetime.datetime.now().strftime("%Y%m%d_%H%M%S.wav")
        path = f"recordings/{recording_state.current_filename}"
        recording_state.recorder = wave.open(path, 'wb')
        recording_state.recorder.setnchannels(1)
        recording_state.recorder.setsampwidth(2)
        recording_state.recorder.setframerate(SAMPLE_RATE)
        recording_state.is_recording = True
        logger.info("Recording started")
        return JSONResponse({"status": "recording started", "filename": recording_state.current_filename})

@app.post("/stop_record")
async def stop_record():
    async with recording_state.lock:
        if not recording_state.is_recording:
            return JSONResponse({"status": "not recording"})
        if recording_state.recorder:
            recording_state.recorder.close()
            recording_state.recorder = None
        recording_state.is_recording = False
        logger.info("Recording stopped")
        return JSONResponse({"status": "recording stopped", "filename": recording_state.current_filename})

@app.get("/records")
async def get_records():
    files = []
    for f in os.listdir("recordings"):
        if f.endswith(".wav"):
            path = os.path.join("recordings", f)
            mtime = os.path.getmtime(path)
            dt = datetime.datetime.fromtimestamp(mtime)
            files.append({
                "file": f,
                "date": dt.strftime("%Y-%m-%d"),
                "time": dt.strftime("%H:%M:%S")
            })
    return JSONResponse(files)

@app.get("/status")
async def get_status():
    devices = sd.query_devices()
    hardware = next((d for d in devices if d['name'] == DEVICE), None) or next((d for d in devices if 'default' in d['name']), None) or (devices[0] if devices else "No devices found")
    mic_status = "Connected" if hardware else "Disconnected"
    system_status = "Running"
    return JSONResponse({
        "mic_status": mic_status,
        "system_status": system_status,
        "hardware": hardware
    })

@app.post("/update_params")
async def update_params(params: dict):
    global auto_record_threshold, min_recording_duration, max_recording_duration, silence_timeout
    if 'auto_record_threshold' in params:
        auto_record_threshold = float(params['auto_record_threshold'])
    if 'min_recording_duration' in params:
        min_recording_duration = float(params['min_recording_duration'])
    if 'max_recording_duration' in params:
        max_recording_duration = float(params['max_recording_duration'])
    if 'silence_timeout' in params:
        silence_timeout = float(params['silence_timeout'])
    logger.info("Parameters updated")
    return JSONResponse({"status": "updated"})

@app.delete("/api/records/{filename}")
async def delete_recording(filename: str):
    if '../' in filename or not filename.endswith('.wav'):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = f"recordings/{filename}"
    if os.path.exists(path):
        try:
            os.remove(path)
            logger.info(f"Deleted recording: {filename}")
            return JSONResponse({"status": "deleted"})
        except Exception as e:
            logger.error(f"Delete error for {filename}: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")
    else:
        raise HTTPException(status_code=404, detail="File not found")

@app.get("/audio-features/{filename}")
async def get_features(filename: str):
    path = f"recordings/{filename}"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    y, sr = librosa.load(path)
    mfcc = librosa.feature.mfcc(y=y, sr=sr)
    return {"mfcc": mfcc.tolist()}

if __name__ == "__main__":
    os.makedirs("recordings", exist_ok=True)
    import uvicorn
    logger.info("Starting FastAPI server")
    uvicorn.run(app, host="0.0.0.0", port=8000)