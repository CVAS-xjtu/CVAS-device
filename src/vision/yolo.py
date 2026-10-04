# vision/yolo.py

import threading
import numpy as np
from typing import List, Dict, Any, Optional, Callable
import torch
from ultralytics import YOLO

class YOLODetector:
    # YOLO 目标检测封装类
    def __init__(self, model_path: str, conf_threshold: float = 0.5, device: str = "cuda"):
        # 依赖注入式初始化，只传参数，不做重IO操作
        self._model_path = model_path
        self._conf = conf_threshold
        self._device = device if torch.cuda.is_available() and device == "cuda" else "cpu"
        self._model: Optional[YOLO] = None
        self._running: bool = False
        self._state_lock = threading.Lock()
        self._op_lock = threading.Lock()

    def start(self) -> None:
        # 负责初始化硬件和加载模型
        with self._state_lock:
            if self._model is None:
                # 加载官方 YOLO 大模型
                self._model = YOLO(self._model_path)
                # 将模型移动到指定设备
                self._model.to(self._device)
                # 预热推理
                dummy = np.zeros((640, 640, 3), dtype=np.uint8)
                self._model.predict(dummy, conf=self._conf, verbose=False)
            self._running = True

    def shutdown(self) -> None:
        # 节点调用此方法停止所有内部循环逻辑
        with self._state_lock:
            self._running = False

    def cleanup(self) -> None:
        # 节点在销毁实例前调用
        with self._state_lock:
            self._running = False
            if self._model is not None:
                # 删除模型对象并清空 CUDA 缓存
                del self._model
                self._model = None
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

    def is_running(self) -> bool:
        # 返回循环是否处于激活状态
        with self._state_lock:
            return self._running

    def get_model_info(self) -> Dict[str, Any]:
        # 返回非布尔值的模型信息
        with self._state_lock:
            return {
                "model_path": self._model_path,
                "device": self._device,
                "conf_threshold": self._conf,
                "loaded": self._model is not None
            }

    def detect(self, image: np.ndarray) -> List[Dict[str, Any]]:
        # 核心业务方法
        if self._model is None:
            raise RuntimeError("Model not loaded. Please call start() first.")
        # 禁止持锁调用其他类接口
        with self._op_lock:
            # ultralytics 内部支持 numpy 输入
            results = self._model.predict(image, conf=self._conf, verbose=False, device=self._device)
        detections = []
        if results and len(results) > 0:
            boxes = results[0].boxes
            if boxes is not None and len(boxes) > 0:
                for box in boxes:
                    # 解析边界框（xyxy 格式）
                    x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                    conf = float(box.conf[0])
                    cls_id = int(box.cls[0])
                    detections.append({
                        "bbox": [x1, y1, x2, y2],
                        "class": self._model.names[cls_id],
                        "confidence": round(conf, 4),
                        "class_id": cls_id
                    })
        return detections

    def detect_loop(self, frame_provider: Callable[[], Optional[np.ndarray]], 
                    result_callback: Callable[[List[Dict]], None],
                    timeout: float = 0.05) -> None:
        # :param frame_provider: 获取图像帧的回调函数
        # :param result_callback: 推理结果回调
        # :param timeout: 取帧超时时间（秒），用于及时检查 _running 状态

        # 确保模型已启动
        if self._model is None:
            raise RuntimeError("Model not started. Call start() before running loop.")
        # 重置运行标志
        with self._state_lock:
            self._running = True
        # 循环退出条件由 _running 控制
        while self._running:
            try:
                # 获取最新帧
                frame = frame_provider()
                if frame is None:
                    # 若没有帧，短暂休眠避免死循环吃满 CPU
                    import time
                    time.sleep(timeout)
                    continue
                # 调用业务方法执行推理
                results = self.detect(frame)
                # 将结果通过回调传给上层节点
                if result_callback:
                    result_callback(results)
            except Exception as e:
                pass
        # 循环结束后清理
        with self._state_lock:
            self._running = False