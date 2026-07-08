# asr.py
import time
import threading
import logging
import numpy as np
from typing import Optional

# 环境检测
from drivers import is_real_jetson

# 唤醒/语音活动检测模块
from .wakeup import WakeupDetector

# 降噪模块
from .preprocess import Denoiser


# ------------------------------------------------------------
# _ASRImpl 内部实现（语音识别流水线）
# ------------------------------------------------------------
class _ASRImpl:
    """ASR 核心处理类，持有 WakeupDetector、模型接口和降噪器"""

    def __init__(self, cfg: dict, model_interface, logger: logging.Logger):
        self.cfg = cfg
        self.model_interface = model_interface
        self.logger = logger

        # 在线/离线模式
        self.use_online_asr = cfg.get("use_online_asr", False)

        # 创建 WakeupDetector（可传递 VAD 相关配置）
        wakeup_cfg = {
            "sample_rate": cfg.get("sample_rate", 16000),
            "frame_duration_ms": cfg.get("frame_duration_ms", 30),
            "mic_index": cfg.get("mic_index", 0),
            "max_silence_frames": cfg.get("max_silence_frames", 90),
            "vad_threshold": cfg.get("vad_threshold", 500),
            "user_speech_path": cfg.get("user_speech_path", "/tmp/user_speech.wav"),
            "vad": cfg.get("vad"),  # 可注入自定义 VAD 实例
        }
        self.wakeup = WakeupDetector(wakeup_cfg)

        # 降噪器
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
        """启动识别服务：打开麦克风监听、降噪器，并启动后台处理线程"""
        if self._running:
            return
        self._running = True

        # 打开唤醒器（开始准备录音）
        self.wakeup.open()

        # 打开降噪器（可传入环境噪声样本）
        if self.denoiser_enabled and self.denoiser:
            self.denoiser.open(noise_audio=noise_sample)
            self.logger.info("降噪器已开启（ASR 集成模式）")

        # 启动后台线程，持续调用 detect() -> 降噪 -> 识别 -> 结果入队
        self._listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._listen_thread.start()
        self.logger.info("语音识别服务已启动")

    def stop(self):
        """停止识别服务"""
        if not self._running:
            return
        self._running = False
        # 唤醒器的 stop 会设置内部停止事件，使阻塞的 detect() 返回 None
        self.wakeup.stop()
        if self._listen_thread and self._listen_thread.is_alive():
            self._listen_thread.join(timeout=2.0)
        if self.denoiser_enabled and self.denoiser:
            self.denoiser.stop()
        self.logger.info("语音识别服务已停止")

    def listen(self) -> Optional[str]:
        """非阻塞获取一条识别结果，若无新结果则返回 None"""
        with self._lock:
            if self._result_queue:
                return self._result_queue.pop(0)
            return None

    # ---------- 内部线程函数 ----------
    def _listen_loop(self):
        """后台线程：循环等待语音输入，执行识别"""
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
                except Exception as e:
                    self.logger.error(f"降噪处理失败: {e}")

            # ASR 识别
            text = self._asr_conversion(audio_path)
            if text and text != 'error':
                with self._lock:
                    self._result_queue.append(text)
                self.logger.info(f"识别结果: {text}")
            else:
                self.logger.warning("未识别到有效语音")

    def _asr_conversion(self, audio_file: str) -> str:
        """调用在线或离线 ASR 模型"""
        if not self.model_interface:
            self.logger.error("ASR 模型接口未提供")
            return 'error'
        try:
            if self.use_online_asr:
                status, text = self.model_interface.online_asr(audio_file)
            else:
                status, text = self.model_interface.SenseVoiceSmall_ASR(audio_file)
            if status == 'ok' and len(text) > 4:
                return text
            else:
                self.logger.error(f"ASR 返回错误: {text}")
                return 'error'
        except Exception as e:
            self.logger.error(f"ASR 调用异常: {e}")
            return 'error'


# ------------------------------------------------------------
# SpeechRecognizer 对外顶层类
# ------------------------------------------------------------
class SpeechRecognizer:
    """
    语音识别器，仅暴露 open/stop/listen 三个方法。
    可配置是否启用降噪、VAD 阈值、模型接口等。
    """

    def __init__(self, cfg: dict = None, model_interface=None):
        if cfg is None:
            cfg = {}
        self.logger = logging.getLogger("SpeechRecognizer")
        # 环境日志
        if is_real_jetson():
            self.logger.info("ASR 模块运行在 Jetson 真机环境")
        else:
            self.logger.info("ASR 模块运行在非 Jetson 环境")
        self.impl = _ASRImpl(cfg, model_interface, self.logger)

    def open(self, noise_sample: np.ndarray = None):
        """启动语音识别服务，可传入环境噪声样本用于降噪器校准"""
        self.impl.open(noise_sample)

    def stop(self):
        """停止语音识别服务"""
        self.impl.stop()

    def listen(self) -> Optional[str]:
        """非阻塞获取识别结果，无新结果时返回 None"""
        return self.impl.listen()