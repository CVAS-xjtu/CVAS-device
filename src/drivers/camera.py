import atexit
import cv2
import numpy as np
import threading
import traitlets
import time
from jetcam.csi_camera import CSICamera  # type: ignore
from .env_check import is_real_jetson, SIM_LOG
from .time_sync import TimeSyncMatcher

# ------------------------------
# _SimCamera 模拟官方Camera类，仿真环境下使用本地视频流代替CSI相机
# 定义底层running&value原生属性
# ------------------------------
class _SimCamera(traitlets.HasTraits):

    # 模仿Camera类的核心属性和逻辑，保持接口一致，禁止外部调用非官方接口
    value = traitlets.Any()
    width = traitlets.Integer(default=224)
    height = traitlets.Integer(default=224)
    format = traitlets.Unicode(default='bgr8')
    running = traitlets.Bool(default=False)
    # 模仿CSICamera的额外属性，禁止外部调用非官方接口
    capture_device = traitlets.Integer(default_value=0)
    capture_fps = traitlets.Integer(default_value=30)
    capture_width = traitlets.Integer(default_value=640)
    capture_height = traitlets.Integer(default_value=480)

    def __init__(self, *args, **kwargs):
        super(_SimCamera, self).__init__(*args, **kwargs)
        # 视频路径映射表
        self.video_map = {
        0: "../sim_data/cam0.mp4",
        1: "../sim_data/cam1.mp4"
        }
        # 模仿Camera类的初始化逻辑，禁止外部调用非官方接口
        if self.format == 'bgr8':
            self.value = np.empty((self.height, self.width, 3), dtype=np.uint8)
        self._running = False
        # 模仿CSICamera的初始化逻辑，禁止外部调用非官方接口
        try:
            self.cap = cv2.VideoCapture(self._video_path())

            re, _ = self.cap.read()

            if not re:
                raise RuntimeError('Could not read image from camera.')
        except:
            raise RuntimeError(
                'Could not initialize camera.  Please see error trace.')

        atexit.register(self.cap.release)

    def _video_path(self):
        # 根据 capture_device 获取对应的视频路径，禁止外部调用非官方接口
        dev_id = self.capture_device
        if dev_id not in self.video_map:
            raise ValueError(f"设备号 {dev_id} 无对应视频配置，当前仅支持设备：{list(self.video_map.keys())}")
        return self.video_map[dev_id]

    def _read(self):
        # 模拟CSICamera从视频中读取一帧，禁止外部调用非官方接口
        re, image = self.cap.read()
        if not re:
            # 视频播放完毕，重置到第一帧
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            re, image = self.cap.read()
            if not re:
                raise RuntimeError('Could not read image from camera (even after reset)')
        return cv2.resize(image, (self.width, self.height))

    def _capture_frames(self):
        # 模拟Camera的帧捕获线程逻辑，禁止外部调用非官方接口
        while True:
            if not self._running:
                break
            self.value = self._read()
            time.sleep(1 / self.capture_fps)

    @traitlets.observe('running')
    def _on_running(self, change):
        # 模拟Camera的running属性观察者逻辑，禁止外部调用非官方接口
        if change['new'] and not change['old']:
            # transition from not running -> running
            self._running = True
            self.thread = threading.Thread(target=self._capture_frames)
            self.thread.start()
        elif change['old'] and not change['new']:
            # transition from running -> not running
            self._running = False
            self.thread.join()


# ------------------------------
# _Camera 单目适配层：封装open/stop/read三个标准方法
# 屏蔽底层running/value原生属性，上层禁止直接访问impl
# ------------------------------
class _Camera:
    def __init__(self, device_id: int, width: int, height: int, video_path=None):
        self.device_id = device_id
        self.width = width
        self.height = height

        self.impl = None

        if is_real_jetson():
            # 真机：原生JetCam CSICamera
            self.impl = CSICamera(
                capture_device=device_id,
                width=width,
                height=height
            )
            if SIM_LOG:
                print(f"[真机相机] 初始化CSI device {device_id}")
        else:
            # 仿真：对齐接口的_SimCamera
            self.impl = _SimCamera(
                video_path=video_path,
                width=width,
                height=height
            )

    def open(self):
        self.impl.running = True

    def stop(self):
        self.impl.running = False

    def read(self):
        return self.impl.value.copy()


# ------------------------------
# StereoCamera 对外顶层类：仅暴露 open() / stop() / read()
# 业务层只调用这三个方法，完全不碰running、value属性
# ------------------------------
class StereoCamera:
    def __init__(self, width=224, height=224, left_video=None, right_video=None, match_threshold_ms=8):
        self.left = _Camera(device_id=0, width=width, height=height, video_path=left_video)
        self.right = _Camera(device_id=1, width=width, height=height, video_path=right_video)
        self.sync_matcher = TimeSyncMatcher(max_cache_ms=100, match_threshold_ms=match_threshold_ms)

    def open(self):
        """启动左右相机采集线程"""
        self.left.open()
        self.right.open()

    def stop(self):
        """停止左右相机采集线程"""
        self.sync_matcher.clear()
        self.left.stop()
        self.right.stop()

    def read(self, timeout_ms=5000):
        """
        阻塞读取一对时间同步的左右帧, 替代直接访问value
        :param timeout_ms: 同步匹配超时时间，防止死循环
        :return: (left_frame, right_frame)
        :raises TimeoutError: 超时未匹配到同步帧
        """
        if not (self.left.impl.running and self.right.impl.running):
            raise RuntimeError("请先调用 .open() 启动相机")

        start_time = time.time()
        timeout_sec = timeout_ms / 1000.0
        self.sync_matcher.clear()

        while time.time() - start_time < timeout_sec:
            ts_l = time.time()
            frame_l = self.left.read()
            ts_r = time.time()
            frame_r = self.right.read()

            self.sync_matcher.add_data_a(frame_l, ts_l)
            self.sync_matcher.add_data_b(frame_r, ts_r)

            match_pair = self.sync_matcher.find_best_match()
            if match_pair is not None:
                return match_pair

            time.sleep(0.001)

        raise TimeoutError(f"同步帧读取超时 {timeout_ms}ms，未匹配到满足时间差的双目帧")