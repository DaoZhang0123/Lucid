"""扫描 Windows 已安装应用的图标，沉淀到 ``<user_data>/launcher_icons/``。

来源（按优先级，先到先用，相同 target 去重）：

1. **Start Menu .lnk** —— ``%ProgramData%\\Microsoft\\Windows\\Start Menu\\Programs\\**`` +
   ``%AppData%\\Microsoft\\Windows\\Start Menu\\Programs\\**``。这是 Windows 视为
   "已安装应用"的权威清单。用 ``win32com.client.Dispatch("WScript.Shell")``
   解 .lnk，拿到 ``TargetPath`` 与 ``IconLocation``。
2. **Uninstall 注册表** —— ``HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*``
   + WOW6432 + HKCU；优先 ``DisplayIcon``，回退 ``InstallLocation``。

图标提取走 ``shell32!PrivateExtractIconsW``，请求 256x256，落到 PNG。

输出：

* ``index.json`` —— ``[{key, name, target, icon_source, file, source, w, h, ms}, ...]``
* ``<key>.png`` —— 一张图标一文件（key = sha1(target)[:12]，target 为标准化绝对路径）

设计取舍：

* 不打算扫 Microsoft Store / UWP（结构不同，单独通道）。
* 不入 ``icon_memory``（那里是应用内控件图标，混入会污染模板匹配）。
* 全量重写：每次 scan 清掉旧的，重新写。规模 ~几百条，开销可接受。
* 跨进程互斥：``index.json`` 落盘前先写 ``.tmp`` 再 rename，写期间读到旧版无碍。
* 仅 Windows；其他平台 ``run_full_scan`` 直接返回 ``{"skipped": "non-windows"}``。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable

from .config import Config

_log = logging.getLogger("lucid.launcher_icons")
# Layout: <user_data>/icons/launchers/{index.json, <key>_<safe_name>.png}
# 与 icon_memory 的 icons/atlas/ 同在 icons/ 下，便于用户管理。
_PARENT = "icons"
_BASENAME = "launchers"
_ICON_SIZE = 256


def _user_data_dir() -> Path:
    return Path.home() / ".lucid"


def store_dir(_cfg: Config) -> Path:
    return _user_data_dir() / _PARENT / _BASENAME


def _index_path(cfg: Config) -> Path:
    return store_dir(cfg) / "index.json"


# ---------------- index ----------------

def list_icons(cfg: Config) -> list[dict[str, Any]]:
    p = _index_path(cfg)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return [it for it in data["items"] if isinstance(it, dict)]
    except Exception as exc:  # pragma: no cover - corrupted file
        _log.warning("launcher_icons index unreadable: %s", exc)
    return []


def read_png(cfg: Config, key: str) -> bytes | None:
    items = list_icons(cfg)
    target = next((it for it in items if it.get("key") == key), None)
    if target is None:
        return None
    fname = target.get("file") or ""
    if not fname:
        return None
    p = store_dir(cfg) / fname
    if not p.exists():
        return None
    try:
        return p.read_bytes()
    except Exception:
        return None


def list_installed_apps(cfg: Config) -> list[dict[str, Any]]:
    """Return ``[{key, name, file, png_bytes}, ...]`` ordered the same way as
    ``<user_data>/icons/atlas.txt``. Items missing from atlas.txt are appended
    at the end in index.json order (so newly-scanned apps still surface).
    Apps without a readable PNG are still returned with ``png_bytes=b""``."""
    items = list_icons(cfg)
    by_name: dict[str, dict[str, Any]] = {}
    for it in items:
        name = (it.get("name") or "").strip()
        if name and name not in by_name:
            by_name[name] = it

    ordered_names: list[str] = []
    seen: set[str] = set()
    _, atlas_txt = _atlas_cache_paths(cfg)
    if atlas_txt.is_file():
        try:
            for raw in atlas_txt.read_text("utf-8").splitlines():
                # format: "[N] App name"
                line = raw.strip()
                if not line or not line.startswith("["):
                    continue
                _, _, rest = line.partition("]")
                nm = rest.strip()
                if nm and nm not in seen:
                    seen.add(nm)
                    ordered_names.append(nm)
        except OSError:
            pass

    for it in items:
        nm = (it.get("name") or "").strip()
        if nm and nm not in seen:
            seen.add(nm)
            ordered_names.append(nm)

    out: list[dict[str, Any]] = []
    base = store_dir(cfg)
    for nm in ordered_names:
        it = by_name.get(nm)
        if not it:
            continue
        fname = (it.get("file") or "").strip()
        png_bytes = b""
        if fname:
            p = base / fname
            if p.is_file():
                try:
                    png_bytes = p.read_bytes()
                except OSError:
                    png_bytes = b""
        out.append({
            "key": it.get("key") or "",
            "name": nm,
            "file": fname,
            "png_bytes": png_bytes,
        })
    return out


# ---------------- enumeration ----------------

def _start_menu_dirs() -> list[Path]:
    out: list[Path] = []
    pd = os.environ.get("ProgramData")
    if pd:
        out.append(Path(pd) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    ad = os.environ.get("APPDATA")
    if ad:
        out.append(Path(ad) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [p for p in out if p.exists()]


def _iter_lnks() -> Iterable[Path]:
    for root in _start_menu_dirs():
        for p in root.rglob("*.lnk"):
            try:
                if p.is_file():
                    yield p
            except OSError:
                continue


def _resolve_lnk(path: Path) -> tuple[str, str, int] | None:
    """返回 (target_path, icon_path, icon_index) 或 None。

    target_path 可能为空（指向 ms-store URI 之类的非文件 link），跳过这种。
    """
    try:
        import pythoncom  # noqa: F401 - required by win32com on first call
        from win32com.client import Dispatch  # type: ignore
    except Exception:  # pragma: no cover - pywin32 missing
        return None
    try:
        sh = Dispatch("WScript.Shell")
        sc = sh.CreateShortcut(str(path))
        target = (sc.TargetPath or "").strip()
        ico_loc = (sc.IconLocation or "").strip()
        ico_path = ""
        ico_idx = 0
        if ico_loc:
            # IconLocation 形如 "C:\\foo\\bar.dll, 7" 或 "C:\\foo\\bar.exe, 0"
            if "," in ico_loc:
                left, right = ico_loc.rsplit(",", 1)
                ico_path = left.strip().strip('"')
                try:
                    ico_idx = int(right.strip())
                except ValueError:
                    ico_idx = 0
            else:
                ico_path = ico_loc.strip().strip('"')
        return (target, ico_path, ico_idx)
    except Exception as exc:
        _log.debug("lnk parse failed %s: %s", path, exc)
        return None


def _iter_uninstall() -> Iterable[tuple[str, str]]:
    """yield (display_name, icon_or_install_path)。"""
    if os.name != "nt":
        return
    try:
        import winreg  # type: ignore
    except Exception:  # pragma: no cover
        return
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, sub in roots:
        try:
            with winreg.OpenKey(hive, sub) as parent:
                idx = 0
                while True:
                    try:
                        child = winreg.EnumKey(parent, idx)
                    except OSError:
                        break
                    idx += 1
                    try:
                        with winreg.OpenKey(parent, child) as k:
                            try:
                                name = winreg.QueryValueEx(k, "DisplayName")[0]
                            except FileNotFoundError:
                                continue
                            ico = ""
                            for vname in ("DisplayIcon", "InstallLocation"):
                                try:
                                    ico = winreg.QueryValueEx(k, vname)[0] or ""
                                except FileNotFoundError:
                                    continue
                                if ico:
                                    break
                            if name and ico:
                                yield (str(name).strip(), str(ico).strip())
                    except OSError:
                        continue
        except OSError:
            continue


# ---------------- icon extraction ----------------
#
# 走 ``win32api.ExtractIconEx`` 拿大图标（默认 SM_CXICON=32），再按需缩放写 PNG。
# 历史上也可以走 ``shell32!PrivateExtractIconsW`` 直接指定尺寸，但部分 Windows
# 构建上 ctypes 拿不到这个导出名（按序号导出），不如 ExtractIconEx 稳。
# 想要超大图标的话日后改成 SHGetImageList(SHIL_JUMBO) 就行。


def _hicon_to_png(hicon: int, size: int) -> bytes | None:
    try:
        import win32gui  # type: ignore
        import win32ui  # type: ignore
        from PIL import Image
    except Exception:  # pragma: no cover
        return None
    hdc_screen = win32gui.GetDC(0)
    try:
        hdc = win32ui.CreateDCFromHandle(hdc_screen)
        hbmp = win32ui.CreateBitmap()
        hbmp.CreateCompatibleBitmap(hdc, size, size)
        mem_dc = hdc.CreateCompatibleDC()
        old = mem_dc.SelectObject(hbmp)
        try:
            # 透明背景：先填 0；DrawIconEx 会按 alpha 合成。
            win32gui.DrawIconEx(mem_dc.GetSafeHdc(), 0, 0, hicon, size, size, 0, 0, 0x0003)
            bmpstr = hbmp.GetBitmapBits(True)
            img = Image.frombuffer("RGBA", (size, size), bmpstr, "raw", "BGRA", 0, 1)
            # 全透明 → 视为提取失败（DrawIconEx 偶尔在某些 mask-only 图标上输出全 0）。
            if not img.getbbox():
                return None
            from io import BytesIO
            buf = BytesIO()
            img.save(buf, "PNG", optimize=True)
            return buf.getvalue()
        finally:
            mem_dc.SelectObject(old)
            mem_dc.DeleteDC()
    finally:
        try:
            win32gui.ReleaseDC(0, hdc_screen)
        except Exception:
            pass


def _extract_icon_png(icon_path: str, idx: int = 0, size: int = _ICON_SIZE) -> bytes | None:
    """从 exe / dll / ico 里取出 idx 对应的图标，渲染成 size x size 的 PNG。"""
    if os.name != "nt":
        return None
    if not icon_path:
        return None
    try:
        import win32gui  # type: ignore
    except Exception:
        return None
    try:
        large, small = win32gui.ExtractIconEx(icon_path, idx, 1)
    except Exception:
        return None
    handles = list(large) + list(small)
    if not handles:
        return None
    hicon = handles[0]
    extras = handles[1:]
    try:
        return _hicon_to_png(hicon, size)
    finally:
        for h in [hicon] + extras:
            try:
                win32gui.DestroyIcon(h)
            except Exception:
                pass


# ---------------- driver ----------------

def _normalize_target(p: str) -> str:
    if not p:
        return ""
    try:
        return str(Path(os.path.expandvars(p)).resolve()).lower()
    except Exception:
        return p.strip().lower()


def _pick_icon_source(target: str, ico_hint: str) -> tuple[str, int]:
    """挑一个最有戏的图标源 + index。"""
    # IconLocation 优先（它可能直接指向带漂亮图标的 dll/exe）
    if ico_hint:
        # IconLocation 内可能给的是 .ico 文件路径或 dll path,index
        return (ico_hint, 0)
    return (target, 0)


def _key_for(target: str) -> str:
    return hashlib.sha1(target.encode("utf-8", "replace")).hexdigest()[:12]


def _safe_name(s: str) -> str:
    out = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        elif ch in (" ", "."):
            out.append("_")
    return ("".join(out))[:80] or "app"


# ---------------- UWP / MSIX (AppX) enumeration ----------------
#
# The Start-Menu .lnk + Uninstall-registry walk above MISSES every modern
# packaged app: Microsoft Teams (work or school), WhatsApp, Photos, etc.
# Those are surfaced only via the AppX subsystem. We shell out to PowerShell
# once per scan and ask it to merge ``Get-StartApps`` (which includes both
# classic + UWP entries with their AppUserModelID) with ``Get-AppxPackage``
# (which gives us each package's InstallLocation + AppxManifest.xml so we
# can find the right Square44/150 logo asset on disk).
#
# Returns a list of dicts: {name, appid, icon_path}. ``icon_path`` is an
# absolute path to a PNG asset that PIL can open directly — no win32 icon
# extraction needed because UWP apps already ship raster icon assets.
_UWP_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$apps = Get-StartApps
$pkgs = Get-AppxPackage | Group-Object PackageFamilyName -AsHashTable -AsString
$result = New-Object System.Collections.ArrayList
foreach ($a in $apps) {
    $appid = $a.AppID
    if ($appid -notmatch '!') { continue }
    $parts = $appid.Split('!', 2)
    $pfn = $parts[0]
    $entry = $parts[1]
    if (-not $pkgs.ContainsKey($pfn)) { continue }
    $pkg = $pkgs[$pfn]
    if ($pkg -is [array]) { $pkg = $pkg[0] }
    $loc = $pkg.InstallLocation
    if (-not $loc -or -not (Test-Path -LiteralPath $loc)) { continue }
    $manifest = Join-Path $loc 'AppxManifest.xml'
    if (-not (Test-Path -LiteralPath $manifest)) { continue }
    try { [xml]$xml = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 } catch { continue }
    $ns = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
    $ns.AddNamespace('d', 'http://schemas.microsoft.com/appx/manifest/foundation/windows10')
    $ns.AddNamespace('uap', 'http://schemas.microsoft.com/appx/manifest/uap/windows10')
    $appNode = $xml.SelectSingleNode("//d:Application[@Id='$entry']", $ns)
    if (-not $appNode) { continue }
    $vis = $appNode.SelectSingleNode('uap:VisualElements', $ns)
    if (-not $vis) { continue }
    $logo = $vis.GetAttribute('Square44x44Logo')
    if (-not $logo) { $logo = $vis.GetAttribute('Square150x150Logo') }
    if (-not $logo) { $logo = $vis.GetAttribute('Logo') }
    if (-not $logo) { continue }
    $logoFull = Join-Path $loc $logo
    $logoDir  = [IO.Path]::GetDirectoryName($logoFull)
    $logoBase = [IO.Path]::GetFileNameWithoutExtension($logoFull)
    $logoExt  = [IO.Path]::GetExtension($logoFull)
    if (-not (Test-Path -LiteralPath $logoDir)) { continue }
    # Prefer larger, plated, scale-200 variants. Pick file with biggest size.
    $best = $null; $bestSize = 0
    Get-ChildItem -LiteralPath $logoDir -Filter "$logoBase*$logoExt" -File -ErrorAction SilentlyContinue | ForEach-Object {
        # skip alt forms intended for white-on-black (unplated)
        if ($_.Name -match 'altform-unplated|contrast-(white|black)') { return }
        if ($_.Length -gt $bestSize) { $bestSize = $_.Length; $best = $_.FullName }
    }
    if (-not $best -and (Test-Path -LiteralPath $logoFull)) { $best = $logoFull }
    if (-not $best) { continue }
    [void]$result.Add([pscustomobject]@{ Name = $a.Name; AppID = $appid; IconPath = $best })
}
$result | ConvertTo-Json -Compress -Depth 3
"""


