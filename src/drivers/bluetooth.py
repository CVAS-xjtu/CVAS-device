import subprocess
import threading
import time
import os
from typing import Optional
import re


class BluetoothManager:
    def __init__(self, audio_cfg: dict):
        # 配置字典
        self._audio_cfg = audio_cfg
        # 蓝牙MAC地址
        self._target_mac = self._audio_cfg.get("target_mac", "").strip().upper()
        # 蓝牙连接轮询间隔
        self._poll_interval = self._audio_cfg.get("reconnect_poll_interval_sec", 3)
        # 蓝牙管理器后台线程休眠间隔
        self._thread_sleep = self._audio_cfg.get("thread_sleep_sec", 0.5)
        # 蓝牙管理器内部循环休眠间隔
        self._inner_loop_sleep = self._audio_cfg.get("inner_loop_sleep_sec", 0.01)
        # 蓝牙管理器输出缓冲区最大长度
        self._buf_max_len = self._audio_cfg.get("buf_max_len", 2000)
        # 蓝牙管理器输出缓冲区保留长度
        self._buf_reserve_len = self._audio_cfg.get("buf_reserve_len", 1500)
        # 蓝牙管理器命令超时时间
        self._cmd_timeout = self._audio_cfg.get("cmd_timeout_sec", 0.5)
        # 蓝牙管理器线程join超时时间
        self._thread_join_timeout = self._audio_cfg.get("thread_join_timeout_sec", 1.0)

        if self._thread_sleep <= 0:
            self._thread_sleep = 0.5
        if self._cmd_timeout <= 0 or self._cmd_timeout > 2:
            self._cmd_timeout = 0.5
        if self._buf_reserve_len >= self._buf_max_len:
            self._buf_reserve_len = self._buf_max_len - 500
        if self._thread_join_timeout <= 0 or self._thread_join_timeout > 3:
            self._thread_join_timeout = 1.0

        # bluetoothctl子进程
        self._proc: Optional[subprocess.Popen] = None
        # 子进程运行状态
        self._running = True
        # 蓝牙连接状态
        self._connected = False
        # 是否启用断线自动重连
        self._auto_reconnect = True
        # 蓝牙耳机电池电量
        self._battery_level: Optional[int] = None
        # 输出缓冲区
        self._bt_output_buf = ""
        # 电量读取正则表达式
        self._battery_re = re.compile(r"\((\d+)\)")
        # 匹配bluetoothctl提示符：[xxx]#
        self._prompt_re = re.compile(r"\[.*\]#")
        # 线程安全锁
        self._state_lock = threading.Lock() # 状态保护
        self._op_lock = threading.Lock() # 操作保护
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
        with self._op_lock:
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
        """bluetoothctl标准输出消费线程"""
        while True:
            with self._state_lock:
                running = self._running
            if not running:
                break
            if not self._is_alive():
                time.sleep(self._thread_sleep)
                continue
            try:
                line = self._proc.stdout.readline()
                if not line:
                    time.sleep(self._thread_sleep)
                    continue
                with self._buf_lock:
                    self._bt_output_buf += line
                    if len(self._bt_output_buf) > self._buf_max_len:
                        self._bt_output_buf = self._bt_output_buf[-self._buf_reserve_len:]
            except Exception:
                time.sleep(self._thread_sleep)


    def _stderr_consumer(self):
        """bluetoothctl的错误输出消费线程"""
        while True:
            with self._state_lock:
                running = self._running
            if not running:
                break
            if not self._is_alive():
                time.sleep(self._thread_sleep)
                continue
            try:
                line = self._proc.stderr.readline()
                if not line:
                    time.sleep(self._thread_sleep)
                    continue
            except Exception:
                time.sleep(self._thread_sleep)


    def _reconnect_loop(self):
        """断线自动重连线程"""
        while True:
            with self._state_lock:
                running = self._running
                auto_reconnect = self._auto_reconnect
            if not running:
                break
            try:
                self._start_btctl()
                with self._op_lock:
                    real_conn = self._check_connected()
                    if auto_reconnect and not real_conn:
                        self._send_command(f"connect {self._target_mac}")
                with self._state_lock:
                    self._connected = real_conn
            except Exception:
                pass
            time.sleep(self._poll_interval)


    def _check_connected(self) -> bool:
        """查询当前耳机是否处于已连接状态, 调用前请确保已获取_op_lock锁"""
        cmd = f"info {self._target_mac}"
        if not self._send_command(cmd):
            return False
        # 记录发送前缓冲区长度
        with self._buf_lock:
            start_pos = len(self._bt_output_buf)

        start_time = time.time()

        # 循环等待提示符 [xxx]# 出现，代表命令执行结束
        found_prompt = False
        while time.time() - start_time < self._cmd_timeout:
            with self._buf_lock:
                new_part = self._bt_output_buf[start_pos:]
            if self._prompt_re.search(new_part):
                found_prompt = True
                break
            time.sleep(self._inner_loop_sleep)

        if not found_prompt:
            # 命令超时未返回，判定查询失败
            return False

        # 只在本次命令新增输出里判断连接状态
        with self._buf_lock:
            new_output = self._bt_output_buf[start_pos:]
        return "Connected: yes" in new_output


    def _query_battery_level(self) -> Optional[int]:
        """查询电池电量, 调用前请确保已获取_op_lock锁"""
        cmd = f"info {self._target_mac}"
        if not self._send_command(cmd):
            return None
        
        # 记录发送前缓冲区长度
        with self._buf_lock:
            start_pos = len(self._bt_output_buf)

        start_time = time.time()

        # 循环等待提示符 [xxx]# 出现，代表命令执行结束
        found_prompt = False
        while time.time() - start_time < self._cmd_timeout:
            with self._buf_lock:
                new_part = self._bt_output_buf[start_pos:]
            if self._prompt_re.search(new_part):
                found_prompt = True
                break
            time.sleep(self._inner_loop_sleep)
        
        if not found_prompt:
            # 命令超时未返回，判定查询失败
            return None
        
        # 在本次命令输出内容中匹配Battery字段
        with self._buf_lock:
            new_output = self._bt_output_buf[start_pos:]

        for line in new_output.splitlines():
            if "Battery Percentage" in line:
                match = self._battery_re.search(line)
                if match:
                    try:
                        return int(match.group(1))
                    except ValueError:
                        return None
        return None



    # 以下是所有对外提供的连接、断连接口
    def disconnect(self):
        """主动断开耳机连接"""
        new_state = True
        with self._op_lock:
            if not self._check_connected():
                new_state = False
                return
            self._send_command(f"disconnect {self._target_mac}")
            new_state = self._check_connected()
        with self._state_lock:
            self._connected = new_state
            self._battery_level = None
            self._auto_reconnect = False


    def connect(self) -> bool:
        """主动连接耳机"""
        with self._op_lock:
            if self._check_connected():
                return True
            self._send_command(f"connect {self._target_mac}")
            new_state = self._check_connected()
        with self._state_lock:
            self._connected = new_state
            self._auto_reconnect = True
        return new_state


    def stop(self):
        """停止蓝牙管理器"""
        with self._state_lock:
            self._running = False
        #等待线程退出
        for thread in (self._reconn_thread, self._stdout_thread, self._stderr_thread):
            if thread.is_alive():
                thread.join(timeout = self._thread_join_timeout)
        self.disconnect()
        if self._proc is not None:
            self._proc.terminate()
            self._proc.wait()
            self._proc = None



    # 以下是所有对外提供的查询接口
    def get_target_mac(self) -> str:
        """获取目标蓝牙MAC地址"""
        return self._target_mac


    def is_connected(self, force_refresh: bool = False) -> bool:
        """查询当前耳机是否已连接"""
        if force_refresh:
            with self._op_lock:
                res = self._check_connected()
            with self._state_lock:
                self._connected = res
            return self._connected
        with self._state_lock:
            return self._connected


    def get_battery_level(self, force_refresh: bool = False) -> Optional[int]:
        """获取电池电量"""
        if force_refresh:
            with self._op_lock:
                res = self._query_battery_level()
            with self._state_lock:
                self._battery_level = res
            return self._battery_level
        with self._state_lock:
            return self._battery_level