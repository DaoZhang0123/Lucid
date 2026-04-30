"""GitHub Copilot OAuth device-code flow + token manager.

Two-step token model:

1. **GitHub OAuth token** (long-lived, stored)
   - Obtain via the public Copilot CLI client_id `Iv1.b507a08c87ecfe98`
     and `https://github.com/login/device/code`.
   - User visits `verification_uri`, enters `user_code`.
   - We poll `https://github.com/login/oauth/access_token` until success.
   - Saved to `state_file` (default `%LOCALAPPDATA%\\dev.ctrlapp\\copilot.json`).

2. **Copilot session token** (short-lived ~30 min, refreshed on demand)
   - `GET https://api.github.com/copilot_internal/v2/token` with the GitHub
     OAuth token as Bearer + IDE headers.
   - Response: `{token, expires_at}`. The token is a semicolon-encoded blob
     containing `proxy-ep=proxy.<region>.githubcopilot.com`. We derive the
     OpenAI base URL by replacing `proxy.` with `api.`.

Public API (for `loop.Agent` / sidecar RPC):

  m = CopilotTokenManager(state_file_path)
  m.status()                  -> {"logged_in": bool, "github_user"?, "expires_at"?}
  m.begin_login()             -> {"device_code","user_code","verification_uri",
                                  "interval","expires_in"}
  m.poll_login(device_code)   -> {"status":"pending"|"slow_down"|"ok"|"error",
                                  "error"?: str}
  m.logout()                  -> None
  m.get_active()              -> (copilot_token, base_url)   # raises if logged out
"""
from __future__ import annotations

import json
import os
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

try:
    import truststore  # 用 OS 系统证书 (Windows SChannel / macOS Keychain)，
                       # 兼容公司代理 / 杀毒软件做的 TLS 拦截。
    _SSL_CTX: ssl.SSLContext | None = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except Exception:  # pragma: no cover - truststore 不可用就退回 certifi
    _SSL_CTX = None


def _detect_system_proxy() -> str | None:
    """读取 Windows IE 设置里的代理（Clash/V2Ray/SSR 等都会写到这里）。

    返回 ``http://host:port`` 或 ``None``。环境变量 (``HTTPS_PROXY``/``HTTP_PROXY``)
    优先级更高，由 httpx ``trust_env`` 自动处理。
    """
    if os.name != "nt":
        return None
    if os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") \
            or os.environ.get("https_proxy") or os.environ.get("http_proxy"):
        return None
    try:
        import winreg  # type: ignore[import-not-found]
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
            if not enable:
                return None
            server, _ = winreg.QueryValueEx(k, "ProxyServer")
    except OSError:
        return None
    if not server:
        return None
    # 形如 "127.0.0.1:7890" 或 "http=127.0.0.1:7890;https=127.0.0.1:7890"
    https = None
    http_ = None
    for chunk in str(server).split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            scheme, addr = chunk.split("=", 1)
            scheme = scheme.strip().lower()
            if scheme == "https":
                https = addr.strip()
            elif scheme == "http":
                http_ = addr.strip()
        else:
            https = https or chunk
            http_ = http_ or chunk
    chosen = https or http_
    if not chosen:
        return None
    if "://" not in chosen:
        chosen = "http://" + chosen
    return chosen


_SYSTEM_PROXY = _detect_system_proxy()

# 公开的 GitHub Copilot CLI client_id；和 openclaw、copilot.vim、Aider 等使用的相同
_CLIENT_ID = "Iv1.b507a08c87ecfe98"
_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
_COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
_DEFAULT_BASE_URL = "https://api.individual.githubcopilot.com"

# token 提前 5 分钟视为快过期，触发刷新
_REFRESH_MARGIN_SECONDS = 5 * 60

_IDE_HEADERS = {
    "Editor-Version": "vscode/1.96.2",
    "Editor-Plugin-Version": "copilot-chat/0.35.0",
    "User-Agent": "GitHubCopilotChat/0.26.7",
    "X-Github-Api-Version": "2025-04-01",
}


# ----------------------------- helpers -----------------------------

def _client(timeout: float, *, proxy: str | None | bool = False) -> httpx.Client:
    """构造 httpx 客户端。

    proxy=False  → 默认：用 _SYSTEM_PROXY（自动从注册表/环境变量探测）
    proxy=None   → 显式禁用代理（用于失败时直连兜底）
    proxy="http://..."  → 显式指定
    """
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "trust_env": True,
        "verify": _SSL_CTX if _SSL_CTX is not None else True,
    }
    if proxy is False:
        if _SYSTEM_PROXY:
            kwargs["proxy"] = _SYSTEM_PROXY
    elif proxy is None:
        kwargs["trust_env"] = False  # 同时屏蔽 HTTPS_PROXY 环境变量
    else:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