def _iter_uwp_apps() -> list[dict[str, str]]:
    """Enumerate UWP/MSIX-packaged apps via PowerShell. Empty list on failure."""
    if os.name != "nt":
        return []
    import subprocess
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", _UWP_PS_SCRIPT],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        _log.warning("uwp enumeration failed: %s", exc)
        return []
    if proc.returncode != 0:
        _log.warning("uwp ps returncode=%s stderr=%s", proc.returncode, proc.stderr[:300])
        return []
    out = (proc.stdout or "").strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except Exception as exc:
        _log.warning("uwp ps json parse failed: %s; head=%r", exc, out[:200])
        return []
    # ConvertTo-Json yields a single object instead of a one-element array
    # when there's exactly one match — normalise.
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    items: list[dict[str, str]] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        name = str(d.get("Name") or "").strip()
        appid = str(d.get("AppID") or "").strip()
        icon = str(d.get("IconPath") or "").strip()
        if name and appid and icon:
            items.append({"name": name, "appid": appid, "icon_path": icon})
    return items


def _load_uwp_icon_png(icon_path: str, size: int = _ICON_SIZE) -> bytes | None:
    """Open a UWP asset PNG and re-encode to ``size`` × ``size``. UWP assets
    sit on disk as PNGs already, so no win32 icon extraction needed."""
    try:
        from PIL import Image
        from io import BytesIO
        with Image.open(icon_path) as im:
            im = im.convert("RGBA")
            if im.size != (size, size):
                im = im.resize((size, size), Image.LANCZOS)
            buf = BytesIO()
            im.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
    except Exception as exc:
        _log.debug("uwp icon load failed (%s): %s", icon_path, exc)
        return None


