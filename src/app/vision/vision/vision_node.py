import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from vision_msgs.msg import Detection2DArray, Detection2D, ObjectHypothesisWithPose # type: ignore
from cv_bridge import CvBridge
import zmq
import json

class VisionNode(Node):
    def __init__(self):
        super().__init__("vision_node")
        self.frame_id = "camera_link"
        self.detection_pub = self.create_publisher(Detection2DArray, "/vision/detections", 10)
        self.cv_bridge = CvBridge()

        # zmq客户端
        ctx = zmq.Context()
        self.sub_socket = ctx.socket(zmq.PULL)
        self.sub_socket.connect("ipc:///tmp/ds_ros_socket")

        # 创建定时器，循环接收数据
        self.timer_period = 0.01
        self.timer = self.create_timer(self.timer_period, self.recv_data_callback)
        self.get_logger().info("ROS vision node ready, waiting deep-stream data")

    def recv_data_callback(self):
        try:
            recv_json = self.sub_socket.recv_string(zmq.NOBLOCK)
        except zmq.Again:
            return
        data = json.loads(recv_json)
        ts_ns = data["timestamp_ns"]
        dets = data["detections"]
        stamp = rclpy.time.Time(nanoseconds=ts_ns).to_msg()
        det_array = Detection2DArray()
        det_array.header.frame_id = self.frame_id
        det_array.header.stamp = stamp
        for item in dets:
            one_det = Detection2D()
            one_det.bbox.center.x = item["cx"]
            one_det.bbox.center.y = item["cy"]
            one_det.bbox.size_x = item["w"]
            one_det.bbox.size_y = item["h"]
            hyp = ObjectHypothesisWithPose()
            hyp.hypothesis.class_id = item["class_name"]
            hyp.hypothesis.score = item["conf"]
            one_det.results.append(hyp)
            det_array.detections.append(one_det)
        self.detection_pub.publish(det_array)

def main(args=None):
    rclpy.init(args=args)
    node = VisionNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.shutdown()

if __name__ == "__main__":
    main()
