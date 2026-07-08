# tts.py
import time
import threading
import logging
import tempfile
import os
import wave
from typing import Optional

# 音频播放库
from drivers import AudioManager


# ------------------------------------------------------------
# _TTSImpl 内部实现（中文离线语音合成）
# ------------------------------------------------------------
class _TTSImpl:
    """TTS 核心处理类，使用 Piper 中文本地引擎"""

    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger

        # ---------- Piper 中文模型路径（必须配置） ----------
        self.zh_tts_model = cfg.get("zh_tts_model", "/path/to/piper/zh_CN.pth")
        self.zh_tts_json = cfg.get("zh_tts_json", "/path/to/piper/zh_CN.json")

        self.synthesizer = None  # Piper 引擎实例

        # 线程控制
        self._running = False
        self._play_thread = None
        self._audio_queue = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------
    # 公开方法：open / stop / speak
    # ------------------------------------------------------------
    def open(self):
        """启动合成服务：加载模型并启动后台线程"""
        if self._running:
            return
        self._running = True

        # 加载 Piper 中文模型
        self._load_piper_model()

        # 启动后台线程
        self._play_thread = threading.Thread(target=self._play_loop, daemon=True)
        self._play_thread.start()
        self.logger.info("中文离线 TTS 服务已启动")

    def stop(self):
        """停止服务，清空队列"""
        if not self._running:
            return
        self._running = False

        with self._lock:
            self._audio_queue.clear()

        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=2.0)

        self.logger.info("中文离线 TTS 服务已停止")

    def speak(self, text: str, wait_finish: bool = False) -> Optional[str]:
        """
        合成并播放中文语音
        :param text: 要说的中文文本
        :param wait_finish: True=阻塞直到播完，False=后台排队
        :return: 音频文件路径或 None
        """
        if not self._running:
            self.logger.warning("服务未启动，请先调用 open()")
            return None

        if not text or len(text.strip()) == 0:
            self.logger.warning("文本为空，跳过")
            return None

        # 同步模式：立即合成并播放
        if wait_finish:
            audio_path = self._synthesize(text)
            if audio_path and audio_path != 'error':
                self._play_audio(audio_path)
                return audio_path
            return None

        # 异步模式：入队
        with self._lock:
            self._audio_queue.append(text)
        self.logger.debug(f"已入队: {text[:20]}...")
        return None

    # ------------------------------------------------------------
    # 内部核心方法
    # ------------------------------------------------------------
    def _load_piper_model(self):
        """加载 Piper 中文模型"""
        try:
            import piper

            # 检查文件是否存在
            if not os.path.exists(self.zh_tts_model):
                raise FileNotFoundError(f"模型文件不存在: {self.zh_tts_model}")
            if not os.path.exists(self.zh_tts_json):
                raise FileNotFoundError(f"配置文件不存在: {self.zh_tts_json}")

            self.synthesizer = piper.PiperVoice.load(
                self.zh_tts_model,
                config_path=self.zh_tts_json,
                use_cuda=False  # Jetson 可改为 True
            )
            self.logger.info(f"Piper 中文模型加载成功")

        except ImportError:
            self.logger.error("请安装: pip install piper-tts")
            raise
        except Exception as e:
            self.logger.error(f"模型加载失败: {e}")
            raise

    def _synthesize(self, text: str) -> str:
        """合成中文音频，返回文件路径，失败返回 'error'"""
        if not self.synthesizer:
            self.logger.error("引擎未初始化")
            return 'error'

        try:
            fd, audio_path = tempfile.mkstemp(suffix=".wav", prefix="tts_")
            os.close(fd)

            with wave.open(audio_path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(self.synthesizer.config.sample_rate)
                self.synthesizer.synthesize(text, wav)

            if os.path.getsize(audio_path) > 1000:
                return audio_path
            else:
                os.remove(audio_path)
                return 'error'

        except Exception as e:
            self.logger.error(f"合成失败: {e}")
            return 'error'

    def _play_audio(self, audio_path: str):
        """播放音频"""
        try:
          AudioManager.playsound(audio_path)
        except Exception as e:
            self.logger.error(f"播放失败: {e}")

    def _play_loop(self):
        """后台线程：循环播放"""
        while self._running:
            with self._lock:
                if not self._audio_queue:
                    text = None
                else:
                    text = self._audio_queue.pop(0)

            if text is None:
                time.sleep(0.2)
                continue

            audio_path = self._synthesize(text)
            if audio_path == 'error':
                continue

            self._play_audio(audio_path)


# ------------------------------------------------------------
# TextToSpeech 对外门面类
# ------------------------------------------------------------
class TextToSpeech:
    """
    纯中文离线语音合成器
    用法：
        cfg = {
            "zh_tts_model": "/home/user/piper/zh_CN.pth",
            "zh_tts_json": "/home/user/piper/zh_CN.json",
        }
        tts = TextToSpeech(cfg)
        tts.open()
        tts.speak("你好，欢迎使用")
        tts.stop()
    """

    def __init__(self, cfg: dict = None):
        if cfg is None:
            cfg = {}
        self.logger = logging.getLogger("TextToSpeech")
        self.impl = _TTSImpl(cfg, self.logger)

    def open(self):
        self.impl.open()

    def stop(self):
        self.impl.stop()

    def speak(self, text: str, wait_finish: bool = False) -> Optional[str]:
        return self.impl.speak(text, wait_finish)