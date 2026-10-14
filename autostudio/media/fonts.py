"""Typography: the fonts a video can use — system families plus free (SIL OFL, commercial use allowed) Chinese
display fonts from Google Fonts, downloaded on demand into ~/.autostudio/fonts/."""
from __future__ import annotations

import asyncio
import urllib.request
from pathlib import Path
from typing import Any

import httpx

from ..config import DATA_DIR

FONT_DIR = DATA_DIR / "fonts"
MIRRORS = ["https://cdn.jsdelivr.net/gh/google/fonts@main/ofl", "https://raw.githubusercontent.com/google/fonts/main/ofl"]

# name → (path in google/fonts/ofl, family name the font declares, what it looks like)
CATALOG: dict[str, tuple[str, str, str]] = {
    "站酷快乐体": ("zcoolkuaile/ZCOOLKuaiLe-Regular.ttf", "ZCOOL KuaiLe", "圆润可爱的手绘感，轻松、种草、萌宠、儿童"),
    "站酷庆科黄油体": ("zcoolqingkehuangyou/ZCOOLQingKeHuangYou-Regular.ttf", "ZCOOL QingKe HuangYou", "粗壮方正、冲击力强的标题字"),
    "站酷小薇体": ("zcoolxiaowei/ZCOOLXiaoWei-Regular.ttf", "ZCOOL XiaoWei", "清秀的文艺宋体，杂志感、文艺、人文"),
    "马善政楷书": ("mashanzheng/MaShanZheng-Regular.ttf", "Ma Shan Zheng", "毛笔楷书，国风、传统文化、美食"),
    "龙藏体": ("longcang/LongCang-Regular.ttf", "Long Cang", "随性的手写行书，日记、vlog、情绪"),
    "志莽行书": ("zhimangxing/ZhiMangXing-Regular.ttf", "Zhi Mang Xing", "奔放的毛笔行书，武侠、热血、书法感"),
    "刘建毛草": ("liujianmaocao/LiuJianMaoCao-Regular.ttf", "Liu Jian Mao Cao", "草书，极强的艺术感，适合一两个字的大标题"),
    "思源黑体": ("notosanssc/NotoSansSC%5Bwght%5D.ttf", "Noto Sans SC", "现代、中性、干净的无衬线体"),
    "思源宋体": ("notoserifsc/NotoSerifSC%5Bwght%5D.ttf", "Noto Serif SC", "端正优雅的衬线体，纪录片、高级感"),
}


def _file(name: str) -> Path:
    return FONT_DIR / CATALOG[name][0].split("/")[-1].replace("%5B", "[").replace("%5D", "]")


def lookup(name: str) -> tuple[Path, str] | None:
    """A downloaded catalog font by Chinese name or family name → (file, family)."""
    for k, (_p, family, _d) in CATALOG.items():
        if name in (k, family) or name.lower() == family.lower():
            f = _file(k)
            return (f, family) if f.exists() else None
    return None


def catalog() -> list[dict[str, Any]]:
    return [{"name": k, "family": fam, "look": desc, "installed": _file(k).exists()} for k, (_p, fam, desc) in CATALOG.items()]


def _proxy() -> str | None:
    try:
        p = urllib.request.getproxies()
        return p.get("https") or p.get("http")
    except Exception:  # noqa: BLE001
        return None


async def ensure(name: str) -> tuple[Path, str]:
    key = next((k for k, (_p, fam, _d) in CATALOG.items() if name in (k, fam) or name.lower() == fam.lower()), None)
    if key is None:
        raise ValueError(f"没有「{name}」这个字体。可下载：{'、'.join(CATALOG)}")
    dest = _file(key)
    if dest.exists():
        return dest, CATALOG[key][1]
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    errors = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=120), follow_redirects=True, proxy=_proxy()) as c:
        for base in MIRRORS:
            try:
                r = await c.get(f"{base}/{CATALOG[key][0]}")
                r.raise_for_status()
                if len(r.content) < 10_000:
                    raise ValueError("文件不完整")
                part = dest.with_suffix(".part")
                await asyncio.to_thread(part.write_bytes, r.content)
                part.replace(dest)
                return dest, CATALOG[key][1]
            except Exception as e:  # noqa: BLE001 - try the next mirror
                errors.append(f"{base.split('/')[2]}: {type(e).__name__}")
    raise RuntimeError(f"字体下载失败：{'；'.join(errors)}")
