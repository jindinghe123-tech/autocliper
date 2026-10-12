"""Lightweight beat / tempo detection (numpy, no librosa needed)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import ffmpeg as ff

SR = 22050
HOP = 512


async def load_mono(path: Path, sr: int = SR, max_seconds: float | None = None):
    import numpy as np

    args = [ff.ffmpeg_bin(), "-hide_banner", "-nostdin", "-i", str(path)]
    if max_seconds:
        args += ["-t", str(max_seconds)]
    args += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    import asyncio
    import subprocess

    def run() -> bytes:
        p = subprocess.run(args, capture_output=True, creationflags=ff.CREATE_NO_WINDOW)
        if p.returncode != 0:
            raise ff.FFmpegError(p.stderr.decode("utf-8", "replace")[-500:])
        return p.stdout

    raw = await asyncio.to_thread(run)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def analyze(y, sr: int = SR) -> dict[str, Any]:
    import numpy as np

    n_fft = 2048
    if len(y) < n_fft * 4:
        return {"bpm": 0, "beats": [], "downbeats": [], "onsets": []}
    window = np.hanning(n_fft).astype(np.float32)
    frames = 1 + (len(y) - n_fft) // HOP
    idx = np.arange(n_fft)[None, :] + HOP * np.arange(frames)[:, None]
    spec = np.abs(np.fft.rfft(y[idx] * window, axis=1))
    logspec = np.log1p(spec * 10)
    flux = np.maximum(0, np.diff(logspec, axis=0)).sum(axis=1)
    flux = np.concatenate([[0], flux])
    # normalise + smooth onset envelope
    flux = flux - np.convolve(flux, np.ones(16) / 16, mode="same")
    flux = np.maximum(flux, 0)
    if flux.max() > 0:
        flux = flux / flux.max()
    fps = sr / HOP
    # tempo by autocorrelation in 60..180 BPM, weighted towards ~120
    ac = np.correlate(flux, flux, mode="full")[len(flux) - 1:]
    min_lag, max_lag = int(fps * 60 / 180), int(fps * 60 / 60)
    lags = np.arange(min_lag, min(max_lag, len(ac) - 1))
    if len(lags) == 0:
        return {"bpm": 0, "beats": [], "downbeats": [], "onsets": []}
    bpms = 60 * fps / lags
    weight = np.exp(-0.5 * (np.log2(bpms / 120) / 0.9) ** 2)
    best = lags[np.argmax(ac[lags] * weight)]
    period = float(best)
    bpm = 60 * fps / period
    # dynamic-programming beat tracking (Ellis 2007 style)
    n = len(flux)
    score = flux.copy()
    back = np.full(n, -1)
    lo, hi = int(period * 0.5), int(period * 2)
    for i in range(hi, n):
        prev = np.arange(i - hi, i - lo)
        penalty = -100 * (np.log((i - prev) / period) ** 2)
        cand = score[prev] + penalty
        j = int(np.argmax(cand))
        score[i] = flux[i] + cand[j]
        back[i] = prev[j]
    tail = max(0, n - int(period))
    i = int(tail + np.argmax(score[tail:]))
    beats = []
    while i >= 0:
        beats.append(i)
        i = int(back[i])
    beats = sorted(beats)
    times = [round(b / fps, 3) for b in beats]
    # downbeats: pick the phase (0..3) with the strongest summed onset
    strengths = [flux[b] for b in beats]
    phase = int(np.argmax([sum(strengths[p::4]) for p in range(4)])) if len(beats) >= 8 else 0
    downbeats = times[phase::4]
    # strong onsets (drops / hits)
    peaks = [i for i in range(1, n - 1) if flux[i] > 0.35 and flux[i] >= flux[i - 1] and flux[i] >= flux[i + 1]]
    onsets = [round(p / fps, 3) for p in peaks]
    # energy curve per second (to find the "drop")
    secs = len(y) // sr
    rms = np.sqrt((y[: secs * sr].reshape(secs, sr) ** 2).mean(axis=1)) if secs else np.array([0.0])
    loudest = int(np.argmax(rms)) if len(rms) else 0
    return {"bpm": round(float(bpm), 1), "beats": times, "downbeats": downbeats, "onsets": onsets[:500],
            "loudest_second": loudest}
