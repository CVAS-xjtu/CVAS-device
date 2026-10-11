# bluetooth.py
import subprocess
import threading
import time
import os
import re
from typing import Optional


class BluetoothModule:
    """
    bluetoothctl 常驻子进程管理：
      - 不创建线程，由节点创建 stdout_loop / stderr_loop / reconnect_loop
      - 生命周期 start / shutdown / cleanup
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._audio_cfg = cfg

        # 蓝牙MAC地址
        self._target_mac = self._audio_cfg.get("target_mac", "").strip().upper()
        # 重连轮询间隔
        self._poll_interval = self._audio_cfg.get("reconnect_poll_interval_sec", 5)
        # 无数据时休眠间隔
        self._thread_sleep = self._audio_cfg.get("thread_sleep_sec", 0.5)
        # 内部循环休眠间隔
        self._inner_loop_sleep = self._audio_cfg.get("inner_loop_sleep_sec", 0.01)
        # 输出缓冲区
        self._buf_max_len = self._audio_cfg.get("buf_max_len", 2000)
        self._buf_reserve_len = self._audio_cfg.get("buf_reserve_len", 1500)
        # 命令超时
        self._cmd_timeout = self._audio_cfg.get("cmd_timeout_sec", 0.5)

        # 参数边界检查
        if self._thread_sleep <= 0:
            self._thread_sleep = 0.5
        if self._cmd_timeout <= 0 or self._cmd_timeout > 2:
            self._cmd_timeout = 0.5
        if self._buf_reserve_len >= self._buf_max_len:
            self._buf_reserve_len = self._buf_max_len - 500

        # 子进程
        self._proc: Optional[subprocess.Popen] = None

        # 状态
        self._running = False
        self._connected = False
        self._auto_reconnect = True
        self._battery_level: Optional[int] = None
        self._bt_output_buf = ""

        # 正则
        self._battery_re = re.compile(r"\((\d+)\)")
        self._prompt_re = re.compile(r"\[.*\]#")

        # 锁
        self._state_lock = threading.Lock()
        self._op_lock = threading.Lock()
        self._buf_lock = threading.Lock()

    # ==================== 生命周期 ====================
    def start(self):
        with self._state_lock:
            self._running = True
        self._start_btctl()

    def shutdown(self):
        with self._state_lock:
            self._running = False

    def cleanup(self):
        # 终止子进程，让阻塞的 readline 返回
        with self._op_lock:
            proc = self._proc
            self._proc = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        # 重置状态
        with self._state_lock:
            self._connected = False
            self._battery_level = None
            self._auto_reconnect = True
            self._bt_output_buf = ""

    # ==================== 进程循环 ====================
    def stdout_loop(self):
        while True:
            with self._state_lock:
                if not self._running:
                    break
            self._stdout_step()

    def stderr_loop(self):
        while True:
            with self._state_lock:
                if not self._running:
                    break
            self._stderr_step()

    def reconnect_loop(self):
        while True:
            with self._state_lock:
                if not self._running:
                    break
            self._reconnect_step()
            time.sleep(self._poll_interval)

    # ==================== 状态查询 ====================
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    def is_connected(self, force_refresh: bool = False) -> bool:
        if force_refresh:
            with self._op_lock:
                res = self._check_connected()
            with self._state_lock:
                self._connected = res
            return self._connected
        with self._state_lock:
            return self._connected

    def get_battery_level(self, force_refresh: bool = False) -> Optional[int]:
        if force_refresh:
            with self._op_lock:
                res = self._query_battery_level()
            with self._state_lock:
                self._battery_level = res
            return self._battery_level
        with self._state_lock:
            return self._battery_level

    def get_target_mac(self) -> str:
        return self._target_mac

    # ==================== 业务操作 ====================
    def connect(self) -> bool:
        with self._op_lock:
            if self._check_connected():
                with self._state_lock:
                    self._connected = True
                    self._auto_reconnect = True
                return True
            self._send_command(f"connect {self._target_mac}")
            new_state = self._check_connected()

        with self._state_lock:
            self._connected = new_state
            self._auto_reconnect = True
        return new_state

    def disconnect(self):
        with self._op_lock:
            if not self._check_connected():
                with self._state_lock:
                    self._connected = False
                    self._battery_level = None
                    self._auto_reconnect = False
                return
            self._send_command(f"disconnect {self._target_mac}")
            new_state = self._check_connected()

        with self._state_lock:
            self._connected = new_state
            self._battery_level = None
            self._auto_reconnect = False

    # ==================== 内部（子进程操作，调用前须持 _op_lock） ====================
    def _start_btctl(self):
        with self._op_lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            env = os.environ.copy()
            env["LANG"] = "C"
            env["LC_ALL"] = "C"
            self._proc = subprocess.Popen(
                ["bluetoothctl"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
            self._send_command("power on")

    def _send_command(self, cmd: str) -> bool:
        # 调用前须持 _op_lock
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(f"{cmd}\n")
            proc.stdin.flush()
            return True
        except Exception:
            return False

    def _stdout_step(self):
        with self._op_lock:
            proc = self._proc
        if proc is None or proc.stdout is None or proc.poll() is not None:
            time.sleep(self._thread_sleep)
            return
        try:
            line = proc.stdout.readline()
            if not line:
                time.sleep(self._thread_sleep)
                return
            with self._buf_lock:
                self._bt_output_buf += line
                if len(self._bt_output_buf) > self._buf_max_len:
                    self._bt_output_buf = self._bt_output_buf[-self._buf_reserve_len:]
        except Exception:
            time.sleep(self._thread_sleep)

    def _stderr_step(self):
        with self._op_lock:
            proc = self._proc
        if proc is None or proc.stderr is None or proc.poll() is not None:
            time.sleep(self._thread_sleep)
            return
        try:
            proc.stderr.readline()
        except Exception:
            time.sleep(self._thread_sleep)

    def _reconnect_step(self):
        with self._state_lock:
            auto_reconnect = self._auto_reconnect
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

    def _check_connected(self) -> bool:
        # 调用前须持 _op_lock
        if not self._send_command(f"info {self._target_mac}"):
            return False
        with self._buf_lock:
            start_pos = len(self._bt_output_buf)

        start_time = time.time()
        found_prompt = False
        while time.time() - start_time < self._cmd_timeout:
            with self._buf_lock:
                new_part = self._bt_output_buf[start_pos:]
            if self._prompt_re.search(new_part):
                found_prompt = True
                break
            time.sleep(self._inner_loop_sleep)

        if not found_prompt:
            return False
        with self._buf_lock:
            new_output = self._bt_output_buf[start_pos:]
        return "Connected: yes" in new_output

    def _query_battery_level(self) -> Optional[int]:
        # 调用前须持 _op_lock
        if not self._send_command(f"info {self._target_mac}"):
            return None
        with self._buf_lock:
            start_pos = len(self._bt_output_buf)

        start_time = time.time()
        found_prompt = False
        while time.time() - start_time < self._cmd_timeout:
            with self._buf_lock:
                new_part = self._bt_output_buf[start_pos:]
            if self._prompt_re.search(new_part):
                found_prompt = True
                break
            time.sleep(self._inner_loop_sleep)

        if not found_prompt:
            return None
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