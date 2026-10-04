# depth.py
import numpy as np
import cv2
import threading
from typing import Optional


class _DepthImpl:
    """深度计算内部实现"""

    def __init__(self, cfg: dict):
        # ---------- 相机参数/立体标定参数 ----------
        self.baseline = cfg.get("baseline", 0.12)           #基线长度（米），12cm
        self.focal_length = cfg.get("focal_length", 600)    #焦距（像素），需标定获得
        self.depth_scale = cfg.get("depth_scale", 1.0)      #深度缩放系数

        # ---------- SGBM 参数 ----------
        self.min_disparity = cfg.get("min_disparity", 0)
        self.num_disparities = cfg.get("num_disparities", 128)
        self.block_size = cfg.get("block_size", 11)                     #SAD窗口大小
        self.p1 = cfg.get("p1", 8 * 3 * self.block_size ** 2)
        self.p2 = cfg.get("p2", 32 * 3 * self.block_size ** 2)
        self.disp12_max_diff = cfg.get("disp12_max_diff", 1)
        self.uniqueness_ratio = cfg.get("uniqueness_ratio", 10)
        self.speckle_window_size = cfg.get("speckle_window_size", 100)
        self.speckle_range = cfg.get("speckle_range", 32)
        self.mode = cfg.get("mode", cv2.STEREO_SGBM_MODE_SGBM_3WAY)

        # WLS 滤波，提升视差图质量
        self.use_wls = cfg.get("use_wls", False)
        self.wls_lambda = cfg.get("wls_lambda", 8000.0)
        self.wls_sigma = cfg.get("wls_sigma", 1.5)

        # ---------- 状态锁与运行标志 ----------
        self._state_lock = threading.Lock()
        self._running = False
        self._stereo = None
        self._right_matcher = None
        self._wls_filter = None

    # ==================== 生命周期方法 ====================
    def start(self):
        """初始化立体匹配器（打开硬件逻辑在这里模拟）"""
        with self._state_lock:
            if self._running:
                return
            # 创建 SGBM 匹配器
            self._stereo = cv2.StereoSGBM_create(
                minDisparity=self.min_disparity,
                numDisparities=self.num_disparities,
                blockSize=self.block_size,
                P1=self.p1,
                P2=self.p2,
                disp12MaxDiff=self.disp12_max_diff,
                uniquenessRatio=self.uniqueness_ratio,
                speckleWindowSize=self.speckle_window_size,
                speckleRange=self.speckle_range,
                mode=self.mode
            )
            if self.use_wls:
                self._right_matcher = cv2.ximgproc.createRightMatcher(self._stereo)
                self._wls_filter = cv2.ximgproc.createDisparityWLSFilter(self._stereo)
                self._wls_filter.setLambda(self.wls_lambda)
                self._wls_filter.setSigmaColor(self.wls_sigma)
            self._running = True

    def shutdown(self):
        """将 _running 置否定，释放 SGBM 等对象"""
        with self._state_lock:
            self._running = False

    def cleanup(self):
        """释放资源（SGBM 等对象置 None）"""
        with self._state_lock:
            self._stereo = None
            self._right_matcher = None
            self._wls_filter = None

    # ==================== 状态查询方法 ====================
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    # ==================== 业务操作方法 ====================
    def compute(self, left: np.ndarray, right: np.ndarray) -> Optional[np.ndarray]:
        """
        从左右校正图像计算深度图（单位：米）。
        输入: left, right - 灰度或BGR图 (H,W)
        输出: 深度图 (H,W) float32，无效区域值为0
        """
        if not self._running:
            raise RuntimeError("DepthEstimator 未启动，请先调用 start()")

        if left is None or right is None:
            return None
        if left.shape != right.shape:
            raise ValueError("左右图像尺寸不一致")

        # 统一转为灰度
        if len(left.shape) == 3:
            left = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY)
        if len(right.shape) == 3:
            right = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)

        # 计算视差
        with self._state_lock:
            stereo = self._stereo
            use_wls = self.use_wls
            right_matcher = self._right_matcher
            wls_filter = self._wls_filter

        if stereo is None:
            raise RuntimeError("立体匹配器未初始化")

        if use_wls and right_matcher is not None and wls_filter is not None:
            disp_left = stereo.compute(left, right).astype(np.int16)
            disp_right = right_matcher.compute(right, left).astype(np.int16)
            disp = wls_filter.filter(disp_left, left, None, disp_right)
        else:
            disp = stereo.compute(left, right).astype(np.float32) / 16.0

        # 过滤无效视差
        disp[disp <= 0] = 0.0

        # 视差转深度
        depth = np.where(
            disp > 0,
            (self.baseline * self.focal_length) / (disp + 1e-6),
            0.0
        )
        depth *= self.depth_scale
        return depth.astype(np.float32)


class DepthEstimator:
    """
    双目深度估计模块。
    """

    def __init__(self, cfg: dict = None):
        if cfg is None:
            cfg = {}
        self._impl = _DepthImpl(cfg)

    def start(self):
        self._impl.start()

    def shutdown(self):
        self._impl.shutdown()

    def cleanup(self):
        self._impl.cleanup()

    def is_running(self) -> bool:
        return self._impl.is_running()

    def compute(self, left: np.ndarray, right: np.ndarray) -> Optional[np.ndarray]:
        return self._impl.compute(left, right)