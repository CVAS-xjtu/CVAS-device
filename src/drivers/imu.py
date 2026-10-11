# drivers/imu.py
import struct
import threading
import logging
import time
import serial


class IMU:
    """
    JY901P 九轴 IMU 模块

    """

    # JY901P 帧类型
    FRAME_ACC = 0x51
    FRAME_GYRO = 0x52
    FRAME_ANGLE = 0x53

    # 量程
    ACC_RANGE_G = 16.0          # ±16g
    GYRO_RANGE_DPS = 2000.0     # ±2000°/s
    ANGLE_RANGE_DEG = 180.0     # ±180°

    def __init__(
        self,
        port: str = "/dev/ttyUSB1",
        baudrate: int = 115200,
        logger_name: str = "IMU",
    ):
        self.port = port
        self.baudrate = baudrate
        self.logger = logging.getLogger(logger_name)

        self._serial = None
        self._running = False
        self._lock = threading.Lock()

        # 最新数据（原始单位：g, °/s, °）
        self._latest = {
            "roll": 0.0, "pitch": 0.0, "yaw": 0.0,
            "ax": 0.0, "ay": 0.0, "az": 0.0,
            "gx": 0.0, "gy": 0.0, "gz": 0.0,
        }

        # 帧同步标志：记录本轮哪些帧已更新
        self._frame_flags = {
            self.FRAME_ACC: False,
            self.FRAME_GYRO: False,
            self.FRAME_ANGLE: False,
        }

        # 统计
        self._rx_frames = 0
        self._bad_frames = 0
        self._last_rx_time = 0.0

    # ================================================================
    #   生命周期
    # ================================================================
    def open(self):
        """打开串口"""
        if self._running:
            return True
        try:
            self._serial = serial.Serial(
                self.port,
                self.baudrate,
                timeout=0.02,
                write_timeout=0.1,
            )
            self._serial.reset_input_buffer()
            self._running = True
            self.logger.info(
                f"IMU 已启动：{self.port} @ {self.baudrate}bps"
            )
            return True
        except Exception as e:
            self.logger.error(f"IMU 打开串口失败: {e}")
            return False

    def stop(self):
        """关闭串口"""
        if not self._running:
            return
        self._running = False
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception as e:
                self.logger.error(f"IMU 关闭串口出错: {e}")
        self.logger.info(
            f"IMU 已停止（收帧 {self._rx_frames}，坏帧 {self._bad_frames}）"
        )

    # ================================================================
    #   核心：读取 + 解析
    # ================================================================
    def read(self) -> dict:
        """
        读一帧并解析（非阻塞，带校验和验证）。
        返回最新数据字典（原始单位）。
        """
        if not self._running or not self._serial:
            self.logger.warning("IMU 未启动，请先 open()")
            return self.get_latest()

        try:
            # 1. 找帧头 0x55
            header = self._serial.read(1)
            if not header or header[0] != 0x55:
                return self.get_latest()

            # 2. 读类型字节
            type_byte = self._serial.read(1)
            if not type_byte:
                return self.get_latest()
            frame_type = type_byte[0]

            # 3. 读 8 字节数据 + 1 字节校验
            rest = self._serial.read(9)
            if len(rest) != 9:
                return self.get_latest()

            data_bytes = rest[:8]
            checksum = rest[8]

            # 4. 校验和：0x55 + type + 8 数据字节，取低 8 位
            calc_sum = (0x55 + frame_type + sum(data_bytes)) & 0xFF
            if calc_sum != checksum:
                self._bad_frames += 1
                self.logger.debug(
                    f"校验失败: type=0x{frame_type:02X} "
                    f"calc=0x{calc_sum:02X} recv=0x{checksum:02X}"
                )
                return self.get_latest()

            # 5. 按类型解析
            with self._lock:
                if frame_type == self.FRAME_ACC:
                    ax, ay, az = struct.unpack("<hhh", data_bytes[:6])
                    self._latest["ax"] = ax / 32768.0 * self.ACC_RANGE_G
                    self._latest["ay"] = ay / 32768.0 * self.ACC_RANGE_G
                    self._latest["az"] = az / 32768.0 * self.ACC_RANGE_G
                    self._frame_flags[self.FRAME_ACC] = True

                elif frame_type == self.FRAME_GYRO:
                    gx, gy, gz = struct.unpack("<hhh", data_bytes[:6])
                    self._latest["gx"] = gx / 32768.0 * self.GYRO_RANGE_DPS
                    self._latest["gy"] = gy / 32768.0 * self.GYRO_RANGE_DPS
                    self._latest["gz"] = gz / 32768.0 * self.GYRO_RANGE_DPS
                    self._frame_flags[self.FRAME_GYRO] = True

                elif frame_type == self.FRAME_ANGLE:
                    roll, pitch, yaw = struct.unpack("<hhh", data_bytes[:6])
                    self._latest["roll"] = roll / 32768.0 * self.ANGLE_RANGE_DEG
                    self._latest["pitch"] = pitch / 32768.0 * self.ANGLE_RANGE_DEG
                    self._latest["yaw"] = yaw / 32768.0 * self.ANGLE_RANGE_DEG
                    self._frame_flags[self.FRAME_ANGLE] = True

            self._rx_frames += 1
            self._last_rx_time = time.time()
            return self.get_latest()

        except Exception as e:
            self.logger.debug(f"IMU 解析出错: {e}")
            return self.get_latest()

    def read_until_complete(self, max_frames: int = 15) -> dict:
        """
        持续读取，直到本轮 0x51/0x52/0x53 三帧都更新过（或超过 max_frames）。
        保证拿到的加速度、角速度、姿态角来自同一轮采样。
        """
        with self._lock:
            for k in self._frame_flags:
                self._frame_flags[k] = False

        for _ in range(max_frames):
            self.read()
            with self._lock:
                if all(self._frame_flags.values()):
                    break

        return self.get_latest()

    # ================================================================
    #   查询接口（原始单位）
    # ================================================================
    def get_latest(self) -> dict:
        with self._lock:
            return dict(self._latest)

    def get_attitude(self) -> tuple:
        """返回 (roll, pitch, yaw)，单位：度"""
        d = self.get_latest()
        return d["roll"], d["pitch"], d["yaw"]

    def get_accel(self) -> tuple:
        """返回 (ax, ay, az)，单位：g"""
        d = self.get_latest()
        return d["ax"], d["ay"], d["az"]

    def get_gyro(self) -> tuple:
        """返回 (gx, gy, gz)，单位：度/秒"""
        d = self.get_latest()
        return d["gx"], d["gy"], d["gz"]

    # ================================================================
    #   调试 / 诊断
    # ================================================================
    def get_stats(self) -> dict:
        """返回接收统计"""
        return {
            "rx_frames": self._rx_frames,
            "bad_frames": self._bad_frames,
            "last_rx_time": self._last_rx_time,
            "running": self._running,
        }

    def reset_stats(self):
        self._rx_frames = 0
        self._bad_frames = 0