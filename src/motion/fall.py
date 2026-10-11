'''JY901P IMU 跌倒检测
detect_fall 接收来自其他Python模块解析完成的JY901P采样数据，
仅返回布尔值：检测到跌倒时返回True，否则返回False。
FallDetector.ingest_frame 接收维特标准协议的原始11字节帧数据。
'''
from __future__ import annotations
import math
import struct
import time
from collections.abc import Mapping
from typing import Dict, Optional

# 加速度标度：16g量程，32768为满量程原始值
_ACCELERATION_SCALE_G = 16.0 / 32768.0
# 陀螺仪标度：2000°/s量程
_GYRO_SCALE_DEG_PER_SECOND = 2000.0 / 32768.0
# 角度输出标度：±180°
_ANGLE_SCALE_DEG = 180.0 / 32768.0


def decode_jy901p_frame(frame: bytes) -> Dict[str, float]:
    '''解析JY901P的加速度、陀螺仪或姿态角度帧。
    JY901P/维特串口协议使用11字节数据包：
    0x55, 帧类型, 8字节数据, 校验和。加速度单位g，
    陀螺仪单位°/s，姿态角单位度。
    '''
    if len(frame) != 11:
        raise ValueError('JY901P帧长度必须恰好11字节')
    if frame[0] != 0x55:
        raise ValueError('JY901P帧头必须为0x55')
    # 校验和：前10个字节求和，取低8位，与第11字节对比
    if (sum(frame[:10]) & 0xFF) != frame[10]:
        raise ValueError('JY901P帧校验和错误')
    kind = frame[1]
    # 解析后面8个字节，分成4个short类型（小端）
    x, y, z, _fourth_value = struct.unpack('<hhhh', frame[2:10])
    if kind == 0x51:
        # 0x51：加速度帧
        return {
            'accX': x * _ACCELERATION_SCALE_G,
            'accY': y * _ACCELERATION_SCALE_G,
            'accZ': z * _ACCELERATION_SCALE_G,
        }
    if kind == 0x52:
        # 0x52：陀螺仪帧
        return {
            'gyroX': x * _GYRO_SCALE_DEG_PER_SECOND,
            'gyroY': y * _GYRO_SCALE_DEG_PER_SECOND,
            'gyroZ': z * _GYRO_SCALE_DEG_PER_SECOND,
        }
    if kind == 0x53:
        # 0x53：姿态角度帧（roll,pitch,yaw）
        return {
            'angleX': x * _ANGLE_SCALE_DEG,
            'angleY': y * _ANGLE_SCALE_DEG,
            'angleZ': z * _ANGLE_SCALE_DEG,
        }
    raise ValueError(f'不支持的JY901P帧类型: {kind:#04x}')


def _first_finite_value(
    values: Mapping[str, float], *names: str
) -> Optional[float]:
    '''按顺序查找第一个可用的有限浮点数。支持多个别名键名。'''
    for name in names:
        value = values.get(name)
        if value is not None:
            converted = float(value)
            if math.isfinite(converted):
                return converted
    return None


def _angle_difference(angle_a: float, angle_b: float) -> float:
    '''计算两个角度之间最小差值（处理角度环绕0/360°）'''
    return abs((angle_a - angle_b + 180.0) % 360.0 - 180.0)


