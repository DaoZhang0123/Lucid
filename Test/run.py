"""E2E harness — feed the 20 queries to a fresh `lucid --sidecar` process and
record where each thread landed. The sidecar's own priority queue does the
scheduling; we are merely an enqueuer + a poller.

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
            self.proc.stdin.write(payload + "\n")
            self.proc.stdin.flush()
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
                        help="delay between successive start_task calls")
    parser.add_argument("--poll-interval", type=float, default=5.0,
                        help="seconds between get_status polls")
    parser.add_argument("--per-task-cap", type=int, default=20 * 60,
                        help="cancel the CURRENT task if its events.jsonl has "
                             "not been updated for this many seconds (queue "
                             "keeps going)")
    parser.add_argument("--queue-idle-cap", type=int, default=10 * 60,
                        help="bail out the whole run if neither the running "
                             "thread nor the queue length has changed for "
                             "this many seconds (sidecar truly wedged)")
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
    try:
        if not client.ready.wait(timeout=45):
            raise SystemExit("sidecar did not emit `ready` within 45s")
        print(f"[run.py] sidecar ready; enqueueing {len(queries)} queries")

        manifest: dict[str, Any] = {
            "run_id": run_id,
            "started_ms": _now_ms(),
            "queries_file": str(args.queries),
            "threads_root": str(threads_root),
            "queries": [],
        }
        write_manifest(manifest)

        for q in queries:
            res = client.send(
                "start_task",
                {
                    "instruction": q["instruction"],
                    "autonomy": "full",
                    # Hard cap for the agent loop. `max_steps` in queries.json
                    # is an analytical "soft budget"; allow some overshoot.
                    "max_steps": int(q.get("max_steps") or 30) * 2 + 10,
                },
                timeout=20,
            ) or {}
            tid = res.get("thread_id")
            entry = {
                "id": q["id"],
                "category": q.get("category", ""),
                "instruction": q["instruction"],
                "expect_files": q.get("expect_files"),
                "max_steps_soft": q.get("max_steps"),
                "thread_id": tid,
                "thread_dir": str(threads_root / tid) if tid else None,
                "queued_ms": _now_ms(),
                "queue_position": res.get("position"),
                "started_immediately": bool(res.get("started")),
            }
            manifest["queries"].append(entry)
            mark = "running" if res.get("started") else f"pos={res.get('position')}"
            print(f"[run.py] enqueued {q['id']:>3} → {tid} ({mark})")
            write_manifest(manifest)
            time.sleep(args.enqueue_gap_ms / 1000.0)

        print(f"[run.py] all enqueued; polling every {args.poll_interval:.1f}s")

        # Map thread_id -> manifest entry, so we can resolve events.jsonl path
        # given just a tid (used by stuck detection below).
        tid_to_entry: dict[str, dict[str, Any]] = {
            e["thread_id"]: e for e in manifest["queries"] if e.get("thread_id")
        }

        def _events_mtime(tid: str | None) -> float:
            if not tid:
                return 0.0
            entry = tid_to_entry.get(tid)
            if not entry:
                return 0.0
            tdir = entry.get("thread_dir")
            if not tdir:
                return 0.0
            ev = Path(tdir) / "events.jsonl"
            try:
                return ev.stat().st_mtime
            except FileNotFoundError:
                return 0.0
            except OSError:
                return 0.0

        deadline = time.time() + args.global_timeout
        # Per-task progress: when did the current thread's events.jsonl last
        # grow? If `--per-task-cap` seconds pass with no growth, cancel that
        # ONE task (sidecar moves on to the next) — do NOT shutdown the run.
        cur_tid: str | None = None
        cur_mtime: float = 0.0
        cur_mtime_seen_at: float = time.time()
        cancelled_tids: set[str] = set()
        # Queue-level wedge: neither tid nor queue_len has changed in a long
        # time AND no events progress either. Triggers a hard abort.
        last_qlen = -1
        queue_unchanged_since = time.time()

        while True:
            if time.time() > deadline:
                print("[run.py] global-timeout hit; bailing out")
                manifest["aborted"] = "global_timeout"
                break
            try:
                st = client.send("get_status", {}, timeout=10) or {}
            except Exception as exc:
                print(f"[run.py] get_status failed: {exc}; retrying")
                time.sleep(args.poll_interval)
                continue
            running = st.get("running")
            current_tid = st.get("current_thread_id")
            qlen = len(st.get("queue") or [])
            if not running and qlen == 0:
                print("[run.py] queue drained, no worker running — done")
                break

            # Track current task's events.jsonl mtime
            now = time.time()
            if current_tid != cur_tid:
                cur_tid = current_tid
                cur_mtime = _events_mtime(current_tid)
                cur_mtime_seen_at = now
                print(f"[run.py] running={current_tid} queue_len={qlen}")
            else:
                m = _events_mtime(current_tid)
                if m > cur_mtime:
                    cur_mtime = m
                    cur_mtime_seen_at = now

            # Per-task stuck → cancel just this one
            if (
                current_tid
                and current_tid not in cancelled_tids
                and now - cur_mtime_seen_at > args.per_task_cap
            ):
                print(
                    f"[run.py] per-task-cap ({args.per_task_cap}s) hit on "
                    f"{current_tid}; cancelling this task, queue continues"
                )
                try:
                    client.send("cancel", {}, timeout=10)
                except Exception as exc:
                    print(f"[run.py] cancel RPC failed: {exc}")
                cancelled_tids.add(current_tid)
                # Reset the per-task clock so we don't immediately re-cancel
                # in case the cancel takes a few seconds to take effect.
                cur_mtime_seen_at = now

            # Queue-level wedge: queue length stuck AND no events activity
            if qlen != last_qlen:
                last_qlen = qlen
                queue_unchanged_since = now
            stuck = (
                now - queue_unchanged_since > args.queue_idle_cap
                and now - cur_mtime_seen_at > args.queue_idle_cap
            )
            if stuck:
                print(
                    f"[run.py] queue-idle-cap ({args.queue_idle_cap}s) hit "
                    f"with no events progress; bailing out"
                )
                manifest["aborted"] = "stuck"
                break

            time.sleep(args.poll_interval)

        manifest["ended_ms"] = _now_ms()
        # Backfill end-of-task info by scanning each thread's events.jsonl
        # while the sidecar is still up (file handles are flushed on close()).
        for q in manifest["queries"]:
            tdir = q.get("thread_dir")
            if not tdir:
                continue
            ev_path = Path(tdir) / "events.jsonl"
            if not ev_path.exists():
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

        write_manifest(manifest)
        print(f"[run.py] done. manifest: {manifest_path}")
        return 0
    finally:
        client.stop()


if __name__ == "__main__":
    raise SystemExit(main())
