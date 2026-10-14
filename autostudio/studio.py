"""Low-level building blocks for the Python the agent writes and runs with `studio_run`.

Nothing in here decides how a video looks. It only gives direct access to pixels, audio samples, fonts, voices
and ffmpeg, so the agent can build whatever it designs — kinetic typography, motion graphics, masks, custom
transitions, data animations, generative backgrounds, audio processing — frame by frame and sample by sample.

    from autostudio.studio import *
    W, H, FPS = 1080, 1920, 30
    with VideoWriter("title.webm", W, H, FPS, alpha=True) as out:       # transparent layer for the timeline
        for i in range(FPS * 2):
            t = i / FPS
            img = canvas(W, H)                                           # RGBA, fully transparent
            d = draw(img)
            f = font("站酷快乐体", int(lerp(60, 140, ease(min(t / 0.6, 1), "out_back"))))
            d.text((W / 2, H * 0.35), "不晒太阳也能活", font=f, anchor="mm", fill=(255, 225, 77, 255))
            out.write(img)

Images are PIL.Image (RGBA or RGB) or numpy uint8 arrays (H, W, 3|4). Audio is float32 numpy (samples, channels).
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any, Iterator

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps  # noqa: F401 - re-exported

from .media import ffmpeg as _ff

FFMPEG = _ff.ffmpeg_bin()
NO_WINDOW = _ff.CREATE_NO_WINDOW

__all__ = ["FFMPEG", "ffmpeg", "probe", "font", "font_path", "canvas", "draw", "to_array", "to_image", "VideoWriter",
           "read_frames", "frame_at", "read_audio", "write_audio", "tts", "beats", "lerp", "clamp", "ease",
           "Image", "ImageDraw", "ImageFont", "ImageFilter", "ImageChops", "ImageEnhance", "ImageOps", "np", "math"]


# ---------------------------------------------------------------------------
# ffmpeg

def ffmpeg(*args: Any) -> str:
    """Run ffmpeg with these arguments (-y added). Returns stderr; raises RuntimeError with the tail on failure."""
    p = subprocess.run([FFMPEG, "-y", "-hide_banner", *[str(a) for a in args]], capture_output=True, creationflags=NO_WINDOW)
    err = p.stderr.decode("utf-8", "replace")
    if p.returncode != 0:
        raise RuntimeError("ffmpeg 失败：\n" + "\n".join(err.splitlines()[-15:]))
    return err


def probe(path: str | Path) -> dict[str, Any]:
    """duration, width, height, fps, codec, has_audio, has_alpha of a media file."""
    out: dict[str, Any] = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0, "codec": "", "has_audio": False,
                           "has_alpha": False}
    fp = _ff.ffprobe_bin()
    if fp:
        p = subprocess.run([fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                           capture_output=True, creationflags=NO_WINDOW)
        data = json.loads(p.stdout or b"{}")
        out["duration"] = float((data.get("format") or {}).get("duration") or 0)
        for st in data.get("streams") or []:
            if st.get("codec_type") == "video" and not out["width"]:
                out.update(width=int(st.get("width") or 0), height=int(st.get("height") or 0), codec=st.get("codec_name") or "")
                num, _, den = (st.get("avg_frame_rate") or "0/1").partition("/")
                out["fps"] = round(float(num) / float(den), 3) if den and float(den) else 0.0
                out["has_alpha"] = "yuva" in (st.get("pix_fmt") or "") or (st.get("tags") or {}).get("alpha_mode") == "1"
            if st.get("codec_type") == "audio":
                out["has_audio"] = True
        return out
    # no ffprobe (imageio-ffmpeg ships ffmpeg only): read what `ffmpeg -i` prints
    err = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True,
                         creationflags=NO_WINDOW).stderr.decode("utf-8", "replace")
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    if m:
        out["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    vm = re.search(r"Video: (\w+)[^\n]*?, (\d{2,5})x(\d{2,5})", err)
    if vm:
        out.update(codec=vm.group(1), width=int(vm.group(2)), height=int(vm.group(3)))
        fm = re.search(r"([\d.]+) fps", err)
        out["fps"] = float(fm.group(1)) if fm else 0.0
        out["has_alpha"] = bool(re.search(r"alpha_mode\s*:\s*1|yuva|rgba|argb|bgra", err))
    out["has_audio"] = "Audio:" in err
    return out


def _decoder(info: dict[str, Any]) -> list[str]:
    """ffmpeg's built-in VP8/VP9 decoders drop the alpha channel; libvpx keeps it."""
    if info.get("has_alpha") and info.get("codec") in ("vp9", "vp8"):
        return ["-c:v", "libvpx-vp9" if info["codec"] == "vp9" else "libvpx"]
    return []


