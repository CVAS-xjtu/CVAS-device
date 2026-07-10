import atexit
import cv2
import numpy as np
import threading
import traitlets
from jetcam.csi_camera import CSICamera  # type: ignore
from .env_check import is_real_jetson, SIM_LOG

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

    def __init__(self, sim_video_map: dict, *args, **kwargs):
        super(_SimCamera, self).__init__(*args, **kwargs)
        # 视频路径映射表
        self._sim_video_map = sim_video_map
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
        if dev_id not in self._sim_video_map:
            raise ValueError(f"设备号 {dev_id} 无对应视频配置，当前仅支持设备：{list(self._sim_video_map.keys())}")
        return self._sim_video_map[dev_id]

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
            self.thread.join(timeout=1.0)


# ------------------------------
# _Camera 单目适配层：封装open/stop/read三个标准方法
# 屏蔽底层running/value原生属性，上层禁止直接访问impl
# ------------------------------
class _Camera:
    def __init__(self, cfg: dict, side: str, sim_video_map: dict):
        self.side = side
        cam_cfg = cfg.get(self.side, {})
        required_keys = ["device_id", "width", "height", "capture_fps", "capture_width", "capture_height"]
        missing_keys = [k for k in required_keys if k not in cam_cfg]
        if missing_keys:
            raise ValueError(f"[{self.side}] 相机配置缺失必填字段：{missing_keys}")

        self.sim_video_map = sim_video_map
        self.device_id = cam_cfg["device_id"]
        self.width = cam_cfg["width"]
        self.height = cam_cfg["height"]
        self.capture_fps = cam_cfg["capture_fps"]
        self.capture_width = cam_cfg["capture_width"]
        self.capture_height = cam_cfg["capture_height"]

        self.impl = None

        if is_real_jetson():
            # 真机：原生JetCam CSICamera
            self.impl = CSICamera(
                capture_device=self.device_id,
                width=self.width,
                height=self.height,
                capture_fps=self.capture_fps,
                capture_width=self.capture_width,
                capture_height=self.capture_height
            )
        else:
            # 仿真：对齐接口的_SimCamera
            self.impl = _SimCamera(
                sim_video_map=self.sim_video_map,
                capture_device=self.device_id,
                width=self.width,
                height=self.height,
                capture_fps=self.capture_fps,
                capture_width=self.capture_width,
                capture_height=self.capture_height
            )
            if SIM_LOG:
                video_path = self.sim_video_map[self.device_id]
                print(f"[仿真相机] {self.side} device:{self.device_id} | 分辨率:{self.width}x{self.height} | 视频文件:{video_path}")

    def open(self):
        if self.impl.running:
            return
        self.impl.running = True

    def stop(self):
        if not self.impl.running:
            return
        self.impl.running = False

    def read(self):
        if not self.impl.running:
            raise RuntimeError(f"{self.side}相机未启动, 请先调用open()")
        if self.impl.value is None or self.impl.value.size == 0:
            raise RuntimeError(f"{self.side}相机无有效图像帧")
        frame = self.impl.value.copy()
        return frame


# ------------------------------
# StereoCamera 对外顶层类：仅暴露 open() / stop() / read()
# 业务层只调用这三个方法，完全不碰running、value属性
# ------------------------------
class StereoCamera:
    def __init__(self, camera_cfg: dict):
        self.camera_cfg = camera_cfg
        sim_video_map = self.camera_cfg["sim_video_map"]
        self.left = _Camera(cfg=self.camera_cfg, side="left", sim_video_map=sim_video_map)
        self.right = _Camera(cfg=self.camera_cfg, side="right", sim_video_map=sim_video_map)

    def open(self):
        """启动左右相机采集线程"""
        self.left.open()
        self.right.open()

    def stop(self):
        """停止左右相机采集线程"""
        self.left.stop()
        self.right.stop()

    def read(self):
        """读取左右相机帧"""
        left_frame = self.left.read()
        right_frame = self.right.read()
        return left_frame, right_frame