# audio.py
import subprocess
import time
import threading
from typing import Optional, Tuple, Literal


class AudioModule:
    """
    蓝牙音频模块：
      - 注入 BluetoothModule
      - 不创建线程，由节点创建 audio_loop
      - 生命周期 start / shutdown / cleanup
      - 播放/录音通过 paplay / parecord 子进程完成
    """

    def __init__(self, cfg: dict, bt_manager):
        self.cfg = cfg
        self.bt_manager = bt_manager

        # 状态
        self._audio_state: Literal["idle", "playing", "recording"] = "idle"
        self._active_process: Optional[subprocess.Popen] = None
        self._sink_name: Optional[str] = None
        self._source_name: Optional[str] = None

        # 锁
        self._state_lock = threading.Lock()   # 保护状态：_audio_state/_active_process/_sink_name/_source_name/_running
        self._op_lock = threading.Lock()      # 保护子进程操作：Popen/terminate/设备查询

        self._running = False

    # ==================== 生命周期 ====================
    def start(self):
        self._update_device_names()
        with self._state_lock:
            self._running = True

    def shutdown(self):
        with self._state_lock:
            self._running = False

    def cleanup(self):
        # 先停当前操作
        self.stop_audio()
        with self._state_lock:
            self._sink_name = None
            self._source_name = None

    # ==================== 进程循环 ====================
    def audio_loop(self):
        while True:
            with self._state_lock:
                if not self._running:
                    break
                proc = self._active_process

            if proc is not None and proc.poll() is not None:
                with self._state_lock:
                    if self._active_process is proc:
                        self._audio_state = "idle"
                        self._active_process = None
            time.sleep(0.1)

    # ==================== 状态查询 ====================
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    def is_connected(self, force_refresh: bool = False) -> bool:
        if not self.bt_manager.is_connected():
            return False
        if not force_refresh:
            with self._state_lock:
                if self._sink_name is not None:
                    return True
        self._update_device_names()
        with self._state_lock:
            return self._sink_name is not None

    def is_full_duplex_ready(self, force_refresh: bool = False) -> bool:
        if not self.bt_manager.is_connected():
            return False
        if not force_refresh:
            with self._state_lock:
                if self._sink_name is not None and self._source_name is not None:
                    return True
        self._update_device_names()
        with self._state_lock:
            return self._sink_name is not None and self._source_name is not None

    def is_idle(self) -> bool:
        return self.get_audio_state() == "idle"

    def is_playing(self) -> bool:
        with self._state_lock:
            return self._audio_state == "playing"

    def is_recording(self) -> bool:
        with self._state_lock:
            return self._audio_state == "recording"

    def get_device_names(self, force_refresh: bool = False) -> Tuple[Optional[str], Optional[str]]:
        if force_refresh:
            self._update_device_names()
        with self._state_lock:
            return (self._sink_name, self._source_name)

    def get_audio_state(self) -> str:
        with self._state_lock:
            return self._audio_state

    # ==================== 业务操作 ====================
    def wait_until_ready(self, timeout: float = 10.0,
                         check_interval: float = 0.5,
                         require_mic: bool = False) -> bool:
        start_time = time.time()
        while time.time() - start_time < timeout:
            ready = (self.is_full_duplex_ready(force_refresh=True)
                     if require_mic else
                     self.is_connected(force_refresh=True))
            if ready:
                return True
            time.sleep(check_interval)
        return False

    def refresh_devices(self):
        self._update_device_names()

    def set_profile(self, profile: str):
        mac = self.bt_manager.get_target_mac()
        mac_underscore = mac.replace(":", "_").lower()
        with self._op_lock:
            try:
                output = subprocess.check_output(
                    ["pactl", "list", "short", "cards"],
                    text=True, stderr=subprocess.DEVNULL,
                )
                card_idx = None
                for line in output.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2 and mac_underscore in parts[1]:
                        card_idx = parts[0]
                        break
                if card_idx is None:
                    raise RuntimeError("未找到蓝牙声卡")
                subprocess.check_call(
                    ["pactl", "set-card-profile", card_idx, profile],
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                raise RuntimeError(f"切换配置文件失败: {e}")
        # 锁外刷新
        self._update_device_names()

    def play_file(self, file_path: str, wait: bool = True) -> Optional[subprocess.Popen]:
        self._ensure_audio_devices(require_mic=False)
        with self._state_lock:
            sink_name = self._sink_name
        cmd = ["paplay", "-d", sink_name, file_path]
        return self._start_operation("playing", cmd, wait)

    def record_file(self, file_path: str, duration: float, wait: bool = True):
        self._ensure_audio_devices(require_mic=True)
        with self._state_lock:
            source_name = self._source_name
        cmd = [
            "parecord",
            "-d", source_name,
            "--file-format=wav",
            "--time", str(int(duration)),
            file_path,
        ]
        return self._start_operation("recording", cmd, wait)

    def stop_audio(self):
        """
        立即终止当前播放或录音，并重置状态。
        用 _op_lock 串行化，避免与 _start_operation 竞争。
        """
        with self._op_lock:
            with self._state_lock:
                proc = self._active_process
                self._audio_state = "idle"
                self._active_process = None

            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    # ==================== 内部 ====================
    def _update_device_names(self):
        mac = self.bt_manager.get_target_mac()
        sink = None
        source = None
        if mac:
            mac_underscore = mac.replace(":", "_").lower()
            with self._op_lock:
                try:
                    output = subprocess.check_output(
                        ["pactl", "list", "short", "sinks"],
                        text=True, stderr=subprocess.DEVNULL,
                    )
                    for line in output.splitlines():
                        parts = line.split("\t")
                        if len(parts) >= 2 and mac_underscore in parts[1]:
                            sink = parts[1]
                            break
                except Exception:
                    sink = None
                try:
                    output = subprocess.check_output(
                        ["pactl", "list", "short", "sources"],
                        text=True, stderr=subprocess.DEVNULL,
                    )
                    for line in output.splitlines():
                        parts = line.split("\t")
                        if len(parts) >= 2 and mac_underscore in parts[1]:
                            source = parts[1]
                            break
                except Exception:
                    source = None
        with self._state_lock:
            self._sink_name = sink
            self._source_name = source

    def _ensure_audio_devices(self, require_mic: bool = False):
        if not self.bt_manager.is_connected():
            raise RuntimeError("蓝牙耳机未连接，无法进行音频操作")
        with self._state_lock:
            sink = self._sink_name
            source = self._source_name
        if sink is None or (require_mic and source is None):
            self._update_device_names()
            with self._state_lock:
                sink = self._sink_name
                source = self._source_name
        if sink is None:
            raise RuntimeError("未找到蓝牙扬声器设备（sink）")
        if require_mic and source is None:
            raise RuntimeError("未找到蓝牙麦克风设备（source）")

    def _start_operation(self, op_type, cmd, wait):
        """
        整段用 _op_lock 串行化：
          - 状态检查 + 置 playing/recording
          - Popen 子进程
          - 登记 _active_process
        这样 stop_audio 一旦拿到 _op_lock，就一定处于「没有任何起/停操作正在进行」的状态。
        """
        with self._op_lock:
            with self._state_lock:
                if self._audio_state != "idle":
                    raise RuntimeError(
                        f"无法启动{op_type}，当前正在进行 {self._audio_state} 操作"
                    )
                self._audio_state = op_type

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except Exception:
                with self._state_lock:
                    self._audio_state = "idle"
                raise

            with self._state_lock:
                self._active_process = proc

        # 锁外等待（同步模式）
        if wait:
            try:
                proc.wait()
                if proc.returncode != 0:
                    err = proc.stderr.read() if proc.stderr else ""
                    raise RuntimeError(
                        f"{op_type}失败，返回码 {proc.returncode}: {err}"
                    )
            finally:
                with self._state_lock:
                    if self._active_process is proc:
                        self._audio_state = "idle"
                        self._active_process = None
            return None

        return proc