# ---------------------------------------------------------------------------
# images

def font_path(name: str | None = None) -> str:
    """Font file for a family (黑体/雅黑/等线/细体/楷体/宋体/仿宋), a downloaded font (站酷快乐体…), or a file."""
    return _ff.font_file(name)


def font(name: str | None = None, size: int = 48) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(name), int(size))


def canvas(w: int, h: int, color: Any = (0, 0, 0, 0)) -> Image.Image:
    return Image.new("RGBA", (int(w), int(h)), color)


def draw(img: Image.Image) -> ImageDraw.ImageDraw:
    return ImageDraw.Draw(img, "RGBA")


def to_array(img: Image.Image) -> np.ndarray:
    return np.asarray(img)


def to_image(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(np.asarray(arr).astype(np.uint8))


# ---------------------------------------------------------------------------
# video I/O

class VideoWriter:
    """Pipe frames into ffmpeg. alpha=True writes a transparent layer (.webm VP9 or .mov ProRes 4444) that the
    timeline can overlay; otherwise H.264. `audio` muxes an audio file in."""

    def __init__(self, path: str | Path, w: int, h: int, fps: float = 30, alpha: bool = False, crf: int = 18,
                 audio: str | Path | None = None):
        self.path, self.w, self.h, self.alpha = Path(path), int(w), int(h), alpha
        self.frames = 0
        args = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba" if alpha else "rgb24",
                "-s", f"{self.w}x{self.h}", "-r", str(fps), "-i", "-"]
        if audio:
            args += ["-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "192k", "-shortest"]
        suffix = self.path.suffix.lower()
        if alpha and suffix == ".mov":
            args += ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"]
        elif alpha:
            args += ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "30", "-row-mt", "1", "-deadline", "realtime"]
        else:
            args += ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        self.proc = subprocess.Popen(args + [str(self.path)], stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=NO_WINDOW)

    def write(self, frame: Image.Image | np.ndarray) -> None:
        img = frame if isinstance(frame, Image.Image) else to_image(frame)
        img = img.convert("RGBA" if self.alpha else "RGB")
        if img.size != (self.w, self.h):
            img = img.resize((self.w, self.h), Image.LANCZOS)
        assert self.proc.stdin is not None
        self.proc.stdin.write(img.tobytes())
        self.frames += 1

    def close(self) -> Path:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        err = self.proc.stderr.read().decode("utf-8", "replace") if self.proc.stderr else ""
        if self.proc.wait() != 0:
            raise RuntimeError("写视频失败：" + err[-800:])
        return self.path

    def __enter__(self) -> "VideoWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def read_frames(path: str | Path, w: int | None = None, h: int | None = None, fps: float | None = None,
                start: float = 0, duration: float | None = None, alpha: bool = False) -> Iterator[np.ndarray]:
    """Decode frames as uint8 arrays (H, W, 3 or 4). Optionally scaled, resampled and trimmed."""
    info = probe(path)
    w = int(w or info["width"])
    h = int(h or info["height"])
    if not w or not h:
        raise RuntimeError(f"读不出画面尺寸：{path}")
    vf = [f"scale={w}:{h}"] + ([f"fps={fps}"] if fps else [])
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", str(start), *(_decoder(info) if alpha else []), "-i", str(path)]
    if duration:
        args += ["-t", str(duration)]
    ch = 4 if alpha else 3
    args += ["-vf", ",".join(vf), "-f", "rawvideo", "-pix_fmt", "rgba" if alpha else "rgb24", "-"]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    size = w * h * ch
    try:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w, ch)
    finally:
        proc.kill()


