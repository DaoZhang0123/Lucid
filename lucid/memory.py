"""长期记忆 memory.md。

设计：把 ``~/.lucid/memory.md`` 当作一个**带时间戳条目的
追加式 Markdown 文件**。每次任务起手时，把整份文件的正文塞到 system prompt
末尾，让模型知道用户的偏好/常用路径/约束。

文件格式（人可读 + 机器可读）::

    # lucid 长期记忆

    - [2026-05-01 10:23 · user] 我的桌面在 D:\\Desktop
    - [2026-05-01 10:24 · agent] 用户偏好用 PowerShell 而不是 cmd

写入路径有两条：

* 主动：用户在聊天里说 "记住…"，模型调用 ``remember`` 工具写入；
* 被动：未来的 heartbeat 反思（暂留接口，本版本不启用）。

"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from .config import MemoryConfig

_HEADER = "# lucid Long-term Memory\n"
_ENTRY_RE = re.compile(r"^- \[", re.MULTILINE)

# Regex to strip the timestamp+source prefix from an entry line for comparison.
_ENTRY_PREFIX_RE = re.compile(r"^- \[[^\]]*\]\s*")

# Characters to strip when computing token overlap for dedup.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

# CJK character ranges (Unified Ideographs + common extensions).
_CJK_RE = re.compile(
    r"([\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
    r"\U00020000-\U0002a6df\U0002a700-\U0002b73f])"
)


def _normalize_for_dedup(text: str) -> set[str]:
    """Extract a bag-of-tokens from *text* for fuzzy dedup comparison.

    For CJK text (Chinese/Japanese/Korean), each character is treated as a
    separate token since these languages don't use whitespace word boundaries.
    For Latin/etc. text, splits on whitespace after stripping punctuation.
    Tokens shorter than 2 chars are dropped ONLY for non-CJK tokens.
    """
    text = _ENTRY_PREFIX_RE.sub("", text)
    text = _PUNCT_RE.sub(" ", text.lower())
    # Insert spaces around each CJK character so they become individual tokens.
    text = _CJK_RE.sub(r" \1 ", text)
    tokens: set[str] = set()
    for t in text.split():
        # Keep all CJK single chars; drop short Latin tokens (articles etc.)
        if len(t) >= 2 or _CJK_RE.match(t):
            tokens.add(t)
    return tokens


def _is_duplicate(new_text: str, existing_entries: list[str], threshold: float = 0.65) -> bool:
    """Return True if *new_text* is semantically redundant with any existing entry.

    Uses token-overlap comparison as a lightweight safety net. The primary dedup
    mechanism is the LLM seeing full memory content in the doze prompt; this
    catches cases where the LLM ignores the instruction anyway.

    A threshold of 0.65 means 65% of the smaller token-set must appear in the
    larger one (overlap coefficient).
    """
    new_tokens = _normalize_for_dedup(new_text)
    if not new_tokens:
        return False
    for entry in existing_entries:
        existing_tokens = _normalize_for_dedup(entry)
        if not existing_tokens:
            continue
        # Use overlap coefficient: |intersection| / min(|A|, |B|)
        # This is more forgiving than Jaccard when one entry is a superset of
        # the other (which is the typical duplication pattern here).
        intersection = new_tokens & existing_tokens
        smaller = min(len(new_tokens), len(existing_tokens))
        if smaller == 0:
            continue
        if len(intersection) / smaller >= threshold:
            return True
    return False


def memory_path(cfg: MemoryConfig) -> Path:
    """解析 memory.md 的绝对路径。相对路径落到 ~/.lucid/。"""
    p = Path(cfg.path)
    if p.is_absolute():
        return p
    return Path.home() / ".lucid" / cfg.path


def read_memory(cfg: MemoryConfig) -> str:
    """读取整份 memory.md（含 header）。文件不存在或被禁用时返回空串。"""
    if not cfg.enabled:
        return ""
    p = memory_path(cfg)
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def memory_for_prompt(cfg: MemoryConfig) -> str:
    """把 memory.md 内容裁成可注入 prompt 的片段；空时返回 ""。"""
    raw = read_memory(cfg).strip()
    if not raw:
        return ""
    # 去掉 header（注入时有自己的小标题）
    body = raw.split("\n", 1)[1].strip() if raw.startswith("#") else raw
    if cfg.max_chars > 0 and len(body) > cfg.max_chars:
        # 只保留最后 max_chars，按行切齐
        body = body[-cfg.max_chars:]
        nl = body.find("\n")
        if nl > 0:
            body = body[nl + 1:]
    if not body.strip():
        return ""
    return "\n## Long-term memory (user preferences / agreed conventions, please respect)\n" + body.strip() + "\n"


def append_memory(cfg: MemoryConfig, text: str, source: str = "agent") -> bool:
    """追加一条带时间戳的条目。空文本忽略。返回是否成功。

    内置去重：如果新条目与现有条目在 token 重叠度 ≥ 70% 时视为重复，跳过写入。
    """
    if not cfg.enabled:
        return False
    text = (text or "").strip()
    if not text:
        return False
    # 单行化：换行替换为空格，避免破坏 bullet 结构
    text = re.sub(r"\s+", " ", text)
    if cfg.max_entry_chars > 0 and len(text) > cfg.max_entry_chars:
        text = text[: cfg.max_entry_chars - 1] + "…"
    p = memory_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)

    # --- dedup check ---
    if p.is_file():
        try:
            existing_raw = p.read_text(encoding="utf-8")
        except OSError:
            existing_raw = ""
        existing_entries = [
            ln for ln in existing_raw.splitlines() if ln.startswith("- [")
        ]
        if _is_duplicate(text, existing_entries):
            return False  # skip duplicate
    # --- end dedup ---

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"- [{ts} · {source}] {text}\n"
    if not p.exists():
        p.write_text(_HEADER + "\n" + entry, encoding="utf-8")
    else:
        with p.open("a", encoding="utf-8") as f:
            f.write(entry)
    _rotate(cfg, p)
    return True


def clear_memory(cfg: MemoryConfig) -> bool:
    """清空 memory.md（保留 header）。"""
    p = memory_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_HEADER, encoding="utf-8")
    return True


def write_memory_raw(cfg: MemoryConfig, text: str) -> bool:
    """整份覆盖写（设置页里直接编辑全文用）。会确保以 header 开头。"""
    p = memory_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = text or ""
    if not text.startswith("#"):
        text = _HEADER + "\n" + text
    p.write_text(text, encoding="utf-8")
    return True


def _rotate(cfg: MemoryConfig, p: Path) -> None:
    """超过 max_entries 时丢掉最早的条目。"""
    if cfg.max_entries <= 0:
        return
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return
    lines = raw.splitlines()
    entry_idxs = [i for i, ln in enumerate(lines) if ln.startswith("- [")]
    if len(entry_idxs) <= cfg.max_entries:
        return
    keep_from = entry_idxs[len(entry_idxs) - cfg.max_entries]
    head = lines[: entry_idxs[0]]  # header 部分
    new_lines = head + lines[keep_from:]
    p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# 远期：heartbeat 反思接口占位
def heartbeat_tick(cfg: MemoryConfig) -> None:
    """周期反思钩子（当前未启用）。后续在主循环里按 heartbeat_interval_sec 触发。"""
    _ = cfg, time
    return
