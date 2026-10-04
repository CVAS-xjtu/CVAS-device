# audio_node.py
import rclpy
from rclpy.node import Node
from std_msgs.msg import String, Bool
import threading
import time
import json
from typing import Optional

from interaction.kws import KWSModule
from audio.wakeup import EnergyVAD
from audio.preprocess import PreprocessModule
from audio.asr import ASRModule



class InteractionNode(Node):
    """
    ROS2 interaction 节点：唯一组合根
      - 统一创建所有模块实例
      - 统一创建所有线程
      - 统一管理生命周期（start / shutdown / cleanup）
      - KWS 常开监听：检测到关键词后自己录音，放入音频队列
      - ASR 从 KWS 队列取音频做识别，结果放入结果队列
      - 节点轮询唤醒事件 + ASR 结果，通过 ROS 发布
    """

    def __init__(self):
        super().__init__("interaction_node")

        # ---------------- 参数声明 ----------------
        self.declare_parameter("sample_rate", 16000)
        self.declare_parameter("mic_index", 0)
        self.declare_parameter("frame_duration_ms", 30)
        self.declare_parameter("max_silence_frames", 90)
        self.declare_parameter("keywords", [])           # 要检测的关键词列表
        self.declare_parameter("enable_denoiser", False)
        self.declare_parameter("use_online_asr", False)
        self.declare_parameter("vad_threshold", 500)
        self.declare_parameter("wakeup_timeout", 10.0)   # 唤醒后监听时长（秒）

        cfg = {
            "sample_rate": self.get_parameter("sample_rate").value,
            "mic_index": self.get_parameter("mic_index").value,
            "frame_duration_ms": self.get_parameter("frame_duration_ms").value,
            "max_silence_frames": self.get_parameter("max_silence_frames").value,
            "keywords": list(self.get_parameter("keywords").value),
            "enable_denoiser": self.get_parameter("enable_denoiser").value,
            "use_online_asr": self.get_parameter("use_online_asr").value,
            "vad_threshold": self.get_parameter("vad_threshold").value,
            "record_timeout": self.get_parameter("record_timeout").value,
        }
        self.cfg = cfg

        # ---------------- ROS 发布器 ----------------
        self.text_pub = self.create_publisher(String, "/audio/asr_text", 10)
        self.wakeup_pub = self.create_publisher(Bool, "/audio/wakeup", 10)
        self.status_pub = self.create_publisher(String, "/audio/status", 10)

         # ---------------- 节点状态 ----------------
        self._state_lock = threading.Lock()
        self._running = False

        # ---------------- 创建模块（依赖注入） ----------------
        self._create_modules(cfg)
        # 模块列表：start 顺序，cleanup 逆序
        self.modules = [
            self.preprocess,
            self.kws,
            self.asr,
        ]

        # 循环注册：模块 + 循环方法
        self.loops = [
            (self.kws, self.kws.kws_loop),
            (self.asr, self.asr.asr_loop),
        ]
        self._threads = []

        # ---------------- ROS 定时器 ----------------
        self.timer = self.create_timer(0.1, self._poll_results)

        self.get_logger().info("Interaction node initialized")


       # ================================================================
    #   创建模块
    # ================================================================
    def _create_modules(self, cfg):
        # VAD 工具（供 KWS 使用）
        self.vad = EnergyVAD(threshold=cfg["vad_threshold"])

        # 降噪
        self.preprocess = PreprocessModule({
            "enable": cfg["enable_denoiser"],
            "sample_rate": cfg["sample_rate"],
        })

        # KWS
        self.kws = KWSModule(
            cfg={
                "sample_rate": cfg["sample_rate"],
                "mic_index": cfg["mic_index"],
                "frame_duration_ms": cfg["frame_duration_ms"],
                "max_silence_frames": cfg["max_silence_frames"],
                "keywords": cfg["keywords"],
                "kws_model": self._create_kws_model(),
                "audio_path": "/tmp/interaction_kws_utterance.wav",
                "record_timeout": cfg["record_timeout"],
            },
            vad=self.vad,
        )

        # ASR
        self.asr = ASRModule(
            cfg={"use_online_asr": cfg["use_online_asr"]},
            model_interface=self._create_model_interface(),
            kws=self.kws,
            preprocess=self.preprocess,
        )

    def _create_kws_model(self):
        """返回 KWS 模型对象，需实现 detect(frame_bytes) -> str|None"""
        class _KWSModel:
            def detect(self, frame_bytes: bytes) -> Optional[str]:
                # TODO: 替换为真实 KWS 模型
                return None
        return _KWSModel()

    def _create_model_interface(self):
        """返回 ASR 模型接口，需实现 online_asr / SenseVoiceSmall_ASR"""
        class _ModelInterface:
            def online_asr(self, path):
                # TODO: 替换为真实在线 ASR
                return ("ok", "在线识别测试文本")

            def SenseVoiceSmall_ASR(self, path):
                # TODO: 替换为真实离线模型
                return ("ok", "离线识别测试文本")
        return _ModelInterface()

    # ================================================================
    #   生命周期
    # ================================================================
    def start(self):
        # 1. 启动所有模块（只初始化 PyAudio 实例，不打开麦克风）
        for m in self.modules:
            m.start()

        # 2. 统一创建线程，跑各模块的 xxx_loop
        for module, loop_func in self.loops:
            t = threading.Thread(
                target=loop_func,
                name=f"{module.__class__.__name__}_{loop_func.__name__}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

        # 3. 打开麦克风，KWS 常开监听
        self.kws.activate()

        with self._state_lock:
            self._running = True

        self._publish_status({"event": "started"})
        self.get_logger().info("Interaction node started (KWS listening)")

    def shutdown(self):
        with self._state_lock:
            self._running = False

        # 先关闭麦克风
        self.kws.deactivate()

        # 通知所有模块停止循环
        for m in self.modules:
            m.shutdown()

        self._publish_status({"event": "shutdown"})
        self.get_logger().info("Interaction node shutdown")

    def cleanup(self):
        # 1. 逆序 cleanup：关硬件、关子进程（让阻塞 IO 退出）
        for m in reversed(self.modules):
            m.cleanup()

        # 2. 等所有线程退出
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads.clear()

        self.get_logger().info("Interaction node cleaned up")

    # ================================================================
    #   状态查询
    # ================================================================
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    # ================================================================
    #   ROS 定时器：轮询唤醒事件与 ASR 结果
    # ================================================================
    def _poll_results(self):
        if not self.is_running():
            return
        self._poll_wakeup_event()
        self._poll_asr_result()

    def _poll_wakeup_event(self):
        if not self.kws.is_keyword_detected():
            return

        keyword = self.kws.get_detected_keyword()
        self.kws.clear_detection()

        msg = Bool()
        msg.data = True
        self.wakeup_pub.publish(msg)

        self._publish_status({
            "event": "wakeup",
            "keyword": keyword,
            "timestamp": time.time(),
        })
        self.get_logger().info(f"Wakeup detected: {keyword}")

    def _poll_asr_result(self):
        text = self.asr.get_result()
        if not text:
            return

        msg = String()
        msg.data = text
        self.text_pub.publish(msg)

        self._publish_status({
            "event": "asr",
            "text": text,
            "timestamp": time.time(),
        })
        self.get_logger().info(f"ASR: {text}")

    # ================================================================
    #   状态发布辅助
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
    node = InteractionNode()
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
