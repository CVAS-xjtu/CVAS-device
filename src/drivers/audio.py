import subprocess
import threading
import time
import os
from typing import Optional, Tuple, Literal
from drivers import BluetoothManager

class AudioManager:
   
    def __init__(self, audio_cfg: dict):
        
        # 创建蓝牙管理实例
        self._bt_manager = BluetoothManager(audio_cfg)
        # 配置字典
        self._audio_cfg = audio_cfg
        # pactl运行超时时间
        self._pactl_run_timeout = self._audio_cfg.get("pactl_run_timeout_sec", 1.0)

        # 扬声器名称
        self._sink_name: Optional[str] = None
        #麦克风名称
        self._source_name: Optional[str] = None



        self._audio_state: Literal['idle', 'playing', 'recording'] = 'idle'
        self._state_lock = threading.Lock()        
        self._active_process: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None

        self._update_device_names()


    def _pactl_run(self, args: list, check: bool = False) -> str:
        """
        统一封装pactl命令调用
        :param args: pactl子命令参数列表, 无需带pactl本身
        :param check: 是否校验返回码, 失败抛出异常 (操作类用True, 查询类用False)
        :return: 命令标准输出文本，失败且不校验时返回空字符串
        """
        # 强制英文环境，避免本地化导致输出格式变化
        env = os.environ.copy()
        env["LANG"] = "C"
        env["LC_ALL"] = "C"

        cmd = ["pactl"] + args
        try:
            result = subprocess.run(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=self._pactl_run_timeout,
                check=check
            )
            return result.stdout
        except Exception:
            if check:
                # 操作类场景：异常向上抛出，由外层业务逻辑处理
                raise
            # 查询类场景：异常兜底返回空串，和原有try-except行为完全一致
            return ""


    def _update_device_names(self):
        """更新扬声器和麦克风的名称"""
        with self._state_lock:
            mac = self._bt_manager.get_target_mac()
            if not mac:
                self._sink_name = None
                self._source_name = None
                return
            
            mac_underscore = mac.replace(":", "_").lower()

            # 查询扬声器sink
            self._sink_name = None
            output = self._pactl_run(["list", "short", "sinks"])
            for line in output.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    sink_name = parts[1]
                    if mac_underscore in sink_name:
                        self._sink_name = sink_name
                        break

            # 查询麦克风source
            self._source_name = None
            output = self._pactl_run(["list", "short", "sources"])
            for line in output.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    src_name = parts[1]
                    if mac_underscore in src_name:
                        self._source_name = src_name
                        break


    def _ensure_audio_devices(self, require_mic: bool = False):
        """在播放或录音前确保蓝牙已连接且所需设备存在，否则显示异常。"""
        if not self._bt_manager.is_connected(force_refresh = True):
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


    def _check_device_ready(self, mode: Literal["a2dp", "hfp"], force_refresh: bool) -> bool:
        """查询相应的设备是否准备好"""
        # 蓝牙物理连接断开直接返回false
        if not self._bt_manager.is_connected(force_refresh = True):
            return False

        with self._state_lock:
            if not force_refresh:
                if mode == "hfp":
                    # HFP模式：只看麦克风source
                    return self._source_name is not None
                else:
                    # A2DP模式：只看扬声器sink
                    return self._sink_name is not None

        # 强制刷新时调用pactl更新设备名称
        self._update_device_names()

        with self._state_lock:
            if mode == "hfp":
                return self._source_name is not None
            else:
                return self._sink_name is not None





    # 对外play播放接口
    def play_file(self, file_path: str, wait: bool = True) -> Optional[subprocess.Popen]:
        """
        通过蓝牙扬声器播放音频文件，若当前有其他操作正在运行，抛出 RuntimeError。
        """
        self._ensure_audio_devices(require_mic=False)
        cmd = ["paplay", "-d", self._sink_name, file_path]
        return self._start_operation('playing', cmd, wait)


    def play_memory(self):
        pass


    # 对外record录音接口
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
    

    def record_stream(self):
        pass


    # 对外查询类接口
    def is_sink_ready(self, force_refresh: bool = False) -> bool:
        """判断A2DP扬声器就绪, 用于导航播报"""
        return self._check_device_ready("a2dp", force_refresh)


    def is_mic_ready(self, force_refresh: bool = False) -> bool:
        """判断HFP麦克风就绪, 用于PTT与Wakeup录音"""
        return self._check_device_ready("hfp", force_refresh)


    def is_idle(self) -> bool:
        """检查当前是否空闲（无播放或录音）"""
        return self.get_audio_state() == 'idle'


    def _wait_until_ready(self, profile: Literal["a2dp", "hfp"], timeout: float = 10.0, check_interval: float = 0.5) -> bool:
        """等待蓝牙音频设备就绪"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if profile == "hfp":
                ready = self.is_mic_ready(force_refresh=True)
            else:
                ready = self.is_sink_ready(force_refresh=True)
            if ready:
                return True
            time.sleep(check_interval)
        return False




    def stop_audio(self):
        """立即终止当前播放或录音操作，并重置状态"""
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


    def set_profile(
        self, profile: Literal["a2dp", "hfp"], wait: bool = True, timeout: float = 5.0):
        """切换蓝牙音频配置文件"""
        # 执行pactl切换profile
        output = self._pactl_run(["list", "short", "cards"])
        mac = self._bt_manager.get_target_mac()
        mac_underscore = mac.replace(":", "_").lower()
        card_idx = None
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and mac_underscore in parts[1]:
                card_idx = parts[0]
                break
        if card_idx is None:
            raise RuntimeError("找不到蓝牙声卡")
        self._pactl_run(["set-card-profile", card_idx, profile], check = True)
        self._update_device_names()

        # 切换完成之后自动等待对应profile就绪
        if wait:
            success = self._wait_until_ready(profile, timeout)
            if not success:
                raise RuntimeError(f"切换 {profile} 超时，音频设备未就绪")


    def stop(self):
        """停止音频管理器"""
        self.stop_audio() # 先停止音频操作
        self._bt_manager.stop()
        # 关闭监视线程