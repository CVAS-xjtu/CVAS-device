import subprocess
import threading
import time
import os
from typing import Optional, Literal
from .env_check import is_real_jetson, SIM_LOG
from drivers import BluetoothManager


class AudioManager:
    """
    音频管理器（符合 interaction 节点设计规范）

    职责：
      - 管理蓝牙音频设备的播放、录音、配置文件切换。
      - 支持文件播放、内存 PCM 播放、文件录音、流式录音。
      - 在仿真环境下自动创建虚拟声卡用于音频捕获（不对外暴露）。
    与 BluetoothManager 协作（通过注入）获取连接状态与设备 MAC。
    子进程按需创建，操作结束后释放；类自身不创建线程，所有轮询任务由节点调度。
    """

    def __init__(self, audio_cfg: dict, bt_manager: BluetoothManager):
        """
        初始化配置与依赖（不启动硬件或线程）。
        :param audio_cfg: 音频相关配置字典。
        :param bt_manager: 蓝牙管理器实例（由节点注入）。
        """
        # ---------- 注入的依赖 ----------
        self._bt_manager = bt_manager

        # ---------- 配置参数 ----------
        self._pactl_run_timeout = audio_cfg.get("pactl_run_timeout_sec", 1.0)

        # ---------- 运行时状态 ----------
        self._running = True                      # 循环控制标志
        self._audio_state: Literal['idle', 'playing', 'recording'] = 'idle'
        self._sink_name: Optional[str] = None     # 当前蓝牙扬声器 sink 名称
        self._source_name: Optional[str] = None   # 当前蓝牙麦克风 source 名称
        self._active_process: Optional[subprocess.Popen] = None

        # ---------- 虚拟声卡内部管理（不对外暴露） ----------
        self._virtual_sink_name: Optional[str] = None
        self._virtual_sink_module_id: Optional[int] = None
        self._loopback_module_id: Optional[int] = None

        # ---------- 锁 ----------
        self._state_lock = threading.Lock()       # 保护状态属性（_audio_state, _sink_name, _source_name, 虚拟声卡状态等）
        self._op_lock = threading.Lock()          # 保护子进程操作（创建/终止/替换 _active_process）

    # ================================================================
    #   生命周期方法
    # ================================================================

    def start(self):
        """初始化音频模块：更新设备名称，并按需自动创建虚拟声卡。"""
        self._update_device_names()
        self._auto_setup_virtual_sink()

    def shutdown(self):
        """将 _running 置为 False，结束操作监控循环。"""
        with self._state_lock:
            self._running = False

    def cleanup(self):
        """关闭音频模块：强制终止所有操作，并销毁虚拟声卡（若有）。"""
        self.stop_audio()
        self._auto_teardown_virtual_sink()


    # ================================================================
    #   状态查询方法
    # ================================================================

    def is_sink_ready(self, force_refresh: bool = False) -> bool:
        """查询 A2DP 扬声器是否就绪"""
        return self._check_device_ready("a2dp", force_refresh)

    def is_mic_ready(self, force_refresh: bool = False) -> bool:
        """查询 HFP 麦克风是否就绪"""
        return self._check_device_ready("hfp", force_refresh)

    def is_idle(self) -> bool:
        """查询是否空闲（无播放/录音任务）"""
        return self.get_audio_state() == 'idle'

    def get_audio_state(self) -> str:
        """获取当前音频操作状态（'idle', 'playing', 'recording'）"""
        with self._state_lock:
            return self._audio_state

    def get_sink_name(self) -> Optional[str]:
        """获取当前识别到的蓝牙扬声器名称"""
        with self._state_lock:
            return self._sink_name

    def get_source_name(self) -> Optional[str]:
        """获取当前识别到的蓝牙麦克风名称"""
        with self._state_lock:
            return self._source_name

    # ================================================================
    #   业务操作方法
    # ================================================================

    def play_file(self, file_path: str, wait: bool = True) -> Optional[subprocess.Popen]:
        """
        播放音频文件到蓝牙扬声器。
        :param file_path: 音频文件路径。
        :param wait: True 同步等待；False 异步返回 Popen 对象。
        :return: 同步返回 None，异步返回 Popen 对象。
        """
        self._ensure_audio_devices(require_mic=False)
        cmd = ["paplay", "-d", self._sink_name, file_path]
        return self._start_operation('playing', cmd, wait)

    def play_memory(self, audio_data: bytes, sample_rate: int = 44100,
                    channels: int = 2, wait: bool = True) -> Optional[subprocess.Popen]:
        """
        播放内存中的 PCM 音频数据（S16LE 格式）。
        :param audio_data: 原始 PCM 字节数据。
        :param sample_rate: 采样率（默认 44100）。
        :param channels: 声道数（默认 2）。
        :param wait: 同步等待完成。
        :return: 同步返回 None，异步返回 Popen 对象（外部可向 proc.stdin 写入更多数据）。
        """
        self._ensure_audio_devices(require_mic=False)
        cmd = [
            "paplay", "-d", self._sink_name,
            "--raw",
            f"--rate={sample_rate}",
            f"--channels={channels}",
            "--format=s16le",
            "-"
        ]
        return self._start_operation('playing', cmd, wait, stdin_data=audio_data)

    def record_file(self, file_path: str, duration: float, wait: bool = True) -> Optional[subprocess.Popen]:
        """录音并保存为 WAV 文件。"""
        self._ensure_audio_devices(require_mic=True)
        cmd = [
            "parecord",
            "-d", self._source_name,
            "--file-format=wav",
            "--time", str(int(duration)),
            file_path
        ]
        return self._start_operation('recording', cmd, wait)

    def record_stream(self, duration: float, sample_rate: int = 44100,
                      channels: int = 1, wait: bool = True) -> Optional[subprocess.Popen]:
        """
        录音并获取原始 PCM 流（通过 stdout 管道）。
        :return: 异步模式下返回 Popen 对象，可从 proc.stdout 读取 PCM 数据。
        """
        self._ensure_audio_devices(require_mic=True)
        cmd = [
            "parecord", "-d", self._source_name,
            "--raw",
            f"--rate={sample_rate}",
            f"--channels={channels}",
            "--format=s16le",
            "--time", str(int(duration))
        ]
        return self._start_operation('recording', cmd, wait)

    def set_profile(self, profile: Literal["a2dp", "hfp"], wait: bool = True, timeout: float = 5.0):
        """切换蓝牙音频配置文件"""
        output = self._pactl_run(["list", "short", "cards"])
        mac = self._bt_manager.get_target_mac()   # 无锁调用外部类
        mac_underscore = mac.replace(":", "_").lower()
        card_idx = None
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and mac_underscore in parts[1]:
                card_idx = parts[0]
                break
        if card_idx is None:
            raise RuntimeError("找不到蓝牙声卡，无法切换 profile")

        self._pactl_run(["set-card-profile", card_idx, profile], check=True)
        self._update_device_names()

        if wait:
            success = self._wait_until_ready(profile, timeout)
            if not success:
                raise RuntimeError(f"切换 {profile} 超时，音频设备未就绪")

    def stop_audio(self):
        """立即终止当前播放/录音操作，并重置状态为 idle"""
        with self._op_lock:
            proc = self._active_process
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    pass
                finally:
                    with self._state_lock:
                        self._audio_state = 'idle'
                        self._active_process = None

    def check_and_reset_idle(self):
        """
        检查异步操作是否结束，若结束则重置状态为 idle。
        应由节点在其主循环/统一监控线程中周期性调用。
        本方法内部已用锁保护，对频繁调用友好（开销极低）。
        """
        with self._op_lock:
            proc = self._active_process
            if proc is not None and proc.poll() is not None:
                # 进程已结束，清理状态
                with self._state_lock:
                    self._audio_state = 'idle'
                    self._active_process = None
    # ================================================================
    #   内部辅助方法
    # ================================================================

    def _pactl_run(self, args: list, check: bool = False) -> str:
        """执行一次性 pactl 命令（非交互式），不持锁"""
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
                raise
            return ""

    def _update_device_names(self):
        """根据蓝牙 MAC 从 PulseAudio 设备列表中查找扬声器与麦克风名称。不持锁调用外部类。"""
        mac = self._bt_manager.get_target_mac()   # 无锁调用，安全
        if not mac:
            with self._state_lock:
                self._sink_name = None
                self._source_name = None
            return
        mac_underscore = mac.replace(":", "_").lower()

        # 查找 sink
        sink_name = None
        output = self._pactl_run(["list", "short", "sinks"])
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and mac_underscore in parts[1]:
                sink_name = parts[1]
                break

        # 查找 source
        source_name = None
        output = self._pactl_run(["list", "short", "sources"])
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and mac_underscore in parts[1]:
                source_name = parts[1]
                break

        with self._state_lock:
            self._sink_name = sink_name
            self._source_name = source_name

    def _ensure_audio_devices(self, require_mic: bool = False):
        """
        在播放/录音前确保蓝牙已连接且所需设备存在。
        所有外部类调用均在锁外完成。
        """
        # 1. 检查蓝牙连接（无锁调用外部类）
        if not self._bt_manager.is_connected(force_refresh=True):
            raise RuntimeError("蓝牙耳机未连接，无法进行音频操作")

        # 2. 检查设备名称是否需要更新（读取状态用锁）
        with self._state_lock:
            need_update = (self._sink_name is None) or (require_mic and self._source_name is None)
        if need_update:
            self._update_device_names()   # 内部无锁

        # 3. 最终检查设备是否存在（只读状态）
        with self._state_lock:
            if self._sink_name is None:
                raise RuntimeError("未找到蓝牙扬声器设备（sink）")
            if require_mic and self._source_name is None:
                raise RuntimeError("未找到蓝牙麦克风设备（source）")

    def _start_operation(self, op_type: Literal['playing', 'recording'],
                         cmd: list, wait: bool,
                         stdin_data: Optional[bytes] = None) -> Optional[subprocess.Popen]:
        """
        启动子进程并管理音频状态互斥。
        使用 _op_lock 保护子进程创建，避免并发操作。
        """
        # 操作互斥由 _op_lock 保证
        with self._op_lock:
            if self._audio_state != 'idle':
                raise RuntimeError(f"无法启动{op_type}，当前正在进行 {self._audio_state} 操作")

            # 创建子进程
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if stdin_data else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False
            )
            # 更新状态
            with self._state_lock:
                self._audio_state = op_type
                self._active_process = proc

        # 同步等待处理（在锁外完成）
        if wait:
            try:
                if stdin_data:
                    try:
                        proc.stdin.write(stdin_data)
                        proc.stdin.close()
                    except Exception:
                        pass
                proc.wait()
                if proc.returncode != 0:
                    err = proc.stderr.read()
                    raise RuntimeError(f"{op_type}失败，返回码 {proc.returncode}: {err}")
            finally:
                # 重置状态
                with self._op_lock:
                    if self._active_process is proc:
                        with self._state_lock:
                            self._audio_state = 'idle'
                            self._active_process = None
            return None
        else:
            # 异步模式：返回进程对象，状态重置由 operation_monitor_loop 负责
            return proc

    def _check_device_ready(self, mode: Literal["a2dp", "hfp"], force_refresh: bool) -> bool:
        """检查指定模式设备是否就绪。外部类调用在锁外进行。"""
        if not self._bt_manager.is_connected(force_refresh=True):
            return False
        if force_refresh:
            self._update_device_names()
        with self._state_lock:
            if mode == "hfp":
                return self._source_name is not None
            else:
                return self._sink_name is not None

    def _wait_until_ready(self, profile: Literal["a2dp", "hfp"],
                          timeout: float = 10.0, check_interval: float = 0.5) -> bool:
        """轮询等待设备就绪"""
        start = time.time()
        while time.time() - start < timeout:
            if profile == "hfp":
                ready = self.is_mic_ready(force_refresh=True)
            else:
                ready = self.is_sink_ready(force_refresh=True)
            if ready:
                return True
            time.sleep(check_interval)
        return False

    # ================================================================
    #   虚拟声卡自动管理（私有，不对外暴露）
    # ================================================================

    def _auto_setup_virtual_sink(self):
        """
        根据环境标志自动创建虚拟声卡。
        仅在非真实 Jetson 且 SIM_LOG 开启的仿真环境下启用。
        """
        if is_real_jetson() or not SIM_LOG:
            return

        with self._state_lock:
            if self._virtual_sink_module_id is not None:
                return

        sink_name = "sim_audio_capture"
        if SIM_LOG:
            print("[AudioManager] 创建仿真虚拟声卡:", sink_name)

        output = self._pactl_run([
            "load-module", "module-null-sink",
            f"sink_name={sink_name}",
            "sink_properties=device.description=SimAudioCapture"
        ], check=False).strip()

        if not output.isdigit():
            return

        module_id = int(output)
        with self._state_lock:
            self._virtual_sink_module_id = module_id
            self._virtual_sink_name = sink_name

        # 建立环回：将蓝牙扬声器的 monitor 接入虚拟 sink，用于录制播放内容
        with self._state_lock:
            bt_sink = self._sink_name
        if bt_sink:
            loop_source = f"{bt_sink}.monitor"
            loopback_output = self._pactl_run([
                "load-module", "module-loopback",
                f"source={loop_source}",
                f"sink={sink_name}"
            ], check=False).strip()
            if loopback_output.isdigit():
                with self._state_lock:
                    self._loopback_module_id = int(loopback_output)
                if SIM_LOG:
                    print("[AudioManager] 已建立环回: {bt_sink} -> {sink_name}")

    def _auto_teardown_virtual_sink(self):
        """卸载虚拟声卡及环回模块（清理资源）。"""
        with self._state_lock:
            loopback_id = self._loopback_module_id
            sink_module_id = self._virtual_sink_module_id
            self._loopback_module_id = None
            self._virtual_sink_module_id = None
            self._virtual_sink_name = None

        if loopback_id is not None:
            self._pactl_run(["unload-module", str(loopback_id)], check=False)
            if SIM_LOG:
                print("[AudioManager] 已移除环回模块")
        if sink_module_id is not None:
            self._pactl_run(["unload-module", str(sink_module_id)], check=False)
            if SIM_LOG:
                print("[AudioManager] 已移除仿真虚拟声卡")