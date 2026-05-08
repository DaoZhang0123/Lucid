"""Webpage reading without screenshot — two backends:

1. **Headless dump** (`dump_dom_headless`): runs ``chrome.exe --headless --dump-dom <url>``
   in a subprocess and captures the post-JS DOM as HTML. No login state, no cookies, no
   user session.  Fast (~1-3s for typical pages), works on any URL.

2. **CDP active-tab read** (`cdp_read_active_tab`): connects to a running Chrome / Edge
   that was started with ``--remote-debugging-port=9222`` and pulls the live DOM of one
   of the user's open tabs (so login state IS preserved).  Requires the browser to have
   been launched WITH the debug port — the default chrome/edge launcher specs in
   ``apps/chrome.py`` and ``apps/edge.py`` add this flag automatically.

Both return HTML; ``html_to_text`` then reduces it to a compact, readable plaintext
summary the LLM can ingest cheaply.

The WS client for CDP is inlined (~80 lines of stdlib socket code) so we don't add
``websocket-client`` to the dependency list.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Browser executable lookup
# ---------------------------------------------------------------------------

_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
_EDGE_CANDIDATES = (
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)


def find_browser_exe(browser: str = "edge") -> str | None:
    """Locate a browser executable; returns absolute path or None.

    Default backend is Edge (preinstalled on every Windows 10+/11). If the
    requested browser is missing, automatically fall back to the other one
    so a default `read_webpage(url=...)` call still works on Chrome-less
    machines (and vice versa).
    """
    browser = (browser or "edge").strip().lower()

    def _lookup(b: str) -> str | None:
        if b == "edge":
            cands = list(_EDGE_CANDIDATES)
        else:
            cands = list(_CHROME_CANDIDATES)
            local = os.environ.get("LOCALAPPDATA")
            if local:
                cands.append(str(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"))
        for p in cands:
            if p and Path(p).is_file():
                return p
        name = "msedge.exe" if b == "edge" else "chrome.exe"
        return shutil.which(name) or shutil.which(name.replace(".exe", ""))

    primary = _lookup(browser)
    if primary:
        return primary
    # Auto-fallback: try the other browser. Most Windows machines have at
    # least one of Edge / Chrome.
    other = "chrome" if browser == "edge" else "edge"
    return _lookup(other)


# ---------------------------------------------------------------------------
# HTML → text
# ---------------------------------------------------------------------------

_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if t == "title":
            self._in_title = True
            return
        if t == "br":
            self._chunks.append("\n")
            return
        if t in _HEADING_TAGS:
            self._chunks.append(f"\n\n## ")
            return
        if t == "li":
            self._chunks.append("\n- ")
            return
        if t in _BLOCK_TAGS:
            self._chunks.append("\n")
            return
        if t == "a":
            href = ""
            for k, v in attrs:
                if k.lower() == "href":
                    href = v or ""
                    break
            if href and not href.startswith(("javascript:", "#")):
                # Inline-link form; truncate long URLs
                short = href if len(href) <= 80 else href[:77] + "..."
                self._chunks.append(f"[")
                self._chunks.append("")  # text will fill
                self._pending_href = f"]({short})"
                return
        if t == "img":
            alt = ""
            for k, v in attrs:
                if k.lower() == "alt":
                    alt = (v or "").strip()
                    break
            if alt:
                self._chunks.append(f"[image: {alt}] ")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if t == "title":
            self._in_title = False
            return
        if t == "a" and getattr(self, "_pending_href", ""):
            self._chunks.append(self._pending_href)
            self._pending_href = ""
            return
        if t in _BLOCK_TAGS or t in _HEADING_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title += data
            return
        self._chunks.append(data)

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse runs of whitespace within lines, preserve blank-line boundaries
        lines = []
        for line in raw.splitlines():
            line = re.sub(r"[ \t\f\v]+", " ", line).strip()
            lines.append(line)
        # Collapse 3+ blank lines into 2
        out_lines: list[str] = []
        blank = 0
        for ln in lines:
            if not ln:
                blank += 1
                if blank <= 1:
                    out_lines.append("")
            else:
                blank = 0
                out_lines.append(ln)
        return "\n".join(out_lines).strip()


def html_to_text(html: str, max_chars: int = 8000) -> tuple[str, str]:
    """Convert HTML to readable plaintext-ish (markdown-flavoured).

    Returns ``(title, text)``.  Text is truncated to ``max_chars`` with a marker.
    """
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    text = parser.get_text()
    title = re.sub(r"\s+", " ", parser.title).strip()
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... truncated, {len(text) - max_chars} more chars]"
    return title, text


# ---------------------------------------------------------------------------
# Backend 1: headless DOM dump
# ---------------------------------------------------------------------------

def dump_dom_headless(
    url: str,
    browser: str = "edge",
    timeout_s: float = 60.0,
    extra_args: tuple[str, ...] = (),
) -> str:
    """Run ``<browser> --headless --dump-dom <url>`` and return raw HTML.

    Raises ``RuntimeError`` on browser-not-found / non-zero exit / timeout.
    """
    exe = find_browser_exe(browser)
    if not exe:
        raise RuntimeError(
            f"could not locate {browser}.exe — install Chrome/Edge or pass an existing path"
        )
    if not url or not urlparse(url).scheme:
        raise RuntimeError(f"invalid url: {url!r} (must include scheme, e.g. https://...)")
    # Use a per-call unique user-data-dir so that a foreground Edge / Chrome
    # process the user already has running can't lock our headless instance
    # (which would manifest as a silent 25s+ hang on `--dump-dom`).
    udd_root = Path(os.environ.get("TEMP", ".")) / "klawbot-headless"
    udd = udd_root / f"{browser}-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        udd.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    args = [
        exe,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-extensions",
        "--hide-scrollbars",
        "--mute-audio",
        "--disable-software-rasterizer",
        "--no-first-run",
        "--no-default-browser-check",
        "--user-data-dir=" + str(udd),
        *extra_args,
        "--dump-dom",
        url,
    ]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{browser} --headless --dump-dom timed out after {timeout_s}s") from e
    finally:
        # Best-effort cleanup of the per-call profile dir to avoid bloating
        # %TEMP% over time. Ignore failures (file still locked, etc.).
        try:
            shutil.rmtree(udd, ignore_errors=True)
        except Exception:
            pass
    if proc.returncode != 0 and not proc.stdout:
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")[-400:]
        raise RuntimeError(
            f"{browser} --headless exited {proc.returncode}; stderr tail: {stderr}"
        )
    return (proc.stdout or b"").decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Backend 2: CDP via inline minimal WebSocket client
# ---------------------------------------------------------------------------

def _cdp_list_tabs(port: int = 9222, timeout_s: float = 2.0) -> list[dict[str, Any]]:
    url = f"http://127.0.0.1:{port}/json"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected /json response shape: {type(data).__name__}")
    return data


def _cdp_pick_tab(tabs: list[dict[str, Any]], url_match: str | None = None) -> dict[str, Any]:
    pages = [t for t in tabs if t.get("type") == "page"]
    if not pages:
        raise RuntimeError("no page-type tabs found via CDP (only background pages?)")
    if url_match:
        m = url_match.lower()
        for t in pages:
            if m in (t.get("url") or "").lower() or m in (t.get("title") or "").lower():
                return t
        raise RuntimeError(
            f"no tab url/title contains {url_match!r}; "
            f"available: {[(t.get('title','?'), t.get('url','?')) for t in pages]}"
        )
    # Prefer non-extension, non-blank tabs
    for t in pages:
        u = t.get("url") or ""
        if u and not u.startswith(("chrome://", "edge://", "chrome-extension://")):
            return t
    return pages[0]


# --- Minimal WS client (RFC 6455, text frames only, server→client unmasked) ---


class _WSConn:
    """Tiny socket+buffer wrapper for our minimal WS use."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.residual = b""

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


