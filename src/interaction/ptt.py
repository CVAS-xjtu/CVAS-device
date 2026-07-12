import time
import threading
import os
import subprocess
import glob
from drivers import BluetoothManager, AudioManager
from audio import asr
from drivers import button


class VoiceRecorderNode:
    """
    假设drivers.button 模块提供的是 on_press(callback) 和 on_release(callback) 接口。
    """

    def __init__(
        self,
        device_name: str,
        record_file_base: str = "/tmp/record_voice",   
        a2dp_profile: str = "a2dp_sink",
        hsp_profile: str = "headset_head_unit",
        auto_switch_profile: bool = True,
        asr_func=None,
        debounce_ms: int = 50,
        max_keep_files: int = 5,                       # 最多保留文件数
    ):
        
        self.device_name = device_name
        self.record_file_base = record_file_base
        self.a2dp_profile = a2dp_profile
        self.hsp_profile = hsp_profile
        self.auto_switch_profile = auto_switch_profile
        self.debounce_ms = debounce_ms
        self.max_keep_files = max_keep_files

        # 确定 ASR 函数
        if asr_func is not None:
            self.asr_func = asr_func
        else:
            if hasattr(asr, 'transcribe'):
                self.asr_func = asr.transcribe
            elif hasattr(asr, 'SenseVoiceSmall_ASR'):
                def _asr_wrapper(f):
                    status, text = asr.SenseVoiceSmall_ASR(f)
                    return text if status == 'ok' else None
                self.asr_func = _asr_wrapper
            else:
                self.asr_func = None

        self._audio_manager = None
        self._current_mac = None

        # 按键状态
        self._key_pressed = False
        self._recording_proc = None
        self._last_press_time = 0
        self._current_record_file = None   # 当前录音文件路径

        # 线程锁
        self._lock = threading.Lock()

        # 识别结果回调
        self.on_result = None
        self.on_error = None

        # button 回调句柄
        self._press_handler = None
        self._release_handler = None

        # 初始化设备
        self._init_device()


    def _find_mac(self):
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
                        raise RuntimeError(f"初始化设备失败: {e2}")
                else:
                    raise RuntimeError(f"初始化设备失败: {e}")

    def _refresh_device_if_needed(self):
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

    # ---------- 文件清理 ----------
    def _cleanup_old_files(self):
        """检查录音文件数量，若超过 max_keep_files 则删除最旧的"""
        try:
            # 构建匹配模式
            dirname = os.path.dirname(self.record_file_base)
            basename = os.path.basename(self.record_file_base)
            pattern = os.path.join(dirname, f"{basename}_*.wav")
            files = glob.glob(pattern)

            if len(files) <= self.max_keep_files:
                return

            # 按修改时间排序
            files.sort(key=os.path.getmtime)
            # 删除超出数量的最旧文件
            for f in files[:-self.max_keep_files]:
                try:
                    os.remove(f)
                    print(f"[清理] 已删除旧录音文件: {f}")
                except Exception as e:
                    print(f"[清理] 删除文件失败: {f} - {e}")
        except Exception as e:
            print(f"[清理] 清理过程异常: {e}")

    def _start_recording(self):
        if self._key_pressed:
            return

        self._key_pressed = True

        # 生成带时间戳的文件名
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self._current_record_file = f"{self.record_file_base}_{timestamp}.wav"
        print(f"[录音] 文件将保存至: {self._current_record_file}")

        # 停止当前播放
        if self._audio_manager and self._audio_manager.get_audio_state() == 'playing':
            self._audio_manager.stop_audio()
            time.sleep(0.1)

        # 切换模式
        if self.auto_switch_profile:
            try:
                self._audio_manager.set_profile(self.hsp_profile)
                print(f"切换到 {self.hsp_profile}")
                time.sleep(0.2)
            except Exception as e:
                print(f"切换模式失败: {e}，尝试刷新设备...")
                if not self._refresh_device_if_needed():
                    self._key_pressed = False
                    if self.on_error:
                        self.on_error("无法切换到通话模式")
                    return
                try:
                    self._audio_manager.set_profile(self.hsp_profile)
                    time.sleep(0.2)
                except Exception:
                    self._key_pressed = False
                    if self.on_error:
                        self.on_error("重试切换通话模式失败")
                    return

        # 开始录音（异步）
        try:
            self._recording_proc = self._audio_manager.record_file(
                self._current_record_file, duration=600, wait=False
            )
            print("录音中...（请释放按键停止）")
        except Exception as e:
            print(f"录音启动失败: {e}")
            self._key_pressed = False
            if self.on_error:
                self.on_error(f"录音启动失败: {e}")

    def _stop_recording_and_recognize(self):
        if not self._key_pressed:
            return

        self._key_pressed = False
        print("[按键] 释放 -> 停止录音，切换模式...")

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
                print(f"切回 {self.a2dp_profile}")
            except Exception as e:
                print(f"切回 A2DP 失败: {e}")

        # 检查录音文件
        if not self._current_record_file or not os.path.exists(self._current_record_file) or os.path.getsize(self._current_record_file) == 0:
            msg = "录音文件为空或不存在"
            print(msg)
            if self.on_error:
                self.on_error(msg)
            # 即使文件无效，也执行清理
            if self._current_record_file and os.path.exists(self._current_record_file):
                try:
                    os.remove(self._current_record_file)
                except:
                    pass
            self._current_record_file = None
            self._cleanup_old_files()   # 依然执行清理
            return

        # 调用 ASR
        text = None
        if self.asr_func is not None:
            print("正在识别语音...")
            try:
                text = self.asr_func(self._current_record_file)
                if text:
                    print(f" 识别结果: {text}")
                    if self.on_result:
                        self.on_result(text)
                else:
                    msg = "识别结果为空"
                    print(msg)
                    if self.on_error:
                        self.on_error(msg)
            except Exception as e:
                msg = f"ASR 异常: {e}"
                print(msg)
                if self.on_error:
                    self.on_error(msg)
        else:
            print("录音完成（未启用识别）")

        # 清理旧文件
        self._cleanup_old_files()
        self._current_record_file = None


    def _on_press(self):
        with self._lock:
            now = time.time() * 1000
            if now - self._last_press_time < self.debounce_ms:
                return
            self._last_press_time = now
            if self._key_pressed:
                return
            self._start_recording()

    def _on_release(self):
        with self._lock:
            if not self._key_pressed:
                return
            self._stop_recording_and_recognize()

    def start(self):
        if self._audio_manager is None:
            raise RuntimeError("设备未初始化，无法启动")

        try:
            self._press_handler = button.on_press(self._on_press)
            self._release_handler = button.on_release(self._on_release)
        except AttributeError:
            raise RuntimeError("button 模块未提供 on_press/on_release 方法，请检查实现。")

        print("录音节点已启动，按下物理按键触发录音。")

    def stop(self):
        if hasattr(button, 'unhook_all'):
            button.unhook_all()
        elif self._press_handler is not None and hasattr(button, 'unhook'):
            button.unhook(self._press_handler)
            button.unhook(self._release_handler)

        # 如果正在录音，停止并识别
        with self._lock:
            if self._key_pressed:
                self._stop_recording_and_recognize()
            if self._audio_manager:
                self._audio_manager.stop()

        print("录音节点已停止。")

    def set_asr_func(self, func):
        self.asr_func = func

    def set_result_callback(self, cb):
        self.on_result = cb

    def set_error_callback(self, cb):
        self.on_error = cb