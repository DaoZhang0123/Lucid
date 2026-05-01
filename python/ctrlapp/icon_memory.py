"""图标记忆 icon_memory.

把"小图标 ↔ 文字含义"持久化到 ``%LOCALAPPDATA%\\dev.ctrlapp\\icons\\``，并在
每次任务起手把所有图标拼成**一张带文字标注的合集图**，作为额外"用户教学"
对话注入到 prompt 头部。这样模型即便不认识系统托盘里的微信小图标，也能
通过"对照合集图查找"来定位。

存储布局::

    %LOCALAPPDATA%\\dev.ctrlapp\\icons\\
        index.json        # 元数据列表
        <id>.png          # 原始裁剪出来的图标 PNG（保留原尺寸）

``index.json`` 结构::

    {
      "items": [
        {"id": "a1b2c3", "file": "a1b2c3.png", "label": "微信",
         "description": "Windows 系统托盘里的绿色聊天气泡图标，"
                        "代表微信主程序常驻进程",
         "added_ms": 1764567890123},
        ...
      ]
    }

注入格式：在 messages 序列的 system 之后、用户 instruction 之前，插入一对
``user`` + ``assistant`` 伪对话——user 给出合集图 + 文字索引，assistant
回 "已记住"。这样既能跨多家模型工作（不依赖 system 接受 image），又能让
后续的 prune 规则识别这张图（它带有特殊 level 标签 ``L0``，不会被丢弃）。
"""
from __future__ import annotations

import io
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .config import IconMemoryConfig


# ----------------------------------------------------------------------
# 路径 / 索引
# ----------------------------------------------------------------------
def icons_dir(cfg: IconMemoryConfig) -> Path:
    """解析 icons/ 目录绝对路径。"""
    p = Path(cfg.path)
    if p.is_absolute():
        return p
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "dev.ctrlapp" / cfg.path
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".ctrlapp" / cfg.path
    return Path.cwd() / cfg.path


def _index_path(cfg: IconMemoryConfig) -> Path:
    return icons_dir(cfg) / "index.json"


