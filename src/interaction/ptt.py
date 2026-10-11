# ptt.py
import time
import os
import glob
import threading
from typing import Optional


class PTTModule:
    """
    PTT 模块：
      - 注入 asr、audio、button
      - 不创建线程，由节点创建 ptt_loop
      - 生命周期 start / shutdown / cleanup
      - 按键按下 → 开始录音；按键释放 → 停止录音 + 触发 ASR
    """

    def __init__(self, cfg: dict, asr, audio, button):
        self.cfg = cfg
        self.asr = asr
        self.audio = audio
        self.button = button

        self.record_file_base = cfg.get("record_file_base", "/tmp/record_voice")
        self.a2dp_profile = cfg.get("a2dp_profile", "a2dp_sink")
        self.hsp_profile = cfg.get("hsp_profile", "headset_head_unit")
        self.auto_switch_profile = cfg.get("auto_switch_profile", True)
        self.max_keep_files = cfg.get("max_keep_files", 5)
        self.poll_interval = cfg.get("poll_interval", 0.02)
        self.record_max_duration = cfg.get("record_max_duration", 600)

        # 锁
        self._state_lock = threading.Lock()
        self._op_lock = threading.Lock()

        # 状态
        self._running = False
        self._key_pressed = False
        self._is_recording = False

        # 录音相关
        self._recording_proc = None
        self._current_record_file: Optional[str] = None

    # ==================== 生命周期 ====================
    def start(self):
        with self._state_lock:
            if self._running:
                return

        # 切换 HSP 确保麦克风可用
        if self.auto_switch_profile:
            try:
                self.audio.set_profile(self.hsp_profile)
                time.sleep(0.2)
                if not self.audio.wait_until_ready(timeout=3, require_mic=True):
                    raise RuntimeError("麦克风设备未就绪")
                self.audio.set_profile(self.a2dp_profile)
                time.sleep(0.2)
            except Exception as e:
                raise RuntimeError(f"PTT 初始化失败: {e}")

        with self._state_lock:
            self._running = True

    def shutdown(self):
        # 只置标志，不在这里做识别（避免阻塞节点关闭流程）
        with self._state_lock:
            self._running = False

    def cleanup(self):
        # 如果还在录音，收尾（含识别）
        with self._state_lock:
            was_recording = self._is_recording
        if was_recording:
            self._stop_recording_and_recognize()

        # 兜底清子进程引用
        with self._op_lock:
            proc = self._recording_proc
            self._recording_proc = None
        if proc is not None:
            try:
                proc.wait(timeout=1)
            except Exception:
                pass

    # ==================== 进程循环 ====================
    def ptt_loop(self):
        last_state = False
        while True:
            with self._state_lock:
                if not self._running:
                    break

            current = self.button.is_pressed()

            if current and not last_state:
                self._start_recording()
            elif not current and last_state:
                self._stop_recording_and_recognize()

            last_state = current
            time.sleep(self.poll_interval)

    # ==================== 状态查询 ====================
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    def is_pressed(self) -> bool:
        with self._state_lock:
            return self._key_pressed

    def is_recording(self) -> bool:
        with self._state_lock:
            return self._is_recording

    # ==================== 业务操作 ====================
    def _cleanup_old_files(self):
        try:
            dirname = os.path.dirname(self.record_file_base)
            basename = os.path.basename(self.record_file_base)
            files = glob.glob(os.path.join(dirname, f"{basename}_*.wav"))
            if len(files) <= self.max_keep_files:
                return
            files.sort(key=os.path.getmtime)
            for f in files[:-self.max_keep_files]:
                try:
                    os.remove(f)
                except Exception:
                    pass
        except Exception:
            pass

    def _start_recording(self):
        with self._state_lock:
            if self._is_recording:
                return
            self._is_recording = True
            self._key_pressed = True
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            suffix = f"{int(time.time() * 1000) % 1000:03d}"
            self._current_record_file = (
                f"{self.record_file_base}_{timestamp}_{suffix}.wav"
            )

        # 停当前播放
        try:
            if self.audio.get_audio_state() == "playing":
                self.audio.stop_audio()
                time.sleep(0.1)
        except Exception:
            pass

        # 切 HSP
        if self.auto_switch_profile:
            try:
                self.audio.set_profile(self.hsp_profile)
                time.sleep(0.2)
            except Exception:
                with self._state_lock:
                    self._is_recording = False
                    self._key_pressed = False
                return

        # 启动录音
        with self._state_lock:
            file_path = self._current_record_file
        try:
            proc = self.audio.record_file(
                file_path, duration=self.record_max_duration, wait=False
            )
            with self._op_lock:
                self._recording_proc = proc
        except Exception:
            with self._state_lock:
                self._is_recording = False
                self._key_pressed = False

    def _stop_recording_and_recognize(self):
        with self._state_lock:
            if not self._is_recording:
                return
            self._is_recording = False
            self._key_pressed = False
            file_path = self._current_record_file
            self._current_record_file = None

        with self._op_lock:
            proc = self._recording_proc
            self._recording_proc = None

        # 停止录音（锁外）
        try:
            self.audio.stop_audio()
        except Exception:
            pass

        if proc is not None:
            try:
                proc.wait(timeout=1)
            except Exception:
                pass

        # 切回 A2DP
        if self.auto_switch_profile:
            try:
                self.audio.set_profile(self.a2dp_profile)
            except Exception:
                pass

        # 文件检查
        if not file_path or not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
            self._cleanup_old_files()
            return

        # 锁外调用 ASR 业务方法
        text = self.asr.transcribe_file(file_path)
        if text:
            self.asr.push_result(text)

        self._cleanup_old_files()