import threading
import time
import cv2
import numpy as np
import traitlets
from jetcam.csi_camera import CSICamera # type: ignore

from .env_check import is_real_jetson, is_sim_env, SIM_LOG
from .time_sync import TimeSyncMatcher


# ------------------------------
# 私有仿真单目相机 _SimCamera
# 对齐 jetcam.Camera 接口规范
# ------------------------------
class _SimCamera(traitlets.HasTraits):
    width = traitlets.Integer()
    height = traitlets.Integer()
    value = traitlets.Any()

    def __init__(self, video_path, width, height, fps=30):
        super().__init__()
        self.width = width
        self.height = height
        self.fps = fps
        self.video_path = video_path
        self.cap = cv2.VideoCapture(video_path)
        self._running = False
        self._capture_thread = None
        # 初始化空白帧
        self.value = np.zeros((self.height, self.width, 3), dtype=np.uint8)

    def _read(self):
        ret, frame = self.cap.read()
        # 视频循环播放
        if not ret:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = self.cap.read()
        if ret:
            frame = cv2.resize(frame, (self.width, self.height))
        return frame

    def _capture_frames(self):
        while self._running:
            self.value = self._read()
            time.sleep(1 / self.fps)

    @property
    def running(self):
        return self._running

    @running.setter
    def running(self, val):
        if val:
            if not self._running:
                self._running = True
                self._capture_thread = threading.Thread(target=self._capture_frames, daemon=True)
                self._capture_thread.start()
                if SIM_LOG:
                    print(f"[仿真相机] 启动视频 {self.video_path}")
        else:
            self._running = False
            if self._capture_thread is not None:
                self._capture_thread.join()
                self._capture_thread = None
                if SIM_LOG:
                    print(f"[仿真相机] 停止视频 {self.video_path}")

    def read(self):
        return self._read()

    def stop(self):
        self.running = False
        if self.cap.isOpened():
            self.cap.release()


# ------------------------------
# 私有适配层 _Camera
# 根据环境自动切换 CSICamera / _SimCamera
# 透传所有底层接口，统一对外单目标准
# ------------------------------
class _Camera:
    def __init__(self, device_id: int, width: int, height: int, video_path=None):
        """
        :param device_id: jetson CSI设备号 0/1
        :param width: 分辨率宽
        :param height: 分辨率高
        :param video_path: 仿真视频路径，真机忽略
        """
        self.device_id = device_id
        self.width = width
        self.height = height
        self.video_path = video_path

        if is_real_jetson():
            # 真机使用原生CSI相机
            self.impl = CSICamera(
                capture_device=device_id,
                width=width,
                height=height
            )
            if SIM_LOG:
                print(f"[真机相机] 初始化CSI device {device_id}")
        else:
            # 仿真使用本地视频
            self.impl = _SimCamera(
                video_path=video_path,
                width=width,
                height=height
            )

    # 透传 value 属性
    @property
    def value(self):
        return self.impl.value

    # 透传 running 启停
    @property
    def running(self):
        return self.impl.running

    @running.setter
    def running(self, val):
        self.impl.running = val

    # 透传 read 单帧读取
    def read(self):
        return self.impl.read()

    # 透传 stop 释放资源
    def stop(self):
        self.impl.stop()


# ------------------------------
# 对外公开双目 StereoCamera
# 上层业务唯一调用入口，内置软件时间戳匹配
# ------------------------------


class StereoCamera:
    def __init__(self, width=224, height=224, left_video=None, right_video=None, match_threshold_ms=8):
        self.left = _Camera(device_id=0, width=width, height=height, video_path=left_video)
        self.right = _Camera(device_id=1, width=width, height=height, video_path=right_video)
        # 通用时间匹配器
        self.sync_matcher = TimeSyncMatcher(max_cache_ms=100, match_threshold_ms=match_threshold_ms)

    @property
    def running(self):
        return self.left.running and self.right.running

    @running.setter
    def running(self, enable: bool):
        self.left.running = enable
        self.right.running = enable

    @property
    def value(self):
        return self.left.value, self.right.value

    def read_sync(self):
        # 持续读取，直到匹配到符合时间差的左右帧
        while True:
            ts_l = time.time()
            frame_l = self.left.read()
            ts_r = time.time()
            frame_r = self.right.read()

            self.sync_matcher.add_data_a(frame_l, ts_l)
            self.sync_matcher.add_data_b(frame_r, ts_r)

            pair = self.sync_matcher.find_best_match()
            if pair is not None:
                return pair

    def stop(self):
        self.sync_matcher.clear()
        self.left.stop()
        self.right.stop()