def _load_index(cfg: IconMemoryConfig) -> dict[str, Any]:
    p = _index_path(cfg)
    if not p.is_file():
        return {"items": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return {"items": []}
        return data
    except (OSError, json.JSONDecodeError):
        return {"items": []}


def _save_index(cfg: IconMemoryConfig, data: dict[str, Any]) -> None:
    p = _index_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------
def list_icons(cfg: IconMemoryConfig) -> list[dict[str, Any]]:
    """返回元数据列表（不含 PNG 字节）。"""
    if not cfg.enabled:
        return []
    return list(_load_index(cfg).get("items", []))


def add_icon(cfg: IconMemoryConfig, png_bytes: bytes, label: str,
             description: str = "") -> dict[str, Any] | None:
    """登记一张图标。返回新条目；空标签/PNG 时返回 None。"""
    if not cfg.enabled:
        return None
    label = (label or "").strip()
    if not label or not png_bytes:
        return None
    description = (description or "").strip()
    # 校验 PNG 可解码
    try:
        with Image.open(io.BytesIO(png_bytes)) as im:
            im.verify()
    except Exception:
        return None
    data = _load_index(cfg)
    items: list[dict[str, Any]] = data.setdefault("items", [])
    # 超过上限则丢最早的
    if cfg.max_icons > 0 and len(items) >= cfg.max_icons:
        drop = items[: len(items) - cfg.max_icons + 1]
        for old in drop:
            try:
                (icons_dir(cfg) / old.get("file", "")).unlink(missing_ok=True)
            except OSError:
                pass
        items = items[len(items) - cfg.max_icons + 1 :]
    iid = uuid.uuid4().hex[:8]
    fname = f"{iid}.png"
    icons_dir(cfg).mkdir(parents=True, exist_ok=True)
    (icons_dir(cfg) / fname).write_bytes(png_bytes)
    entry = {
        "id": iid,
        "file": fname,
        "label": label[: cfg.max_label_chars] if cfg.max_label_chars > 0 else label,
        "description": (description[: cfg.max_desc_chars]
                        if cfg.max_desc_chars > 0 else description),
        "added_ms": int(time.time() * 1000),
    }
    items.append(entry)
    data["items"] = items
    _save_index(cfg, data)
    return entry


def remove_icon(cfg: IconMemoryConfig, icon_id: str) -> bool:
    data = _load_index(cfg)
    items: list[dict[str, Any]] = data.get("items", [])
    new_items = [it for it in items if it.get("id") != icon_id]
    if len(new_items) == len(items):
        return False
    for it in items:
        if it.get("id") == icon_id:
            try:
                (icons_dir(cfg) / it.get("file", "")).unlink(missing_ok=True)
            except OSError:
                pass
    data["items"] = new_items
    _save_index(cfg, data)
    return True


def clear_icons(cfg: IconMemoryConfig) -> bool:
    data = _load_index(cfg)
    for it in data.get("items", []):
        try:
            (icons_dir(cfg) / it.get("file", "")).unlink(missing_ok=True)
        except OSError:
            pass
    _save_index(cfg, {"items": []})
    return True


def read_icon_png(cfg: IconMemoryConfig, icon_id: str) -> bytes | None:
    for it in _load_index(cfg).get("items", []):
        if it.get("id") == icon_id:
            try:
                return (icons_dir(cfg) / it.get("file", "")).read_bytes()
            except OSError:
                return None
    return None


# ----------------------------------------------------------------------
# 拼图（atlas）
# ----------------------------------------------------------------------
@dataclass
class IconAtlas:
    png_bytes: bytes
    width: int
    height: int
    captions: str  # 文字索引（每行 "[N] label — description"）


def _try_load_font(size: int) -> ImageFont.ImageFont:
    """尽量找一个能渲染中文的字体，找不到就用默认位图字体。"""
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttf",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            if Path(path).is_file():
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def build_atlas(cfg: IconMemoryConfig) -> IconAtlas | None:
    """把所有已登记图标拼成一张带编号 / 标签的合集图。无图标时返回 None。"""
    items = list_icons(cfg)
    if not items:
        return None

    tile = max(48, int(cfg.tile_size or 96))   # 每个图标渲染区的高度
    label_h = 28
    cell_w = max(tile + 8, 180)                # 每格宽度（图标 + 文字预留）
    cell_h = tile + label_h + 12
    cols = max(1, int(cfg.atlas_cols or 4))
    rows = (len(items) + cols - 1) // cols
    pad = 12
    img_w = pad * 2 + cols * cell_w
    img_h = pad * 2 + rows * cell_h + 40       # 顶部预留标题

    canvas = Image.new("RGB", (img_w, img_h), (250, 250, 250))
    draw = ImageDraw.Draw(canvas)
    title_font = _try_load_font(16)
    label_font = _try_load_font(14)

    draw.text((pad, pad), "[图标合集] 编号 [N] 对应下方文字索引",
              fill=(60, 60, 60), font=title_font)

    captions: list[str] = []
    for idx, it in enumerate(items):
        row = idx // cols
        col = idx % cols
        x0 = pad + col * cell_w
        y0 = pad + 32 + row * cell_h
        # 边框
        draw.rectangle([x0, y0, x0 + cell_w - 4, y0 + cell_h - 4],
                       outline=(200, 200, 200), width=1)
        # 渲染图标（保持纵横比，居中放在 tile x tile 区域里）
        try:
            png = (icons_dir(cfg) / it.get("file", "")).read_bytes()
            with Image.open(io.BytesIO(png)) as src:
                src = src.convert("RGBA")
                src.thumbnail((tile, tile), Image.LANCZOS)
                ix = x0 + (cell_w - src.width) // 2
                iy = y0 + 6
                # 棋盘底（让透明图标看得清）
                bg = Image.new("RGB", (src.width, src.height), (240, 240, 240))
                bg.paste(src, mask=src.split()[3] if src.mode == "RGBA" else None)
                canvas.paste(bg, (ix, iy))
        except Exception:
            draw.text((x0 + 8, y0 + 8), "(损坏)", fill=(180, 0, 0), font=label_font)
        # 编号 + label
        label = it.get("label", "?")
        text = f"[{idx + 1}] {label}"
        draw.text((x0 + 6, y0 + tile + 8), text, fill=(20, 20, 20), font=label_font)
        # 文字索引
        desc = (it.get("description") or "").strip()
        cap = f"[{idx + 1}] {label}" + (f" — {desc}" if desc else "")
        captions.append(cap)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return IconAtlas(
        png_bytes=buf.getvalue(),
        width=img_w,
        height=img_h,
        captions="\n".join(captions),
    )


# ----------------------------------------------------------------------
# 从一张大截图里裁剪出图标区域并登记
# ----------------------------------------------------------------------
def crop_and_add(cfg: IconMemoryConfig, source_png: bytes,
                 x: int, y: int, w: int, h: int,
                 label: str, description: str = "") -> dict[str, Any] | None:
    """从 ``source_png`` 里裁出 (x,y,w,h)（图片像素坐标）作为新图标。"""
    if not source_png or w <= 0 or h <= 0:
        return None
    try:
        with Image.open(io.BytesIO(source_png)) as src:
            src = src.convert("RGBA")
            sw, sh = src.size
            x0 = max(0, min(int(x), sw - 1))
            y0 = max(0, min(int(y), sh - 1))
            x1 = max(x0 + 1, min(int(x + w), sw))
            y1 = max(y0 + 1, min(int(y + h), sh))
            crop = src.crop((x0, y0, x1, y1))
            buf = io.BytesIO()
            crop.save(buf, format="PNG", optimize=True)
            png = buf.getvalue()
    except Exception:
        return None
    return add_icon(cfg, png, label, description)
