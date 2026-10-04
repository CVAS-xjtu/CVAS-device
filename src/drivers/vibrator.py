# drivers/vibrator.py
import threading
import logging

from .gpio import BaseGPIO


class Vibrator:
    """
    振动马达控制类（连续功率可调）
    用法：
        vib = Vibrator(gpio_pin=18)
        vib.open()
        vib.set_power(1.0)   # 全功率震动
        vib.set_power(0.5)   # 半功率震动
        vib.set_power(0.0)   # 停止
        vib.stop()
    """

    def __init__(self, gpio_pin: int = 18):
        """
        gpio_pin : 接马达的 GPIO 引脚号
        """
        self.gpio_pin = gpio_pin
        self.logger = logging.getLogger("Vibrator")

        self._running = False          # 开关
        self._lock = threading.Lock()  # 防止并发修改

    # ---------- 启动 / 停止 ----------
    def open(self):
        """启动：初始化 GPIO"""
        if self._running:
            return
        self._running = True

        BaseGPIO.setmode(BaseGPIO.BOARD)
        BaseGPIO.setup(self.gpio_pin, BaseGPIO.OUT, initial=BaseGPIO.LOW)
        self.logger.info(f"振动马达已启动（GPIO {self.gpio_pin}）")

    def stop(self):
        """停止：关掉 GPIO"""
        if not self._running:
            return
        self._running = False

        try:
            BaseGPIO.output(self.gpio_pin, BaseGPIO.LOW)
            BaseGPIO.cleanup(self.gpio_pin)
        except Exception as e:
            self.logger.error(f"关闭振动马达出错: {e}")

        self.logger.info("振动马达已停止")

    # ---------- 核心：设置功率 ----------
    def set_power(self, power: float):
        """
        设置震动功率。
        :param power: 0.0 ~ 1.0 之间的浮点数
                      0.0 = 完全停止
                      1.0 = 全功率震动
        """
        if not self._running:
            self.logger.warning("振动器未启动，请先 open()")
            return

        # 限制在 [0, 1] 之间，防止传入越界值
        power = max(0.0, min(1.0, float(power)))

        with self._lock:
            if power > 0.0:
                BaseGPIO.output(self.gpio_pin, BaseGPIO.HIGH)
                self.logger.debug(f"震动 ON (power={power})")
            else:
                BaseGPIO.output(self.gpio_pin, BaseGPIO.LOW)
                self.logger.debug("震动 OFF")

    # ---------- 便捷方法（可选） ----------
    def on(self):
        """全功率打开"""
        self.set_power(1.0)

    def off(self):
        """关闭"""
        self.set_power(0.0)