def frame_at(path: str | Path, t: float = 0.0, w: int | None = None, h: int | None = None) -> Image.Image:
    """One frame (or an image file) as a PIL image."""
    if Path(path).suffix.lower() in _ff.IMAGE_EXTS:
        img = Image.open(path).convert("RGB")
        return img.resize((int(w), int(h)), Image.LANCZOS) if w and h else img
    return to_image(next(read_frames(path, w, h, start=t)))


# ---------------------------------------------------------------------------
# audio I/O

def read_audio(path: str | Path, sr: int = 48000, mono: bool = False) -> tuple[np.ndarray, int]:
    ch = 1 if mono else 2
    p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", str(ch), "-ar", str(sr),
                        "-f", "f32le", "-"], capture_output=True, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[-500:])
    return np.frombuffer(p.stdout, np.float32).reshape(-1, ch).copy(), sr


def write_audio(path: str | Path, samples: np.ndarray, sr: int = 48000) -> Path:
    a = np.asarray(samples, np.float32)
    a = a.reshape(-1, 1) if a.ndim == 1 else a
    p = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "f32le", "-ar", str(sr), "-ac", str(a.shape[1]),
                        "-i", "-", str(path)], input=np.clip(a, -1, 1).tobytes(), capture_output=True, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[-500:])
    return Path(path)


def tts(text: str, voice: str | None = None, rate: str | None = None, out: str | Path | None = None) -> dict[str, Any]:
    """Speak `text` with the configured engines. → {path, duration, engine, cues: [(start, end, text)]}"""
    from .config import Settings
    from .media import tts as engines

    out = Path(out or f"voice_{abs(hash(text)) % 10**8}.mp3")
    settings = Settings().get()
    res = asyncio.run(engines.synthesize(text, out, settings, voice=voice, rate=rate))
    dur = probe(out)["duration"]
    cues = engines.cues_for(text, res, dur, 14)
    return {"path": str(out), "duration": dur, "engine": res["engine"], "cues": [(c.start, c.end, c.text) for c in cues]}


def beats(path: str | Path, max_seconds: float = 300) -> dict[str, Any]:
    """Tempo and beat times of a piece of music: {bpm, beats, downbeats, onsets}."""
    from .media.beats import analyze, load_mono

    return analyze(asyncio.run(load_mono(Path(path), max_seconds=max_seconds)))


# ---------------------------------------------------------------------------
# a little maths for motion

def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def ease(t: float, kind: str = "in_out") -> float:
    """Easing curves on 0..1: linear, in, out, in_out, out_back, out_elastic, out_bounce."""
    t = clamp(t)
    if kind == "in":
        return t ** 3
    if kind == "out":
        return 1 - (1 - t) ** 3
    if kind == "in_out":
        return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2
    if kind == "out_back":
        c = 1.70158
        return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2
    if kind == "out_elastic":
        return 0 if t == 0 else 1 if t == 1 else 2 ** (-10 * t) * math.sin((t * 10 - 0.75) * (2 * math.pi) / 3) + 1
    if kind == "out_bounce":
        n, d = 7.5625, 2.75
        if t < 1 / d:
            return n * t * t
        if t < 2 / d:
            t -= 1.5 / d
            return n * t * t + 0.75
        if t < 2.5 / d:
            t -= 2.25 / d
            return n * t * t + 0.9375
        t -= 2.625 / d
        return n * t * t + 0.984375
    return t