def _request_with_fallback(method: str, url: str, *, headers: dict[str, str],
                            data: dict[str, str] | None, timeout: float) -> tuple[int, dict[str, Any]]:
    """带重试 + 代理兜底的 HTTP 调用。

    顺序：1) 用系统代理，重试 2 次；2) 直连，重试 1 次。
    任何一次拿到 HTTP 响应（含 4xx/5xx）就立刻返回——只在网络层异常时才回退。
    """
    last_err: str = ""
    attempts: list[tuple[str, str | None | bool]] = [
        ("system_proxy", False),
        ("system_proxy", False),
        ("direct", None),
    ]
    for label, proxy_arg in attempts:
        try:
            with _client(timeout, proxy=proxy_arg) as cli:
                if method == "POST":
                    resp = cli.post(url, data=data, headers=headers)
                else:
                    resp = cli.get(url, headers=headers)
        except httpx.HTTPError as e:
            last_err = f"{label}: {type(e).__name__}: {e}"
            continue
        raw = resp.text or ""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"error": raw}
        return resp.status_code, body
    hint = ""
    if _SYSTEM_PROXY:
        hint = f"（已尝试系统代理 {_SYSTEM_PROXY} + 直连均失败；请检查代理是否在线、能否访问 github.com）"
    else:
        hint = "（无系统代理；如在中国大陆，请打开 V2Ray/Clash 等代理后重试）"
    return 0, {"error": f"network: {last_err} {hint}"}


def _http_post_form(url: str, fields: dict[str, str], *,
                    headers: dict[str, str] | None = None,
                    timeout: float = 20.0) -> tuple[int, dict[str, Any]]:
    h = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    if headers:
        h.update(headers)
    return _request_with_fallback("POST", url, headers=h, data=fields, timeout=timeout)


def _http_get_json(url: str, *, headers: dict[str, str], timeout: float = 20.0) -> tuple[int, dict[str, Any]]:
    h = {"Accept": "application/json", **headers}
    return _request_with_fallback("GET", url, headers=h, data=None, timeout=timeout)


def _derive_base_url(token: str) -> str:
    """`proxy-ep=proxy.<region>.githubcopilot.com` → https://api.<region>.githubcopilot.com"""
    for chunk in token.split(";"):
        chunk = chunk.strip()
        if chunk.startswith("proxy-ep="):
            host = chunk[len("proxy-ep="):].strip()
            if host.lower().startswith(("http://", "https://")):
                # 已带 scheme
                tail = host.split("//", 1)[1]
                host = tail
            host = host.replace("proxy.", "api.", 1)
            return f"https://{host}"
    return _DEFAULT_BASE_URL


