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


class TTSModule:
    
     def __init__(self, cfg: dict = None, logger: logging.Logger = None):
        if cfg is None:
            cfg = {}
        self.cfg = cfg
        self.logger = logger or logging.getLogger("TTSModule")

        # ---------- Piper 中文模型路径（必须配置） ----------
        self.zh_tts_model = cfg.get("zh_tts_model", "/path/to/piper/zh_CN.pth")
        self.zh_tts_json = cfg.get("zh_tts_json", "/path/to/piper/zh_CN.json")


        self._state_lock = threading.Lock()
        self._op_lock = threading.Lock()
        self._running = False
        self._synthesizer = None
        self._audio_queue = []

    # ---------------- 生命周期 ----------------
def start(self):
        with self._op_lock:
            self._load_piper_model()
        with self._state_lock:
            self._running = True

def shutdown(self):
        with self._state_lock:
            self._running = False

def cleanup(self):
        with self._state_lock:
            self._audio_queue.clear()
        with self._op_lock:
            self._synthesizer = None

    # ---------------- 进程循环 ----------------
def tts_loop(self):
        while True:
            with self._state_lock:
                if not self._running:
                    break
                text = self._audio_queue.pop(0) if self._audio_queue else None

            if text is None:
                time.sleep(0.1)
                continue

            audio_path = self._synthesize(text)
            if audio_path:
                self._play_audio(audio_path)

    # ---------------- 状态查询 ----------------
def is_running(self) -> bool:
        with self._state_lock:
            return self._running

def get_pending_count(self) -> int:
        with self._state_lock:
            return len(self._audio_queue)

    # ---------------- 业务操作 ----------------
def speak(self, text: str, wait_finish: bool = False) -> Optional[str]:
        with self._state_lock:
            running = self._running
        if not running or not text or not text.strip():
            return None

        if wait_finish:
            audio_path = self._synthesize(text)
            if audio_path:
                self._play_audio(audio_path)
                return audio_path
            return None

        with self._state_lock:
            self._audio_queue.append(text)
        return None

    # ---------------- 内部 ----------------
def _load_piper_model(self):
        try:
            import piper
            if not os.path.exists(self.zh_tts_model):
                raise FileNotFoundError(f"模型文件不存在: {self.zh_tts_model}")
            if not os.path.exists(self.zh_tts_json):
                raise FileNotFoundError(f"配置文件不存在: {self.zh_tts_json}")
            self._synthesizer = piper.PiperVoice.load(
                self.zh_tts_model, config_path=self.zh_tts_json, use_cuda=False
            )
            self.logger.info("Piper 中文模型加载成功")
        except ImportError:
            self.logger.error("请安装 piper-tts")
            raise
        except Exception as e:
            self.logger.error(f"模型加载失败: {e}")
            raise

def _synthesize(self, text: str) -> Optional[str]:
        with self._op_lock:
            synth = self._synthesizer
        if synth is None:
            return None
        try:
            fd, audio_path = tempfile.mkstemp(suffix=".wav", prefix="tts_")
            os.close(fd)
            with wave.open(audio_path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(synth.config.sample_rate)
                synth.synthesize(text, wav)
            if os.path.getsize(audio_path) > 1000:
                return audio_path
            os.remove(audio_path)
            return None
        except Exception as e:
            self.logger.error(f"合成失败: {e}")
            return None

def _play_audio(self, audio_path: str):
        try:
            AudioManager.playsound(audio_path)
        except Exception as e:
            self.logger.error(f"播放失败: {e}")