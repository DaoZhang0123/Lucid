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

_HEADER = "# ctrlapp Operation Tips Library\n"

# Initial seed — mirrors the original "common tips" section that used to live in
# loop.SYSTEM_PROMPT. Each line is one tip, prefixed with a tag for grep-ability.
_SEED_BODY = """\
- [seed · keyboard] Prefer keyboard shortcuts over mouse clicks; screenshot coordinates aren't always exact, so anything you can do via the keyboard, do via the keyboard.
- [seed · keyboard] Switch / browse open windows with alt+tab / alt+shift+tab / win+tab; do NOT switch windows by clicking taskbar icons.
- [seed · browser] Browser: new window ctrl+n, new tab ctrl+t, close tab ctrl+w, address bar ctrl+l or alt+d, back/forward alt+left/right, refresh f5.
- [seed · browser] Don't click the '+' in the top-right corner to open a new tab — use ctrl+t, it's more reliable.
- [seed · search] Default search engine is bing.com. Fastest path: win+r to open Run, type `https://bing.com/search?q=keywords` and Enter —
  Windows opens the result page in the default browser (no need to launch the browser first, no impact on the user's existing tabs).
  Alternative: in the browser ctrl+t for a new tab, type `bing.com` Enter, then type the keywords.
  Only switch to Google / Baidu / etc. when the user explicitly asks.
- [seed · text-edit] Select all ctrl+a, copy ctrl+c, paste ctrl+v, undo ctrl+z, save ctrl+s, find ctrl+f.
- [seed · system] Run dialog win+r, File Explorer win+e, show desktop win+d; for Notepad use win+r then type notepad and Enter.
- [seed · window] Window layout: win+left/right snap to half, win+up maximise, win+down minimise / restore, win+m minimise all.
- [seed · type-text] Use action="type" + text="..." for text input; the local driver pastes via clipboard, works for CJK / paths / English alike.
- [seed · don't-overwrite] Before any 'open / new / save', assume the user already has work in progress; opening a fresh instance is always safer than overwriting.
- [seed · don't-overwrite] Writing a doc: win+r -> notepad Enter to open a **new** Notepad; do NOT type into an already-open Notepad.
- [seed · don't-overwrite] Word/WPS: ctrl+n for a new document, do NOT overwrite an already-open document.
- [seed · don't-overwrite] Browser: open a new page with ctrl+n or ctrl+t; do NOT navigate the current tab via the address bar, it loses the user's current page.
- [seed · don't-overwrite] When saving, if the dialog's default filename points at an existing file, ctrl+a select-all and type a new explicit name (a timestamp helps).
- [seed · don't-overwrite] Before closing any window/tab, confirm it's one you opened yourself; never close the user's pre-existing windows.
- [seed · launch-app] Before using an App, alt+tab and screenshot to see what's already open; if it's running, ctrl+n a new window to continue work — don't take over the user's existing window;
  only launch via win+r / start menu when it's not open at all.
- [seed · launch-app] **Reliable order to decide 'is it already running'**: (1) check the **taskbar at the bottom of the screen** (the horizontal bar with icons of open windows; the system tray on the right has the small icons of resident background Apps);
  (2) when unsure, alt+tab once and look at the thumbnail list; (3) **only when both above show nothing**, double-click the desktop icon / win+r to launch.
  Never blindly double-click a desktop icon — that often relaunches or focuses the wrong window. Resident apps like WeChat, QQ, Steam, NetEase Cloud Music are almost always running in the system tray on the right side of the taskbar.
- [seed · launch-app · wechat] WeChat lives in the system tray (the green chat-bubble icon among the small icons on the **right of the bottom taskbar**). To open the main window: right-click the tray icon -> "Show main panel",
  or just left-click the tray icon. **Do NOT** double-click the desktop WeChat.exe icon — that just spawns a duplicate which is usually rejected by the running instance.
- [seed · save-dialog] Prefer typing a full absolute path into the filename field and pressing Enter; or click the address bar (path breadcrumb) at the top of the dialog and type a path then Enter;
  do NOT navigate by clicking through the 'Quick access / This PC' tree on the left.
- [seed · paths] Desktop path: %USERPROFILE%\\Desktop or C:\\Users\\<user>\\Desktop, can be typed directly as an absolute path.
- [seed · dialog] When a filename field already has a default value (e.g. *.txt), ctrl+a select-all first then type the new path, to avoid concatenation errors.
- [seed · screenshot] Before clicking small buttons / icons, take an L2 active_window or L3 cursor_local screenshot to see clearly and avoid misclicks.
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
    return "\n## Operation tips (dynamically learned; use learn_tip to add new ones when you discover them)\n" + body.strip() + "\n"


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
