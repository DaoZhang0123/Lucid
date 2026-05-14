"""E2E harness — feed the queries to a fresh `lucid --sidecar` process one
at a time. We send `start_task` for query N, poll `get_status` until that
thread is no longer the running worker, then move on to query N+1. This
way you can watch a single task run start-to-finish without an opaque
queue piling up ahead of it.

Usage::

    python Test/run.py
    python Test/run.py --only A1,B2,C1
    python Test/run.py --queries Test/queries.json \
                       --global-timeout 3600

Outputs::

    Test/runs/<YYYYmmdd-HHMMSS>/manifest.json     # live, kept up-to-date
    Test/runs/<YYYYmmdd-HHMMSS>/sidecar.stderr.log

NOTE: do not start the GUI app while this is running — they would fight over
the sidecar / config.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# After sys.path tweak — these come from the project itself.
from lucid.config import load_config  # noqa: E402
from lucid.runlog import resolve_threads_root  # noqa: E402

DEFAULT_QUERIES = REPO_ROOT / "Test" / "queries.json"
RUNS_ROOT = REPO_ROOT / "Test" / "runs"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _log(msg: str) -> None:
    """Print a line prefixed with the current HH:MM:SS so a stuck task is
    obvious from the terminal scrollback. All harness prints go through
    this so the user can tell at a glance whether a `running…` line was
    written 5 seconds ago or 50 minutes ago."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _snapshot_expect_files(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stat every `expect_files` spec right now and return a per-spec record
    of (exists / size / mtime / contains_match). Used by the harness to pin
    deliverables state at task-close time, so a later task that deletes the
    same path cannot retroactively flip an earlier task's verdict.
    """
    out: list[dict[str, Any]] = []
    for spec in specs or []:
        path_raw = spec.get("path") or ""
        path = os.path.expandvars(path_raw)
        rec: dict[str, Any] = {
            "path": path_raw,
            "resolved": path,
            "must_exist": bool(spec.get("must_exist", True)),
            "must_contain": spec.get("must_contain"),
            "snapshot_ms": _now_ms(),
        }
        try:
            st = os.stat(path)
            rec["exists"] = True
            rec["size"] = st.st_size
            rec["mtime_ms"] = int(st.st_mtime * 1000)
        except (FileNotFoundError, NotADirectoryError):
            rec["exists"] = False
        except OSError as exc:
            rec["exists"] = False
            rec["stat_error"] = str(exc)
        if rec.get("exists") and rec.get("must_contain"):
            sub = str(rec["must_contain"])
            data: str | None = None
            for enc in ("utf-8-sig", "utf-8", "utf-16", "gbk", "cp1252"):
                try:
                    with open(path, "r", encoding=enc) as f:
                        data = f.read()
                    break
                except (UnicodeError, OSError):
                    continue
            if data is None:
                rec["contains_match"] = None
                rec["decode_failed"] = True
            else:
                rec["contains_match"] = sub.lower() in data.lower()
        out.append(rec)
    return out


def _load_queries(path: Path, only: list[str] | None) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = list(data.get("queries") or [])
    if only:
        wanted = {x.strip() for x in only}
        items = [q for q in items if q.get("id") in wanted]
        missing = wanted - {q.get("id") for q in items}
        if missing:
            raise SystemExit(f"--only refers to unknown ids: {sorted(missing)}")
    return items


class SidecarClient:
    """Thin stdio JSON-RPC client. One reader thread for stdout, one for stderr.

    We do NOT try to interpret events here; analyze.py reads each thread's
    on-disk events.jsonl. We only need RPC results + the initial `ready` line.
    """

    def __init__(self, stderr_log: Path) -> None:
        self.stderr_log = stderr_log
        self.proc: subprocess.Popen | None = None
        self._req_id = 0
        self._results: dict[int, dict[str, Any]] = {}
        self._cv = threading.Condition()
        self.ready = threading.Event()
        self._reader: threading.Thread | None = None
        self._errreader: threading.Thread | None = None
        self._write_lock = threading.Lock()

    def start(self) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "lucid", "--sidecar"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
            env=env,
            bufsize=1,
            text=True,
            encoding="utf-8",
        )
        self._reader = threading.Thread(target=self._read_stdout,
                                        name="sidecar-stdout", daemon=True)
        self._reader.start()
        self._errreader = threading.Thread(target=self._tee_stderr,
                                           name="sidecar-stderr", daemon=True)
        self._errreader.start()

    def stop(self) -> None:
        if not self.proc:
            return
        if self.proc.poll() is None:
            try:
                self.send("shutdown", {}, timeout=5)
            except Exception:
                pass
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self._cv:
                    self._results[int(msg["id"])] = msg
                    self._cv.notify_all()
                continue
            if msg.get("event") == "ready":
                self.ready.set()
            # Other events ignored — analyze.py reads events.jsonl directly.

    def _tee_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        with open(self.stderr_log, "a", encoding="utf-8", errors="replace") as f:
            for raw in self.proc.stderr:
                f.write(raw)
                f.flush()

    def send(self, method: str, params: dict[str, Any], timeout: float = 60.0) -> Any:
        assert self.proc and self.proc.stdin
        with self._write_lock:
            self._req_id += 1
            rid = self._req_id
            payload = json.dumps(
                {"id": rid, "method": method, "params": params},
                ensure_ascii=False,
            )
            # Watchdog: if the sidecar wedges and stops draining stdin, the
            # OS pipe buffer (~64 KB on Windows) eventually fills and a
            # plain `stdin.write` will block forever — the `timeout=` below
            # only governs the read side. Push the write into a worker
            # thread and time it out so we surface a TimeoutError instead
            # of deadlocking the harness on L-something forever.
            write_done = threading.Event()
            write_exc: list[BaseException] = []

            def _do_write() -> None:
                try:
                    assert self.proc and self.proc.stdin
                    self.proc.stdin.write(payload + "\n")
                    self.proc.stdin.flush()
                except BaseException as exc:  # noqa: BLE001
                    write_exc.append(exc)
                finally:
                    write_done.set()

            t = threading.Thread(target=_do_write, name="sidecar-stdin-write", daemon=True)
            t.start()
            if not write_done.wait(timeout=max(2.0, min(timeout, 10.0))):
                raise TimeoutError(
                    f"RPC {method} stdin.write blocked >"
                    f"{max(2.0, min(timeout, 10.0)):.0f}s (sidecar pipe full?)"
                )
            if write_exc:
                raise RuntimeError(f"RPC {method} stdin.write failed: {write_exc[0]}")
        deadline = time.time() + timeout
        with self._cv:
            while rid not in self._results:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"RPC {method} timed out after {timeout}s")
                self._cv.wait(timeout=remaining)
            msg = self._results.pop(rid)
        if "error" in msg:
            raise RuntimeError(f"RPC {method} failed: {msg['error']}")
        return msg.get("result")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Lucid E2E suite (20 queries by default)."
    )
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES,
                        help="path to queries.json")
    parser.add_argument("--only", default="",
                        help="comma-separated query ids to run")
    parser.add_argument("--global-timeout", type=int, default=120 * 60,
                        help="hard cap in seconds for the whole run (default 120 min; 48 queries serial typically take 65–80 min)")
    parser.add_argument("--enqueue-gap-ms", type=int, default=300,
                        help="(deprecated, ignored — tasks now run strictly serially)")
    parser.add_argument("--poll-interval", type=float, default=5.0,
                        help="seconds between get_status polls")
    parser.add_argument("--per-task-cap", type=int, default=20 * 60,
                        help="cancel the CURRENT task if its events.jsonl has "
                             "not been updated for this many seconds (queue "
                             "keeps going)")
    parser.add_argument("--cancel-grace", type=int, default=15,
                        help="after sending `cancel` to a wedged task, wait "
                             "this many seconds for the worker to yield. If "
                             "events.jsonl still does not grow, kill+restart "
                             "the sidecar and advance to the next query.")
    parser.add_argument("--queue-idle-cap", type=int, default=10 * 60,
                        help="(deprecated, ignored — only one task is in "
                             "flight at a time now; per-task-cap is what "
                             "matters)")
    args = parser.parse_args(argv)

    only = [x for x in (args.only or "").split(",") if x.strip()]
    queries = _load_queries(args.queries, only or None)
    if not queries:
        print("no queries to run.", file=sys.stderr)
        return 2

    cfg = load_config()
    threads_root = resolve_threads_root(cfg.logging)

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    stderr_log = run_dir / "sidecar.stderr.log"
    manifest_path = run_dir / "manifest.json"

    def write_manifest(m: dict[str, Any]) -> None:
        manifest_path.write_text(
            json.dumps(m, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    client = SidecarClient(stderr_log)
    client.start()

    def _restart_sidecar(reason: str) -> tuple[SidecarClient, bool]:
        """Kill the (presumed wedged) sidecar and start a fresh one.

        Returns (new_client, ready). Caller is responsible for storing the
        new client and bailing out if ready is False.
        """
        _log(f"restarting sidecar ({reason})")
        try:
            if client.proc is not None and client.proc.poll() is None:
                client.proc.kill()
                try:
                    client.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        except Exception as kexc:
            _log(f"sidecar kill failed: {kexc}")
        fresh = SidecarClient(stderr_log)
        try:
            fresh.start()
            if not fresh.ready.wait(timeout=45):
                _log("fresh sidecar did not emit `ready` within 45s")
                return fresh, False
            _log("fresh sidecar ready; resuming")
            return fresh, True
        except Exception as sexc:
            _log(f"sidecar restart failed: {sexc}")
            return fresh, False
    try:
        if not client.ready.wait(timeout=45):
            raise SystemExit("sidecar did not emit `ready` within 45s")
        _log(f"sidecar ready; running {len(queries)} queries serially")

        manifest: dict[str, Any] = {
            "run_id": run_id,
            "started_ms": _now_ms(),
            "queries_file": str(args.queries),
            "threads_root": str(threads_root),
            "queries": [],
        }
        write_manifest(manifest)

        def _events_mtime(tdir: str | None) -> float:
            if not tdir:
                return 0.0
            ev = Path(tdir) / "events.jsonl"
            try:
                return ev.stat().st_mtime
            except (FileNotFoundError, OSError):
                return 0.0

        def _has_task_close(tdir: str | None) -> bool:
            """Cheap fallback for "task done" when RPC is stale: tail the
            events.jsonl and look for a task_close event near the end. We
            only scan the last ~32KB so it stays cheap on long threads."""
            if not tdir:
                return False
            ev = Path(tdir) / "events.jsonl"
            try:
                size = ev.stat().st_size
                with ev.open("rb") as f:
                    if size > 32_768:
                        f.seek(-32_768, 2)
                    tail = f.read().decode("utf-8", errors="replace")
            except (FileNotFoundError, OSError):
                return False
            for line in reversed(tail.splitlines()):
                if not line.strip():
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("event") == "task_close":
                    return True
            return False

        total = len(queries)
        global_deadline = time.time() + args.global_timeout
        aborted_reason: str | None = None

        for idx, q in enumerate(queries, start=1):
            tag = f"[query = {idx}/{total}] {q['id']}"
            if time.time() > global_deadline:
                _log(f"{tag} skipped — global-timeout reached")
                aborted_reason = "global_timeout"
                break
            if client.proc is not None and client.proc.poll() is not None:
                _log(f"{tag} skipped — sidecar process has exited (code {client.proc.returncode})")
                aborted_reason = "sidecar_died"
                break

            # 每个任务前新建 thread，避免截图/manifest 冲突
            client.send("thread_new", {"title": q["instruction"][:80]}, timeout=10)
            _log(f"{tag} → start_task: {q['instruction'][:80]}")
            try:
                res = client.send(
                    "start_task",
                    {
                        "instruction": q["instruction"],
                    },
                    timeout=20,
                ) or {}
            except Exception as exc:
                _log(f"{tag} start_task RPC failed: {exc}; recording harness_error")
                manifest["queries"].append({
                    "id": q["id"],
                    "category": q.get("category", ""),
                    "instruction": q["instruction"],
                    "expect_files": q.get("expect_files"),
                    "thread_id": None,
                    "thread_dir": None,
                    "queued_ms": _now_ms(),
                    "queue_position": None,
                    "started_immediately": False,
                    "status": "harness_error",
                    "final_text": f"start_task RPC failed: {exc}",
                })
                write_manifest(manifest)
                if client.proc is not None and client.proc.poll() is not None:
                    aborted_reason = "sidecar_died_during_enqueue"
                    break
                # The sidecar is alive but wedged — its main thread isn't
                # servicing RPCs. If we just `continue`, every subsequent
                # start_task will also time out, and eventually the OS
                # stdin pipe fills (~64 KB) and the next `stdin.write`
                # deadlocks the harness for hours (the L4 hang). Kill the
                # wedged sidecar and start a fresh one so the rest of the
                # suite can still run. Each restart is reported in the
                # manifest via the harness_error rows above.
                _log(f"{tag} sidecar appears wedged; restarting it for next query")
                client, ok = _restart_sidecar("start_task RPC failed")
                if not ok:
                    aborted_reason = "sidecar_restart_failed"
                    break
                continue

            tid = res.get("thread_id")
            tdir = str(threads_root / tid) if tid else None
            entry: dict[str, Any] = {
                "id": q["id"],
                "category": q.get("category", ""),
                "instruction": q["instruction"],
                "expect_files": q.get("expect_files"),
                "thread_id": tid,
                "thread_dir": tdir,
                "queued_ms": _now_ms(),
                "queue_position": res.get("position"),
                "started_immediately": bool(res.get("started")),
            }
            manifest["queries"].append(entry)
            write_manifest(manifest)
            _log(f"{tag} thread={tid}; waiting for completion")

            # Wait for THIS task to finish before sending the next one.
            cur_mtime = _events_mtime(tdir)
            cur_mtime_seen_at = time.time()
            cancelled = False
            cancelled_at = 0.0
            last_log = time.time()
            while True:
                if time.time() > global_deadline:
                    _log(f"{tag} global-timeout reached while waiting; bailing out")
                    aborted_reason = "global_timeout"
                    break
                if client.proc is not None and client.proc.poll() is not None:
                    _log(f"{tag} sidecar process exited (code {client.proc.returncode}); bailing out")
                    aborted_reason = "sidecar_died"
                    break

                # ALWAYS update events.jsonl mtime first, BEFORE any RPC.
                # The sidecar's RPC dispatch can starve under heavy GUI load
                # (screenshot / click takes seconds while the worker holds
                # the main thread); if we tied the idle clock to get_status
                # success, a healthy task that's actively writing events
                # would look idle just because the RPC reply is slow.
                now = time.time()
                m = _events_mtime(tdir)
                if m > cur_mtime:
                    cur_mtime = m
                    cur_mtime_seen_at = now

                try:
                    st = client.send("get_status", {}, timeout=10) or {}
                    rpc_ok = True
                except Exception as exc:
                    if client.proc is not None and client.proc.poll() is not None:
                        _log(f"{tag} sidecar process exited; bailing out")
                        aborted_reason = "sidecar_died"
                        break
                    # Don't retry-continue: events.jsonl growth is enough
                    # signal that the task is alive. Just log and proceed
                    # so per-task-cap / log cadence still see real progress.
                    _log(f"{tag} get_status failed: {exc} "
                         f"(events still growing? mtime_age="
                         f"{int(now - cur_mtime_seen_at)}s)")
                    st = {}
                    rpc_ok = False

                current_tid = st.get("current_thread_id") if rpc_ok else None
                running = st.get("running") if rpc_ok else None
                # Done when RPC succeeded AND there is no running worker AND
                # our tid is not the current one anymore. (`current_thread_id`
                # clears once the worker finishes the task.) When RPC keeps
                # failing we cannot tell — fall back to "no events growth +
                # per-task-cap elapsed" as the kill switch.
                if rpc_ok and not running and current_tid != tid:
                    break
                # RPC-stale fallback: if events.jsonl already contains a
                # task_close event for this thread, the task is done and we
                # should move on regardless of the wedged RPC.
                if not rpc_ok and _has_task_close(tdir):
                    _log(f"{tag} task_close seen on disk while RPC was stale; advancing")
                    break

                if now - last_log >= max(args.poll_interval * 4, 20.0):
                    rpc_tag = "" if rpc_ok else " [rpc-stale]"
                    _log(f"{tag} running… (idle {int(now - cur_mtime_seen_at)}s){rpc_tag}")
                    last_log = now

                # Per-task-cap fires on real events idleness, regardless of
                # RPC health. current_tid may be None when RPC is stale —
                # don't gate the cancel on it.
                if (
                    not cancelled
                    and now - cur_mtime_seen_at > args.per_task_cap
                ):
                    _log(
                        f"{tag} per-task-cap ({args.per_task_cap}s) hit with "
                        f"no events progress; cancelling this task"
                    )
                    try:
                        client.send("cancel", {}, timeout=10)
                    except Exception as exc:
                        _log(f"{tag} cancel RPC failed: {exc}")
                    cancelled = True
                    cancelled_at = now
                    cur_mtime_seen_at = now

                # Cancel-grace escalation: if cancel was already sent and the
                # worker still hasn't yielded (no new events) within
                # `cancel_grace_s`, the worker is wedged inside a blocking
                # syscall (e.g. pyautogui hung on a focus-stealing modal,
                # LLM SDK ignoring its timeout, or a UIA call deadlocked).
                # Soft-cancel won't reach it — escalate to a hard sidecar
                # restart so the rest of the suite can proceed. In the
                # current thread-based worker model, the sidecar restart is
                # the only reliable hard-kill primitive. The current query is
                # recorded as harness_error.
                if (
                    cancelled
                    and now - cancelled_at > args.cancel_grace
                    and now - cur_mtime_seen_at > args.cancel_grace
                ):
                    _log(
                        f"{tag} cancel-grace ({args.cancel_grace}s) elapsed "
                        f"with no progress; killing sidecar and advancing"
                    )
                    entry["status"] = "harness_error"
                    entry["final_text"] = (
                        f"worker wedged after cancel; sidecar restarted "
                        f"after {int(now - cancelled_at)}s grace"
                    )
                    write_manifest(manifest)
                    client, ok = _restart_sidecar("cancel ignored")
                    if not ok:
                        aborted_reason = "sidecar_restart_failed"
                    break

                time.sleep(args.poll_interval)

            if aborted_reason:
                break
            # Snapshot expect_files on disk at this exact moment, BEFORE the
            # next task can mutate them (B3 delete-after-B1 was the canonical
            # bug: B1 wrote the file correctly, B3 deleted it as part of its
            # own goal, then end-of-run deliverables check saw "missing" and
            # blamed B1). The skill's report.md trusts this snapshot over a
            # post-hoc disk read.
            entry["deliverables_snapshot"] = _snapshot_expect_files(
                q.get("expect_files") or []
            )
            _log(f"{tag} done")

        if aborted_reason:
            manifest["aborted"] = aborted_reason
        manifest["ended_ms"] = _now_ms()
        # Backfill end-of-task info by scanning each thread's events.jsonl
        # while the sidecar is still up (file handles are flushed on close()).
        # Threads that never produced a task_close (sidecar died mid-run, etc.)
        # are tagged `no_close` so analysis can tell them apart from genuine
        # task failures.
        for q in manifest["queries"]:
            if q.get("status") == "harness_error":
                continue
            tdir = q.get("thread_dir")
            if not tdir:
                continue
            ev_path = Path(tdir) / "events.jsonl"
            if not ev_path.exists():
                q.setdefault("status", "no_events")
                continue
            close_evt = None
            try:
                for line in ev_path.read_text(encoding="utf-8",
                                              errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("event") == "task_close":
                        close_evt = evt
            except Exception:
                pass
            if close_evt:
                q["status"] = close_evt.get("status")
                q["final_text"] = close_evt.get("final_text")
                q["ended_ms"] = close_evt.get("ts_ms")
            else:
                q.setdefault("status", "no_close")

        write_manifest(manifest)
        _log(f"done. manifest: {manifest_path}")
        return 0
    finally:
        client.stop()


if __name__ == "__main__":
    raise SystemExit(main())
