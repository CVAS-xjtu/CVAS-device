import os
import platform

os.environ["JETSON_MODEL_NAME"] = "JETSON_ORIN_NANO"

def is_real_jetson() -> bool:
    """判断是否为真实Jetson硬件"""
    if platform.system() != "Linux":
        return False
    if not os.path.exists("/proc/device-tree/model"):
        return False
    with open("/proc/device-tree/model", "r", encoding="utf-8") as f:
        board_info = f.read().lower()
    return "orin" in board_info

def is_sim_env() -> bool:
    return not is_real_jetson()

# 仿真日志开关，launch可通过环境变量控制打印
SIM_LOG = os.getenv("SIM_DEBUG_LOG", "1") == "1"