# denoiser.py
import logging
import numpy as np
from typing import Optional
from drivers import is_real_jetson, SIM_LOG

# ------------------------------------------------------------
# 谱减法核心算法
# ------------------------------------------------------------
class SpectralSubtractionDenoiser:
    """基于幅度谱减法的语音降噪核心"""

    def __init__(self,
                 sample_rate: int = 16000,
                 frame_length_ms: float = 25.0,
                 frame_shift_ms: float = 10.0,
                 noise_frames: int = 10,
                 over_subtraction_factor: float = 2.0,
                 spectral_floor: float = 0.01):
        self.sample_rate = sample_rate
        self.frame_length = int(frame_length_ms * sample_rate / 1000)
        self.frame_shift = int(frame_shift_ms * sample_rate / 1000)
        self.fft_size = 1
        while self.fft_size < self.frame_length:
            self.fft_size <<= 1
        self.noise_frames = noise_frames
        self.alpha = over_subtraction_factor
        self.beta = spectral_floor
        self.window = np.hanning(self.frame_length)
        self.window_sum = np.sum(self.window ** 2)
        self.noise_psd: Optional[np.ndarray] = None
        self._noise_estimated = False

    def estimate_noise(self, noise_signal: np.ndarray):
        """从纯噪声片段（无语音）估计噪声功率谱 (PSD)"""
        if noise_signal.dtype != np.float32:
            noise_signal = noise_signal.astype(np.float32) / np.iinfo(noise_signal.dtype).max

        noise_psd = np.zeros(self.fft_size // 2 + 1, dtype=np.float32)
        num_frames = 0
        for start in range(0, len(noise_signal) - self.frame_length + 1, self.frame_shift):
            frame = noise_signal[start:start + self.frame_length] * self.window
            spectrum = np.fft.rfft(frame, n=self.fft_size)
            noise_psd += np.abs(spectrum) ** 2
            num_frames += 1
        if num_frames == 0:
            raise ValueError("噪声信号太短，无法估计噪声谱，请提供至少 0.5 秒的纯噪声。")
        self.noise_psd = noise_psd / num_frames
        self._noise_estimated = True

    def process_array(self, audio: np.ndarray) -> np.ndarray:
        """
        对整段音频进行降噪。
        输入: 1D numpy 数组,int16 或 float32。
        输出: 与输入相同 dtype、形状的降噪后音频。
        """
        if not self._noise_estimated:
            # 自动利用音频开头前 noise_frames 帧作为噪声估计（假设开头为静音）
            n_frames = min(self.noise_frames, len(audio) // self.frame_shift)
            if n_frames == 0:
                raise RuntimeError("音频太短，无法自动估计噪声，请先调用 estimate_noise()")
            noise_snippet = audio[:n_frames * self.frame_shift + self.frame_length]
            self.estimate_noise(noise_snippet)

        original_dtype = audio.dtype
        if audio.dtype != np.float32:
            audio_float = audio.astype(np.float32) / np.iinfo(audio.dtype).max
        else:
            audio_float = audio.copy()

        out = np.zeros_like(audio_float)
        window_sum_buffer = np.zeros_like(audio_float)

        for start in range(0, len(audio_float) - self.frame_length + 1, self.frame_shift):
            frame = audio_float[start:start + self.frame_length] * self.window
            spectrum = np.fft.rfft(frame, n=self.fft_size)
            magnitude = np.abs(spectrum)
            phase = np.angle(spectrum)

            noise_mag = np.sqrt(self.noise_psd)
            clean_mag = magnitude - self.alpha * noise_mag
            clean_mag = np.maximum(clean_mag, self.beta * noise_mag)

            clean_spec = clean_mag * np.exp(1j * phase)
            clean_frame = np.fft.irfft(clean_spec, n=self.fft_size)[:self.frame_length]

            out[start:start + self.frame_length] += clean_frame * self.window
            window_sum_buffer[start:start + self.frame_length] += self.window ** 2

        nonzero = window_sum_buffer > 1e-10
        out[nonzero] /= window_sum_buffer[nonzero]

        if original_dtype != np.float32:
            out = (out * np.iinfo(original_dtype).max).astype(original_dtype)
        return out


# ------------------------------------------------------------
# _DenoiserImpl 内部实现（封装噪声估计与降噪处理）
# ------------------------------------------------------------
class _DenoiserImpl:
    """实际降噪处理类，不对外暴露"""

    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        self.denoiser = SpectralSubtractionDenoiser(
            sample_rate=cfg.get("sample_rate", 16000),
            frame_length_ms=cfg.get("frame_length_ms", 25.0),
            frame_shift_ms=cfg.get("frame_shift_ms", 10.0),
            noise_frames=cfg.get("noise_frames", 10),
            over_subtraction_factor=cfg.get("over_subtraction_factor", 2.0),
            spectral_floor=cfg.get("spectral_floor", 0.01),
        )
        self._opened = False

    def open(self, noise_audio: np.ndarray = None):
        """
        开启降噪器，并可选地传入一段纯噪声样本用于估计噪声谱。
        若未提供噪声，会在第一次调用 process() 时自动从音频开头静音段估计。
        """
        if noise_audio is not None:
            self.denoiser.estimate_noise(noise_audio)
            self.logger.info("已从传入的噪声样本估计噪声谱。")
        self._opened = True
        self.logger.info("降噪器已开启。")

    def stop(self):
        """关闭降噪器（暂无资源释放操作）"""
        self._opened = False
        self.logger.info("降噪器已关闭。")

    def process(self, audio: np.ndarray) -> np.ndarray:
        """执行降噪，返回处理后的音频数组。"""
        if not self._opened:
            self.logger.warning("降噪器未 open，自动调用 open 并使用默认噪声估计。")
            self.open()  # 自动打开，将使用音频开头静音段估计噪声
        return self.denoiser.process_array(audio)


# ------------------------------------------------------------
# Denoiser 对外顶层类（仅暴露 open/stop/process）
# ------------------------------------------------------------
class Denoiser:
    """
    语音降噪器。
    - 构造时传入配置字典（可空，使用默认参数）。
    - open(noise_audio=None)：可选传入环境纯噪声数组以估计噪声谱。
    - stop()：关闭降噪器。
    - process(audio)：对 1D numpy 音频数组(int16 或 float32)降噪,返回同类型数组。
    """

    def __init__(self, cfg: dict = None):
        if cfg is None:
            cfg = {}
        self.logger = logging.getLogger("Denoiser")
        # 环境日志（仅用于提示，不改变行为）
        if is_real_jetson():
            self.logger.info("降噪器运行在 Jetson 真机环境。")
        else:
            self.logger.info("降噪器运行在非 Jetson 环境（功能相同）。")
        self.impl = _DenoiserImpl(cfg, self.logger)

    def open(self, noise_audio: np.ndarray = None):
        """开启降噪器，可传入环境噪声片段。"""
        self.impl.open(noise_audio)

    def stop(self):
        """关闭降噪器。"""
        self.impl.stop()

    def process(self, audio: np.ndarray) -> np.ndarray:
        """对音频进行降噪处理。"""
        return self.impl.process(audio)
