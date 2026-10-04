# wakeup.py
import time
import wave
import threading
import pyaudio
import numpy as np
from typing import Optional


# ------------------------------------------------------------
# 简单能量 VAD
# ------------------------------------------------------------
class EnergyVAD:
    """基于短时能量的语音活动检测器"""
    def __init__(self, threshold: float = 500):
        self.threshold = threshold

    def is_speech(self, frame_bytes: bytes, sample_rate: int) -> bool:
        data = np.frombuffer(frame_bytes, dtype=np.int16)
        return np.max(np.abs(data)) > self.threshold


# ------------------------------------------------------------

class _WakeupModule:
    """内部录音与语音端点检测"""
    
    # 音频参数
    def __init__(self, cfg: dict,vad):
        self.cfg=cfg
        self.vad=vad #注入

        self.sample_rate = cfg.get("sample_rate", 16000)
        self.frame_duration_ms = cfg.get("frame_duration_ms", 30)
        self.frame_bytes = int(self.sample_rate * (self.frame_duration_ms / 1000.0) * 2)
        self.mic_index = cfg.get("mic_index", 0)
        self.max_silence_frames = cfg.get("max_silence_frames", 90)
        self.user_speech_path = cfg.get("user_speech_path", "/tmp/user_speech.wav")

        self._state_lock = threading.Lock()
        self._op_lock = threading.Lock()
        self._running = False
        self._stop_event = threading.Event()
        self._pyaudio: Optional[pyaudio.PyAudio] = None
        self._audio_queue = []


        # ---------------- 生命周期 ----------------
    def start(self):
        with self._op_lock:
            if self._pyaudio is None:
                self._pyaudio = pyaudio.PyAudio()
        with self._state_lock:
            self._stop_event.clear()
            self._running = True

    def shutdown(self):
        with self._state_lock:
            self._running = False
        self._stop_event.set()

    def cleanup(self):
        with self._op_lock:
            if self._pyaudio is not None:
                try:
                    self._pyaudio.terminate()
                except Exception:
                    pass
                self._pyaudio = None
        with self._state_lock:
            self._audio_queue.clear()

    # ---------------- 进程循环 ----------------
    def wakeup_loop(self):
        while True:
            with self._state_lock:
                if not self._running:
                    break
                stop_event = self._stop_event

            path = self._record_utterance(stop_event)
            if path:
                with self._state_lock:
                    self._audio_queue.append(path)

    # ---------------- 状态查询 ----------------
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    def get_audio_path(self) -> Optional[str]:
        with self._state_lock:
            if self._audio_queue:
                return self._audio_queue.pop(0)
            return None

    def get_pending_count(self) -> int:
        with self._state_lock:
            return len(self._audio_queue)

    # ---------------- 业务操作 ----------------
    def _record_utterance(self, stop_event) -> Optional[str]:
        with self._op_lock:
            p = self._pyaudio
        if p is None:
            return None

        audio_buffer = []
        silence_counter = 0
        speaking = False

        stream_kwargs = {
            "format": pyaudio.paInt16,
            "channels": 1,
            "rate": self.sample_rate,
            "input": True,
            "frames_per_buffer": self.frame_bytes,
        }
        if self.mic_index != 0:
            stream_kwargs["input_device_index"] = self.mic_index

        stream = None
        try:
            stream = p.open(**stream_kwargs)
            while not stop_event.is_set():
                frame = stream.read(self.frame_bytes, exception_on_overflow=False)
                if self.vad.is_speech(frame, self.sample_rate):
                    speaking = True
                    audio_buffer.append(frame)
                    silence_counter = 0
                else:
                    if speaking:
                        silence_counter += 1
                        audio_buffer.append(frame)
                        if silence_counter >= self.max_silence_frames:
                            break
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

        if not speaking or not audio_buffer:
            return None

        if len(audio_buffer) > self.max_silence_frames:
            clean = audio_buffer[:-self.max_silence_frames]
        else:
            clean = audio_buffer

        with wave.open(self.user_speech_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(b"".join(clean))
        return self.user_speech_path