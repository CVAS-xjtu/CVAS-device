import time
import threading
from abc import ABC, abstractmethod

from drivers import gpio as GPIO
from .env_check import is_sim_env, SIM_LOG

class BaseGpioInput(ABC):

    @abstractmethod
    def read(self) -> bool:
        """读取原始电平（True=高电平，False=低电平）"""
        pass

    @abstractmethod
    def is_pressed(self) -> bool:
        """返回消抖后的稳定状态（True=按下，False=释放）"""
        pass

class ButtonModule:
   
    def __init__(self, cfg: dict):
       
        self.pin = cfg.get("pin",17)
        self.pull_up = cfg.get("pull_up",True)
        self.debounce_ms = cfg.get("debounce_ms", 50)
        self.poll_interval = cfg.get("poll_interval_ms",10) / 1000.0

        self._state_lock = threading.Lock()
        self._op_lock = threading.Lock()

        self._running = False
        self._stable_state = False
        self._last_raw_state = False
        self._last_change_time = 0.0
        
        
         # ---------------- 生命周期 ----------------
    def start(self):
        with self._op_lock:
            if hasattr(GPIO, "PUD_UP") and hasattr(GPIO, "PUD_DOWN"):
                pull = GPIO.PUD_UP if self.pull_up else GPIO.PUD_DOWN
                GPIO.setup(self.pin, GPIO.IN, pull_up_down=pull)
            else:
                GPIO.setup(self.pin, GPIO.IN)
            raw = GPIO.input(self.pin) == GPIO.HIGH

        with self._state_lock:
            self._last_raw_state = raw
            self._stable_state = (not raw) if self.pull_up else raw
            self._last_change_time = time.time()
            self._running = True

    def shutdown(self):
        with self._state_lock:
            self._running = False

    def cleanup(self):
        with self._op_lock:
            GPIO.cleanup(self.pin)

    # ---------------- 进程循环 ----------------
    def button_loop(self):
        while True:
            with self._state_lock:
                if not self._running:
                    break
            self._poll_step()
            time.sleep(self.poll_interval)

    # ---------------- 状态查询 ----------------
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    def is_pressed(self) -> bool:
        with self._state_lock:
            return self._stable_state

    # ---------------- 业务操作 ----------------
    def read(self) -> bool:
        with self._op_lock:
            return GPIO.input(self.pin) == GPIO.HIGH

    def sim_press(self):
        if not is_sim_env() or not hasattr(GPIO, "_pin_state"):
            return
        level = GPIO.LOW if self.pull_up else GPIO.HIGH
        GPIO._pin_state[self.pin] = level
        if SIM_LOG:
            print(f"[模拟] 按键按下 (pin {self.pin}, 电平 {level})")

    def sim_release(self):
        if not is_sim_env() or not hasattr(GPIO, "_pin_state"):
            return
        level = GPIO.HIGH if self.pull_up else GPIO.LOW
        GPIO._pin_state[self.pin] = level
        if SIM_LOG:
            print(f"[模拟] 按键释放 (pin {self.pin}, 电平 {level})")

    # ---------------- 内部 ----------------
    def _poll_step(self):
        with self._op_lock:
            raw = GPIO.input(self.pin) == GPIO.HIGH
        now = time.time()
        with self._state_lock:
            if raw != self._last_raw_state:
                self._last_raw_state = raw
                self._last_change_time = now
            else:
                if (now - self._last_change_time) * 1000 >= self.debounce_ms:
                    new_state = (not raw) if self.pull_up else raw
                    if self._stable_state != new_state:
                        self._stable_state = new_state