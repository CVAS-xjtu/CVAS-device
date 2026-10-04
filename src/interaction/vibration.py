import time
import threading
from typing import Optional
from drivers import vibrator


class VibrationModule:

    def __init__(self, vibrator_left: vibrator, vibrator_right: vibrator, cfg: dict):
        self.cfg = cfg
        self.vibrator_left = vibrator_left
        self.vibrator_right = vibrator_right

        # 距离参数
        self.max_distance = cfg.get("max_distance", 2.0)    # 超过此距离不震动
        self.min_distance = cfg.get("min_distance", 0.2)    # 最近距离（此时占空比最大）

        # 脉冲周期与占空比
        self.period = cfg.get("period", 0.5)                # 脉冲周期（秒）
        self.max_duty = cfg.get("max_duty", 0.8)            # 最近时的震动占空比
        self.min_duty = cfg.get("min_duty", 0.2)            # 最远时的震动占空比
        self.tick = cfg.get("tick", 0.05)                   # 控制循环检查间隔（秒）

         # 锁
        self._state_lock = threading.Lock()
        self._op_lock = threading.Lock()

        # 状态
        self._running = False
        self._current_side: Optional[str] = None
        self._current_duty: float = 0.0

     # ==================== 生命周期 ====================
    def start(self):
        # 先确保电机全关
        self._stop_all_hw()
        with self._state_lock:
            self._running = True

    def shutdown(self):
        with self._state_lock:
            self._running = False

    def cleanup(self):
        # 关闭电机
        self._stop_all_hw()

    # ==================== 进程循环 ====================
    def vibration_loop(self):
        period_start = time.time()
        while True:
            with self._state_lock:
                if not self._running:
                    break
                side = self._current_side
                duty = self._current_duty

            # 无震动信号
            if side is None or duty <= 0.0:
                self._stop_all_hw()
                period_start = time.time()
                time.sleep(self.tick)
                continue

            vibration_duration = duty * self.period

            now = time.time()
            elapsed = now - period_start
            if elapsed >= self.period:
                periods_passed = int(elapsed // self.period)
                period_start += periods_passed * self.period
                elapsed = now - period_start

            if elapsed < vibration_duration:
                self._apply_motors_hw(side, on=True)
            else:
                self._stop_all_hw()

            time.sleep(self.tick)

        # 循环退出时兜底关闭
        self._stop_all_hw()

    # ==================== 状态查询 ====================
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    def is_vibrating(self) -> bool:
        with self._state_lock:
            return self._current_side is not None and self._current_duty > 0.0

    def get_current_side(self) -> Optional[str]:
        with self._state_lock:
            return self._current_side

    def get_current_duty(self) -> float:
        with self._state_lock:
            return self._current_duty

    # ==================== 业务操作 ====================
    def update(self, direction: str, distance: float):
        """更新障碍物信息，触发单侧震动"""
        side, duty = self._compute_params(direction, distance)
        with self._state_lock:
            self._current_side = side
            self._current_duty = duty

    def stop_vibration(self):
        """立即停止震动"""
        with self._state_lock:
            self._current_side = None
            self._current_duty = 0.0
        self._stop_all_hw()

    # ==================== 内部计算 ====================
    def _compute_params(self, direction: str, distance: float) -> Tuple[Optional[str], float]:
        if distance > self.max_distance or distance <= 0:
            return None, 0.0

        if direction in ("left", "front_left"):
            side = "right"
        elif direction in ("right", "front_right"):
            side = "left"
        else:
            return None, 0.0

        norm = (self.max_distance - distance) / (self.max_distance - self.min_distance)
        norm = max(0.0, min(1.0, norm))
        duty = self.min_duty + norm * (self.max_duty - self.min_duty)
        return side, duty

    def _stop_all_hw(self):
        """锁外调用：关闭左右马达"""
        try:
            self.vibrator_left.set_power(0.0)
            self.vibrator_right.set_power(0.0)
        except Exception:
            pass

    def _apply_vibrators_hw(self, side: str, on: bool):
        """锁外调用：根据震动侧开/关马达"""
        power = 1.0 if on else 0.0
        try:
            if side == "right":
                self.vibrator_left.set_power(power)
                self.vibrator_right.set_power(0.0)
            elif side == "left":
                self.vibrator_left.set_power(0.0)
                self.vibrator_right.set_power(power)
            else:
                self._stop_all_hw()
        except Exception:
            pass