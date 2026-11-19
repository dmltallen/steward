"""Job scheduler: threaded runner, catch-up, persisted run history.

Jobs are user-visible, user-editable configuration. Each job has:

    name       unique id, e.g. 'morning-brief'
    schedule   cron-lite: 'daily:HH:MM' (local time) or 'every:N<unit>'
               where <unit> is h, m, s, or bare seconds ('every:6h',
               'every:90m', 'every:30s', 'every:3600')
    prompt     free-text instruction to the model for this job
    scope      retrieval scope hint (a topic or 'today')
    window     optional awake window 'HH:MM'-'HH:MM' in local 24h time.
               A daily job whose scheduled time falls outside the window is
               still caught up on the next tick while the window is open, and
               is SKIPPED (recorded, not run) when the catch-up happens after
               the window closed. Manual `run_job` bypasses the window on
               purpose: a human asked for it.

Jobs live in <data>/jobs.json (seeded with a 'morning-brief' default on first
run so the user can see and edit them). Scheduler state (per-job last_run /
next_run, and a job_runs history) is persisted in SQLite at
<data>/db/scheduler.db, WAL mode.

The runner is a daemon thread that calls tick() every `tick_s` seconds.
tick() runs every job whose next_run is due, catches up a missed daily
occurrence exactly once (never re-fires batches for many skipped days), and
reschedules the next run forward from now. One job raising is isolated: it is
recorded as an error and the loop keeps going. The thread needs no cron binary
and writes no system crontab. Timing is injectable: pass now() for tests with
a fake clock.

Awake-window honesty: the box is awake while the daemon (and therefore this
scheduler thread) is up. If the box is off at the scheduled moment, the job is
not written then; on the next tick the scheduler either catches it up (still
inside the window) or records a skipped run for that day (window closed). The
'writes only when awake' claim above refers to exactly this: no brief is
backfilled into a day whose window has already passed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from steward.paths import data_root

from .memory.brief import DEFAULT_INSTRUCTION

if TYPE_CHECKING:  # pragma: no cover - type hints only, no import cycle
    from .license import License as _LicenseType

DEFAULT_TICK_S = 60.0
DEFAULT_MODEL = "qwen3:8b"

# Free tier holds exactly one scheduled brief. More scheduled jobs need the
# scheduled_jobs entitlement (features.py / license.py), enforced in tick().
FREE_SCHEDULED_JOBS = 1


def _active_license_for(root) -> "_LicenseType | None":
    """The verified active license for a data root, or None on any failure.

    This is the fail-closed boundary for the scheduler: a malformed or
    tampered license resolves to None (free tier) here, never a crash.
    """
    from . import license as _lic

    try:
        return _lic.active_license(root)
    except Exception:  # noqa: BLE001 - a bad license never takes the loop down
        return None


# The seeded job a fresh install ships with. window 05:00-12:00 is the honest
# awake-gating: a morning brief is written only while the box is awake in the
# morning; catch-up after 12:00 skips that day rather than backfilling an
# afternoon "morning" brief.
DEFAULT_JOBS = [
    {
        "name": "morning-brief",
        "schedule": "daily:07:00",
        "prompt": DEFAULT_INSTRUCTION,
        "scope": "today",
        "window_start": "05:00",
        "window_end": "12:00",
    },
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    name TEXT PRIMARY KEY,
    schedule TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    window_start TEXT,
    window_end TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run TEXT,
    next_run REAL NOT NULL,
    last_status TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    detail TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job, id);
"""


# --------------------------------------------------------------------------
# Schedule parsing
# --------------------------------------------------------------------------


