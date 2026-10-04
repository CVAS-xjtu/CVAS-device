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
# _WakeupImpl 内部实现（录音 + VAD 端点检测）
# ------------------------------------------------------------
class _WakeupImpl:
    """内部录音与语音端点检测"""
    
    # 音频参数
    def __init__(self, cfg: dict):
        self.sample_rate = cfg.get("sample_rate", 16000)
        self.frame_duration_ms = cfg.get("frame_duration_ms", 30)
        self.frame_bytes = int(self.sample_rate * (self.frame_duration_ms / 1000.0) * 2)
        self.mic_index = cfg.get("mic_index", 0)
        self.max_silence_frames = cfg.get("max_silence_frames", 90)

        # VAD 实例（可注入自定义 VAD）
        vad = cfg.get("vad")
        if vad is not None:
            self.vad = vad
        else:
            vad_threshold = cfg.get("vad_threshold", 500)
            self.vad = EnergyVAD(threshold=vad_threshold)

        # 录音保存路径
        self.user_speech_path = cfg.get("user_speech_path", "/tmp/user_speech.wav")
        
        # 状态
        self._running = False
        self._stop_event = threading.Event()

    def open(self):
        self._running = True
        self._stop_event.clear()

    def stop(self):
        self._running = False
        self._stop_event.set()

    def detect(self) -> Optional[str]:
        if not self._running:
            self.open()
        return self._record_utterance()

    def _record_utterance(self) -> Optional[str]:
        p = pyaudio.PyAudio()
        audio_buffer = []
        silence_counter = 0
        speaking = False

        stream_kwargs = {
            'format': pyaudio.paInt16,
            'channels': 1,
            'rate': self.sample_rate,
            'input': True,
            'frames_per_buffer': self.frame_bytes,
        }
        if self.mic_index != 0:
            stream_kwargs['input_device_index'] = self.mic_index

        try:
            stream = p.open(**stream_kwargs)
            while not self._stop_event.is_set():
                frame = stream.read(self.frame_bytes, exception_on_overflow=False)
                is_speech = self.vad.is_speech(frame, self.sample_rate)

                if is_speech:
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
            stream.stop_stream()
            stream.close()
            p.terminate()

        if not speaking or len(audio_buffer) == 0:
            return None

        # 去掉尾部静音帧
        clean = audio_buffer[:-self.max_silence_frames] if len(audio_buffer) > self.max_silence_frames else audio_buffer
        with wave.open(self.user_speech_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
            wf.setframerate(self.sample_rate)
            wf.writeframes(b''.join(clean))
        return self.user_speech_path


# ------------------------------------------------------------
# WakeupDetector 对外顶层类
# ------------------------------------------------------------
class WakeupDetector:
    """语音唤醒/活动检测器，对外提供 open/stop/detect"""

    def __init__(self, cfg: dict = None):
        if cfg is None:
            cfg = {}
        self.impl = _WakeupImpl(cfg)

    def open(self):
        self.impl.open()

    def stop(self):
        self.impl.stop()

    def detect(self) -> Optional[str]:
        return self.impl.detect()