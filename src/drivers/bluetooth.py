import subprocess
import threading
import time
import os
from typing import Optional
import re


class BluetoothManager:
    """
    蓝牙管理器（符合 interaction 节点设计规范）

    职责：管理蓝牙耳机连接、断连、状态查询。
    子进程（bluetoothctl）由模块内部在 start() 中创建，cleanup() 中销毁。
    类自身不创建线程；stdout/stderr 消费循环仍保留为线程方法（因 readline 阻塞），
    重连逻辑改为 check_and_reconnect() 供节点统一轮询。
    """

    def __init__(self, audio_cfg: dict):
        # ---------- 配置参数 ----------
        self._target_mac = audio_cfg.get("target_mac", "").strip().upper()
        self._poll_interval = audio_cfg.get("reconnect_poll_interval_sec", 3)
        self._cmd_timeout = audio_cfg.get("cmd_timeout_sec", 0.5)
        self._buf_max_len = audio_cfg.get("buf_max_len", 2000)
        self._buf_reserve_len = audio_cfg.get("buf_reserve_len", 1500)
        self._thread_sleep = audio_cfg.get("thread_sleep_sec", 0.5)

        if self._cmd_timeout <= 0 or self._cmd_timeout > 2:
            self._cmd_timeout = 0.5
        if self._buf_reserve_len >= self._buf_max_len:
            self._buf_reserve_len = self._buf_max_len - 500

        # ---------- 运行时状态 ----------
        self._running = True
        self._connected = False
        self._auto_reconnect = True
        self._battery_level: Optional[int] = None

        self._proc: Optional[subprocess.Popen] = None

        # 输出缓冲区与条件变量
        self._output_buf = ""
        self._buf_lock = threading.Lock()
        self._buf_cond = threading.Condition(self._buf_lock)

        self._battery_re = re.compile(r"\((\d+)\)")
        self._prompt_re = re.compile(r"\[.*\]#")

        # 重连间隔控制
        self._last_reconnect_time = 0.0

        # ---------- 锁 ----------
        self._state_lock = threading.Lock()
        self._op_lock = threading.Lock()

    # ================================================================
    #   生命周期方法
    # ================================================================

    def start(self):
        """初始化硬件资源：创建 bluetoothctl 子进程，发送 power on"""
        with self._op_lock:
            if self._proc is not None:
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
                env=env
            )
            self._send_command("power on")
            with self._state_lock:
                self._auto_reconnect = True

    def shutdown(self):
        """置 _running 为 False，结束 stdout/stderr 消费循环"""
        with self._state_lock:
            self._running = False

    def cleanup(self):
        """关闭硬件资源：断开连接，终止子进程"""
        try:
            self.disconnect()
        except Exception:
            pass
        with self._op_lock:
            if self._proc is not None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2)
                except Exception:
                    pass
                finally:
                    self._proc = None

    # ================================================================
    #   进程循环方法（由节点负责放入线程执行）
    # ================================================================

    def stdout_consumer_loop(self):
        """消费 bluetoothctl 标准输出（需独立线程，因为 readline 会阻塞）"""
        while self._running:
            if self._proc is None or self._proc.poll() is not None:
                time.sleep(self._thread_sleep)
                continue
            try:
                line = self._proc.stdout.readline()
                if not line:
                    time.sleep(self._thread_sleep)
                    continue
                with self._buf_cond:
                    self._output_buf += line
                    if len(self._output_buf) > self._buf_max_len:
                        self._output_buf = self._output_buf[-self._buf_reserve_len:]
                    self._buf_cond.notify_all()
            except Exception:
                time.sleep(self._thread_sleep)

    def stderr_consumer_loop(self):
        """消费 bluetoothctl 错误输出（需独立线程）"""
        while self._running:
            if self._proc is None or self._proc.poll() is not None:
                time.sleep(self._thread_sleep)
                continue
            try:
                line = self._proc.stderr.readline()
                if not line:
                    time.sleep(self._thread_sleep)
                    continue
                # 可记录错误日志
            except Exception:
                time.sleep(self._thread_sleep)

    # ---------- 以下为节点统一轮询的重连检查方法 ----------
    def check_and_reconnect(self):
        """
        检查蓝牙连接状态，若需要则尝试重连（由节点统一监控循环调用）。
        内部自动控制重连间隔，避免频繁操作。
        本方法不阻塞，无长时间持锁，适合高频调用。
        """
        # 1. 保活：若子进程已死，尝试重启
        with self._op_lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start_btctl()  # 内部发送 power on

        # 2. 检查是否到重连间隔
        now = time.time()
        if now - self._last_reconnect_time < self._poll_interval:
            return

        # 3. 检查是否启用自动重连（读取状态，持锁时间极短）
        with self._state_lock:
            auto = self._auto_reconnect
        if not auto:
            return

        # 4. 执行重连逻辑（持 _op_lock 保护命令序列）
        with self._op_lock:
            # 刷新连接状态
            real_conn = self._check_connected()
            if not real_conn:
                self._send_command(f"connect {self._target_mac}")
                real_conn = self._check_connected()
            # 更新状态
            with self._state_lock:
                self._connected = real_conn
                if real_conn:
                    self._auto_reconnect = True
            # 更新最后重连时间
            self._last_reconnect_time = time.time()

    # ================================================================
    #   状态查询方法
    # ================================================================

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

    # ================================================================
    #   业务操作方法
    # ================================================================

    def connect(self) -> bool:
        with self._op_lock:
            if self._check_connected():
                with self._state_lock:
                    self._connected = True
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
                return
            self._send_command(f"disconnect {self._target_mac}")
            new_state = self._check_connected()
        with self._state_lock:
            self._connected = new_state
            self._battery_level = None
            self._auto_reconnect = False

    # ================================================================
    #   内部辅助方法
    # ================================================================

    def _send_command(self, cmd: str) -> bool:
        if self._proc is None or self._proc.poll() is not None:
            return False
        try:
            self._proc.stdin.write(f"{cmd}\n")
            self._proc.stdin.flush()
            return True
        except Exception:
            return False

    def _start_btctl(self):
        """启动 bluetoothctl 进程（需在 _op_lock 内调用）"""
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
            env=env
        )
        self._send_command("power on")

    def _send_and_wait_for_prompt(self, cmd: str) -> bool:
        if not self._send_command(cmd):
            return False
        with self._buf_lock:
            start_pos = len(self._output_buf)
        deadline = time.time() + self._cmd_timeout
        found_prompt = False
        while time.time() < deadline:
            with self._buf_lock:
                new_part = self._output_buf[start_pos:]
            if self._prompt_re.search(new_part):
                found_prompt = True
                break
            with self._buf_cond:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._buf_cond.wait(timeout=remaining)
        return found_prompt

    def _check_connected(self) -> bool:
        cmd = f"info {self._target_mac}"
        if not self._send_and_wait_for_prompt(cmd):
            return False
        with self._buf_lock:
            output = self._output_buf
        return "Connected: yes" in output

    def _query_battery_level(self) -> Optional[int]:
        cmd = f"info {self._target_mac}"
        if not self._send_and_wait_for_prompt(cmd):
            return None
        with self._buf_lock:
            output = self._output_buf
        for line in output.splitlines():
            if "Battery Percentage" in line:
                match = self._battery_re.search(line)
                if match:
                    try:
                        return int(match.group(1))
                    except ValueError:
                        return None
        return None