def run_full_scan(cfg: Config) -> dict[str, Any]:
    """全量扫描；幂等地清空目录后重写。返回汇总信息（成功 / 跳过 / 失败计数）。"""
    if os.name != "nt":
        return {"skipped": "non-windows", "total": 0}

    started_ms = int(time.time() * 1000)
    out_dir = store_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 旧文件先清掉（保留 index.json，最后一次性覆盖）
    for old in out_dir.glob("*.png"):
        try:
            old.unlink()
        except OSError:
            pass

    seen_keys: set[str] = set()
    items: list[dict[str, Any]] = []
    stats = {"lnks_seen": 0, "lnk_no_target": 0, "from_lnk": 0,
             "uninstall_seen": 0, "from_uninstall": 0, "extract_failed": 0,
             "uwp_seen": 0, "from_uwp": 0, "uwp_failed": 0}

    # 1) Start Menu .lnk
    for lnk in _iter_lnks():
        stats["lnks_seen"] += 1
        resolved = _resolve_lnk(lnk)
        if not resolved:
            continue
        target, ico_path, ico_idx = resolved
        if not target or not target.lower().endswith((".exe", ".bat", ".cmd")):
            stats["lnk_no_target"] += 1
            continue
        norm = _normalize_target(target)
        if not norm or norm in seen_keys:
            continue
        ico_src, idx = _pick_icon_source(target, ico_path)
        if ico_idx:
            idx = ico_idx
        try:
            png = _extract_icon_png(ico_src, idx)
        except Exception as exc:
            _log.debug("icon extract crash for %s: %s", ico_src, exc)
            png = None
        if not png:
            # 回退：直接用 target
            try:
                png = _extract_icon_png(target, 0)
            except Exception:
                png = None
        if not png:
            stats["extract_failed"] += 1
            continue
        key = _key_for(norm)
        if key in seen_keys:
            continue
        fname = f"{key}_{_safe_name(lnk.stem)}.png"
        (out_dir / fname).write_bytes(png)
        items.append({
            "key": key,
            "name": lnk.stem,
            "target": target,
            "icon_source": ico_src,
            "icon_index": idx,
            "file": fname,
            "source": "start_menu",
            "lnk": str(lnk),
            "w": _ICON_SIZE,
            "h": _ICON_SIZE,
            "ms": int(time.time() * 1000),
        })
        seen_keys.add(key)
        stats["from_lnk"] += 1

    # 2) Uninstall 注册表
    for name, raw in _iter_uninstall():
        stats["uninstall_seen"] += 1
        # raw 可能是 "C:\\foo\\bar.exe,0" 或 "C:\\foo\\install_dir"
        ico_src = raw
        idx = 0
        if "," in raw and raw.lower().endswith((".exe", ".dll", ".ico", "0", "1", "2", "3", "4", "5")):
            left, right = raw.rsplit(",", 1)
            ico_src = left.strip().strip('"')
            try:
                idx = int(right.strip())
            except ValueError:
                idx = 0
        ico_src = os.path.expandvars(ico_src)
        if os.path.isdir(ico_src):
            # 目录 → 找一个 exe
            cand = next((str(p) for p in Path(ico_src).glob("*.exe")), "")
            if not cand:
                continue
            ico_src = cand
        if not Path(ico_src).exists():
            continue
        norm = _normalize_target(ico_src)
        if norm in seen_keys:
            continue
        try:
            png = _extract_icon_png(ico_src, idx)
        except Exception:
            png = None
        if not png:
            stats["extract_failed"] += 1
            continue
        key = _key_for(norm)
        if key in seen_keys:
            continue
        fname = f"{key}_{_safe_name(name)}.png"
        (out_dir / fname).write_bytes(png)
        items.append({
            "key": key,
            "name": name,
            "target": ico_src,
            "icon_source": ico_src,
            "icon_index": idx,
            "file": fname,
            "source": "uninstall_registry",
            "w": _ICON_SIZE,
            "h": _ICON_SIZE,
            "ms": int(time.time() * 1000),
        })
        seen_keys.add(key)
        stats["from_uninstall"] += 1

    # 3) UWP / MSIX (AppX) packaged apps — Teams (work), WhatsApp, etc.
    # These never appear as Start-Menu .lnk files. Get-StartApps + AppxManifest
    # gives us {Name, AppID, IconPath} where IconPath is a real PNG on disk.
    for entry in _iter_uwp_apps():
        stats["uwp_seen"] += 1
        appid = entry["appid"]
        norm = "uwp:" + appid.lower()
        key = _key_for(norm)
        if key in seen_keys:
            continue
        png = _load_uwp_icon_png(entry["icon_path"])
        if not png:
            stats["uwp_failed"] += 1
            continue
        fname = f"{key}_{_safe_name(entry['name'])}.png"
        (out_dir / fname).write_bytes(png)
        items.append({
            "key": key,
            "name": entry["name"],
            "target": appid,            # AppUserModelID — caller can `start shell:appsFolder\\<appid>`
            "icon_source": entry["icon_path"],
            "icon_index": 0,
            "file": fname,
            "source": "uwp",
            "w": _ICON_SIZE,
            "h": _ICON_SIZE,
            "ms": int(time.time() * 1000),
        })
        seen_keys.add(key)
        stats["from_uwp"] += 1

    # 写 index
    index = {
        "scanned_ms": started_ms,
        "duration_ms": int(time.time() * 1000) - started_ms,
        "stats": stats,
        "total": len(items),
        "items": items,
    }
    tmp = out_dir / "index.json.tmp"
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(_index_path(cfg))
    # 顺手把 atlas 大图缓存到 <user_data>/icons/atlas.png，省得每次 prompt 都重组。
    _write_atlas_cache(cfg, items)
    return {"total": len(items), "stats": stats,
            "duration_ms": index["duration_ms"], "dir": str(out_dir)}