def _default_state_file() -> Path:
    """`%LOCALAPPDATA%\\dev.ctrlapp\\copilot.json` on Windows，其他平台用 ~/.ctrlapp/copilot.json"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "dev.ctrlapp" / "copilot.json"
    return Path.home() / ".ctrlapp" / "copilot.json"


# ----------------------------- state -----------------------------

@dataclass
class _State:
    github_token: str = ""
    github_user: str = ""           # 可选，仅展示用
    copilot_token: str = ""
    copilot_expires_at: int = 0      # unix seconds

    def to_json(self) -> dict[str, Any]:
        return {
            "github_token": self.github_token,
            "github_user": self.github_user,
            "copilot_token": self.copilot_token,
            "copilot_expires_at": int(self.copilot_expires_at),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> _State:
        s = cls()
        s.github_token = str(raw.get("github_token") or "")
        s.github_user = str(raw.get("github_user") or "")
        s.copilot_token = str(raw.get("copilot_token") or "")
        try:
            s.copilot_expires_at = int(raw.get("copilot_expires_at") or 0)
        except (TypeError, ValueError):
            s.copilot_expires_at = 0
        return s


# ----------------------------- in-flight device flow -----------------------------

@dataclass
class _PendingDevice:
    device_code: str
    user_code: str
    interval_seconds: int
    expires_at: float  # epoch seconds


# ----------------------------- public manager -----------------------------

class CopilotAuthError(RuntimeError):
    """Copilot 登录或换 token 失败。"""


class CopilotTokenManager:
    def __init__(self, state_file: str | Path | None = None) -> None:
        self._state_path: Path = Path(state_file) if state_file else _default_state_file()
        self._state: _State = self._load()
        self._pending: _PendingDevice | None = None

    # ------------------ persistence ------------------

    def _load(self) -> _State:
        try:
            if self._state_path.is_file():
                with open(self._state_path, "r", encoding="utf-8") as f:
                    return _State.from_json(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass
        return _State()

    def _save(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state.to_json(), f, indent=2)
            os.replace(tmp, self._state_path)
            # 收紧权限到 owner-only（POSIX；Windows 上 NTFS ACL 由系统继承）
            if os.name != "nt":
                try:
                    os.chmod(self._state_path, 0o600)
                except OSError:
                    pass
        except OSError as e:
            raise CopilotAuthError(f"写入 {self._state_path} 失败：{e}")

    # ------------------ public API ------------------

    def status(self) -> dict[str, Any]:
        s = self._state
        return {
            "logged_in": bool(s.github_token),
            "github_user": s.github_user or None,
            "copilot_expires_at": s.copilot_expires_at or None,
            "state_file": str(self._state_path),
        }

    def logout(self) -> None:
        self._state = _State()
        self._pending = None
        try:
            if self._state_path.is_file():
                self._state_path.unlink()
        except OSError:
            pass

    def begin_login(self, scope: str = "read:user") -> dict[str, Any]:
        code, body = _http_post_form(_DEVICE_CODE_URL, {"client_id": _CLIENT_ID, "scope": scope})
        if code != 200 or "device_code" not in body:
            raise CopilotAuthError(f"GitHub device code 失败 (HTTP {code}): {body}")
        interval = max(1, int(body.get("interval") or 5))
        expires_in = int(body.get("expires_in") or 900)
        self._pending = _PendingDevice(
            device_code=str(body["device_code"]),
            user_code=str(body.get("user_code") or ""),
            interval_seconds=interval,
            expires_at=time.time() + expires_in,
        )
        return {
            "device_code": self._pending.device_code,
            "user_code": self._pending.user_code,
            "verification_uri": body.get("verification_uri") or "https://github.com/login/device",
            "interval": interval,
            "expires_in": expires_in,
        }

    def poll_login(self, device_code: str | None = None) -> dict[str, Any]:
        """轮询授权结果。返回 status: pending|slow_down|ok|error。

        UI 应每隔 ``interval`` 秒调一次，直到 status != "pending" 为止。
        """
        pending = self._pending
        if pending is None:
            return {"status": "error", "error": "no pending login; call begin_login first"}
        if device_code and device_code != pending.device_code:
            return {"status": "error", "error": "device_code mismatch"}
        if time.time() >= pending.expires_at:
            self._pending = None
            return {"status": "error", "error": "device code expired; restart login"}
        code, body = _http_post_form(_ACCESS_TOKEN_URL, {
            "client_id": _CLIENT_ID,
            "device_code": pending.device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        if code == 200 and isinstance(body.get("access_token"), str):
            access_token = str(body["access_token"]).strip()
            if not access_token:
                return {"status": "error", "error": "empty access_token"}
            self._state = _State(github_token=access_token)
            self._pending = None
            try:
                self._fetch_github_user()
                # eager-fetch Copilot token so we fail fast if entitlement is missing
                self._refresh_copilot_token()
            except CopilotAuthError as e:
                self._save()
                return {"status": "error", "error": str(e)}
            self._save()
            return {"status": "ok"}
        err = body.get("error") or "unknown"
        if err == "authorization_pending":
            return {"status": "pending"}
        if err == "slow_down":
            return {"status": "slow_down"}
        if err == "expired_token":
            self._pending = None
            return {"status": "error", "error": "device code expired; restart login"}
        if err == "access_denied":
            self._pending = None
            return {"status": "error", "error": "access denied"}
        return {"status": "error", "error": str(err)}

    def get_active(self) -> tuple[str, str]:
        """拿一个仍在有效期内的 Copilot token + 它对应的 base_url。需要时刷新。"""
        if not self._state.github_token:
            raise CopilotAuthError(
                "未登录 GitHub Copilot。请到设置页点 “登录 GitHub Copilot”。"
            )
        if (
            self._state.copilot_token
            and self._state.copilot_expires_at - int(time.time()) > _REFRESH_MARGIN_SECONDS
        ):
            return self._state.copilot_token, _derive_base_url(self._state.copilot_token)
        self._refresh_copilot_token()
        return self._state.copilot_token, _derive_base_url(self._state.copilot_token)

    # ------------------ internals ------------------

    def _fetch_github_user(self) -> None:
        if not self._state.github_token:
            return
        code, body = _http_get_json(
            "https://api.github.com/user",
            headers={
                "Authorization": f"token {self._state.github_token}",
                "User-Agent": _IDE_HEADERS["User-Agent"],
            },
        )
        if code == 200 and isinstance(body.get("login"), str):
            self._state.github_user = body["login"]
        # 401/403 → 留空 github_user；真正的错误会在换 Copilot token 时再抛

    def _refresh_copilot_token(self) -> None:
        if not self._state.github_token:
            raise CopilotAuthError("缺少 GitHub OAuth token；请重新登录")
        code, body = _http_get_json(
            _COPILOT_TOKEN_URL,
            headers={
                "Authorization": f"Bearer {self._state.github_token}",
                **_IDE_HEADERS,
            },
        )
        if code == 401 or code == 403:
            raise CopilotAuthError(
                f"GitHub 账号无 Copilot 订阅或 token 已被吊销 (HTTP {code})。"
            )
        if code != 200 or "token" not in body:
            raise CopilotAuthError(f"换 Copilot token 失败 (HTTP {code}): {body}")
        token = str(body["token"]).strip()
        # GitHub 返回的是秒级 unix 时间；防御性兼容毫秒
        raw_exp = body.get("expires_at")
        try:
            exp = int(raw_exp)  # type: ignore[arg-type]
            if exp > 100_000_000_000:
                exp //= 1000
        except (TypeError, ValueError):
            exp = int(time.time()) + 25 * 60  # 兜底 25 分钟
        self._state.copilot_token = token
        self._state.copilot_expires_at = exp
        self._save()


__all__ = ["CopilotTokenManager", "CopilotAuthError"]
