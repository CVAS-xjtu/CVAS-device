import time
import threading
import os
import glob
import subprocess
from typing import Optional, Callable



class PTTModule:

    def __init__(
        self,
        cfg: dict,
        asr,
        audio_manager_factory: Callable,
        button
    ):
        self.cfg = cfg
        self.asr = asr
        self.audio_manager_factory = audio_manager_factory
        self.button = button

        self.device_name = cfg.get("device_name", "")
        self.record_file_base = cfg.get("record_file_base", "/tmp/record_voice")
        self.a2dp_profile = cfg.get("a2dp_profile", "a2dp_sink")
        self.hsp_profile = cfg.get("hsp_profile", "headset_head_unit")
        self.auto_switch_profile = cfg.get("auto_switch_profile", True)
        self.max_keep_files = cfg.get("max_keep_files", 5)
        self.poll_interval = cfg.get("poll_interval", 0.02)

         # 锁
        self._state_lock = threading.Lock()
        self._op_lock = threading.Lock()

        # 状态
        self._running = False
        self._key_pressed = False
        self._is_recording = False

        # 硬件 / 子进程
        self._audio_manager = None
        self._current_mac: Optional[str] = None
        self._recording_proc = None
        self._current_record_file: Optional[str] = None


       # ==================== 生命周期 ====================
    def start(self):
        # 1. 找到 MAC，创建 AudioManager
        mac = self._find_mac()
        if not mac:
            raise RuntimeError(f"未找到蓝牙设备（名称包含：{self.device_name}）")

        with self._op_lock:
            self._current_mac = mac
            self._audio_manager = self.audio_manager_factory(mac)
            am = self._audio_manager

        # 2. 锁外调用 am 的接口
        if self.auto_switch_profile:
            try:
                am.set_profile(self.hsp_profile)
                time.sleep(0.2)
                if not am.wait_until_ready(timeout=3, require_mic=True):
                    raise RuntimeError("麦克风设备未就绪")
                am.set_profile(self.a2dp_profile)
                time.sleep(0.2)
            except Exception as e:
                # 尝试重新获取 MAC
                new_mac = self._find_mac()
                if new_mac and new_mac != mac:
                    with self._op_lock:
                        old_am = self._audio_manager
                        self._current_mac = new_mac
                        self._audio_manager = self.audio_manager_factory(new_mac)
                        am = self._audio_manager
                    if old_am:
                        try:
                            old_am.stop()
                        except Exception:
                            pass
                    am.set_profile(self.hsp_profile)
                    time.sleep(0.2)
                    if not am.wait_until_ready(timeout=3, require_mic=True):
                        raise RuntimeError("重试后麦克风仍不可用")
                    am.set_profile(self.a2dp_profile)
                    time.sleep(0.2)
                else:
                    raise RuntimeError(f"初始化设备失败: {e}")

        with self._state_lock:
            self._running = True

    def shutdown(self):
        with self._state_lock:
            self._running = False
            was_recording = self._is_recording
        # 如果在录音，需要主动停止并识别
        if was_recording:
            self._stop_recording_and_recognize()

    def cleanup(self):
        with self._op_lock:
            am = self._audio_manager
            self._audio_manager = None
            proc = self._recording_proc
            self._recording_proc = None

        if proc is not None:
            try:
                proc.wait(timeout=1)
            except Exception:
                pass
        if am is not None:
            try:
                am.stop()
            except Exception:
                pass

    # ==================== 进程循环 ====================
    def ptt_loop(self):
        last_state = False
        while True:
            with self._state_lock:
                if not self._running:
                    break

            # 锁外调用 button 驱动
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
    def _find_mac(self) -> Optional[str]:
        try:
            output = subprocess.check_output(
                ["bluetoothctl", "paired-devices"],
                text=True, stderr=subprocess.DEVNULL,
            )
            for line in output.splitlines():
                parts = line.split(" ", 2)
                if len(parts) >= 3:
                    mac, name = parts[1], parts[2]
                    if self.device_name.lower() in name.lower():
                        return mac
        except Exception:
            pass
        return None

    def _refresh_device_if_needed(self) -> bool:
        new_mac = self._find_mac()
        if not new_mac:
            return False

        with self._op_lock:
            old_am = self._audio_manager
            self._current_mac = new_mac
            self._audio_manager = self.audio_manager_factory(new_mac)
            am = self._audio_manager

        if old_am is not None:
            try:
                old_am.stop()
            except Exception:
                pass

        try:
            am.set_profile(self.hsp_profile)
            time.sleep(0.2)
            if am.wait_until_ready(timeout=2, require_mic=True):
                am.set_profile(self.a2dp_profile)
                return True
        except Exception:
            pass
        return False

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
            self._current_record_file = f"{self.record_file_base}_{timestamp}.wav"

        with self._op_lock:
            am = self._audio_manager
        if am is None:
            with self._state_lock:
                self._is_recording = False
                self._key_pressed = False
            return

        # 停止当前播放（锁外）
        try:
            if am.get_audio_state() == "playing":
                am.stop_audio()
                time.sleep(0.1)
        except Exception:
            pass

        # 切换至 HSP
        if self.auto_switch_profile:
            try:
                am.set_profile(self.hsp_profile)
                time.sleep(0.2)
            except Exception:
                if not self._refresh_device_if_needed():
                    with self._state_lock:
                        self._is_recording = False
                        self._key_pressed = False
                    return
                with self._op_lock:
                    am = self._audio_manager
                try:
                    am.set_profile(self.hsp_profile)
                    time.sleep(0.2)
                except Exception:
                    with self._state_lock:
                        self._is_recording = False
                        self._key_pressed = False
                    return

        # 启动录音（锁外调用 am 的接口）
        with self._state_lock:
            file_path = self._current_record_file
        try:
            proc = am.record_file(file_path, duration=600, wait=False)
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
            am = self._audio_manager
            proc = self._recording_proc
            self._recording_proc = None

        # 停止录音（锁外）
        if am is not None:
            try:
                am.stop_audio()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.wait(timeout=1)
            except Exception:
                pass

        # 切回 A2DP
        if self.auto_switch_profile and am is not None:
            try:
                am.set_profile(self.a2dp_profile)
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