__all__ = ["run_full_scan", "list_icons", "list_installed_apps", "read_png",
           "store_dir", "build_atlas", "LauncherAtlas"]


# ---------------- atlas (for taskbar_notify confirm prompt) ----------------

from dataclasses import dataclass


@dataclass
class LauncherAtlas:
    png_bytes: bytes
    width: int
    height: int
    captions: str


# 缓存路径：<user_data>/icons/atlas.png + atlas.txt（captions）。
# 父目录就是 launchers/ 的上一级，让 atlas 与 launchers/ 平级，便于人工查看。
def _atlas_cache_paths(cfg: Config) -> tuple[Path, Path]:
    parent = store_dir(cfg).parent  # = <user_data>/icons
    return parent / "atlas.png", parent / "atlas.txt"


def _try_load_font(size: int):
    from PIL import ImageFont
    # msyh.ttc (Microsoft YaHei) 放在最前，能同时覆盖中英文；缺失再回退。
    for name in ("msyh.ttc", "msyhbd.ttc", "simsun.ttc",
                 "segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _truncate_to_width(draw, text: str, font, max_w: int) -> str:
    """按像素宽度裁剪，超长用 '…' 结尾。"""
    if not text:
        return ""
    bbox = draw.textbbox((0, 0), text, font=font)
    if bbox[2] - bbox[0] <= max_w:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    # 二分找最大可放下的前缀长度
    while lo < hi:
        mid = (lo + hi + 1) // 2
        b = draw.textbbox((0, 0), text[:mid] + ell, font=font)
        if b[2] - b[0] <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ell


def _wrap_label(draw, text: str, font, max_w: int, max_lines: int = 2) -> list[str]:
    """把 label 按像素宽折行；超出 max_lines 时最后一行截断加 '…'。"""
    if not text:
        return [""]
    # 先按字符贪心累加
    lines: list[str] = []
    cur = ""
    for ch in text:
        cand = cur + ch
        bbox = draw.textbbox((0, 0), cand, font=font)
        if bbox[2] - bbox[0] <= max_w:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = ch
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) >= max_lines:
        # 把剩下的字塞进最后一行并截断
        consumed = sum(len(s) for s in lines)
        if consumed < len(text):
            lines[-1] = _truncate_to_width(draw, text[consumed - len(lines[-1]):], font, max_w)
    return lines or [""]


