# asr.py
import time
import threading
import numpy as np
from typing import Optional
from audio import wakeup
from audio import preprocess


class ASRModule:

    def __init__(self, cfg: dict, model_interface):
        self.cfg = cfg
        self.model_interface = model_interface
        self.wakeup = wakeup          # 注入
        self.preprocess = preprocess  # 注入
        self.use_online_asr = cfg.get("use_online_asr", False)

        self._state_lock = threading.Lock()
        self._op_lock = threading.Lock()
        self._running = False
        self._result_queue = []

        # ---------------- 生命周期 ----------------
    def start(self):
        with self._state_lock:
            self._running = True

    def shutdown(self):
        with self._state_lock:
            self._running = False

    def cleanup(self):
        with self._state_lock:
            self._result_queue.clear()

    # ---------------- 进程循环 ----------------
    def asr_loop(self):
        while True:
            with self._state_lock:
                if not self._running:
                    break

            # 锁外调用其他类
            path = self.wakeup.get_audio_path()
            if path is None:
                time.sleep(0.1)
                continue

            if self.preprocess.is_enabled():
                self.preprocess.process_file(path)

            text = self.transcribe_file(path)
            if text:
                with self._state_lock:
                    self._result_queue.append(text)

    # ---------------- 状态查询 ----------------
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    def get_result(self) -> Optional[str]:
        with self._state_lock:
            if self._result_queue:
                return self._result_queue.pop(0)
            return None

    def get_pending_count(self) -> int:
        with self._state_lock:
            return len(self._result_queue)

    # ---------------- 业务操作 ----------------
    def transcribe_file(self, path: str) -> Optional[str]:
        if not self.model_interface:
            return None
        try:
            if self.use_online_asr:
                status, text = self.model_interface.online_asr(path)
            else:
                status, text = self.model_interface.SenseVoiceSmall_ASR(path)
            if status == "ok" and len(text) > 4:
                return text
            return None
        except Exception:
            return None

    def push_result(self, text: str):
        """供 PTT 等外部模块将 ASR 结果并入统一结果队列"""
        if not text:
            return
        with self._state_lock:
            self._result_queue.append(text)
 