def _ws_handshake(c: _WSConn, host: str, port: int, path: str) -> None:
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    c.sock.sendall(req.encode("ascii"))
    buf = b""
    deadline = time.monotonic() + 5.0
    while b"\r\n\r\n" not in buf:
        if time.monotonic() > deadline:
            raise RuntimeError("WS handshake timed out reading response")
        chunk = c.sock.recv(4096)
        if not chunk:
            raise RuntimeError("WS handshake: server closed connection")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    if b" 101 " not in head.split(b"\r\n", 1)[0]:
        raise RuntimeError(
            f"WS handshake failed: {head.split(b'\r\n', 1)[0].decode('ascii', 'replace')!r}"
        )
    expect = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
    ).decode("ascii")
    if expect.encode("ascii") not in head:
        raise RuntimeError("WS handshake: bad Sec-WebSocket-Accept")
    c.residual = rest


def _ws_recv_exact(c: _WSConn, n: int) -> bytes:
    buf = c.residual
    c.residual = b""
    while len(buf) < n:
        chunk = c.sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise RuntimeError("WS recv: connection closed mid-frame")
        buf += chunk
    if len(buf) > n:
        c.residual = buf[n:]
        buf = buf[:n]
    return buf


def _ws_send_text(c: _WSConn, text: str) -> None:
    payload = text.encode("utf-8")
    n = len(payload)
    header = bytearray([0x81])  # FIN=1, opcode=1 text
    mask_bit = 0x80
    if n < 126:
        header.append(mask_bit | n)
    elif n < 65536:
        header.append(mask_bit | 126)
        header += struct.pack(">H", n)
    else:
        header.append(mask_bit | 127)
        header += struct.pack(">Q", n)
    mask = secrets.token_bytes(4)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    c.sock.sendall(bytes(header) + masked)