def _render_atlas(items: list[dict], src_dir: Path, *,
                  tile: int = 64, cols: int = 10) -> tuple[bytes, str, int, int]:
    """实际拼图函数。返回 (png_bytes, captions_text, width, height)。"""
    from PIL import Image, ImageDraw
    from io import BytesIO

    label_h = 30          # 给 2 行文字
    cell_w = 112          # 容纳常见英文 / 中文 app 名
    cell_h = tile + label_h + 8
    rows = (len(items) + cols - 1) // cols
    pad = 8
    img_w = pad * 2 + cols * cell_w
    img_h = pad * 2 + rows * cell_h + 24

    canvas = Image.new("RGB", (img_w, img_h), (250, 250, 250))
    draw = ImageDraw.Draw(canvas)
    title_font = _try_load_font(13)
    label_font = _try_load_font(10)
    draw.text((pad, pad), "[Launcher icons] each cell = an installed app",
              fill=(60, 60, 60), font=title_font)

    captions: list[str] = []
    for idx, it in enumerate(items):
        row = idx // cols
        col = idx % cols
        x0 = pad + col * cell_w
        y0 = pad + 22 + row * cell_h
        try:
            with Image.open(src_dir / it.get("file", "")) as src:
                src = src.convert("RGBA")
                src.thumbnail((tile, tile), Image.LANCZOS)
                ix = x0 + (cell_w - src.width) // 2
                iy = y0 + 2
                bg = Image.new("RGB", (src.width, src.height), (240, 240, 240))
                bg.paste(src, mask=src.split()[3] if src.mode == "RGBA" else None)
                canvas.paste(bg, (ix, iy))
        except Exception:
            pass
        name = it.get("name") or "?"
        text = f"[{idx + 1}] {name}"
        # 按像素宽 wrap 到最多 2 行（用 cell_w-4 留点边距）
        lines = _wrap_label(draw, text, label_font, cell_w - 4, max_lines=2)
        ty = y0 + tile + 3
        for line in lines:
            draw.text((x0 + 2, ty), line, fill=(20, 20, 20), font=label_font)
            ty += 12
        captions.append(f"[{idx + 1}] {name}")

    buf = BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), "\n".join(captions), img_w, img_h


