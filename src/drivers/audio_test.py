from asyncio import subprocess
from typing import Literal, Optional

class AudioManager:
    def __init__(self, audio_cfg: dict):
        self._audio_cfg = audio_cfg
        # 蓝牙耳机模式: "a2dp" 或 "hfp"
        self._mode: Optional[Literal["a2dp", "hfp"]] = None
        # 麦克风输入 source ID
        self._mic_source_id: Optional[int] = None
        # 扬声器输出 sink ID
        self._speaker_sink_id: Optional[int] = None

    def _query_pulse_mic_source_id(self) -> Optional[int]:
        """查询蓝牙耳机麦克风对应的PulseAudio声卡索引号"""
        try:
            output = subprocess.check_output(
                ["pactl", "list", "short", "sources"],
                text=True,
                stderr=subprocess.STDOUT
            )
            mac_underscore = self._target_mac.replace(":", "_").lower()
            for line in output.splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                src_id_str, name = parts[0], parts[1]
                if mac_underscore in name:
                    return int(src_id_str)
        except Exception:
            pass
        return None


    def _query_pulse_speaker_sink_id(self) -> Optional[int]:
        """查询蓝牙耳机扬声器对应的PulseAudio声卡索引号"""
        try:
            output = subprocess.check_output(
                ["pactl", "list", "short", "sinks"],
                text=True,
                stderr=subprocess.STDOUT
            )
            mac_underscore = self._target_mac.replace(":", "_").lower()
            for line in output.splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                sink_id_str, name = parts[0], parts[1]
                if mac_underscore in name:
                    return int(sink_id_str)
        except Exception:
            pass
        return None




    def _clear_audio_id_cache(self):
        """作废旧ID"""
        self._mic_source_id = None
        self._speaker_sink_id = None
    

    def mode_switch(self, mode: str):
        """切换耳机模式"""
        with self._op_lock:
            if mode == self._mode:
                return
            if mode == "hfp":
                self._send_command(f"connect {self._target_mac}")
            elif mode == "a2dp":
                self._send_command(f"connect {self._target_mac}")
            else:
                raise ValueError(f"Unsupported mode: {mode}")
            

    def get_mic_source_id(self, force_refresh: bool = False) -> Optional[int]:
        """获取麦克风ID, 带缓存"""
        with self._state_lock:
            if force_refresh or self._mic_source_id is None:
                self._mic_source_id = self._query_pulse_mic_source_id()
            return self._mic_source_id


    def get_speaker_sink_id(self, force_refresh: bool = False) -> Optional[int]:
        """获取扬声器ID, 带缓存"""
        with self._state_lock:
            if force_refresh or self._speaker_sink_id is None:
                self._speaker_sink_id = self._query_pulse_speaker_sink_id()
            return self._speaker_sink_id
