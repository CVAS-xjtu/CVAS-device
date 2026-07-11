import subprocess
import time
import threading
from typing import Optional, Tuple, Literal
from drivers import BluetoothManager

class AudioManager:
   
    def __init__(self, audio_cfg: dict):
        
        self._bt_manager = BluetoothManager(audio_cfg)
        
        self._sink_name: Optional[str] = None     
        self._source_name: Optional[str] = None    
        self._lock = threading.Lock()             


        self._audio_state: Literal['idle', 'playing', 'recording'] = 'idle'
        self._state_lock = threading.Lock()        
        self._active_process: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None

        self._update_device_names()

    def _update_device_names(self):
        
        with self._lock:
            mac = self._bt_manager._target_mac
            if not mac:
                self._sink_name = None
                self._source_name = None
                return

            # 查询 sinks
            try:
                output = subprocess.check_output(
                    ["pactl", "list", "short", "sinks"],
                    text=True, stderr=subprocess.DEVNULL
                )
                mac_underscore = mac.replace(":", "_").lower()
                self._sink_name = None
                for line in output.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        sink_name = parts[1]
                        if mac_underscore in sink_name:
                            self._sink_name = sink_name
                            break
            except Exception:
                self._sink_name = None

            # 查询 sources
            try:
                output = subprocess.check_output(
                    ["pactl", "list", "short", "sources"],
                    text=True, stderr=subprocess.DEVNULL
                )
                mac_underscore = mac.replace(":", "_").lower()
                self._source_name = None
                for line in output.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        src_name = parts[1]
                        if mac_underscore in src_name:
                            self._source_name = src_name
                            break
            except Exception:
                self._source_name = None

    def _ensure_audio_devices(self, require_mic: bool = False):
        """在播放或录音前确保蓝牙已连接且所需设备存在，否则显示异常。"""
        if not self._bt_manager.is_connected():
            raise RuntimeError("蓝牙耳机未连接，无法进行音频操作")

        if self._sink_name is None or (require_mic and self._source_name is None):
            self._update_device_names()

        if self._sink_name is None:
            raise RuntimeError("未找到蓝牙扬声器设备（sink），请确认耳机已正确配对并加载音频模块")
        if require_mic and self._source_name is None:
            raise RuntimeError("未找到蓝牙麦克风设备（source），请确认耳机支持 HSP/HFP 模式并已正确加载")

    def _start_operation(self, op_type: Literal['playing', 'recording'], cmd: list, wait: bool) -> Optional[subprocess.Popen]:
       
        with self._state_lock:
            if self._audio_state != 'idle':
                raise RuntimeError(f"无法启动{op_type}，当前正在进行 {self._audio_state} 操作")
            # 重置状态并启动进程
            self._audio_state = op_type
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self._active_process = proc

        if wait:
            # 同步等待
            try:
                proc.wait()
                if proc.returncode != 0:
                    err = proc.stderr.read()
                    raise RuntimeError(f"{op_type}失败，返回码 {proc.returncode}: {err}")
            finally:
                # 无论成功与否，重置状态
                with self._state_lock:
                    self._audio_state = 'idle'
                    self._active_process = None
            return None
        else:
            # 异步：启动监控线程，进程结束后自动重置状态
            def monitor():
                proc.wait()
                with self._state_lock:
                    if self._active_process == proc:
                        self._audio_state = 'idle'
                        self._active_process = None
            t = threading.Thread(target=monitor, daemon=True)
            t.start()
            # 保存监控线程引用，可选
            with self._state_lock:
                self._monitor_thread = t
            return proc

    def is_connected(self, force_refresh: bool = False) -> bool:
        """判断蓝牙音频是否完全就绪（至少扬声器可用）。"""
        if not self._bt_manager.is_connected():
            return False
        if not force_refresh and self._sink_name is not None:
            return True
        self._update_device_names()
        return self._sink_name is not None

    def is_full_duplex_ready(self, force_refresh: bool = False) -> bool:
        """判断是否同时具备播放和录音能力（即 HSP/HFP 通话模式）。"""
        if not self._bt_manager.is_connected():
            return False
        if not force_refresh and self._sink_name is not None and self._source_name is not None:
            return True
        self._update_device_names()
        return self._sink_name is not None and self._source_name is not None

    def wait_until_ready(self, timeout: float = 10.0, check_interval: float = 0.5, require_mic: bool = False) -> bool:
        """等待蓝牙音频设备就绪。"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if require_mic:
                ready = self.is_full_duplex_ready(force_refresh=True)
            else:
                ready = self.is_connected(force_refresh=True)
            if ready:
                return True
            time.sleep(check_interval)
        return False

    def get_device_names(self, force_refresh: bool = False) -> Tuple[Optional[str], Optional[str]]:
        """返回当前缓存的设备名称 (sink_name, source_name)。"""
        if force_refresh:
            self._update_device_names()
        with self._lock:
            return (self._sink_name, self._source_name)

    def refresh_devices(self):
        """强制刷新设备名称。"""
        self._update_device_names()

    def play_file(self, file_path: str, wait: bool = True) -> Optional[subprocess.Popen]:
        """
        通过蓝牙扬声器播放音频文件，若当前有其他操作正在运行，抛出 RuntimeError。
        """
        self._ensure_audio_devices(require_mic=False)
        cmd = ["paplay", "-d", self._sink_name, file_path]
        return self._start_operation('playing', cmd, wait)

    def record_file(self, file_path: str, duration: float, wait: bool = True) -> Optional[subprocess.Popen]:
        """
        通过蓝牙麦克风录制音频到 WAV 文件（互斥控制），若当前有其他操作正在运行，抛出 RuntimeError。
        """
        self._ensure_audio_devices(require_mic=True)
        cmd = [
            "parecord",
            "-d", self._source_name,
            "--file-format=wav",
            "--time", str(int(duration)),
            file_path
        ]
        return self._start_operation('recording', cmd, wait)

    def stop_audio(self):
        """
        立即终止当前播放或录音操作，并重置状态。
        """
        with self._state_lock:
            if self._active_process is not None:
                try:
                    self._active_process.terminate()
                    self._active_process.wait(timeout=2)
                except Exception:
                    pass
                finally:
                    self._audio_state = 'idle'
                    self._active_process = None

    def get_audio_state(self) -> str:
        """返回当前音频状态：'idle', 'playing', 'recording'"""
        with self._state_lock:
            return self._audio_state

    def is_idle(self) -> bool:
        """检查当前是否空闲（无播放或录音）"""
        return self.get_audio_state() == 'idle'

    def set_profile(self, profile: str):
        """切换蓝牙音频配置文件。"""
        try:
            output = subprocess.check_output(
                ["pactl", "list", "short", "cards"],
                text=True, stderr=subprocess.DEVNULL
            )
            mac_underscore = self._bt_manager._target_mac.replace(":", "_").lower()
            card_idx = None
            for line in output.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and mac_underscore in parts[1]:
                    card_idx = parts[0]
                    break
            if card_idx is None:
                raise RuntimeError("未找到蓝牙声卡")
            subprocess.check_call(["pactl", "set-card-profile", card_idx, profile],
                                  stderr=subprocess.DEVNULL)
            self._update_device_names()
        except Exception as e:
            raise RuntimeError(f"切换配置文件失败: {e}")

    def stop(self):
        # 先停止音频操作
        self.stop_audio()
        self._bt_manager.stop()

    def get_bt_manager(self):
        return self._bt_manager