class FallDetector:
    '''佩戴在人体上的JY901P状态机跌倒检测器。
    跌倒判定需要同时满足瞬态异常 + 姿态变化两个条件：
    自由下落、撞击或者高速旋转触发候选跌倒窗口；
    在窗口时间内，传感器相对直立基准发生大倾角，判定跌倒。
    一旦标记跌倒，状态保持True，直到检测到稳定直立恢复。
    '''
    def __init__(
        self,
        *,
        free_fall_g: float = 0.55,
        impact_g: float = 2.5,
        rotation_deg_per_second: float = 250.0,
        fall_tilt_deg: float = 55.0,
        confirmation_window_s: float = 1.5,
        confirmation_hold_s: float = 0.2,
        recovery_tilt_deg: float = 30.0,
        recovery_hold_s: float = 2.0,
    ) -> None:
        # 自由下落阈值：合加速度小于该值判定失重
        self.free_fall_g = free_fall_g
        # 撞击阈值：合加速度大于该值判定冲击
        self.impact_g = impact_g
        # 角速度阈值：超过该值判定剧烈旋转
        self.rotation_deg_per_second = rotation_deg_per_second
        # 跌倒倾角阈值：相对直立基准倾斜超过该角度
        self.fall_tilt_deg = fall_tilt_deg
        # 候选确认窗口：异常事件触发后，在此时间内等待姿态倾斜确认
        self.confirmation_window_s = confirmation_window_s
        # 倾斜保持时间：倾斜状态必须持续至少该时长才确认跌倒
        self.confirmation_hold_s = confirmation_hold_s
        # 恢复直立倾角阈值：小于该角度认为回到直立姿态
        self.recovery_tilt_deg = recovery_tilt_deg
        # 恢复保持时间：直立稳定状态持续该时长才清除跌倒标记
        self.recovery_hold_s = recovery_hold_s

        self._fallen = False                # 当前跌倒状态标记
        self._candidate_until: Optional[float] = None  # 候选跌倒窗口截止时间戳
        self._tilted_since: Optional[float] = None     # 开始倾斜的时间戳
        self._upright_since: Optional[float] = None    # 恢复直立的时间戳
        self._reference_roll: Optional[float] = None   # 直立基准横滚角
        self._reference_pitch: Optional[float] = None  # 直立基准俯仰角
        self._latest_frame_values: Dict[str, float] = {} # 缓存最新一帧的各传感器数据

    @property
    def fallen(self) -> bool:
        '''获取当前跌倒状态'''
        return self._fallen

    def reset(self) -> None:
        '''清空所有状态，包括自动学习到的直立姿态基准'''
        self._fallen = False
        self._candidate_until = None
        self._tilted_since = None
        self._upright_since = None
        self._reference_roll = None
        self._reference_pitch = None
        self._latest_frame_values.clear()

    def calibrate_reference(self, roll: float, pitch: float) -> None:
        '''设置传感器安装对应的人体正常直立姿态基准角'''
        self._reference_roll = float(roll)
        self._reference_pitch = float(pitch)

    def ingest_frame(self, frame: bytes, timestamp: Optional[float] = None) -> bool:
        '''处理单路原始传感器帧，仅返回True/False跌倒状态。
        加速度、陀螺仪、角度分为独立帧发送；
        收到新加速度帧时，合并缓存的最新所有传感器数据进行处理。
        '''
        try:
            decoded = decode_jy901p_frame(frame)
        except ValueError:
            # 帧解析失败，维持原有跌倒状态
            return self._fallen
        # 更新缓存，保存本次解析出的数据
        self._latest_frame_values.update(decoded)
        # 不是加速度帧，只更新缓存，不做跌倒判断
        if 'accX' not in decoded:
            return self._fallen
        # 使用合并后的最新完整数据进入处理逻辑
        return self.process(self._latest_frame_values, timestamp)

    def process(
        self,
        imu_data: Mapping[str, float],
        timestamp: Optional[float] = None,
    ) -> bool:
        '''处理一组解析完成的IMU样本，仅返回布尔值。
        优先读取键名：accX, accY, accZ, gyroX, gyroY, gyroZ, angleX, angleY。
        同时支持别名 acc_x, gyro_x, roll, pitch。单位：g，°/s，度。
        '''
        # 使用传入时间戳，没有则用系统单调时钟
        now = time.monotonic() if timestamp is None else float(timestamp)
        # 读取三轴加速度，兼容多种键名
        ax = _first_finite_value(imu_data, 'accX', 'acc_x', 'ax')
        ay = _first_finite_value(imu_data, 'accY', 'acc_y', 'ay')
        az = _first_finite_value(imu_data, 'accZ', 'acc_z', 'az')
        if ax is None or ay is None or az is None:
            # 加速度数据缺失，维持当前跌倒状态
            return self._fallen
        # 读取陀螺仪，无数据则填充0
        gx = _first_finite_value(imu_data, 'gyroX', 'gyro_x', 'gx') or 0.0
        gy = _first_finite_value(imu_data, 'gyroY', 'gyro_y', 'gy') or 0.0
        gz = _first_finite_value(imu_data, 'gyroZ', 'gyro_z', 'gz') or 0.0
        # 读取姿态角，angleX=roll横滚，angleY=pitch俯仰
        roll = _first_finite_value(imu_data, 'angleX', 'angle_x', 'roll')
        pitch = _first_finite_value(imu_data, 'angleY', 'angle_y', 'pitch')

        # 计算加速度合模值
        acceleration_g = math.sqrt(ax * ax + ay * ay + az * az)
        # 计算陀螺仪合角速度
        rotation = math.sqrt(gx * gx + gy * gy + gz * gz)

        # 自动标定直立基准：静止、加速度接近1g时自动保存roll/pitch作为基准
        if (
            self._reference_roll is None
            and roll is not None
            and pitch is not None
            and 0.75 <= acceleration_g <= 1.25
            and rotation < 30.0
        ):
            self.calibrate_reference(roll, pitch)

        # 判断姿态基准是否可用：当前roll/pitch有效，且已经标定好直立基准
        orientation_known = (
            roll is not None
            and pitch is not None
            and self._reference_roll is not None
            and self._reference_pitch is not None
        )
        tilt = 0.0
        if orientation_known:
            # 取横滚、俯仰中与基准差值最大的那个作为倾斜角
            tilt = max(
                _angle_difference(roll, self._reference_roll),
                _angle_difference(pitch, self._reference_pitch),
            )

        # 异常事件判定：满足任意一项就打开跌倒候选窗口
        abnormal_event = (
            acceleration_g <= self.free_fall_g    # 失重（自由下落）
            or acceleration_g >= self.impact_g     # 撞击大加速度
            or rotation >= self.rotation_deg_per_second # 剧烈旋转
        )
        if abnormal_event:
            # 更新候选窗口截止时间
            self._candidate_until = now + self.confirmation_window_s

        # 候选窗口超时，清空候选标记与倾斜计时
        if self._candidate_until is not None and now > self._candidate_until:
            self._candidate_until = None
            self._tilted_since = None

        # 当前未标记跌倒：进入跌倒检测逻辑
        if not self._fallen:
            if (
                self._candidate_until is not None
                and orientation_known
                and tilt >= self.fall_tilt_deg
            ):
                # 在候选窗口内，姿态倾角超过跌倒阈值
                if self._tilted_since is None:
                    self._tilted_since = now
                elif now - self._tilted_since >= self.confirmation_hold_s:
                    # 倾斜状态持续足够时间 → 确认跌倒
                    self._fallen = True
                    self._candidate_until = None
                    self._upright_since = None
            else:
                # 不满足倾斜条件，重置倾斜计时
                self._tilted_since = None
            return self._fallen

        # === 已经处于跌倒状态，检测是否恢复直立 ===
        upright_and_still = (
            orientation_known
            and tilt <= self.recovery_tilt_deg
            and 0.75 <= acceleration_g <= 1.25
            and rotation < 50.0
        )
        if upright_and_still:
            # 姿态回到直立且静止
            if self._upright_since is None:
                self._upright_since = now
            elif now - self._upright_since >= self.recovery_hold_s:
                # 稳定直立保持足够时间，清除跌倒标记
                self._fallen = False
                self._upright_since = None
        else:
            # 不满足稳定直立，重置恢复计时
            self._upright_since = None
        return self._fallen


# 创建全局默认检测器实例
_default_detector = FallDetector()


def detect_fall(
    imu_data: Mapping[str, float], timestamp: Optional[float] = None
) -> bool:
    '''接收其他模块解析完成的单组IMU数据，返回跌倒布尔结果'''
    return _default_detector.process(imu_data, timestamp)