def _write_atlas_cache(cfg: Config, items: list[dict]) -> None:
    """全量扫描结束后调用：把拼好的 atlas 落到 <user_data>/icons/atlas.{png,txt}。"""
    if not items:
        # 没图标就清掉旧缓存
        png_path, txt_path = _atlas_cache_paths(cfg)
        for p in (png_path, txt_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        return
    src_dir = store_dir(cfg)
    try:
        png_bytes, captions, _w, _h = _render_atlas(items[:80], src_dir)
    except Exception as exc:
        _log.warning("atlas render failed: %s", exc)
        return
    png_path, txt_path = _atlas_cache_paths(cfg)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = png_path.with_suffix(".png.tmp")
    tmp.write_bytes(png_bytes)
    tmp.replace(png_path)
    txt_path.write_text(captions, "utf-8")


def build_atlas(cfg: Config, *, max_items: int = 80,
                names: list[str] | None = None) -> "LauncherAtlas | None":
    """返回 launcher icons 的 atlas（一张大合集图 + captions 索引）。

    优先读 <user_data>/icons/atlas.png 缓存；若缺失或比 launcher index 旧则按需重建。
    传入 ``names`` 时只保留名字严格匹配（不区分大小写）的项，并会重新渲染
    一张临时 atlas（不进缓存）。无任何图标时返回 None。
    """
    items = list_icons(cfg)
    if not items:
        return None
    if names:
        wanted = {str(n).strip().lower() for n in names if str(n).strip()}
        if wanted:
            items = [it for it in items if str(it.get("name") or "").strip().lower() in wanted]
            if not items:
                return None
            # 白名单分支：重新渲染一张小图，不动缓存。
            src_dir = store_dir(cfg)
            try:
                png_bytes, captions, w, h = _render_atlas(items[:max_items], src_dir)
            except Exception as exc:
                _log.warning("atlas (filtered) render failed: %s", exc)
                return None
            return LauncherAtlas(png_bytes=png_bytes, width=w, height=h, captions=captions)
    items = items[:max_items]
    png_path, txt_path = _atlas_cache_paths(cfg)
    idx_path = _index_path(cfg)
    fresh = (
        png_path.is_file()
        and txt_path.is_file()
        and idx_path.is_file()
        and png_path.stat().st_mtime >= idx_path.stat().st_mtime
    )
    if fresh:
        try:
            from PIL import Image
            png_bytes = png_path.read_bytes()
            captions = txt_path.read_text("utf-8")
            with Image.open(png_path) as im:
                w, h = im.size
            return LauncherAtlas(png_bytes=png_bytes, width=w, height=h, captions=captions)
        except Exception as exc:
            _log.warning("atlas cache read failed, rebuilding: %s", exc)
    # Fallback：按需重建并缓存。
    src_dir = store_dir(cfg)
    try:
        png_bytes, captions, w, h = _render_atlas(items, src_dir)
    except Exception as exc:
        _log.warning("atlas render failed: %s", exc)
        return None
    try:
        png_path.parent.mkdir(parents=True, exist_ok=True)
        png_path.write_bytes(png_bytes)
        txt_path.write_text(captions, "utf-8")
    except OSError:
        pass
    return LauncherAtlas(png_bytes=png_bytes, width=w, height=h, captions=captions)
