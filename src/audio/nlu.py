# nlu.py
import re
import threading
from typing import Dict, List, Tuple


class NLUModule:

     def __init__(self, cfg: dict = None):
        if cfg is None:
            cfg = {}
        self.cfg = cfg

        self.wakeup_words: List[str] = cfg.get("wakeup_words", [
            "你好视觉助手", "视觉助手", "小视", "嗨小视",
            "hey视觉助手", "hey小视", "你好小视"
        ])

        # 意图规则列表，每项为 (intent_name, [regex_patterns])
        # 规则顺序决定匹配优先级
        self.intent_rules: List[Tuple[str, List[str]]] = [
            ("navigate", [
                r"(?:帮我|请|我要|我想|带我去|导航到|去|前往|怎么去)(.*?)(?:吧|。|$)",
                r"导航(?:去|到)(.*?)(?:吧|。|$)",
                r"到(.*?)(?:怎么走|的路线)",
                r"帮我找一下(.*)",
                r"带我去(.*)"
            ]),
            ("describe_scene", [
                r"(?:描述一下|介绍一下|看看|这是什么|那是什么|前面是什么|周围有什么|环境)(.*?)(?:吧|。|$)",
                r"(?:帮我|请)(?:看看|描述|介绍)(.*?)(?:吧|。|$)",
                r"(?:这是|那是)(什么)"
            ]),
            ("read_text", [
                r"(?:读一下|念一下|朗读|读|帮我读)(.*?)(?:吧|。|$)",
                r"(?:念|朗读)(.*)"
            ]),
            ("stop", [
                r"(?:停止|退出|关机|别说了|安静|停下|结束|不要了|取消)(.*?)"
            ]),
             # 可以继续添加其他意图，如物体识别、颜色识别等

        ]

        # 自定义规则（可运行时注入）
        custom_rules: Dict[str, List[str]] = cfg.get("custom_rules", {})
        for intent, patterns in custom_rules.items():
           self.intent_rules.append((intent, patterns))

        self._compiled_rules: List[Tuple[str, List[re.Pattern]]] = []
        for intent, patterns in self.intent_rules:
            self._compiled_rules.append(
                (intent, [re.compile(p, re.IGNORECASE) for p in patterns])
            )

        self._state_lock = threading.Lock()
        self._op_lock = threading.Lock()
        self._running = False
        

     # ---------------- 生命周期 ----------------
def start(self):
        with self._state_lock:
            self._running = True

def shutdown(self):
        with self._state_lock:
            self._running = False

def cleanup(self):
        pass

    # ---------------- 状态查询 ----------------
def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    # ---------------- 业务操作 ----------------
def parse(self, text: str) -> Dict:
        if not text:
            return self._empty_result(text)

        wakeup_hit = any(w in text for w in self.wakeup_words)

        for intent, patterns in self._compiled_rules:
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    argument = ""
                    for group in match.groups():
                        if group is not None:
                            argument = group.strip().rstrip("吧。？?！! ")
                            if argument:
                                break
                    return {
                        "is_wakeup": wakeup_hit,
                        "intent": intent,
                        "argument": argument,
                        "raw_text": text,
                    }

        return {
            "is_wakeup": wakeup_hit,
            "intent": "unknown",
            "argument": "",
            "raw_text": text,
        }

def _empty_result(self, text: str) -> Dict:
        return {
            "is_wakeup": False,
            "intent": "unknown",
            "argument": "",
            "raw_text": text,
        }