def parse_schedule(schedule: str) -> dict:
    """Parse a cron-lite schedule string into a spec dict.

    Returns {"kind": "daily", "time": "HH:MM"} or {"kind": "interval",
    "seconds": N}. Raises ValueError on anything else.
    """
    schedule = (schedule or "").strip()
    if schedule.startswith("daily:"):
        clock = schedule[len("daily:"):].strip()
        try:
            hour, minute = clock.split(":")
            hour_i, minute_i = int(hour), int(minute)
        except (ValueError, TypeError):
            raise ValueError("daily schedule must be daily:HH:MM, got %r" % schedule) \
                from None
        if not (0 <= hour_i <= 23 and 0 <= minute_i <= 59):
            raise ValueError("daily schedule time out of range: %r" % clock)
        return {"kind": "daily", "time": "%02d:%02d" % (hour_i, minute_i)}
    if schedule.startswith("every:"):
        raw = schedule[len("every:"):].strip()
        seconds = _every_seconds(raw)
        if seconds <= 0:
            raise ValueError("every schedule must be a positive duration: %r" % raw)
        return {"kind": "interval", "seconds": seconds}
    raise ValueError("schedule must be 'daily:HH:MM' or 'every:N<unit>': %r" % schedule)


def _every_seconds(raw: str) -> int:
    raw = raw.strip().lower()
    if not raw:
        return 0
    if raw[-1:] == "h":
        return _int_or_zero(raw[:-1]) * 3600
    if raw[-1:] == "m":
        return _int_or_zero(raw[:-1]) * 60
    if raw[-1:] == "s":
        return _int_or_zero(raw[:-1])
    return _int_or_zero(raw)


