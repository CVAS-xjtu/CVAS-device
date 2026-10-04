# audio_node.py
import rclpy
from rclpy.node import Node
from std_msgs.msg import String, Bool
import threading
import time
import json
from typing import Optional
from interaction.kws import KWSDetector
from audio.asr import SpeechRecognizer


class AudioNode(Node):
    """
    ROS 2 音频节点：
    - 唤醒阶段：KWSDetector 持续监听唤醒词
    - 识别阶段：SpeechRecognizer 进行 ASR 识别
    - 发布：识别文本 / 唤醒事件 / 状态信息
    节点管理所有实例与线程生命周期。
    """

    def __init__(self):
        super().__init__("audio_node")

        # ---------------- 参数声明 ----------------
        self.declare_parameter("sample_rate", 16000)
        self.declare_parameter("mic_index", 0)
        self.declare_parameter("frame_duration_ms", 30)
        self.declare_parameter("max_silence_frames", 90)
        self.declare_parameter("keywords", [])           # 要检测的关键词列表
        self.declare_parameter("enable_denoiser", False)
        self.declare_parameter("use_online_asr", False)
        self.declare_parameter("wakeup_timeout", 10.0)   # 唤醒后监听时长（秒）

        self.sample_rate = self.get_parameter("sample_rate").value
        self.mic_index = self.get_parameter("mic_index").value
        self.frame_duration_ms = self.get_parameter("frame_duration_ms").value
        self.max_silence_frames = self.get_parameter("max_silence_frames").value
        self.keywords = list(self.get_parameter("keywords").value)
        self.enable_denoiser = self.get_parameter("enable_denoiser").value
        self.use_online_asr = self.get_parameter("use_online_asr").value
        self.wakeup_timeout = self.get_parameter("wakeup_timeout").value

        # ---------------- ROS 发布器 ----------------
        self.text_pub = self.create_publisher(String, "/audio/asr_text", 10)
        self.wakeup_pub = self.create_publisher(Bool, "/audio/wakeup", 10)
        self.status_pub = self.create_publisher(String, "/audio/status", 10)

        # ---------------- 状态锁与运行标志 ----------------
        self._state_lock = threading.Lock()
        self._running = False
        self._kws_thread: Optional[threading.Thread] = None
        self._phase = "wakeup"           # "wakeup" 或 "recognition"
        self._wakeup_time = 0.0

        # ---------------- 创建子模块（节点统一创建实例） ----------------
        kws_cfg = {
            "sample_rate": self.sample_rate,
            "mic_index": self.mic_index,
            "frame_duration_ms": self.frame_duration_ms,
            "keywords": self.keywords,
            "kws_model": self._create_kws_model(),
        }
        self.kws = KWSDetector(kws_cfg)

        asr_cfg = {
            "sample_rate": self.sample_rate,
            "mic_index": self.mic_index,
            "frame_duration_ms": self.frame_duration_ms,
            "max_silence_frames": self.max_silence_frames,
            "use_online_asr": self.use_online_asr,
            "enable_denoiser": self.enable_denoiser,
            "user_speech_path": "/tmp/audio_node_speech.wav",
        }
        self.asr = SpeechRecognizer(asr_cfg, model_interface=self._create_model_interface())

        # ---------------- 定时器：轮询结果并发布 ----------------
        self.timer = self.create_timer(0.1, self.poll_results)
        self.get_logger().info("ROS audio node ready")

    # ================================================================
    #   工厂方法：创建 KWS 模型与 ASR 模型接口（用户替换为真实实现）
    # ================================================================
    def _create_kws_model(self):
        """返回一个 KWS 模型对象，需实现 detect(frame_bytes) -> str|None"""
        class _KWSModel:
            def detect(self, frame_bytes: bytes) -> Optional[str]:
                # TODO: 替换为真实关键词检测模型
                return None
        return _KWSModel()

    def _create_model_interface(self):
        """返回 ASR 模型接口，需实现 online_asr / SenseVoiceSmall_ASR"""
        class _ModelInterface:
            def online_asr(self, path):
                # TODO: 替换为真实在线 ASR
                return ('ok', '在线识别测试文本')

            def SenseVoiceSmall_ASR(self, path):
                # TODO: 替换为真实离线模型
                return ('ok', '离线识别测试文本')
        return _ModelInterface()

    # ================================================================
    #   生命周期方法
    # ================================================================
    def start(self):
        """启动节点：初始进入唤醒阶段"""
        with self._state_lock:
            if self._running:
                return
            self._running = True
        self._enter_wakeup_phase()
        self.get_logger().info("Audio node started, waiting wakeup word")

    def shutdown(self):
        """通知所有子模块与线程停止"""
        with self._state_lock:
            self._running = False
        self.kws.shutdown()
        self.asr.stop()

    def cleanup(self):
        """等待线程退出并释放硬件资源"""
        if self._kws_thread and self._kws_thread.is_alive():
            self._kws_thread.join(timeout=2.0)
        self.kws.cleanup()
        self.asr.stop()
        self.get_logger().info("Audio node cleaned up")

    # ================================================================
    #   阶段切换（内部方法）
    # ================================================================
    def _enter_wakeup_phase(self):
        """唤醒阶段：关闭 ASR，启动 KWS 线程"""
        # 先停掉 ASR
        self.asr.stop()

        # 再启动 KWS
        self.kws.start()
        self._kws_thread = threading.Thread(
            target=self.kws.kws_loop, daemon=True, name="kws_loop"
        )
        self._kws_thread.start()

        with self._state_lock:
            self._phase = "wakeup"
        self.get_logger().info("Enter wakeup phase")

    def _enter_recognition_phase(self):
        """识别阶段：停止 KWS，启动 ASR"""
        # 停止 KWS 并等待线程退出
        self.kws.shutdown()
        if self._kws_thread and self._kws_thread.is_alive():
            self._kws_thread.join(timeout=1.0)
        self.kws.cleanup()

        # 启动 ASR
        self.asr.open()

        with self._state_lock:
            self._phase = "recognition"
            self._wakeup_time = time.time()
        self.get_logger().info("Enter recognition phase")

    # ================================================================
    #   定时器回调：轮询 KWS 与 ASR 结果并发布
    # ================================================================
    def poll_results(self):
        if not self._running:
            return

        with self._state_lock:
            phase = self._phase

        # ---------------- 唤醒阶段 ----------------
        if phase == "wakeup":
            if self.kws.is_keyword_detected():
                keyword = self.kws.get_detected_keyword()
                self.kws.clear_detection()

                # 发布唤醒事件
                wakeup_msg = Bool()
                wakeup_msg.data = True
                self.wakeup_pub.publish(wakeup_msg)

                self._publish_status({
                    "event": "wakeup",
                    "keyword": keyword,
                    "timestamp": time.time(),
                })
                self.get_logger().info(f"Wakeup detected: {keyword}")

                # 进入识别阶段
                self._enter_recognition_phase()

        # ---------------- 识别阶段 ----------------
        elif phase == "recognition":
            text = self.asr.listen()
            if text:
                # 发布识别文本
                msg = String()
                msg.data = text
                self.text_pub.publish(msg)

                self._publish_status({
                    "event": "asr",
                    "text": text,
                    "timestamp": time.time(),
                })
                self.get_logger().info(f"ASR: {text}")

                # 识别到结果后回到唤醒阶段
                self._enter_wakeup_phase()
                return

            # 超时保护
            if time.time() - self._wakeup_time > self.wakeup_timeout:
                self.get_logger().info("Wakeup timeout, back to KWS")
                self._enter_wakeup_phase()

    # ================================================================
    #   状态发布辅助方法
    # ================================================================
    def _publish_status(self, status: dict):
        msg = String()
        msg.data = json.dumps(status, ensure_ascii=False)
        self.status_pub.publish(msg)


# ================================================================
#   主函数
# ================================================================
def main(args=None):
    rclpy.init(args=args)
    node = AudioNode()
    try:
        node.start()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.shutdown()
        node.cleanup()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()