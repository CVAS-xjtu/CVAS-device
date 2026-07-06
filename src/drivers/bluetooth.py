import subprocess
import threading
import time
from typing import Optional

class BluetoothManager:
    def __init__(self):
        self._target_mac = None
        self._proc: Optional[subprocess.Popen] = None
        self._running = False
        self._connected = False
        # 麦克风输入 source ID
        self._mic_source_id: Optional[int] = None
        # 扬声器输出 sink ID
        self._speaker_sink_id: Optional[int] = None
        # 输出缓冲区
        self._bt_output_buf = ""
        # 线程安全锁
        self._state_lock = threading.Lock()
        self._op_lock = threading.Lock()
        self._buf_lock = threading.Lock()


    def _is_alive(self) -> bool:
        """检查bluetoothctl进程是否存活"""
        return self._proc is not None and self._proc.poll() is None


    def _can_send_command(self) -> bool:
        """检查是否可以向bluetoothctl发送指令"""
        return self._is_alive() and self._proc.stdin is not None


    def _start_btctl(self):
        """启动常驻交互式bluetoothctl终端进程"""
        if self._is_alive():
            return
        self._proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        self._send_command("power on")
        time.sleep(0.4)
        self._send_command("scan on")


    def _send_command(self, cmd: str) -> bool:
        """向交互式bluetoothctl下发单行指令"""
        if not self._can_send_command():
            return False
        try:
            self._proc.stdin.write(f"{cmd}\n")
            self._proc.stdin.flush()
            return True
        except Exception:
            return False


    def _check_connected(self) -> bool:
        """查询当前耳机是否处于已连接状态"""
        if not self._is_alive():
            return False

        cmd = f"info {self._target_mac}"
        if not self._send_command(cmd):
            return False
        # 记录发送前缓冲区长度
        with self._buf_lock:
            start_pos = len(self._bt_output_buf)

        timeout = 0.5   # 总超时0.5s，防止卡死死等
        start_time = time.time()

        # 循环等待提示符 [bluetooth]# 出现，代表命令执行结束
        found_prompt = False
        while time.time() - start_time < timeout:
            with self._buf_lock:
                new_part = self._bt_output_buf[start_pos:]
            if "[bluetooth]#" in new_part:
                found_prompt = True
                break
            time.sleep(0.01)

        if not found_prompt:
            # 命令超时未返回，判定查询失败
            return False

        # 只在本次命令新增输出里判断连接状态
        with self._buf_lock:
            new_output = self._bt_output_buf[start_pos:]
        return "Connected: yes" in new_output


    def _query_pulse_mic_source_id(self) -> Optional[int]:
        """查询蓝牙耳机麦克风对应的PulseAudio声卡索引号"""
        try:
            output = subprocess.check_output(
                ["pactl", "list", "short", "sources"],
                text=True,
                stderr=subprocess.STDOUT
            )
            mac_underscore = self._target_mac.replace(":", "_").lower()
            for idx, line in enumerate(output.splitlines()):
                if mac_underscore in line:
                    return idx
        except Exception:
            pass
        return None


    def _query_pulse_speaker_sink_id(self) -> Optional[int]:
        """查询蓝牙耳机扬声器对应的PulseAudio声卡索引号"""
        try:
            output = subprocess.check_output(
                ["pactl", "list", "short", "sinks"],
                text=True,
                stderr=subprocess.STDOUT
            )
            mac_underscore = self._target_mac.replace(":", "_").lower()
            for idx, line in enumerate(output.splitlines()):
                if mac_underscore in line:
                    return idx
        except Exception:
            pass
        return None


    def _clear_audio_id_cache(self):
        """作废旧ID"""
        self._mic_source_id = None
        self._speaker_sink_id = None


    def pair_and_connect(self):
        """核心：复刻手动配对→信任→连接整套流程，加锁防并发"""
        with self._op_lock:
            if self._check_connected():
                return
            # 发起配对
            self._send_command(f"pair {self._target_mac}")
            time.sleep(2.2)
            # 信任设备，避免后续弹窗确认
            self._send_command(f"trust {self._target_mac}")
            time.sleep(1.0)
            # 建立音频连接
            self._send_command(f"connect {self._target_mac}")
            time.sleep(1.8)

    def disconnect_headset(self):
        """主动断开耳机连接"""
        with self._op_lock:
            self._send_command(f"disconnect {self._target_mac}")

    def connection_poll_loop(self, poll_interval: float = 3.0):
        """常驻后台轮询线程：检测状态+断线自动重连"""
        self._running = True
        self._start_btctl()

        while self._running:
            try:
                conn_state = self._check_connected()
                with self._state_lock:
                    self._connected = conn_state

                if conn_state:
                    self._mic_source_id = self.get_pulse_audio_source_id()
                else:
                    self._mic_source_id = None
                    self.pair_and_connect()

            except Exception:
                pass
            time.sleep(poll_interval)

    def stop(self):
        """停止管理线程、关闭bluetoothctl进程"""
        self._running = False
        if self._is_alive():
            self._send_command(f"disconnect {self._target_mac}")
            self._proc.terminate()
            self._proc.wait()
            self._proc = None

    # 以下是所有对外接口
    def is_connected(self, force_refresh: bool = False) -> bool:
        """查询当前耳机是否已连接"""
        with self._state_lock:
            if force_refresh:
                self._connected = self._check_connected()
            return self._connected


    def get_mic_source_id(self, force_refresh: bool = False) -> Optional[int]:
        """获取麦克风ID, 带缓存"""
        with self._state_lock:
            if force_refresh or self._mic_source_id is None:
                self._mic_source_id = self._query_pulse_mic_source_id()
            return self._mic_source_id


    def get_speaker_sink_id(self, force_refresh: bool = False) -> Optional[int]:
        """获取扬声器ID, 带缓存"""
        with self._state_lock:
            if force_refresh or self._speaker_sink_id is None:
                self._speaker_sink_id = self._query_pulse_speaker_sink_id()
            return self._speaker_sink_id

        


    def scan_nearby_devices(self, scan_duration: float = 4.0) -> dict[str, str]:
        """
        扫描周边蓝牙设备
        return: {设备名称: MAC地址}
        """
        # 开启扫描
        self._send_command("scan on")
        time.sleep(scan_duration)
        # 一次性读取所有已发现设备
        result = subprocess.check_output(
            ["bluetoothctl", "devices"],
            text=True
        )
        dev_dict = {}
        for line in result.splitlines():
            parts = line.strip().split()
            if len(parts) >= 3:
                mac_addr = parts[1]
                dev_name = " ".join(parts[2:])
                dev_dict[dev_name] = mac_addr
        # 可选：关闭扫描节省功耗
        # self._send_command("scan off")
        return dev_dict
    
    def set_target_mac(self, new_mac: str):
        with self._state_lock:
            self._target_mac = new_mac.strip().upper()
            # 修改MAC后清空旧声卡状态
            self._audio_card_id = None


    def _stdout_consumer(self):
        while self._running and self._proc and self._proc.stdout:
            line = self._proc.stdout.readline()
            if not line:
                break
            with self._buf_lock:
                self._bt_output_buf += line
                # 限制缓冲区上限，避免无限膨胀
                max_buf_len = 2000
                if len(self._bt_output_buf) > max_buf_len:
                    self._bt_output_buf = self._bt_output_buf[-1500:]