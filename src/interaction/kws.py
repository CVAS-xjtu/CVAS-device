import time
import threading
import pyaudio # type: ignore
from typing import Optional


class KWSDetector:
    """
    关键词检测器（符合 interaction 节点设计规范）
    职责：持续监听麦克风，当检测到预设关键词时通知节点。
    类自身不创建线程，节点调用 kws_loop() 放入线程运行。
    """

    def __init__(self, cfg: dict = None):
        if cfg is None:
            cfg = {}
        # ---------- 配置参数 ----------
        self.sample_rate = cfg.get("sample_rate", 16000)
        self.frame_duration_ms = cfg.get("frame_duration_ms", 30)
        self.frame_bytes = int(self.sample_rate * (self.frame_duration_ms / 1000.0) * 2)
        self.mic_index = cfg.get("mic_index", 0)
        self.keywords = cfg.get("keywords", [])          # 要检测的关键词列表
        self.kws_model = cfg.get("kws_model")            # 需实现 detect(frame_bytes) -> str|None

        # ---------- 运行时状态 ----------
        self._running = False
        self._detected_keyword: Optional[str] = None
        self._detected_event = threading.Event()
        self._audio_stream = None
        self._pa = None

        # ---------- 锁 ----------
        self._state_lock = threading.Lock()

    # ================================================================
    #   生命周期方法
    # ================================================================
    def start(self):
        """初始化音频流（打开麦克风）"""
        with self._state_lock:
            if self._running:
                return
            self._running = True
            self._pa = pyaudio.PyAudio()
            stream_kwargs = {
                'format': pyaudio.paInt16,
                'channels': 1,
                'rate': self.sample_rate,
                'input': True,
                'frames_per_buffer': self.frame_bytes,
            }
            if self.mic_index != 0:
                stream_kwargs['input_device_index'] = self.mic_index
            self._audio_stream = self._pa.open(**stream_kwargs)

    def shutdown(self):
        """置 _running 为 False，结束 kws_loop 循环"""
        with self._state_lock:
            self._running = False

    def cleanup(self):
        """关闭音频流，释放 PyAudio 资源"""
        with self._state_lock:
            if self._audio_stream is not None:
                self._audio_stream.stop_stream()
                self._audio_stream.close()
                self._audio_stream = None
            if self._pa is not None:
                self._pa.terminate()
                self._pa = None

    # ================================================================
    #   进程循环方法（由节点负责放入线程执行）
    # ================================================================
    def kws_loop(self):
        """
        持续读取麦克风帧，喂给 KWS 模型。
        检测到关键词后保存并设置事件，节点通过 is_keyword_detected() 查询。
        循环条件：self._running
        """
        while self._running:
            if self._audio_stream is None:
                time.sleep(0.1)
                continue
            try:
                frame = self._audio_stream.read(self.frame_bytes, exception_on_overflow=False)
            except Exception:
                break
            if self.kws_model:
                detected = self.kws_model.detect(frame)
                if detected in self.keywords:
                    with self._state_lock:
                        self._detected_keyword = detected
                        self._detected_event.set()
                    # 检测到一次后可选择暂停或继续（这里继续，节点负责清掉）
            else:
                # 无模型时休眠，避免空转（实际部署必有模型）
                time.sleep(0.1)

    # ================================================================
    #   状态查询方法
    # ================================================================
    def is_keyword_detected(self) -> bool:
        """是否有新的关键词被检测到（非阻塞）"""
        return self._detected_event.is_set()

    def get_detected_keyword(self) -> Optional[str]:
        """获取最近检测到的关键词"""
        with self._state_lock:
            return self._detected_keyword

    # ================================================================
    #   业务操作方法
    # ================================================================
    def clear_detection(self):
        """清除当前检测结果，准备下一次检测"""
        with self._state_lock:
            self._detected_keyword = None
            self._detected_event.clear()