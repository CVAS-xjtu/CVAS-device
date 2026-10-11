# interaction_node.py
import rclpy
from rclpy.node import Node
from std_msgs.msg import String, Bool
import threading
import time
import json
from typing import Optional


from interaction.kws import KWSModule
from interaction.ptt import PTTModule
from drivers.bluetooth import BluetoothModule
from drivers.audio import AudioModule
from drivers.button import ButtonModule
from audio.wakeup import EnergyVAD
from audio.preprocess import PreprocessModule
from audio.asr import ASRModule
from audio.nlu import NLUModule
from audio.tts import TTSModule


# ================================================================
#   模块级 Stub（避免每次创建内嵌类）
# ================================================================
class _KwsModelStub:
    """占位 KWS 模型：需实现 detect(frame_bytes) -> str|None"""
    def detect(self, frame_bytes: bytes) -> Optional[str]:
        # TODO: 替换为真实 KWS 模型
        return None


class _AsrModelStub:
    """占位 ASR 模型接口：需实现 online_asr / SenseVoiceSmall_ASR"""
    def online_asr(self, path):
        # TODO: 替换为真实在线 ASR
        return ("ok", "在线识别测试文本")

    def SenseVoiceSmall_ASR(self, path):
        # TODO: 替换为真实离线模型
        return ("ok", "离线识别测试文本")



#   InteractionNode

