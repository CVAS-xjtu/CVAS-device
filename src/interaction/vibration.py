import time
import threading
from typing import Optional
from drivers import Motor


class _VibrateImpl:
    """内部震动控制逻辑：基于时间占比的单侧脉冲震动"""

    def __init__(self, motor_left: Motor, motor_right: Motor, cfg: dict):
        self.motor_left = motor_left
        self.motor_right = motor_right

        # 距离参数
        self.max_distance = cfg.get("max_distance", 2.0)    # 超过此距离不震动
        self.min_distance = cfg.get("min_distance", 0.2)    # 最近距离（此时占空比最大）

        # 脉冲周期与占空比
        self.period = cfg.get("period", 0.5)                # 脉冲周期（秒）
        self.max_duty = cfg.get("max_duty", 0.8)            # 最近时的震动占空比
        self.min_duty = cfg.get("min_duty", 0.2)            # 最远时的震动占空比
        self.tick = cfg.get("tick", 0.05)                   # 控制循环检查间隔（秒）

        # 内部状态
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._current_side: Optional[str] = None   # 'left' 或 'right' 或 None
        self._current_duty: float = 0.0            # 当前占空比 [0, 1]

    # ==================== 公开接口 ====================
    def open(self):
        if self._running:
            return
        self._running = True
        self._stop_all()
        self._thread = threading.Thread(target=self._vibration_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._stop_all()

    def update(self, direction: str, distance: float):
        """
        更新障碍物信息，触发单侧震动。
        :param direction: 'left', 'front_left' → 右侧马达震动（引导右转）
                          'right', 'front_right' → 左侧马达震动（引导左转）
                          其他值 → 不震动
        :param distance: 障碍物距离（米）
        """
        with self._lock:
            side, duty = self._compute_params(direction, distance)
            self._current_side = side
            self._current_duty = duty

    # ==================== 内部计算 ====================
    def _compute_params(self, direction: str, distance: float):
        """根据方向、距离返回 (震动侧, 占空比)"""
        # 距离过滤
        if distance > self.max_distance or distance <= 0:
            return None, 0.0

        # 方向映射（只保留左右，映射到对侧）
        side = None
        if direction in ('left', 'front_left'):
            side = 'right'       # 左侧障碍 → 震动右侧，提示向右避开
        elif direction in ('right', 'front_right'):
            side = 'left'        # 右侧障碍 → 震动左侧，提示向左避开
        else:
            # 其他方向，不震动
            return None, 0.0

        # 距离归一化 [0,1]，越近 norm 越大
        norm = (self.max_distance - distance) / (self.max_distance - self.min_distance)
        norm = max(0.0, min(1.0, norm))
        duty = self.min_duty + norm * (self.max_duty - self.min_duty)

        return side, duty

    def _stop_all(self):
        """安全关闭左右马达"""
        try:
            self.motor_left.set_power(0.0)
            self.motor_right.set_power(0.0)
        except Exception:
            pass

    # ==================== 脉冲震动循环 ====================
    def _vibration_loop(self):
        period_start = time.time()       # 当前脉冲周期的起始时间

        while self._running:
            with self._lock:
                side = self._current_side
                duty = self._current_duty

            # 无震动信号 → 关闭马达并重置周期起点
            if side is None or duty <= 0.0:
                self._stop_all()
                period_start = time.time()   # 防止长时间静默后节奏错乱
                time.sleep(self.tick)
                continue

            # 震动持续时间 = 占空比 × 周期
            vibration_duration = duty * self.period

            #当前时间在该周期内的偏移
            now = time.time()
            elapsed = now - period_start

            # 若已超出一个周期，将周期起点向前推进整数个周期
            if elapsed >= self.period:
                #跳转到当前周期的起点
                periods_passed = int(elapsed // self.period)
                period_start += periods_passed * self.period
                elapsed = now - period_start        #周期内已过去的时间

            # 当前时刻在震动阶段内 → 开启对应马达；否则关闭
            if elapsed < vibration_duration:
                self._apply_motors(side, on=True)   #开启对应马达
            else:
                self._stop_all()                    #关闭马达

            time.sleep(self.tick)                   #休眠一个tick，避免CPU占用过高

        self._stop_all()

    def _apply_motors(self, side: str, on: bool):
        """
        根据震动侧打开或关闭相应马达。
        """
        power = 1.0 if on else 0.0
        if side == 'right':
            # 右侧有障 → 震动左侧马达，关闭右侧
            self.motor_left.set_power(power)
            self.motor_right.set_power(0.0)
        elif side == 'left':
            # 左侧有障 → 震动右侧马达，关闭左侧
            self.motor_left.set_power(0.0)
            self.motor_right.set_power(power)
        else:
            self._stop_all()


# ==================== 顶层封装 ====================
class Vibrate:
    """
    震动反馈模块。
    - open() : 启动后台脉冲震动控制
    - stop() : 停止震动并关闭马达
    - update(direction, distance) : 更新障碍物，触发左右侧震动
    """

    def __init__(self, motor_left: Motor, motor_right: Motor, cfg: dict = None):
        if cfg is None:
            cfg = {}
        self.impl = _VibrateImpl(motor_left, motor_right, cfg)

    def open(self):
        self.impl.open()

    def stop(self):
        self.impl.stop()

    def update(self, direction: str, distance: float):
        self.impl.update(direction, distance)