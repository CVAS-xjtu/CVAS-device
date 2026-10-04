import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
import pyds # type: ignore
import numpy as np
import cv2
import zmq
import json
import sys
sys.path.append("../")

from vision.yolo import parse_detection_meta, filter_by_confidence
from vision.depth import StereoDepth
from vision.clip import ClipClassifier


class DSPipeline:
    def __init__(self):
        Gst.init(None)
        self.camera_device = 0
        self.model_path = '/opt/nvidia/deepstream/deepstream/sources/yolo26.engine'
        self.conf_thres = 0.5
        self.enable_depth = True
        self.enable_clip = False

        # zmq放到实例初始化里面
        self.zmq_ctx = zmq.Context()
        self.sender_socket = self.zmq_ctx.socket(zmq.PUSH)
        self.sender_socket.bind("ipc:///tmp/ds_ros_socket")

        self.depth_calculator = StereoDepth("config/stereo_calib.yaml") if self.enable_depth else None
        self.clip_classifier = ClipClassifier("ViT-B-32") if self.enable_clip else None

        self.pipeline = self._create_pipeline()
        self._register_probe()
        self.bus = self.pipeline.get_bus()

    def _create_pipeline(self) -> Gst.Pipeline:
        pipeline = Gst.Pipeline.new("ds-pipeline")

        # 左摄像头
        src_left = Gst.ElementFactory.make('nvarguscamerasrc', 'cam-left')
        src_left.set_property('sensor-id', 0)
        src_left.set_property('bufapi-version', True)
        caps_left = Gst.ElementFactory.make('capsfilter', 'caps-left')
        caps_left.set_property('caps', Gst.Caps.from_string(
            'video/x-raw(memory:NVMM), width=1280, height=720, format=NV12, framerate=15/1'
        ))
        # 右摄像头
        src_right = Gst.ElementFactory.make('nvarguscamerasrc', 'cam-right')
        src_right.set_property('sensor-id', 1)
        src_right.set_property('bufapi-version', True)
        caps_right = Gst.ElementFactory.make('capsfilter', 'caps-right')
        caps_right.set_property('caps', Gst.Caps.from_string(
            'video/x-raw(memory:NVMM), width=1280, height=720, format=NV12, framerate=15/1'
        ))

        mux = Gst.ElementFactory.make('nvstreammux', 'mux')
        mux.set_property('batch-size', 2)
        mux.set_property('width', 1280)
        mux.set_property('height', 720)
        mux.set_property('batched-push-timeout', 40000)  # 40ms 超时对齐
        mux.set_property('live-source', True)
        # 必须开启同步，保证左右帧严格对齐（硬件同步时）
        mux.set_property('sync-inputs', 1)               # 强制输入帧同步

        conv1 = Gst.ElementFactory.make('nvvidconv', 'conv1')
        conv1.set_property('nvbuf-memory-type',3)

        infer = Gst.ElementFactory.make('nvinfer','yolo-infer')
        infer.set_property('config-file-path','config/yolo_config.txt')
        infer.set_property('model-engine-file',self.model_path)
        infer.set_property('batch-size',1)
        infer.set_property('process-mode', 1)  # 1 = 只处理第 0 个 sink pad（左帧）

        tracker = Gst.ElementFactory.make("nvtracker", "tracker")
        tracker.set_property("tracker-width", 1280)
        tracker.set_property("tracker-height", 720)
        tracker.set_property("ll-lib-file", "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so")
        tracker.set_property("ll-config-file", "config/tracker_config.yml")  # 需准备配置文件
        tracker.set_property("enable-batch-process", True)  # 批处理加速

        osd = Gst.ElementFactory.make('nvosd','osd')
        osd.set_property("display-bbox", True)

        sink = Gst.ElementFactory.make("fakesink","sink")
        sink.set_property("sync",False)

        for elem in [src_left, src_right, caps_left, caps_right, conv1, infer, osd, sink]:
            pipeline.add(elem)

        src_left.link(caps_left)
        src_right.link(caps_right)
        sinkpad_left = mux.get_request_pad('sink_0')
        caps_left.get_static_pad('src').link(sinkpad_left)
        sinkpad_right = mux.get_request_pad('sink_1')
        caps_right.get_static_pad('src').link(sinkpad_right)
        mux.link(conv1)
        conv1.link(infer)
        infer.link(osd)
        osd.link(sink)
        self.infer_elem = infer
        return pipeline

    def _register_probe(self):
        pad = self.infer_elem.get_static_pad("src")
        pad.add_probe(Gst.PadProbeType.BUFFER, self._probe_callback, self)

    @staticmethod
    def _probe_callback(_pad, info, self_obj):
        buf = info.get_buffer()
        if not buf:
            return Gst.PadProbeReturn.OK
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buf))
        l_frame = batch_meta.frame_meta_list

        left_img = None
        right_img = None
        detections = []   # 只有左帧有检测结果

        while l_frame is not None:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            # pad_index 0 = 左帧，1 = 右帧
            if frame_meta.pad_index == 0:
                left_img = pyds.get_nvds_buf_surface(hash(buf), frame_meta.batch_id)
                # 左帧上有推理结果
                obj_list = pyds.nvds_get_object_meta_list(frame_meta)
                raw_dets = parse_detection_meta(obj_list)
                detections = filter_by_confidence(raw_dets, self_obj.conf_thres)
            else:
                right_img = pyds.get_nvds_buf_surface(hash(buf), frame_meta.batch_id)
            l_frame = l_frame.next

        # 深度计算（需要左右图都存在）
        if self_obj.enable_depth and left_img is not None and right_img is not None:
            left_bgr = cv2.cvtColor(left_img, cv2.COLOR_RGBA2BGR)
            right_bgr = cv2.cvtColor(right_img, cv2.COLOR_RGBA2BGR)
            depth_map = self_obj.depth_calculator.compute(left_bgr, right_bgr)
            # 将深度值附加到每个检测框（例如提取框中心点的深度）
            for det in detections:
                x1, y1, x2, y2 = det['bbox']  # 假设格式如此
                cx, cy = (x1+x2)//2, (y1+y2)//2
                det['depth_m'] = depth_map[cy, cx] if depth_map is not None else -1.0

        # CLIP 等后处理可以继续在 detections 上操作...
        send_data = {"timestamp_ns": frame_meta.ntp_timestamp, "detections": detections}
        self_obj.sender_socket.send_string(json.dumps(send_data))
        return Gst.PadProbeReturn.OK

    def run(self):
        self.pipeline.set_state(Gst.State.PLAYING)
        try:
            # 使用bus.poll阻塞等待消息，CPU占用极低
            while True:
                msg = self.bus.poll(Gst.MessageType.EOS | Gst.MessageType.ERROR, -1)
                if msg:
                    t = msg.type
                    if t == Gst.MessageType.ERROR:
                        err, debug = msg.parse_error()
                        print(f"GST Error:{err.message}, {debug}")
                        break
                    elif t == Gst.MessageType.EOS:
                        print("Reach end-of-stream")
                        break
        except KeyboardInterrupt:
            print("receive ctrl-c exit")
        finally:
            self.pipeline.set_state(Gst.State.NULL)
            self.sender_socket.close()
            self.zmq_ctx.term()


if __name__ == "__main__":
    app = DSPipeline()
    app.run()
