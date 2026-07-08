# wakeup.py
import time
import wave
import threading
import pyaudio
import numpy as np
import logging
from typing import Optional

# 环境检测
from drivers import is_real_jetson


# ------------------------------------------------------------
# 简单能量 VAD
# ------------------------------------------------------------
class EnergyVAD:
    """基于短时能量的语音活动检测器"""
    def __init__(self, threshold: float = 500):
        self.threshold = threshold

    def is_speech(self, frame_bytes: bytes, sample_rate: int) -> bool:
        """输入 bytes 帧，返回是否有人声"""
        data = np.frombuffer(frame_bytes, dtype=np.int16)
        return np.max(np.abs(data)) > self.threshold


# ------------------------------------------------------------
# _WakeupImpl 内部实现（录音 + VAD 端点检测）
# ------------------------------------------------------------
class _WakeupImpl:
    """实际录音与语音端点检测，不对外暴露"""

    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger

        # 音频参数
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
        """开启麦克风监听（准备录音），此时并不开始录音"""
        self._running = True
        self._stop_event.clear()
        self.logger.info("Wakeup 模块已开启")

    def stop(self):
        """停止监听"""
        self._running = False
        self._stop_event.set()
        self.logger.info("Wakeup 模块已停止")

    def detect(self) -> Optional[str]:
        """
        阻塞式等待，直到检测到完整语音并保存为 WAV 文件。
        返回 WAV 文件路径，若被停止则返回 None。
        """
        if not self._running:
            self.logger.warning("Wakeup 模块未启动，自动调用 open()")
            self.open()

        self.logger.info("等待语音输入...")
        return self._record_utterance()

    def _record_utterance(self) -> Optional[str]:
        """基于 VAD 录制一段完整语音，返回 WAV 路径或 None"""
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
            self.logger.info("未检测到有效语音")
            return None

        # 去掉尾部静音帧
        clean = audio_buffer[:-self.max_silence_frames] if len(audio_buffer) > self.max_silence_frames else audio_buffer

        with wave.open(self.user_speech_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
            wf.setframerate(self.sample_rate)
            wf.writeframes(b''.join(clean))
        self.logger.info(f"语音已保存至 {self.user_speech_path}")
        return self.user_speech_path


# ------------------------------------------------------------
# WakeupDetector 对外顶层类
# ------------------------------------------------------------
class WakeupDetector:
    """
    语音活动检测器，对外只暴露 open/stop/detect 三个方法。
    detect() 阻塞等待语音输入，返回 WAV 文件路径。
    """

    def __init__(self, cfg: dict = None):
        if cfg is None:
            cfg = {}
        self.logger = logging.getLogger("WakeupDetector")
        # 环境日志（不影响功能）
        if is_real_jetson():
            self.logger.info("Wakeup 模块运行在 Jetson 真机环境")
        else:
            self.logger.info("Wakeup 模块运行在非 Jetson 环境")
        self.impl = _WakeupImpl(cfg, self.logger)

    def open(self):
        """开启麦克风监听准备"""
        self.impl.open()

    def stop(self):
        """停止监听"""
        self.impl.stop()

    def detect(self) -> Optional[str]:
        """
        阻塞等待语音输入，返回 WAV 文件路径。
        若被 stop() 中断，返回 None。
        """
        return self.impl.detect()