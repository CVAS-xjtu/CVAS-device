import subprocess
import threading
import time
import os
from typing import Optional

class BluetoothManager:
    def __init__(self, audio_cfg: dict):
        # 蓝牙MAC地址
        self._target_mac = audio_cfg.get("target_mac", "").strip().upper()
        # bluetoothctl子进程
        self._proc: Optional[subprocess.Popen] = None
        # 子进程运行状态
        self._running = False
        # 蓝牙连接状态
        self._connected = False
        # 麦克风输入 source ID
        self._mic_source_id: Optional[int] = None
        # 扬声器输出 sink ID
        self._speaker_sink_id: Optional[int] = None
        # 输出缓冲区
        self._bt_output_buf = ""
        # 线程安全锁
        self._state_lock = threading.Lock() # 状态查询保护
        self._op_lock = threading.Lock() # 连接/断连/模式切换保护
        self._buf_lock = threading.Lock() # 输出缓冲保护

        # 启动 bluetoothctl 常驻进程
        self._start_btctl()

        # 拉起后台线程
        self._spawn_background_threads()

    def _is_alive(self) -> bool:
        """检查bluetoothctl进程是否存活"""
        if not self._running:
            return False
        if self._proc is None or self._proc.poll() is not None:
            return False
        return True


    def _can_send_command(self) -> bool:
        """检查是否可以向bluetoothctl发送指令"""
        return self._is_alive() and self._proc.stdin is not None


    def _can_read_output(self) -> bool:
        """检查是否可以读取bluetoothctl输出"""
        return self._is_alive() and self._proc.stdout is not None
    

    def _can_read_stderr(self) -> bool:
        """检查是否可以读取bluetoothctl错误输出"""
        return self._is_alive() and self._proc.stderr is not None


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
        

    def _start_btctl(self):
        """启动常驻交互式bluetoothctl终端进程"""
        if self._is_alive():
            return
        env = os.environ.copy()
        env["LANG"] = "C"
        env["LC_ALL"] = "C" # 强制英文输出
        self._proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env
        )
        self._send_command("power on")


    def _spawn_background_threads(self):
        """统一创建所有后台线程: stdout、stderr、断线重连"""
        # 输出消费线程
        self._stdout_thread = threading.Thread(target=self._stdout_consumer, daemon=True)
        self._stdout_thread.start()

        # 错误流消费线程
        self._stderr_thread = threading.Thread(target=self._stderr_consumer, daemon=True)
        self._stderr_thread.start()

        # 断线自动重连线程
        self._reconn_thread = threading.Thread(target=self._reconnect_loop, daemon=True)
        self._reconn_thread.start()


    def _stdout_consumer(self):
        """输出缓冲消费线程"""
        while self._can_read_output():
            line = self._proc.stdout.readline()
            if not line:
                break
            with self._buf_lock:
                self._bt_output_buf += line
                # 限制缓冲区上限，避免无限膨胀
                max_buf_len = 2000
                if len(self._bt_output_buf) > max_buf_len:
                    self._bt_output_buf = self._bt_output_buf[-1500:]


    def _stderr_consumer(self):
        """错误流消费线程"""
        while self._can_read_stderr():
            line = self._proc.stderr.readline()
            if not line:
                break


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
            for line in output.splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                src_id_str, name = parts[0], parts[1]
                if mac_underscore in name:
                    return int(src_id_str)
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
            for line in output.splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                sink_id_str, name = parts[0], parts[1]
                if mac_underscore in name:
                    return int(sink_id_str)
        except Exception:
            pass
        return None


    def _query_battery_level(self) -> Optional[int]:
        """查询电池电量"""
        try:
            output = subprocess.check_output(
                ["bluetoothctl", "info", self._target_mac],
                text=True,
                stderr=subprocess.STDOUT
            )
            for line in output.splitlines():
                if "Battery" in line:
                    return int(line.split(":")[-1].strip())
        except Exception:
            pass
        return None


    def _clear_audio_id_cache(self):
        """作废旧ID"""
        self._mic_source_id = None
        self._speaker_sink_id = None



    # 以下是所有对外提供的连接、断连和模式切换接口，op_lock保护
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



    # 以下是所有对外提供的查询接口，state_lock保护
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


    def get_battery_level(self, force_refresh: bool = False) -> Optional[int]:
        """获取电池电量"""
        with self._state_lock:
            if force_refresh or self._battery_level is None:
                self._battery_level = self._query_battery_level()
            return self._battery_level