def _ws_recv_text(c: _WSConn, timeout_s: float = 30.0) -> str:
    """Receive one full text message (handling fragmented frames + control pings)."""
    c.sock.settimeout(timeout_s)
    msg = bytearray()
    while True:
        b1, b2 = _ws_recv_exact(c, 2)
        fin = (b1 & 0x80) != 0
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        plen = b2 & 0x7F
        if plen == 126:
            (plen,) = struct.unpack(">H", _ws_recv_exact(c, 2))
        elif plen == 127:
            (plen,) = struct.unpack(">Q", _ws_recv_exact(c, 8))
        if masked:
            mask = _ws_recv_exact(c, 4)
        else:
            mask = b""
        payload = _ws_recv_exact(c, plen) if plen else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x9:  # ping → pong
            pong = bytearray([0x8A])
            if len(payload) < 126:
                pong.append(0x80 | len(payload))
            else:
                pong.append(0x80 | 126)
                pong += struct.pack(">H", len(payload))
            mask4 = secrets.token_bytes(4)
            pong += mask4 + bytes(b ^ mask4[i % 4] for i, b in enumerate(payload))
            c.sock.sendall(bytes(pong))
            continue
        if opcode == 0x8:  # close
            raise RuntimeError("WS recv: server sent close frame")
        if opcode == 0xA:  # pong
            continue
        if opcode in (0x0, 0x1, 0x2):
            msg += payload
            if fin:
                return msg.decode("utf-8", errors="replace")
            continue
        raise RuntimeError(f"WS recv: unsupported opcode {opcode:#x}")