class InteractionNode(Node):
    """
    ROS2 interaction 节点：唯一组合根
      - 统一创建所有模块实例
      - 统一创建所有线程
      - 统一管理生命周期（start / shutdown / cleanup）
      - Bluetooth / Audio / Button 提供底层音频与按键能力
      - KWS 常开监听；检测到关键词后自己录音
      - ASR 从 KWS 队列取音频识别
      - PTT 按键触发录音 + 识别，结果并入 ASR 结果队列
      - NLU 解析 ASR 文本为意图
      - TTS 播报反馈；播放期间暂停 KWS，避免自触发
    """

    TTS_COOLDOWN_SEC = 0.5

    def __init__(self):
        super().__init__("interaction_node")

        # ---------------- 参数声明 ----------------
        # 音频
        self.declare_parameter("sample_rate", 16000)
        self.declare_parameter("mic_index", 0)
        self.declare_parameter("frame_duration_ms", 30)
        self.declare_parameter("max_silence_frames", 90)
        self.declare_parameter("vad_threshold", 500)
        self.declare_parameter("record_timeout", 8.0)

        # 蓝牙
        self.declare_parameter("bt_target_mac", "")

        # 按键
        self.declare_parameter("button_pin", 17)
        self.declare_parameter("button_pull_up", True)
        self.declare_parameter("button_debounce_ms", 50)
        self.declare_parameter("button_poll_interval_ms", 10)

        # PTT
        self.declare_parameter("ptt_record_file_base", "/tmp/record_voice")
        self.declare_parameter("ptt_a2dp_profile", "a2dp_sink")
        self.declare_parameter("ptt_hsp_profile", "headset_head_unit")
        self.declare_parameter("ptt_auto_switch_profile", True)
        self.declare_parameter("ptt_max_keep_files", 5)
        self.declare_parameter("ptt_poll_interval", 0.02)
        self.declare_parameter("ptt_record_max_duration", 600)

        # KWS
        self.declare_parameter("keywords", [])

        # ASR
        self.declare_parameter("enable_denoiser", False)
        self.declare_parameter("use_online_asr", False)

        # NLU
        self.declare_parameter("nlu_wakeup_words", [])
        # 注意：ROS2 参数不支持嵌套 dict，custom_rules 需以 JSON 字符串传入
        self.declare_parameter("nlu_custom_rules", "")

        # TTS
        self.declare_parameter("enable_tts", False)
        self.declare_parameter("tts_model", "")
        self.declare_parameter("tts_json", "")

        cfg = {
            "sample_rate": self.get_parameter("sample_rate").value,
            "mic_index": self.get_parameter("mic_index").value,
            "frame_duration_ms": self.get_parameter("frame_duration_ms").value,
            "max_silence_frames": self.get_parameter("max_silence_frames").value,
            "vad_threshold": self.get_parameter("vad_threshold").value,
            "record_timeout": self.get_parameter("record_timeout").value,

            "bt_target_mac": self.get_parameter("bt_target_mac").value,

            "button_pin": self.get_parameter("button_pin").value,
            "button_pull_up": self.get_parameter("button_pull_up").value,
            "button_debounce_ms": self.get_parameter("button_debounce_ms").value,
            "button_poll_interval_ms": self.get_parameter("button_poll_interval_ms").value,

            "ptt_record_file_base": self.get_parameter("ptt_record_file_base").value,
            "ptt_a2dp_profile": self.get_parameter("ptt_a2dp_profile").value,
            "ptt_hsp_profile": self.get_parameter("ptt_hsp_profile").value,
            "ptt_auto_switch_profile": self.get_parameter("ptt_auto_switch_profile").value,
            "ptt_max_keep_files": self.get_parameter("ptt_max_keep_files").value,
            "ptt_poll_interval": self.get_parameter("ptt_poll_interval").value,
            "ptt_record_max_duration": self.get_parameter("ptt_record_max_duration").value,

            "keywords": list(self.get_parameter("keywords").value),

            "enable_denoiser": self.get_parameter("enable_denoiser").value,
            "use_online_asr": self.get_parameter("use_online_asr").value,

            "nlu_wakeup_words": list(self.get_parameter("nlu_wakeup_words").value),
            "nlu_custom_rules": self.get_parameter("nlu_custom_rules").value,

            "enable_tts": self.get_parameter("enable_tts").value,
            "tts_model": self.get_parameter("tts_model").value,
            "tts_json": self.get_parameter("tts_json").value,
        }
        self.cfg = cfg

        # ---------------- ROS 发布器 ----------------
        self.text_pub = self.create_publisher(String, "/interaction/asr_text", 10)
        self.wakeup_pub = self.create_publisher(Bool, "/interaction/wakeup", 10)
        self.intent_pub = self.create_publisher(String, "/interaction/intent", 10)
        self.status_pub = self.create_publisher(String, "/interaction/status", 10)

        # ---------------- 节点状态 ----------------
        self._state_lock = threading.Lock()
        self._threads_lock = threading.Lock()
        self._running = False
        self._last_tts_end_time = 0.0

        # ---------------- 创建模块（依赖注入） ----------------
        self._create_modules(cfg)

        # 模块列表：start 顺序，cleanup 逆序
        self.modules = [
            self.bluetooth,
            self.audio,
            self.button,
            self.preprocess,
            self.kws,
            self.asr,
            self.nlu,
        ]
        if self.tts is not None:
            self.modules.append(self.tts)
        self.modules.append(self.ptt)

        # 循环注册：模块 + 循环方法
        self.loops = [
            (self.bluetooth, self.bluetooth.stdout_loop),
            (self.bluetooth, self.bluetooth.stderr_loop),
            (self.bluetooth, self.bluetooth.reconnect_loop),
            (self.audio, self.audio.audio_loop),
            (self.button, self.button.button_loop),
            (self.kws, self.kws.kws_loop),
            (self.asr, self.asr.asr_loop),
        ]
        if self.tts is not None:
            self.loops.append((self.tts, self.tts.tts_loop))
        self.loops.append((self.ptt, self.ptt.ptt_loop))

        self._threads = []

        # ---------------- ROS 定时器 ----------------
        self.timer = self.create_timer(0.1, self._poll_results)

        self.get_logger().info(
            f"Interaction node initialized "
            f"(TTS={'on' if self.tts else 'off'}, "
            f"modules={len(self.modules)}, loops={len(self.loops)})"
        )

    # ================================================================
    #   创建模块
    # ================================================================
    def _create_modules(self, cfg):
        # 1. 蓝牙
        self.bluetooth = BluetoothModule({
            "target_mac": cfg["bt_target_mac"],
        })

        # 2. 音频（依赖 bluetooth）
        self.audio = AudioModule(
            cfg={},
            bt_manager=self.bluetooth,
        )

        # 3. 按键
        self.button = ButtonModule({
            "pin": cfg["button_pin"],
            "pull_up": cfg["button_pull_up"],
            "debounce_ms": cfg["button_debounce_ms"],
            "poll_interval_ms": cfg["button_poll_interval_ms"],
        })

        # 4. VAD
        self.vad = EnergyVAD(threshold=cfg["vad_threshold"])

        # 5. 降噪
        self.preprocess = PreprocessModule({
            "enable": cfg["enable_denoiser"],
            "sample_rate": cfg["sample_rate"],
        })

        # 6. KWS（依赖 vad）
        self.kws = KWSModule(
            cfg={
                "sample_rate": cfg["sample_rate"],
                "mic_index": cfg["mic_index"],
                "frame_duration_ms": cfg["frame_duration_ms"],
                "max_silence_frames": cfg["max_silence_frames"],
                "keywords": cfg["keywords"],
                "kws_model": _KwsModelStub(),
                "audio_path": "/tmp/interaction_kws_utterance.wav",
                "record_timeout": cfg["record_timeout"],
            },
            vad=self.vad,
        )

        # 7. ASR（依赖 kws + preprocess）
        self.asr = ASRModule(
            cfg={"use_online_asr": cfg["use_online_asr"]},
            model_interface=_AsrModelStub(),
            kws=self.kws,
            preprocess=self.preprocess,
        )

        # 8. NLU
        self.nlu = NLUModule({
            "wakeup_words": cfg["nlu_wakeup_words"],
            "custom_rules": self._parse_custom_rules(cfg["nlu_custom_rules"]),
        })

        # 9. TTS（可选，播放通道走 AudioModule）
        self.tts = None
        if cfg["enable_tts"] and cfg["tts_model"] and cfg["tts_json"]:
            self.tts = TTSModule(
                cfg={
                    "zh_tts_model": cfg["tts_model"],
                    "zh_tts_json": cfg["tts_json"],
                },
                logger=self.get_logger(),
                audio_manager=self.audio,
            )

        # 10. PTT（依赖 asr + audio + button）
        self.ptt = PTTModule(
            cfg={
                "record_file_base": cfg["ptt_record_file_base"],
                "a2dp_profile": cfg["ptt_a2dp_profile"],
                "hsp_profile": cfg["ptt_hsp_profile"],
                "auto_switch_profile": cfg["ptt_auto_switch_profile"],
                "max_keep_files": cfg["ptt_max_keep_files"],
                "poll_interval": cfg["ptt_poll_interval"],
                "record_max_duration": cfg["ptt_record_max_duration"],
            },
            asr=self.asr,
            audio=self.audio,
            button=self.button,
        )

    def _parse_custom_rules(self, raw) -> dict:
        """ROS2 参数不能是嵌套 dict，用 JSON 字符串传"""
        if not raw:
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except Exception:
            return {}

    # ================================================================
    #   生命周期
    # ================================================================
    def start(self):
        # 1. 启动所有模块（记录失败的模块）
        failed_modules = set()
        for m in self.modules:
            try:
                m.start()
            except Exception as e:
                self.get_logger().error(
                    f"Module {m.__class__.__name__} start failed: {e}"
                )
                failed_modules.add(m)

        # 2. 只对启动成功的模块创建 loop 线程
        for module, loop_func in self.loops:
            if module in failed_modules:
                continue
            t = threading.Thread(
                target=loop_func,
                name=f"{module.__class__.__name__}_{loop_func.__name__}",
                daemon=True,
            )
            t.start()
            with self._threads_lock:
                self._threads.append(t)

        # 3. 打开麦克风，KWS 常开监听
        try:
            self.kws.activate()
        except Exception as e:
            self.get_logger().error(f"KWS activate failed: {e}")
        if hasattr(self.kws, "is_active") and not self.kws.is_active():
            self.get_logger().error("KWS mic activation failed")

        with self._state_lock:
            self._running = True

        self._publish_status({"event": "started"})
        self.get_logger().info("Interaction node started")

    def shutdown(self):
        with self._state_lock:
            self._running = False

        # 先关闭麦克风
        try:
            self.kws.deactivate()
        except Exception as e:
            self.get_logger().error(f"KWS deactivate failed: {e}")

        # 终止当前音频操作（让阻塞的 paplay/parecord 退出）
        try:
            self.audio.stop_audio()
        except Exception as e:
            self.get_logger().error(f"audio stop failed: {e}")

        # 通知所有模块停止循环
        for m in self.modules:
            try:
                m.shutdown()
            except Exception as e:
                self.get_logger().error(
                    f"Module {m.__class__.__name__} shutdown failed: {e}"
                )

        self._publish_status({"event": "shutdown"})
        self.get_logger().info("Interaction node shutdown")

    def cleanup(self):
        # 1. 逆序 cleanup：关硬件、关子进程（让阻塞 IO 退出）
        for m in reversed(self.modules):
            try:
                m.cleanup()
            except Exception as e:
                self.get_logger().error(
                    f"Module {m.__class__.__name__} cleanup failed: {e}"
                )

        # 2. 等所有线程退出
        with self._threads_lock:
            threads = list(self._threads)
            self._threads.clear()
        for t in threads:
            t.join(timeout=2.0)

        self.get_logger().info("Interaction node cleaned up")

    # ================================================================
    #   状态查询
    # ================================================================
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    # ================================================================
    #   ROS 定时器：轮询唤醒、ASR、TTS 状态
    # ================================================================
    def _poll_results(self):
        if not self.is_running():
            return
        for fn in (
            self._poll_wakeup_event,
            self._poll_asr_result,
            self._poll_tts_state,
        ):
            try:
                fn()
            except Exception as e:
                self.get_logger().error(f"{fn.__name__} failed: {e}")

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

        # 1. 发布 ASR 文本
        msg = String()
        msg.data = text
        self.text_pub.publish(msg)

        self._publish_status({
            "event": "asr",
            "text": text,
            "timestamp": time.time(),
        })
        self.get_logger().info(f"ASR: {text}")

        # 2. NLU 解析
        nlu_result = self.nlu.parse(text)
        self._publish_status({
            "event": "nlu",
            "result": nlu_result,
            "timestamp": time.time(),
        })
        self.get_logger().info(f"NLU: {nlu_result}")

        # 3. 分发到业务
        self._dispatch(nlu_result)

    def _poll_tts_state(self):
        """TTS 播放期间暂停 KWS，播放结束后冷却一段时间再恢复"""
        if self.tts is None:
            return

        busy = self.tts.is_busy()
        now = time.time()

        if busy:
            self._last_tts_end_time = now
            if not self.kws.is_paused():
                self.kws.pause()
                self.get_logger().debug("KWS paused (TTS busy)")
        else:
            # 冷却结束再恢复，避免拾到 TTS 尾部回声
            if self.kws.is_paused() and (now - self._last_tts_end_time) > self.TTS_COOLDOWN_SEC:
                self.kws.resume()
                self.get_logger().debug("KWS resumed (TTS idle)")

    # ================================================================
    #   意图分发
    # ================================================================
    def _dispatch(self, nlu_result: dict):
        intent = nlu_result.get("intent", "unknown")
        argument = nlu_result.get("argument", "")

        # 发布意图到 ROS
        intent_msg = String()
        intent_msg.data = json.dumps(nlu_result, ensure_ascii=False)
        self.intent_pub.publish(intent_msg)

        # stop 意图：真正关闭节点
        if intent == "stop":
            self.get_logger().info("Stop intent received, shutting down node")
            threading.Thread(target=self.shutdown, daemon=True).start()
            return

        # 其余意图：生成反馈文本
        if intent == "navigate":
            response = f"正在为您导航到{argument}" if argument else "请告诉我目的地"
        elif intent == "describe_scene":
            response = "正在为您描述场景"
        elif intent == "read_text":
            response = "正在为您朗读"
        else:
            response = "抱歉，我没有听懂"

        self._speak(response)

    def _speak(self, text: str):
        if self.tts is None:
            return
        # 主动暂停 KWS，消除轮询间隙（避免 TTS 起始音被拾到）
        self.kws.pause()
        self._last_tts_end_time = time.time()
        try:
            self.tts.speak(text, wait_finish=False)
        except Exception as e:
            self.get_logger().error(f"TTS speak failed: {e}")
            # 异常时立即恢复，避免 KWS 永久暂停
            self.kws.resume()

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