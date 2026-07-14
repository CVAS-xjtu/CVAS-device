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

class GPIOButton(BaseGpioInput):
   
    def __init__(
        self,
        pin: int,
        pull_up: bool = True,
        debounce_ms: int = 50,
        poll_interval_ms: int = 10,
    ):
       
        self._pin = pin
        self._pull_up = pull_up
        self._debounce_ms = debounce_ms
        self._poll_interval = poll_interval_ms / 1000.0

        if hasattr(GPIO, 'PUD_UP') and hasattr(GPIO, 'PUD_DOWN'):
            pull = GPIO.PUD_UP if pull_up else GPIO.PUD_DOWN
            GPIO.setup(pin, GPIO.IN, pull_up_down=pull)
        else:
            GPIO.setup(pin, GPIO.IN)  # 仿真环境忽略上下拉

        self._stable_state = False          # 消抖后的稳定状态（True=按下）
        self._last_raw_state = False        # 最近一次读取的原始电平（True=高）
        self._last_change_time = 0.0        # 上次电平变化的时间（秒）

        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def read(self) -> bool:
        """读取原始电平（True=高电平）"""
        return GPIO.input(self._pin) == GPIO.HIGH

    def is_pressed(self) -> bool:
        """返回消抖后的稳定状态（True=按下）"""
        with self._lock:
            return self._stable_state

    def _poll_loop(self):
        """后台轮询线程：持续读取原始电平并应用消抖"""
        # 初始化状态
        raw = self.read()
        self._last_raw_state = raw
        self._stable_state = self._raw_to_pressed(raw)
        self._last_change_time = time.time()

        while not self._stop_event.is_set():
            raw = self.read()
            now = time.time()

            if raw != self._last_raw_state:
                # 电平发生变化，记录变化时间
                self._last_raw_state = raw
                self._last_change_time = now
            else:
                # 电平稳定，若持续时间超过消抖时间则更新状态
                if (now - self._last_change_time) * 1000 >= self._debounce_ms:
                    new_state = self._raw_to_pressed(raw)
                    with self._lock:
                        if self._stable_state != new_state:
                            self._stable_state = new_state

            time.sleep(self._poll_interval)

    def _raw_to_pressed(self, raw: bool) -> bool:

        return (not raw) if self._pull_up else raw

    def stop(self):
        """停止后台线程并释放 GPIO 资源"""
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=0.5)
        GPIO.cleanup(self._pin)

# 默认使用 BCM 17 引脚，上拉模式（常见按键接 GND）
DEFAULT_BUTTON_PIN = 17
_button_instance = GPIOButton(
    pin=DEFAULT_BUTTON_PIN,
    pull_up=True,
    debounce_ms=50,
    poll_interval_ms=10,
)


def is_pressed() -> bool:
    """模块级函数，返回按钮稳定状态（True=按下）"""
    return _button_instance.is_pressed()


def get_button() -> GPIOButton:
    """获取全局按钮实例，便于高级控制（如调整参数、停止线程等）"""
    return _button_instance


# 仿真功能
def sim_press():
    if not is_sim_env():
        print("[模拟] 警告：sim_press 仅在仿真环境下可用，真机环境无效果。")
        return

    if not hasattr(GPIO, '_pin_state'):
        print("[模拟] 错误：当前 GPIO 模块不支持 _pin_state，无法模拟。")
        return

    pin = _button_instance._pin
    if _button_instance._pull_up:
        # 上拉，按下为低电平
        level = GPIO.LOW
    else:
        # 下拉，按下为高电平
        level = GPIO.HIGH

    GPIO._pin_state[pin] = level
    if SIM_LOG:
        print(f"[模拟] 按键按下 (pin {pin}, 电平 {level})")


def sim_release():
    
    if not is_sim_env():
        print("[模拟] 警告：sim_release 仅在仿真环境下可用，真机环境无效果。")
        return

    if not hasattr(GPIO, '_pin_state'):
        print("[模拟] 错误：当前 GPIO 模块不支持 _pin_state，无法模拟。")
        return

    pin = _button_instance._pin
    if _button_instance._pull_up:
        # 上拉，释放为高电平
        level = GPIO.HIGH
    else:
        # 下拉，释放为低电平
        level = GPIO.LOW

    GPIO._pin_state[pin] = level
    if SIM_LOG:
        print(f"[模拟] 按键释放 (pin {pin}, 电平 {level})")