# nlu.py
import re
from typing import Dict, List, Tuple


class _NLUImpl:
    """基于正则的意图分类器"""

    def __init__(self, cfg: dict):
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
            self.add_intent(intent, patterns)
            
        # 编译正则
        self._compile_rules()

    def _compile_rules(self):
        self._compiled_rules: List[Tuple[str, List[re.Pattern]]] = []
        for intent, patterns in self.intent_rules:
            compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
            self._compiled_rules.append((intent, compiled))

    def add_intent(self, intent: str, patterns: List[str]):
        compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
        self._compiled_rules.append((intent, compiled))

    def open(self):
        pass

    def stop(self):
        pass

    def parse(self, text: str) -> Dict:
        if not text:
            return self._empty_result(text)
        
        # 1. 检测唤醒词（只要文本中出现任一个唤醒词，就视为唤醒）
        wakeup_hit = any(w in text for w in self.wakeup_words)
        
        # 2. 意图匹配（规则按优先级顺序执行）
        for intent, patterns in self._compiled_rules:
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    # 提取捕获组作为参数（可能有多个，但我们取第一个有意义的值）
                    argument = ""
                    for group in match.groups():
                        if group is not None:
                            argument = group.strip().rstrip("吧。？?！! ")
                            if argument:# 非空就作为参数
                                break
                    # 如果所有组都是空的，保留整个匹配
                    return {
                        "is_wakeup": wakeup_hit,
                        "intent": intent,
                        "argument": argument,
                        "raw_text": text
                    }

        # 3. 无匹配，返回 unknown
        return {
            "is_wakeup": wakeup_hit,
            "intent": "unknown",
            "argument": "",
            "raw_text": text
        }

    def _empty_result(self, text: str) -> Dict:
        return {
            "is_wakeup": False,
            "intent": "unknown",
            "argument": "",
            "raw_text": text
        }


# NLU 对外顶层类
class NLU:
    """自然语言理解器，对外提供 open/stop/parse"""

    def __init__(self, cfg: dict = None):
        if cfg is None:
            cfg = {}
        self.impl = _NLUImpl(cfg)

    def open(self):
        self.impl.open()

    def stop(self):
        self.impl.stop()

    def parse(self, text: str) -> Dict:
        return self.impl.parse(text)