def _int_or_zero(raw: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def next_run_after(schedule: str, after_ts: float) -> float:
    """Next scheduled occurrence strictly after `after_ts` (local wall clock
    for daily schedules, epoch arithmetic for intervals)."""
    spec = parse_schedule(schedule)
    if spec["kind"] == "interval":
        return after_ts + spec["seconds"]
    hour, minute = (int(x) for x in spec["time"].split(":"))
    local = datetime.fromtimestamp(after_ts)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate = candidate + timedelta(days=1)
    return candidate.timestamp()


def _local_minutes(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _in_window(window_start: str | None, window_end: str | None, ts: float) -> bool:
    """Is ts inside the local HH:MM window? Supports windows that cross
    midnight (start > end). No window means always in."""
    if not window_start or not window_end:
        return True
    now = _local_minutes(ts)
    if window_start <= window_end:
        return window_start <= now <= window_end
    return now >= window_start or now <= window_end


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


class JobStore:
    """SQLite jobs + job_runs behind a short-lived-connection store."""

    def __init__(self, db_path: str | Path | None = None,
                 jobs_path: str | Path | None = None,
                 now: Callable[[], float] | None = None) -> None:
        if jobs_path is not None:
            self.root = Path(jobs_path).parent
            self.db_path = (Path(db_path) if db_path is not None
                            else self.root / "db" / "scheduler.db")
        else:
            base = Path(db_path) if db_path is not None else data_root()
            self.db_path = base / "db" / "scheduler.db" \
                if base.suffix != ".db" else base
            self.root = base.parent if base.suffix == ".db" else base
        self.jobs_path = (Path(jobs_path) if jobs_path is not None
                          else Path(self.root) / "jobs.json")
        self.now = now or time.time
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # -- job definition sync -------------------------------------------------

    def ensure_default_jobs(self) -> tuple[list[dict], bool]:
        """Seed <data>/jobs.json with the default morning-brief job and return
        (job definitions, wrote_file). A present, parseable file wins; an
        unreadable or empty file is rewritten with defaults."""
        wrote = False
        jobs: list[dict] = []
        try:
            raw = json.loads(self.jobs_path.read_text(encoding="utf-8"))
            loaded: list | None = raw.get("jobs") if isinstance(raw, dict) else None
            if not isinstance(loaded, list) or not loaded:
                raise ValueError("jobs.json has no usable 'jobs' list")
            jobs = [dict(j) for j in loaded]
        except (OSError, ValueError, json.JSONDecodeError):
            self.jobs_path.parent.mkdir(parents=True, exist_ok=True)
            self.jobs_path.write_text(
                json.dumps({"jobs": DEFAULT_JOBS}, indent=2) + "\n", encoding="utf-8"
            )
            wrote = True
            jobs = [dict(j) for j in DEFAULT_JOBS]
        return jobs, wrote

    def sync_jobs(self, jobs: list[dict] | None = None) -> dict:
        """Ensure the jobs table has a row for each job definition.

        Jobs come from the caller, else the jobs file (seeding the default on
        first run). New or changed schedules get next_run computed from now;
        unchanged, existing jobs keep their scheduling state. Returns a
        summary of add/update counts.
        """
        if jobs is None:
            jobs, _ = self.ensure_default_jobs()
        now_ts = self.now()
        added = updated = 0
        for definition in jobs or []:
            name = (definition.get("name") or "").strip()
            schedule = (definition.get("schedule") or "").strip()
            if not name or not schedule:
                continue
            parse_schedule(schedule)  # validate: raises on a bad schedule
            prompt = (definition.get("prompt") or "").strip()
            scope = (definition.get("scope") or "").strip()
            window_start = definition.get("window_start")
            window_end = definition.get("window_end")
            with self._connect() as conn:
                row = conn.execute("SELECT schedule FROM jobs WHERE name = ?",
                                   (name,)).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO jobs(name, schedule, prompt, scope,"
                        " window_start, window_end, enabled, next_run)"
                        " VALUES(?, ?, ?, ?, ?, ?, 1, ?)",
                        (name, schedule, prompt, scope, window_start or None,
                         window_end or None, next_run_after(schedule, now_ts)),
                    )
                    added += 1
                else:
                    conn.execute(
                        "UPDATE jobs SET prompt=?, scope=?, window_start=?,"
                        " window_end=?"
                        + (", schedule=?, next_run=?" if row["schedule"] != schedule
                           else "")
                        + " WHERE name=?",
                        ([prompt, scope, window_start or None, window_end or None]
                         + ([schedule, next_run_after(schedule, now_ts)]
                            if row["schedule"] != schedule else [])
                         + [name]),
                    )
                    if row["schedule"] != schedule:
                        updated += 1
        return {"added": added, "rescheduled": updated}

    # -- queries -------------------------------------------------------------

    def get_job(self, name: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE name = ?",
                               (name,)).fetchone()
        return self._job_row(row) if row else None

    def list_jobs(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY enabled DESC, name").fetchall()
        return [self._job_row(r) for r in rows]

    def due_jobs(self, now_ts: float) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE enabled = 1 AND next_run <= ?"
                " ORDER BY next_run, name", (now_ts,)).fetchall()
        return [self._job_row(r) for r in rows]

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute("UPDATE jobs SET enabled = ? WHERE name = ?",
                               (1 if enabled else 0, name))
        return cur.rowcount > 0

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict:
        next_ts = float(row["next_run"])
        return {
            "name": row["name"],
            "schedule": row["schedule"],
            "prompt": row["prompt"],
            "scope": row["scope"],
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "enabled": bool(row["enabled"]),
            "last_run": row["last_run"],
            "next_run_ts": next_ts,
            "next_run": _iso(next_ts),
            "last_status": row["last_status"],
            "last_error": row["last_error"],
        }

    # -- run bookkeeping -----------------------------------------------------

    def record_run(self, job: dict, status: str, now_ts: float, *,
                   detail: str = "", error: str | None = None) -> int:
        """Write one job_runs row and update the jobs row. Returns run id."""
        started = _iso(now_ts)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO job_runs(job, started_at, finished_at, status,"
                " detail, error) VALUES(?, ?, ?, ?, ?, ?)",
                (job["name"], started, started, status,
                 (detail or "")[:2000], error),
            )
            run_id = cur.lastrowid
            conn.execute(
                "UPDATE jobs SET last_run = ?, last_status = ?, last_error = ?"
                " WHERE name = ?",
                (started, status, error, job["name"]),
            )
        assert run_id is not None
        return run_id

    def advance_next_run(self, job: dict, now_ts: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET next_run = ? WHERE name = ?",
                (next_run_after(job["schedule"], now_ts), job["name"]),
            )

    def history(self, job_name: str | None = None,
                limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM job_runs"
        params: list = []
        if job_name is not None:
            sql += " WHERE job = ?"
            params.append(job_name)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{
            "id": r["id"], "job": r["job"], "started_at": r["started_at"],
            "finished_at": r["finished_at"], "status": r["status"],
            "detail": r["detail"], "error": r["error"],
        } for r in rows]


# --------------------------------------------------------------------------
# Default runner: wires the morning-brief job to build_brief
# --------------------------------------------------------------------------


def default_runner(cfg=None, *, build=None):
    """Runner factory for the real product wiring.

    'morning-brief' calls memory.brief.build_brief for the local date of the
    run. Any other job does a single runtime chat completion with the job's
    prompt (scope ignored). Returns {"detail": ...}; the scheduler records the
    result in history. The caller's exception is the failure signal.
    """
    build = build if build is not None else _build_brief_default

    def runner(job: dict, now_ts: float) -> dict:
        name = job.get("name")
        if name == "morning-brief":
            local_day = datetime.fromtimestamp(now_ts).date()
            result = build(local_day, job.get("scope") or "today",
                           cfg=cfg, instruction=job.get("prompt"))
            return {
                "detail": "brief %d for %s (%d events, %d hits, %d facts)"
                          % (result["brief_id"], result["date"],
                             result["n_events"], result["n_hits"],
                             result["n_facts"]),
                "brief_id": result["brief_id"],
                "date": result["date"],
            }
        from steward.runtime import make_runtime

        runtime = make_runtime(cfg)
        model = (cfg.model if cfg is not None else "") or DEFAULT_MODEL
        reply = "".join(d["delta"] for d in runtime.chat(
            [{"role": "system", "content": "You are Steward, a local personal "
                                           "assistant. Answer in plain English. "
                                           "Never use em dashes."},
             {"role": "user", "content": job.get("prompt") or ""}],
            stream=False, model=model,
        ))
        return {"detail": (reply or "").strip()[:2000]}

    return runner


def _build_brief_default(day, scope, *, cfg=None, instruction=None):
    from .memory.brief import build_brief

    return build_brief(date=day, scope=scope, cfg=cfg, instruction=instruction)


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------


class Scheduler:
    """Threaded job runner with injectable clock for tests.

    Parameters:
        store       JobStore (created from cfg/data root when omitted)
        runner      callable(job, now_ts) -> dict; defaults to default_runner
        tick_s      seconds between loop ticks
        now         callable() -> float epoch; the fake clock for tests
        cfg         Config used by the default runner and store paths
    """

    def __init__(self, *, store: JobStore | None = None, runner=None,
                 tick_s: float = DEFAULT_TICK_S,
                 now: Callable[[], float] | None = None,
                 cfg=None, jobs: list[dict] | None = None,
                 entitlements: bool | None = None) -> None:
        self.store = store if store is not None else JobStore(now=now)
        self.cfg = cfg
        # None = auto (enforce once a license is active at the data root),
        # True = always enforce, False = never (legacy behavior).
        self.entitlements = entitlements
        self.runner = runner if runner is not None else default_runner(cfg)
        self.tick_s = max(0.01, float(tick_s))
        self._now = now or time.time
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.sync_jobs(jobs)

    def _scheduled_jobs_allowed(self) -> bool:
        """Is the scheduled-jobs entitlement in force for this scheduler run?

        Three states are resolved here:

        - No license file at this data root: legacy behavior is preserved and
          the scheduler runs whatever jobs.json holds, exactly as it always
          has. This is what keeps a fresh install (and every existing test)
          unchanged.
        - A license file exists but fails verification (tampered, wrong
          machine, expired, malformed): the free tier applies, so only the
          one scheduled brief runs. Fail closed, never a crash.
        - A valid license exists: the single feature gate decides
          (features.enabled("scheduled_jobs", ...)).

        entitlements=None is the product default (auto). True forces strict
        enforcement even with no license; False disables enforcement.
        """
        from . import license as _lic

        if self.entitlements is False:
            return True
        lic = _active_license_for(self.store.root)
        if lic is not None:
            from . import features as _features

            return _features.enabled("scheduled_jobs", lic, self.cfg)
        has_file = _lic.license_path(self.store.root).is_file()
        if self.entitlements is True or has_file:
            # Strict mode, or a present-but-invalid license: free tier, so
            # the scheduled-jobs entitlement is not held.
            return False
        return True

    # -- lifecycle -----------------------------------------------------------

    def sync_jobs(self, jobs: list[dict] | None = None) -> dict:
        return self.store.sync_jobs(jobs)

    def start(self) -> None:
        """Start the daemon loop thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="steward-scheduler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a loop error never kills the thread
                pass
            self._stop.wait(self.tick_s)

    def stop(self, join_s: float = 5.0) -> None:
        """Stop the loop and join the thread. Idempotent."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(join_s)

    # -- ticking -------------------------------------------------------------

    def tick(self) -> dict:
        """One pass: run every due job, with awake-window gating and
        catch-up. Never raises for a failing job. Returns a summary dict."""
        now_ts = self._now()
        due = self.store.due_jobs(now_ts)
        scheduled_allowed = self._scheduled_jobs_allowed()
        summary = {
            "ticked_at": _iso(now_ts),
            "due": len(due),
            "ran": 0, "skipped": 0, "errors": 0,
            "ran_names": [], "skipped_names": [], "error_names": [],
        }
        run_index = 0
        for job in due:
            if not _in_window(job["window_start"], job["window_end"], now_ts):
                self.store.record_run(
                    job, "skipped", now_ts,
                    detail="caught up outside awake window %s-%s; not generated"
                           % (job["window_start"] or "?", job["window_end"] or "?"),
                )
                self.store.advance_next_run(job, now_ts)
                summary["skipped"] += 1
                summary["skipped_names"].append(job["name"])
                continue
            # The single feature gate, at this call site:
            #   features.enabled("scheduled_jobs", lic.active_license(root), cfg)
            # Scheduled briefs beyond the first need the scheduled_jobs
            # entitlement. Every future call site reads the same one gate.
            if not scheduled_allowed and run_index >= FREE_SCHEDULED_JOBS:
                self.store.record_run(
                    job, "skipped", now_ts,
                    detail="entitlement required: scheduled_jobs (one "
                           "scheduled brief ships free; a tier license lifts "
                           "the cap)",
                )
                self.store.advance_next_run(job, now_ts)
                summary["skipped"] += 1
                summary["skipped_names"].append(job["name"])
                continue
            run_index += 1
            result = self._execute(job, now_ts)
            if result["status"] == "ok":
                summary["ran"] += 1
                summary["ran_names"].append(job["name"])
            else:
                summary["errors"] += 1
                summary["error_names"].append(job["name"])
        return summary

    def run_job(self, name: str) -> dict:
        """Run one job now, on demand. Bypasses the awake window (a human
        asked). Raises KeyError when the job does not exist."""
        job = self.store.get_job(name)
        if job is None:
            raise KeyError("no job named %r" % name)
        return self._execute(job, self._now(), manual=True)

    def _execute(self, job: dict, now_ts: float, *, manual: bool = False) -> dict:
        status = "ok"
        detail = ""
        error = None
        try:
            result = self.runner(job, now_ts)
            detail = result.get("detail", "")
        except Exception as exc:  # noqa: BLE001 - one bad job stops no other
            status = "error"
            error = str(exc)
        mode = "manual" if manual else "schedule"
        run_id = self.store.record_run(
            job, status, now_ts, detail=detail or "", error=error)
        self.store.advance_next_run(job, now_ts)
        return {
            "job": job["name"], "run_id": run_id, "status": status,
            "mode": mode, "detail": detail, "error": error,
        }

    # -- status --------------------------------------------------------------

    def status(self) -> list[dict]:
        return self.store.list_jobs()

    def history(self, job_name: str | None = None, limit: int = 20) -> list[dict]:
        return self.store.history(job_name, limit)
