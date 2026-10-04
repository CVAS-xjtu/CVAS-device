# asr.py
import time
import threading
import numpy as np
from typing import Optional

from ..interaction.kws import WakeupDetector
from .preprocess import Denoiser


# ------------------------------------------------------------
# _ASRImpl 内部实现（语音识别流水线）
# ------------------------------------------------------------
class _ASRImpl:
    """ASR 核心处理类：持有唤醒器、降噪器、模型接口"""

    def __init__(self, cfg: dict, model_interface):
        self.cfg = cfg
        self.model_interface = model_interface
        self.use_online_asr = cfg.get("use_online_asr", False)
        
        # 创建 WakeupDetector（可传递 VAD 相关配置）
        wakeup_cfg = {
            "sample_rate": cfg.get("sample_rate", 16000),
            "frame_duration_ms": cfg.get("frame_duration_ms", 30),
            "mic_index": cfg.get("mic_index", 0),
            "max_silence_frames": cfg.get("max_silence_frames", 90),
            "vad_threshold": cfg.get("vad_threshold", 500),
            "user_speech_path": cfg.get("user_speech_path", "/tmp/user_speech.wav"),
            "vad": cfg.get("vad"),
        }
        self.wakeup = WakeupDetector(wakeup_cfg)

        # 降噪
        self.denoiser_enabled = cfg.get("enable_denoiser", False)
        self.denoiser = None
        if self.denoiser_enabled:
            denoiser_cfg = {
                "sample_rate": cfg.get("sample_rate", 16000),
                "over_subtraction_factor": cfg.get("denoise_over_subtraction_factor", 2.0),
                "spectral_floor": cfg.get("denoise_spectral_floor", 0.01),
                "noise_frames": cfg.get("denoise_noise_frames", 10),
            }
            self.denoiser = Denoiser(denoiser_cfg)

        # 线程控制
        self._running = False
        self._listen_thread = None
        self._result_queue = []
        self._lock = threading.Lock()

    def open(self, noise_sample: np.ndarray = None):
        if self._running:
            return
        self._running = True
        
        # 打开唤醒器（开始准备录音）
        self.wakeup.open()
        
        # 打开降噪器（可传入环境噪声样本）
        if self.denoiser_enabled and self.denoiser:
            self.denoiser.open(noise_audio=noise_sample)
            
        # 启动后台线程，持续调用 detect() -> 降噪 -> 识别 -> 结果入队
        self._listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._listen_thread.start()

    def stop(self):
        if not self._running:
            return
        self._running = False
        # 唤醒器的 stop 会设置内部停止事件，使阻塞的 detect() 返回 None 
        self.wakeup.stop()
        if self._listen_thread and self._listen_thread.is_alive():
            self._listen_thread.join(timeout=2.0)
        if self.denoiser_enabled and self.denoiser:
            self.denoiser.stop()

    def listen(self) -> Optional[str]:
        with self._lock:
            if self._result_queue:
                return self._result_queue.pop(0)
            return None

    # ---------- 内部线程函数 ----------
    def _listen_loop(self):
        while self._running:
            audio_path = self.wakeup.detect()
            if audio_path is None:
                # 被停止或未检测到语音，稍作休眠避免忙等
                if not self._running:
                    break
                time.sleep(0.2)
                continue

            # 降噪预处理（若启用）
            if self.denoiser_enabled and self.denoiser:
                try:
                    import soundfile as sf
                    audio, sr = sf.read(audio_path)
                    audio = self.denoiser.process(audio)
                    sf.write(audio_path, audio, sr)
                except Exception:
                    pass
                
            # ASR 识别
            text = self._asr_conversion(audio_path)
            if text and text != 'error':
                with self._lock:
                    self._result_queue.append(text)

    def _asr_conversion(self, audio_file: str) -> str:
        if not self.model_interface:
            return 'error'
        try:
            if self.use_online_asr:
                status, text = self.model_interface.online_asr(audio_file)
            else:
                status, text = self.model_interface.SenseVoiceSmall_ASR(audio_file)
            if status == 'ok' and len(text) > 4:
                return text
            else:
                return 'error'
        except Exception:
            return 'error'


# ------------------------------------------------------------
# SpeechRecognizer 对外顶层类
# ------------------------------------------------------------
class SpeechRecognizer:
    """语音识别器，对外提供 open/stop/listen"""

    def __init__(self, cfg: dict = None, model_interface=None):
        if cfg is None:
            cfg = {}
        self.impl = _ASRImpl(cfg, model_interface)

    def open(self, noise_sample: np.ndarray = None):
        self.impl.open(noise_sample)

    def stop(self):
        self.impl.stop()

    def listen(self) -> Optional[str]:
        return self.impl.listen()