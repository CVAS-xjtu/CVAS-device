# denoiser.py
# denoiser.py
import numpy as np
from typing import Optional

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
            raise ValueError("噪声信号太短，无法估计噪声谱")
        self.noise_psd = noise_psd / num_frames
        self._noise_estimated = True

    def process_array(self, audio: np.ndarray) -> np.ndarray:
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


class Denoiser:
    """降噪器顶层类，提供 open/stop/process 接口"""

    def __init__(self, cfg: dict = None):
        if cfg is None:
            cfg = {}
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
        if noise_audio is not None:
            self.denoiser.estimate_noise(noise_audio)
        self._opened = True

    def stop(self):
        self._opened = False

    def process(self, audio: np.ndarray) -> np.ndarray:
        if not self._opened:
            self.open()  # 自动启动并利用默认噪声估计
        return self.denoiser.process_array(audio)