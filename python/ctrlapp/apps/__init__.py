"""Per-app configuration registry —— 每个 App 一个文件，热插拔。

每个 ``ctrlapp.apps.<slug>`` 模块约定导出：

* ``SLUG``: str            —— ASCII 小写、`-` 分隔的标识。也是文件名。
* ``TITLE``: str           —— 人类可读的名字（用作 tips.md 的 ``# Title``）
* ``TIPS``: str            —— Markdown body（``- [seed · ...] ...`` 行）
* ``LAUNCHER``: dict | None —— launchers.py 里那种 spec dict；没有就置 None

加新 App = 在这个目录里 drop 一个新 ``<slug>.py``。``discover_apps()`` 会自动发现。

运行时（用户级）的覆盖仍然分别存：
* tips body  → ``<user data>/apps/<slug>/tips.md``（用户编辑 / agent learn_tip）
* launcher   → ``<user data>/launchers.json``（agent update_launcher / 前端 UI）
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AppDef:
    slug: str
    title: str
    tips: str
    launcher: dict[str, Any] | None
    # Other app slugs whose tips should be auto-appended whenever this app's
    # tips are loaded (e.g. ``edge`` → ``["browser"]`` so cross-browser
    # shortcuts always come along when launch_app("edge") is called).
    includes: tuple[str, ...] = ()


_CACHE: dict[str, AppDef] | None = None


def discover_apps() -> dict[str, AppDef]:
    """Scan this package for app modules and return ``{slug: AppDef}``.

    Cached on first call. Call ``reload()`` to re-scan during development.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    out: dict[str, AppDef] = {}
    for _finder, name, _ispkg in pkgutil.iter_modules(__path__):
        if name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
        except Exception:  # pragma: no cover — broken module shouldn't kill startup
            continue
        slug = getattr(mod, "SLUG", None) or name
        title = getattr(mod, "TITLE", None) or slug
        tips = getattr(mod, "TIPS", "") or ""
        launcher = getattr(mod, "LAUNCHER", None)
        if launcher is not None and not isinstance(launcher, dict):
            launcher = None
        raw_inc = getattr(mod, "INCLUDES", ()) or ()
        if isinstance(raw_inc, str):
            raw_inc = (raw_inc,)
        includes = tuple(s for s in raw_inc if isinstance(s, str) and s)
        out[slug] = AppDef(slug=slug, title=title, tips=tips, launcher=launcher, includes=includes)
    _CACHE = out
    return out


def reload() -> dict[str, AppDef]:
    global _CACHE
    _CACHE = None
    return discover_apps()


def get_app(slug: str) -> AppDef | None:
    return discover_apps().get(slug)


def all_tips_seeds() -> dict[str, tuple[str, str]]:
    """``{slug: (title, tips_body)}`` for every registered app."""
    return {a.slug: (a.title, a.tips) for a in discover_apps().values()}


def all_launchers() -> dict[str, dict[str, Any]]:
    """``{slug: launcher_spec}`` for every app that has one."""
    return {a.slug: a.launcher for a in discover_apps().values() if a.launcher}
