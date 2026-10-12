"""Subtitle model + SRT / VTT / ASS read & write + word-level re-segmentation."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CJK_RE = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿가-힯＀-￯]")
SENT_END = set("。！？!?…")
SOFT_BREAK = set("，,、；;：:")
TRAIL_PUNCT = "，。,.、；;：:！!？?…"


@dataclass
class Cue:
    start: float
    end: float
    text: str

    def to_dict(self, index: int | None = None) -> dict[str, Any]:
        d = asdict(self)
        d["start"] = round(self.start, 3)
        d["end"] = round(self.end, 3)
        if index is not None:
            d["index"] = index
        return d


def ts(sec: float, sep: str = ",") -> str:
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def ts_ass(sec: float) -> str:
    cs = int(round(max(0.0, sec) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[,.](\d{1,3})")


def _parse_ts(s: str) -> float:
    m = _TS.search(s)
    if not m:
        raise ValueError(f"bad timestamp: {s}")
    h = int(m.group(1) or 0)
    frac = m.group(4).ljust(3, "0")
    return h * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(frac) / 1000


def parse_srt_vtt(text: str) -> list[Cue]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.split("\n")]
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        a, b = lines[idx].split("-->", 1)
        try:
            start, end = _parse_ts(a), _parse_ts(b.strip().split(" ")[0])
        except ValueError:
            continue
        body = "\n".join(lines[idx + 1:]).strip()
        body = re.sub(r"</?(?:c|v|i|b|u)(?:[.\s][^>]*)?>", "", body)
        cues.append(Cue(start, end, body))
    return cues


def parse_ass(text: str) -> list[Cue]:
    cues: list[Cue] = []
    fields: list[str] = []
    in_events = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_events = line.lower() == "[events]"
            continue
        if not in_events:
            continue
        if line.startswith("Format:"):
            fields = [f.strip().lower() for f in line[7:].split(",")]
        elif line.startswith("Dialogue:") and fields:
            parts = line[9:].split(",", len(fields) - 1)
            row = dict(zip(fields, parts))
            body = re.sub(r"\{[^}]*\}", "", row.get("text", "")).replace(r"\N", "\n").replace(r"\n", "\n").strip()
            try:
                cues.append(Cue(_parse_ts(row["start"].strip() + "0"), _parse_ts(row["end"].strip() + "0"), body))
            except (KeyError, ValueError):
                continue
    return cues


def load(path: str | Path) -> list[Cue]:
    p = Path(path)
    text = p.read_text("utf-8-sig", errors="replace")
    if p.suffix.lower() in (".ass", ".ssa"):
        return parse_ass(text)
    return parse_srt_vtt(text)


def dump_srt(cues: list[Cue]) -> str:
    return "\n".join(f"{i}\n{ts(c.start)} --> {ts(c.end)}\n{c.text.strip()}\n" for i, c in enumerate(cues, 1))


def dump_vtt(cues: list[Cue]) -> str:
    body = "\n".join(f"{ts(c.start, '.')} --> {ts(c.end, '.')}\n{c.text.strip()}\n" for c in cues)
    return "WEBVTT\n\n" + body


def _ass_color(c: str | None, default: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR'."""
    c = (c or default).strip().lstrip("#")
    names = {"white": "FFFFFF", "black": "000000", "yellow": "FFE000", "red": "FF3B30", "green": "34C759",
             "blue": "0A84FF", "cyan": "32D5FF", "pink": "FF6FB5", "orange": "FF9500"}
    c = names.get(c.lower(), c)
    if not re.match(r"^[0-9a-fA-F]{6}$", c):
        c = default.lstrip("#")
    r, g, b = c[0:2], c[2:4], c[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def dump_ass(cues: list[Cue], width: int, height: int, style: dict[str, Any] | None = None) -> str:
    st = style or {}
    font = st.get("font") or "Microsoft YaHei"
    base = min(width, height)
    size = int(st.get("font_size") or round(base * (0.065 if height > width else 0.055)))
    outline = float(st.get("outline") if st.get("outline") is not None else max(2, round(size * 0.08)))
    shadow = float(st.get("shadow") or 0)
    bold = -1 if st.get("bold", True) else 0
    pos = st.get("position", "bottom")
    align = {"bottom": 2, "top": 8, "middle": 5, "center": 5}.get(pos, 2)
    margin_v = int(st.get("margin_v") if st.get("margin_v") is not None else round(height * (0.08 if pos == "bottom" else 0.06)))
    if st.get("box"):
        border_style, back = 3, _ass_color(st.get("box_color"), "#000000", alpha=0x60)
        outline_col = back
    else:
        border_style, back = 1, _ass_color("#000000", "#000000", alpha=0x80)
        outline_col = _ass_color(st.get("outline_color"), "#000000")
    primary = _ass_color(st.get("color"), "#FFFFFF")
    header = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\nPlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},{primary},&H000000FF,{outline_col},{back},{bold},0,0,0,100,100,0,0,"
        f"{border_style},{outline:g},{shadow:g},{align},{int(width * 0.06)},{int(width * 0.06)},{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    hl = st.get("highlight_color")
    lines = []
    for c in cues:
        text = c.text.strip().replace("\n", r"\N")
        text = text.replace("{", "(").replace("}", ")")
        if st.get("animation") == "pop":
            text = r"{\fscx80\fscy80\t(0,120,\fscx100\fscy100)}" + text
        elif st.get("animation") == "fade":
            text = r"{\fad(120,120)}" + text
        if hl and "\\N" in text:
            first, rest = text.split("\\N", 1)
            second_size = int(size * 0.8)
            text = first + r"\N{\c&H" + _ass_color(hl, "#FFE000")[4:] + r"&\fs" + str(second_size) + "}" + rest
        lines.append(f"Dialogue: 0,{ts_ass(c.start)},{ts_ass(c.end)},Default,,0,0,0,,{text}")
    return header + "\n".join(lines) + "\n"


