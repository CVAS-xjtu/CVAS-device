from .env_check import is_real_jetson, SIM_LOG

# ====================== 仿真实现类，1:1对齐Jetson.GPIO API ======================
class _SimGPIO:
    # 与官方库常量完全一致
    BOARD = "BOARD"
    BCM = "BCM"
    OUT = "OUT"
    IN = "IN"
    HIGH = 1
    LOW = 0

    # 缓存仿真引脚状态
    _pin_state = {}
    _pin_mode = {}

    @classmethod
    def setmode(cls, mode):
        if SIM_LOG:
            print(f"[仿真GPIO] 设置引脚编号模式: {mode}")

    @classmethod
    def setup(cls, pin, mode, initial=None):
        cls._pin_mode[pin] = mode
        init_val = initial if initial is not None else cls.LOW
        cls._pin_state[pin] = init_val
        if SIM_LOG:
            print(f"[仿真GPIO] 引脚{pin}初始化 | 模式:{mode} | 初始电平:{init_val}")

    @classmethod
    def output(cls, pin, value):
        cls._pin_state[pin] = value
        if SIM_LOG:
            print(f"[仿真GPIO] 引脚{pin}输出电平: {value}")

    @classmethod
    def input(cls, pin):
        val = cls._pin_state.get(pin, cls.LOW)
        if SIM_LOG:
            print(f"[仿真GPIO] 读取引脚{pin}电平: {val}")
        return val

    @classmethod
    def cleanup(cls, pin=None):
        if pin is not None:
            cls._pin_state.pop(pin, None)
            cls._pin_mode.pop(pin, None)
            if SIM_LOG:
                print(f"[仿真GPIO] 释放单个引脚 {pin}")
        else:
            cls._pin_state.clear()
            cls._pin_mode.clear()
            if SIM_LOG:
                print("[仿真GPIO] 释放全部GPIO引脚")

# ====================== 环境自动分支切换 ======================
if is_real_jetson():
    # 真机：加载官方原生库
    import Jetson.GPIO as GPIO # type: ignore
else:
    # WSL/PC仿真：替换仿真类
    GPIO = _SimGPIO