def _cdp_runtime_evaluate(ws_url: str, expression: str, timeout_s: float = 20.0) -> Any:
    """Open WS, send Runtime.evaluate, return the result value (or raise).

    Chrome >= 111 enforces an Origin allowlist on the devtools WS endpoint.
    Without `--remote-allow-origins=*` an Origin header would be rejected, but
    omitting Origin entirely is also accepted, which is what we do.
    """
    parsed = urlparse(ws_url)
    if parsed.scheme not in ("ws", "wss"):
        raise RuntimeError(f"bad ws url scheme: {ws_url!r}")
    if parsed.scheme == "wss":
        raise RuntimeError("wss CDP not supported (we only talk to localhost)")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    sock = socket.create_connection((host, port), timeout=5.0)
    conn = _WSConn(sock)
    try:
        _ws_handshake(conn, host, port, path)
        msg_id = 1
        cmd = {
            "id": msg_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        }
        _ws_send_text(conn, json.dumps(cmd))
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = max(0.5, deadline - time.monotonic())
            text = _ws_recv_text(conn, timeout_s=remaining)
            try:
                msg = json.loads(text)
            except Exception:
                continue
            if msg.get("id") != msg_id:
                continue
            if "error" in msg:
                raise RuntimeError(f"CDP Runtime.evaluate error: {msg['error']}")
            result = msg.get("result", {}).get("result", {})
            if result.get("subtype") == "error":
                raise RuntimeError(f"CDP eval threw: {result.get('description', '?')}")
            return result.get("value")
    finally:
        conn.close()


def cdp_read_active_tab(
    port: int = 9222,
    url_match: str | None = None,
    timeout_s: float = 25.0,
) -> tuple[str, str, str]:
    """Read DOM of a live tab via CDP.

    Returns ``(tab_url, tab_title, html)``.  Raises ``RuntimeError`` if the
    debug port is closed (browser not started with ``--remote-debugging-port=N``).
    """
    try:
        tabs = _cdp_list_tabs(port=port)
    except (urllib.error.URLError, socket.error, ConnectionError) as e:
        raise RuntimeError(
            f"CDP not reachable on port {port}: {e}. "
            f"Start chrome/edge with --remote-debugging-port={port} (the default chrome/edge "
            f"launchers in this app already include this flag — run launch_app('chrome') after "
            f"closing all chrome windows to enable it)."
        ) from e
    tab = _cdp_pick_tab(tabs, url_match=url_match)
    ws_url = tab.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError(f"tab has no webSocketDebuggerUrl (devtools attached?): {tab.get('url')}")
    html = _cdp_runtime_evaluate(
        ws_url,
        "document.documentElement && document.documentElement.outerHTML",
        timeout_s=timeout_s,
    )
    if not isinstance(html, str):
        raise RuntimeError(f"CDP eval returned non-string: {type(html).__name__}")
    return tab.get("url") or "", tab.get("title") or "", html


# ---------------------------------------------------------------------------
# High-level façade for the meta tool
# ---------------------------------------------------------------------------

def read_webpage(
    url: str | None = None,
    active_tab: bool = False,
    browser: str = "edge",
    url_match: str | None = None,
    cdp_port: int = 9222,
    max_chars: int = 8000,
) -> dict[str, Any]:
    """One-call API used by the meta tool.

    - If ``active_tab=True``: read the user's live tab via CDP (login state preserved).
      ``url_match`` is an optional substring filter against tab title/url.
    - Else: ``url`` is required; spawn a headless browser to fetch+render+dump.

    Returns a dict ``{ok, source, url, title, text, raw_html_len}`` or
    ``{ok=False, error}``.
    """
    try:
        if active_tab:
            tab_url, tab_title, html = cdp_read_active_tab(port=cdp_port, url_match=url_match)
            title, text = html_to_text(html, max_chars=max_chars)
            return {
                "ok": True,
                "source": "cdp",
                "url": tab_url,
                "title": title or tab_title,
                "text": text,
                "raw_html_len": len(html),
            }
        if not url:
            return {"ok": False, "error": "must provide either url= or active_tab=true"}
        html = dump_dom_headless(url, browser=browser)
        title, text = html_to_text(html, max_chars=max_chars)
        return {
            "ok": True,
            "source": "headless",
            "url": url,
            "title": title,
            "text": text,
            "raw_html_len": len(html),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