def save(path: str | Path, cues: list[Cue], **ass_kw: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ext = p.suffix.lower()
    cues = sorted(cues, key=lambda c: c.start)
    if ext == ".vtt":
        p.write_text(dump_vtt(cues), "utf-8")
    elif ext in (".ass", ".ssa"):
        p.write_text(dump_ass(cues, ass_kw.get("width", 1920), ass_kw.get("height", 1080), ass_kw.get("style")), "utf-8")
    elif ext == ".txt":
        p.write_text("\n".join(c.text for c in cues) + "\n", "utf-8")
    else:
        p.write_text(dump_srt(cues), "utf-8")
    return p


# ---- word-level helpers -------------------------------------------------

def words_path(sub_path: str | Path) -> Path:
    p = Path(sub_path)
    return p.with_suffix(".words.json")


def save_words(sub_path: str | Path, words: list[dict[str, Any]]) -> None:
    words_path(sub_path).write_text(json.dumps(words, ensure_ascii=False), "utf-8")


def load_words(sub_path: str | Path) -> list[dict[str, Any]] | None:
    p = words_path(sub_path)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            return None
    return None


def text_len(s: str) -> float:
    """Visual length: CJK chars count 1, latin chars ~0.5."""
    n = 0.0
    for ch in s:
        n += 1 if CJK_RE.match(ch) else (0.5 if not ch.isspace() else 0.25)
    return n


def is_cjk_text(s: str) -> bool:
    return len(CJK_RE.findall(s)) > len(s) * 0.3


def join_words(words: list[str]) -> str:
    out = ""
    for w in words:
        if out and not w.startswith(" ") and not CJK_RE.match(w[:1] or " ") and not CJK_RE.match(out[-1:]) \
                and w[:1].isalnum() and out[-1:].isalnum():
            out += " "
        out += w
    return re.sub(r"\s+", " ", out).strip()


def strip_punctuation(text: str) -> str:
    """Short-video style: in-line commas become spaces, trailing punctuation is dropped."""
    text = re.sub(r"\s*[，、；：,;:]\s*", " ", text.strip())
    return text.rstrip(TRAIL_PUNCT + " ").strip()


def segment_words(words: list[dict[str, Any]], max_chars: float = 16, max_gap: float = 0.8,
                  max_duration: float = 6.0, strip_punct: bool = False) -> list[Cue]:
    """Group timestamped words into readable subtitle cues."""
    cues: list[Cue] = []
    buf: list[dict[str, Any]] = []

    def flush() -> None:
        if not buf:
            return
        text = join_words([w["word"] for w in buf])
        if strip_punct:
            text = strip_punctuation(text)
        if text:
            cues.append(Cue(float(buf[0]["start"]), float(buf[-1]["end"]), text))
        buf.clear()

    for w in words:
        word = str(w.get("word", ""))
        if not word.strip():
            continue
        if buf:
            cur = join_words([b["word"] for b in buf])
            gap = float(w["start"]) - float(buf[-1]["end"])
            too_long = text_len(cur + word) > max_chars
            too_slow = float(w["end"]) - float(buf[0]["start"]) > max_duration
            if gap > max_gap or too_long or too_slow:
                flush()
        buf.append(w)
        stripped = word.strip()
        last = stripped[-1:] if stripped else ""
        cur_len = text_len(join_words([b["word"] for b in buf]))
        if last in SENT_END or (last in ".") and len(stripped) > 1:
            flush()
        elif last in SOFT_BREAK and cur_len >= max_chars * 0.45:
            flush()
    flush()
    # avoid overlaps / tiny flashes
    for i, c in enumerate(cues):
        if c.end - c.start < 0.4:
            c.end = c.start + 0.4
        if i + 1 < len(cues) and c.end > cues[i + 1].start:
            c.end = max(c.start + 0.2, cues[i + 1].start - 0.01)
    return cues


def split_long_cues(cues: list[Cue], max_chars: float) -> list[Cue]:
    """Fallback re-segmentation without word timings: split text proportionally."""
    out: list[Cue] = []
    for c in cues:
        text = c.text.replace("\n", " ").strip()
        if text_len(text) <= max_chars:
            out.append(c)
            continue
        pieces = [p for p in re.split(r"(?<=[，。！？,.!?；;、])\s*", text) if p]
        chunks: list[str] = []
        cur = ""
        for p in pieces:
            if cur and text_len(cur + p) > max_chars:
                chunks.append(cur)
                cur = p
            else:
                cur += p
            while text_len(cur) > max_chars * 1.3:
                cut = int(len(cur) * max_chars / max(text_len(cur), 1))
                chunks.append(cur[:cut])
                cur = cur[cut:]
        if cur:
            chunks.append(cur)
        total = sum(max(len(x), 1) for x in chunks)
        t = c.start
        for ch in chunks:
            d = (c.end - c.start) * max(len(ch), 1) / total
            out.append(Cue(t, t + d, ch.strip()))
            t += d
    return out
