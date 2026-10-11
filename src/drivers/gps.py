# drivers/gps.py
import threading
import logging
import serial
import pynmea2

class GPS:
    """
    GPS 定位模块
    """

    def __init__(self, port: str = "/dev/ttyUSB0", baudrate: int = 9600):
        
        self.port = port
        self.baudrate = baudrate
        self.logger = logging.getLogger("GPS")

        self._serial = None
        self._running = False
        self._lock = threading.Lock()
        self._latest = None

    # ---------- 启动 / 停止 ----------
    def open(self):
        if self._running:
            return
        try:
            self._serial = serial.Serial(
                self.port, self.baudrate, timeout=0.5
            )
            self._running = True
            self.logger.info(f"NEO-M9N 已启动：{self.port}")
        except Exception as e:
            self.logger.error(f"GPS 打开串口失败: {e}")

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception as e:
                self.logger.error(f"GPS 关闭串口出错: {e}")
        self.logger.info("GPS 已停止")

    # ---------- 核心：读一次 ----------
    def read(self) -> dict:
        """
        读一行 NMEA 报文，解析后返回。
        读不到 / 无效时返回 None。
        """
        if not self._running or not self._serial:
            self.logger.warning("GPS 未启动，请先 open()")
            return None

        try:
            raw = self._serial.readline()
            if not raw:
                return None

            line = raw.decode("ascii", errors="ignore").strip()
            if not line.startswith("$"):
                return None

            msg = pynmea2.parse(line)

            if msg.sentence_type == "GGA":
                is_valid = (msg.gps_qual != 0)
                data = {
                    "lat": float(msg.latitude) if is_valid else None,
                    "lon": float(msg.longitude) if is_valid else None,
                    "alt": float(msg.altitude) if is_valid else None,
                    "fix": is_valid,
                    "satellites": int(msg.num_sats) if msg.num_sats else 0,
                }
                with self._lock:
                    self._latest = data
                return data

            return None

        except Exception as e:
            self.logger.debug(f"GPS 解析报文出错: {e}")
            return None

    # ---------- 便捷方法 ----------
    def get_latest(self) -> dict:
        """读上次解析到的定位数据（不主动读串口）"""
        with self._lock:
            return self._latest

    def has_fix(self) -> bool:
        """有没有拿到有效定位"""
        d = self.get_latest()
        return bool(d and d.get("fix"))