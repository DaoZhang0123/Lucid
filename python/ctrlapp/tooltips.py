"""操作技巧 tools.md（与 memory.md 平行）。

设计：把"如何操作各类 App 的提示"抽离成一份**可演化的 markdown**，每次任务起手
注入到 system prompt 末尾。与 memory.md 的差异：

* memory.md 记**用户事实**（称呼 / 偏好 / 环境）；
* tools.md 记**操作技法**（针对某个 App / 对话框 / 控件该怎么做最稳妥）。

写入路径有两条：

* 主动：用户说"以后开浏览器都用 Edge / 处理 Excel 时先 Ctrl+End 再…" 时模型可调
  ``learn_tip``；
* 被动：模型在任务中**总结成功/失败经验**（如"Outlook 用 Ctrl+R 回复比点回复按钮稳"）
  时调用 ``learn_tip(text, kind='success' | 'failure')``。

文件首次缺失时会用一份初始 seed（从 loop.SYSTEM_PROMPT 里"常用技巧"段抽出来）写入。
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from .config import ToolsConfig

_HEADER = "# ctrlapp 操作技巧库\n"

# 初始 seed —— 与原 SYSTEM_PROMPT 中的"常用技巧"段保持一致。
_SEED_BODY = """\
- [seed · keyboard] 优先用键盘快捷键，避免鼠标点击；截图坐标不一定精确，能用键盘做到的就别用鼠标。
- [seed · keyboard] 切换/浏览已开窗口用 alt+tab / alt+shift+tab / win+tab；不要靠点任务栏小图标切窗口。
- [seed · browser] 浏览器新建窗口 ctrl+n、新标签页 ctrl+t、关标签 ctrl+w、地址栏 ctrl+l 或 alt+d、后退/前进 alt+左右、刷新 f5。
- [seed · browser] 不要点窗口右上角加号开新标签——直接用 ctrl+t 更稳。
- [seed · search] 默认搜索引擎用 bing.com。最快的方式：win+r 打开"运行"，type `https://bing.com/search?q=关键词` 回车，
  Windows 会用默认浏览器直接打开搜索结果页（无需先开浏览器、也不会动用户已有 tab）。
  备用方式：浏览器里 ctrl+t 新标签页 → 地址栏 type `bing.com` 回车 → 再 type 关键词。
  仅当用户明确要求 Google / 百度等其它引擎时才换。
- [seed · text-edit] 全选 ctrl+a、复制 ctrl+c、粘贴 ctrl+v、撤销 ctrl+z、保存 ctrl+s、查找 ctrl+f。
- [seed · system] 运行命令 win+r、文件资源管理器 win+e、桌面 win+d；记事本可 win+r 后 type notepad 回车。
- [seed · window] 窗口排版：win+左/右 半屏吸附，win+上 最大化，win+下 最小化或还原，win+m 全部最小化。
- [seed · type-text] 文本输入用 action="type" + text="..."，本地驱动走剪贴板粘贴，中英文/路径都可直接传。
- [seed · 不覆盖] 执行任何"打开/新建/保存"前都要假设当前已经有用户工作内容，宁可新开一个也别覆盖。
- [seed · 不覆盖] 写文档：win+r → notepad 回车开**新的**记事本；不要直接往已经打开的记事本里 type。
- [seed · 不覆盖] Word/WPS 用 ctrl+n 新建文档，不要在已打开的文档里直接覆盖写。
- [seed · 不覆盖] 浏览器开新页面一律 ctrl+n 或 ctrl+t，不要复用当前 tab 的地址栏跳转，会丢掉用户在看的页面。
- [seed · 不覆盖] 保存文件时若对话框默认文件名指向已存在文件，先全选删掉再 type 一个新的、明确的文件名（带时间戳更稳）。
- [seed · 不覆盖] 关闭任何窗口/标签前必须确认这是你自己刚开的；不能随手关用户原来的窗口。
- [seed · 启动 App] 使用某 App 前先 alt+tab 截图看已打开窗口；已在跑就用 ctrl+n 新建窗口续工作，不要占用用户原有窗口；
  完全没开时才通过 win+r / 开始菜单搜索启动它。
