import time
import threading
import os
import glob
import subprocess
from typing import Optional, Callable
from drivers import button
from drivers import AudioManager
from audio import asr


class PTTController:

    def __init__(
        self,
        device_name: str,
        record_file_base: str = "/tmp/record_voice",
        a2dp_profile: str = "a2dp_sink",
        hsp_profile: str = "headset_head_unit",
        auto_switch_profile: bool = True,
        asr_func: Optional[Callable] = None,
        max_keep_files: int = 5,
        poll_interval: float = 0.02,          # 轮询间隔（秒）
    ):
    
        self.device_name = device_name
        self.record_file_base = record_file_base
        self.a2dp_profile = a2dp_profile
        self.hsp_profile = hsp_profile
        self.auto_switch_profile = auto_switch_profile
        self.max_keep_files = max_keep_files
        self.poll_interval = poll_interval

        # 确定 ASR 函数
        if asr_func is not None:
            self.asr_func = asr_func
        else:
            if hasattr(asr, 'transcribe'):
                self.asr_func = asr.transcribe
            elif hasattr(asr, 'SenseVoiceSmall_ASR'):
                def _wrapper(f):
                    status, text = asr.SenseVoiceSmall_ASR(f)
                    return text if status == 'ok' else None
                self.asr_func = _wrapper
            else:
                self.asr_func = None


        self._audio_manager: Optional[AudioManager] = None
        self._current_mac: Optional[str] = None
        self._recording_proc = None
        self._current_record_file: Optional[str] = None
        self._key_pressed = False          # 当前按键状态（True=按下）
        self._is_recording = False         # 是否正在录音（防止重复触发）
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._polling_thread: Optional[threading.Thread] = None

        # 回调函数
        self.on_result: Optional[Callable[[str], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

        # 初始化蓝牙设备
        self._init_device()


    def _find_mac(self) -> Optional[str]:
        """根据设备名称查找已配对的蓝牙 MAC 地址"""
        try:
            output = subprocess.check_output(
                ["bluetoothctl", "paired-devices"],
                text=True, stderr=subprocess.DEVNULL
            )
            for line in output.splitlines():
                parts = line.split(" ", 2)
                if len(parts) >= 3:
                    mac = parts[1]
                    name = parts[2]
                    if self.device_name.lower() in name.lower():
                        return mac
        except Exception:
            pass
        return None

    def _init_device(self):
        mac = self._find_mac()
        if not mac:
            raise RuntimeError(f"未找到蓝牙设备（名称包含：{self.device_name}）")
        self._current_mac = mac
        self._audio_manager = AudioManager({"target_mac": mac})

        if self.auto_switch_profile:
            try:
                self._audio_manager.set_profile(self.hsp_profile)
                time.sleep(0.2)
                if not self._audio_manager.wait_until_ready(timeout=3, require_mic=True):
                    raise RuntimeError("麦克风设备未就绪")
                self._audio_manager.set_profile(self.a2dp_profile)
                time.sleep(0.2)
            except Exception as e:
                # 尝试重新获取 MAC
                new_mac = self._find_mac()
                if new_mac and new_mac != mac:
                    self._current_mac = new_mac
                    self._audio_manager = AudioManager({"target_mac": new_mac})
                    try:
                        self._audio_manager.set_profile(self.hsp_profile)
                        time.sleep(0.2)
                        if not self._audio_manager.wait_until_ready(timeout=3, require_mic=True):
                            raise RuntimeError("重试后麦克风仍不可用")
                        self._audio_manager.set_profile(self.a2dp_profile)
                    except Exception as e2:
                        raise RuntimeError(f"重试初始化失败: {e2}")
                else:
                    raise RuntimeError(f"初始化设备失败: {e}")

    def _refresh_device_if_needed(self) -> bool:
        """尝试重新连接设备，返回是否成功"""
        new_mac = self._find_mac()
        if not new_mac:
            return False
        self._current_mac = new_mac
        if self._audio_manager:
            self._audio_manager.stop()
        self._audio_manager = AudioManager({"target_mac": new_mac})
        try:
            self._audio_manager.set_profile(self.hsp_profile)
            time.sleep(0.2)
            if self._audio_manager.wait_until_ready(timeout=2, require_mic=True):
                self._audio_manager.set_profile(self.a2dp_profile)
                return True
        except Exception:
            pass
        return False

    # 文件管理
    def _cleanup_old_files(self):
        try:
            dirname = os.path.dirname(self.record_file_base)
            basename = os.path.basename(self.record_file_base)
            pattern = os.path.join(dirname, f"{basename}_*.wav")
            files = glob.glob(pattern)
            if len(files) <= self.max_keep_files:
                return
            files.sort(key=os.path.getmtime)
            for f in files[:-self.max_keep_files]:
                try:
                    os.remove(f)
                    print(f"[PTT] 已删除旧录音: {f}")
                except Exception as e:
                    print(f"[PTT] 删除文件 {f} 失败: {e}")
        except Exception as e:
            print(f"[PTT] 清理过程异常: {e}")

    # 录音控制
    def _start_recording(self):
        """按下按钮时调用（线程安全）"""
        with self._lock:
            if self._is_recording:
                return
            self._is_recording = True
            self._key_pressed = True

        # 生成带时间戳的文件名
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self._current_record_file = f"{self.record_file_base}_{timestamp}.wav"
        print(f"[PTT] 开始录音 -> {self._current_record_file}")

        # 停止当前播放
        if self._audio_manager and self._audio_manager.get_audio_state() == 'playing':
            self._audio_manager.stop_audio()
            time.sleep(0.1)

        # 切换至 HSP 模式
        if self.auto_switch_profile:
            try:
                self._audio_manager.set_profile(self.hsp_profile)
                time.sleep(0.2)
            except Exception as e:
                print(f"[PTT] 切换 HSP 失败: {e}，尝试刷新设备...")
                if not self._refresh_device_if_needed():
                    self._on_error("无法切换到通话模式")
                    self._is_recording = False
                    self._key_pressed = False
                    return
                try:
                    self._audio_manager.set_profile(self.hsp_profile)
                    time.sleep(0.2)
                except Exception:
                    self._on_error("重试切换通话模式失败")
                    self._is_recording = False
                    self._key_pressed = False
                    return

        # 开始录音（后台异步）
        try:
            self._recording_proc = self._audio_manager.record_file(
                self._current_record_file, duration=600, wait=False
            )
            print("[PTT] 录音中...（请释放按钮停止）")
        except Exception as e:
            self._on_error(f"录音启动失败: {e}")
            self._is_recording = False
            self._key_pressed = False

    def _stop_recording_and_recognize(self):
        """释放按钮时调用（线程安全）"""
        with self._lock:
            if not self._is_recording:
                return
            self._is_recording = False
            self._key_pressed = False

        print("[PTT] 释放按钮，停止录音...")

        # 停止录音
        if self._audio_manager:
            self._audio_manager.stop_audio()
        if self._recording_proc is not None:
            try:
                self._recording_proc.wait(timeout=1)
            except Exception:
                pass
            self._recording_proc = None

        # 切回 A2DP
        if self.auto_switch_profile and self._audio_manager:
            try:
                self._audio_manager.set_profile(self.a2dp_profile)
                print(f"[PTT] 切回 {self.a2dp_profile}")
            except Exception as e:
                print(f"[PTT] 切回 A2DP 失败: {e}")

        # 检查录音文件
        file_path = self._current_record_file
        self._current_record_file = None

        if not file_path or not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            self._on_error("录音文件为空或不存在")
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
            self._cleanup_old_files()
            return

        # 调用 ASR
        text = None
        if self.asr_func is not None:
            print("[PTT] 正在识别语音...")
            try:
                text = self.asr_func(file_path)
                if text:
                    print(f"[PTT] 识别结果: {text}")
                    if self.on_result:
                        self.on_result(text)
                else:
                    self._on_error("识别结果为空")
            except Exception as e:
                self._on_error(f"ASR 异常: {e}")
        else:
            print("[PTT] 录音完成（未启用识别）")

        # 清理旧文件
        self._cleanup_old_files()

    def _on_error(self, msg: str):
        print(f"[PTT] 错误: {msg}")
        if self.on_error:
            self.on_error(msg)

    # 后台轮询线程
    def _poll_loop(self):
        """轮询 button.is_pressed()，按下/释放分别触发录音开始/停止"""
        last_state = False
        while not self._stop_event.is_set():
            current = button.is_pressed()
            # 检测上升沿（释放 -> 按下）
            if current and not last_state:
                self._start_recording()
            # 检测下降沿（按下 -> 释放）
            elif not current and last_state:
                self._stop_recording_and_recognize()
            last_state = current
            time.sleep(self.poll_interval)

    # 启动/停止
    def start(self):
        """启动 PTT 轮询线程"""
        if self._polling_thread and self._polling_thread.is_alive():
            print("[PTT] 轮询线程已在运行")
            return
        self._stop_event.clear()
        self._polling_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._polling_thread.start()
        print("[PTT] 控制器已启动，按下按钮开始录音。")

    def stop(self):
        """停止轮询线程并释放资源"""
        self._stop_event.set()
        if self._polling_thread and self._polling_thread.is_alive():
            self._polling_thread.join(timeout=2)

        # 如果正在录音，强制停止并识别
        if self._is_recording:
            self._stop_recording_and_recognize()

        if self._audio_manager:
            self._audio_manager.stop()

        print("[PTT] 控制器已停止。")

    # 回调设置
    def set_result_callback(self, cb: Callable[[str], None]):
        self.on_result = cb

    def set_error_callback(self, cb: Callable[[str], None]):
        self.on_error = cb