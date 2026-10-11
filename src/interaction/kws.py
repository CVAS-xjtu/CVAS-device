# kws.py
import os
import time
import wave
import threading
import pyaudio  # type: ignore
from typing import Optional


class KWSModule:
    """
    关键词检测 + 关键词后录音：
      - 麦克风常开
      - pause() 时丢弃帧，不检测也不录音
      - 不创建线程，由节点创建 kws_loop
    """

    def __init__(self, cfg: dict, vad):
        self.cfg = cfg
        self.vad = vad

        self.sample_rate = cfg.get("sample_rate", 16000)
        self.frame_duration_ms = cfg.get("frame_duration_ms", 30)
        self.frame_bytes = int(self.sample_rate * (self.frame_duration_ms / 1000.0) * 2)
        self.mic_index = cfg.get("mic_index", 0)
        self.keywords = cfg.get("keywords", [])
        self.kws_model = cfg.get("kws_model")
        self.max_silence_frames = cfg.get("max_silence_frames", 90)
        self.record_timeout = cfg.get("record_timeout", 8.0)
        self.audio_path = cfg.get("audio_path", "/tmp/kws_utterance.wav")

        self._state_lock = threading.Lock()
        self._op_lock = threading.Lock()

        self._running = False
        self._active = False
        self._paused = False
        self._mode = "listening"

        self._detected_keyword: Optional[str] = None
        self._detected_event = threading.Event()
        self._audio_queue = []

        self._audio_buffer = []
        self._silence_counter = 0
        self._speaking = False
        self._record_start_time = 0.0

        self._pa = None
        self._audio_stream = None

    # ---------------- 生命周期 ----------------
    def start(self):
        with self._op_lock:
            if self._pa is None:
                self._pa = pyaudio.PyAudio()
        with self._state_lock:
            self._running = True

    def shutdown(self):
        with self._state_lock:
            self._running = False

    def cleanup(self):
        self.deactivate()
        with self._op_lock:
            if self._pa is not None:
                try:
                    self._pa.terminate()
                except Exception:
                    pass
                self._pa = None
        with self._state_lock:
            self._audio_queue.clear()
            self._audio_buffer = []

    # ---------------- 麦克风开关 ----------------
    def activate(self):
        with self._op_lock:
            if self._audio_stream is not None:
                return
            if self._pa is None:
                self._pa = pyaudio.PyAudio()
            stream_kwargs = {
                "format": pyaudio.paInt16,
                "channels": 1,
                "rate": self.sample_rate,
                "input": True,
                "frames_per_buffer": self.frame_bytes,
            }
            if self.mic_index != 0:
                stream_kwargs["input_device_index"] = self.mic_index
            try:
                self._audio_stream = self._pa.open(**stream_kwargs)
            except Exception:
                self._audio_stream = None
                return
        with self._state_lock:
            self._active = True

    def deactivate(self):
        with self._state_lock:
            self._active = False
        with self._op_lock:
            if self._audio_stream is not None:
                try:
                    self._audio_stream.stop_stream()
                    self._audio_stream.close()
                except Exception:
                    pass
                self._audio_stream = None

    # ---------------- 暂停 / 恢复 ----------------
    def pause(self):
        with self._state_lock:
            self._paused = True
            self._audio_buffer = []
            self._silence_counter = 0
            self._speaking = False
            self._mode = "listening"

    def resume(self):
        with self._state_lock:
            self._paused = False

    # ---------------- 进程循环 ----------------
    def kws_loop(self):
        while True:
            with self._state_lock:
                if not self._running:
                    break
                active = self._active
                paused = self._paused
                mode = self._mode

            if not active:
                time.sleep(0.05)
                continue

            frame = self._read_frame()
            if frame is None:
                continue

            if paused:
                continue

            if mode == "listening":
                self._listening_step(frame)
            elif mode == "recording":
                self._recording_step(frame)

    # ---------------- 状态查询 ----------------
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    def is_active(self) -> bool:
        with self._state_lock:
            return self._active

    def is_paused(self) -> bool:
        with self._state_lock:
            return self._paused

    def is_recording(self) -> bool:
        with self._state_lock:
            return self._mode == "recording"

    def is_keyword_detected(self) -> bool:
        return self._detected_event.is_set()

    def get_detected_keyword(self) -> Optional[str]:
        with self._state_lock:
            return self._detected_keyword

    def get_audio_path(self) -> Optional[str]:
        with self._state_lock:
            if self._audio_queue:
                return self._audio_queue.pop(0)
            return None

    def get_pending_count(self) -> int:
        with self._state_lock:
            return len(self._audio_queue)

    # ---------------- 业务操作 ----------------
    def clear_detection(self):
        with self._state_lock:
            self._detected_keyword = None
            self._detected_event.clear()

    # ---------------- 内部 ----------------
    def _read_frame(self) -> Optional[bytes]:
        with self._op_lock:
            stream = self._audio_stream
        if stream is None:
            time.sleep(0.05)
            return None
        try:
            return stream.read(self.frame_bytes, exception_on_overflow=False)
        except Exception:
            time.sleep(0.05)
            return None

    def _listening_step(self, frame: bytes):
        if self.kws_model is None:
            time.sleep(0.05)
            return
        try:
            detected = self.kws_model.detect(frame)
        except Exception:
            detected = None

        if not detected or detected not in self.keywords:
            return

        with self._state_lock:
            self._detected_keyword = detected
            self._detected_event.set()
            self._mode = "recording"
            self._audio_buffer = []
            self._silence_counter = 0
            self._speaking = False
            self._record_start_time = time.time()

    def _recording_step(self, frame: bytes):
        if time.time() - self._record_start_time > self.record_timeout:
            self._finalize_recording()
            return

        is_speech = self.vad.is_speech(frame, self.sample_rate)
        if is_speech:
            self._speaking = True
            self._audio_buffer.append(frame)
            self._silence_counter = 0
        else:
            if self._speaking:
                self._silence_counter += 1
                self._audio_buffer.append(frame)
                if self._silence_counter >= self.max_silence_frames:
                    self._finalize_recording()

    def _finalize_recording(self):
        with self._state_lock:
            buffer_copy = list(self._audio_buffer)
            self._audio_buffer = []
            self._silence_counter = 0
            self._speaking = False
            self._mode = "listening"

        # 丢弃过短录音
        if not buffer_copy or len(buffer_copy) < 3:
            return

        if len(buffer_copy) > self.max_silence_frames:
            clean = buffer_copy[:-self.max_silence_frames]
        else:
            clean = buffer_copy

        # 每次用带时间戳的新路径，避免覆盖正在被 ASR 读取的文件
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        suffix = f"{int(time.time() * 1000) % 1000:03d}"
        path = f"{self.audio_path}_{timestamp}_{suffix}.wav"

        try:
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
                wf.writeframes(b"".join(clean))
        except Exception:
            return

        with self._state_lock:
            self._audio_queue.append(path)