- [seed · 保存对话框] 优先在文件名输入框 type 完整绝对路径回车；或点击对话框顶部地址栏（路径面包屑）然后 type 路径回车；
  **不要**去左侧『快速访问/此电脑』树里逐层点击导航。
- [seed · 路径] 桌面路径：%USERPROFILE%\\Desktop 或 C:\\Users\\<用户名>\\Desktop，可以直接当成绝对路径 type 进去。
- [seed · 对话框] 文件名框已有默认值（如 *.txt）时，先 ctrl+a 全选再 type 新路径，避免拼接出错。
- [seed · 截图] 点击小按钮/小图标前先 L2 active_window 或 L3 cursor_local 看清，避免点偏。
"""


def tools_path(cfg: ToolsConfig) -> Path:
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


def _ensure_seeded(cfg: ToolsConfig) -> Path:
    p = tools_path(cfg)
    if p.is_file():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_HEADER + "\n" + _SEED_BODY, encoding="utf-8")
    return p


def read_tools(cfg: ToolsConfig) -> str:
    if not cfg.enabled:
        return ""
    p = _ensure_seeded(cfg)
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def tools_for_prompt(cfg: ToolsConfig) -> str:
    raw = read_tools(cfg).strip()
    if not raw:
        return ""
    body = raw.split("\n", 1)[1].strip() if raw.startswith("#") else raw
    if cfg.max_chars > 0 and len(body) > cfg.max_chars:
        body = body[-cfg.max_chars:]
        nl = body.find("\n")
        if nl > 0:
            body = body[nl + 1:]
    if not body.strip():
        return ""
    return "\n## 操作技巧（动态学习，遇到新情况可用 learn_tip 写入）\n" + body.strip() + "\n"


def append_tip(cfg: ToolsConfig, text: str, kind: str = "tip", source: str = "agent") -> bool:
    """追加一条技巧。``kind`` 可选 ``tip``/``success``/``failure``，作为前缀展示。"""
    if not cfg.enabled:
        return False
    text = (text or "").strip()
    if not text:
        return False
    text = re.sub(r"\s+", " ", text)
    if cfg.max_entry_chars > 0 and len(text) > cfg.max_entry_chars:
        text = text[: cfg.max_entry_chars - 1] + "…"
    kind = (kind or "tip").lower()
    if kind not in ("tip", "success", "failure"):
        kind = "tip"
    p = _ensure_seeded(cfg)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"- [{ts} · {source} · {kind}] {text}\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(entry)
    _rotate(cfg, p)
    return True


def write_tools_raw(cfg: ToolsConfig, text: str) -> bool:
    p = tools_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = text or ""
    if not text.startswith("#"):
        text = _HEADER + "\n" + text
    p.write_text(text, encoding="utf-8")
    return True


def reset_to_seed(cfg: ToolsConfig) -> bool:
    """把 tools.md 重置为初始 seed（清掉所有学习到的条目）。"""
    p = tools_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_HEADER + "\n" + _SEED_BODY, encoding="utf-8")
    return True


def _rotate(cfg: ToolsConfig, p: Path) -> None:
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
    # 优先保留 [seed · ...] 条目，再按时间顺序丢早期非 seed 条目。
    seed_idxs = [i for i in entry_idxs if "· seed " in lines[i] or "[seed " in lines[i]]
    learned_idxs = [i for i in entry_idxs if i not in seed_idxs]
    keep = max(0, cfg.max_entries - len(seed_idxs))
    drop_count = max(0, len(learned_idxs) - keep)
    drop_set = set(learned_idxs[:drop_count])
    new_lines = [ln for i, ln in enumerate(lines) if i not in drop_set]
    p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
