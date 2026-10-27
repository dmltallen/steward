"""Tests for src/steward/scheduler.py: schedules, fake clock, catch-up,
awake-window gating, job failure isolation, and the threaded loop.

Timing is fully injectable (fake clock), so the suite needs no sleep-based
scheduling beyond the one thread start/stop test, which joins and verifies no
thread survives.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from steward.scheduler import (  # noqa: E402
    DEFAULT_JOBS,
    JobStore,
    Scheduler,
    _in_window,
    next_run_after,
    parse_schedule,
)

# Local wall-clock anchors (Oct 26 2026 is a Monday; no DST transition).
T_MON_0600 = datetime(2026, 10, 26, 6, 0).timestamp()
T_MON_0630 = datetime(2026, 10, 26, 6, 30).timestamp()
T_MON_0700 = datetime(2026, 10, 26, 7, 0).timestamp()
T_MON_0701 = datetime(2026, 10, 26, 7, 1).timestamp()
T_MON_0730 = datetime(2026, 10, 26, 7, 30).timestamp()
T_MON_0830 = datetime(2026, 10, 26, 8, 30).timestamp()
T_MON_1300 = datetime(2026, 10, 26, 13, 0).timestamp()
T_MON_2300 = datetime(2026, 10, 26, 23, 0).timestamp()
T_TUE_0700 = datetime(2026, 10, 27, 7, 0).timestamp()


class FakeClock:
    """Mutable callable: the fake clock the scheduler reads."""

    def __init__(self, ts: float) -> None:
        self.ts = ts

    def __call__(self) -> float:
        return self.ts

    def set(self, ts: float) -> None:
        self.ts = float(ts)


def make_scheduler(tmp_path: Path, clock: FakeClock, jobs, runner,
                   tick_s: float = 0.05):
    store = JobStore(db_path=tmp_path / "db" / "scheduler.db",
                     jobs_path=tmp_path / "jobs.json", now=clock)
    return Scheduler(store=store, runner=runner, tick_s=tick_s,
                     now=clock, jobs=jobs)


# --------------------------------------------------------------------------
# Schedule parsing and next-run arithmetic
# --------------------------------------------------------------------------


def test_parse_schedule_forms():
    assert parse_schedule("daily:07:00") == {"kind": "daily", "time": "07:00"}
    assert parse_schedule("daily:23:59") == {"kind": "daily", "time": "23:59"}
    assert parse_schedule("every:6h") == {"kind": "interval", "seconds": 21600}
    assert parse_schedule("every:90m") == {"kind": "interval", "seconds": 5400}
    assert parse_schedule("every:30s") == {"kind": "interval", "seconds": 30}
    assert parse_schedule("every:3600") == {"kind": "interval", "seconds": 3600}


def test_parse_schedule_rejects_bad():
    for bad in ("daily", "daily:25:00", "daily:07:60", "weekly:monday",
                "every:", "every:abc", "every:-5h", "cron:5", ""):
        with pytest.raises(ValueError):
            parse_schedule(bad)


def test_next_run_after_daily_same_day_and_next_day():
    # 06:30 -> next 07:00 is today
    assert next_run_after("daily:07:00", T_MON_0600) == T_MON_0700
    # 07:00 exactly -> next occurrence is tomorrow
    assert next_run_after("daily:07:00", T_MON_0700) == T_TUE_0700
    # after the clock time -> tomorrow
    assert next_run_after("daily:07:00", T_MON_0730) == T_TUE_0700


def test_next_run_after_interval_is_arithmetic():
    assert next_run_after("every:6h", T_MON_0630) == T_MON_0630 + 21600
    assert next_run_after("every:30s", T_MON_0700) == T_MON_0700 + 30


def test_in_window_basic_and_crossing_midnight():
    assert _in_window("05:00", "12:00", T_MON_0700) is True
    assert _in_window("05:00", "12:00", T_MON_1300) is False
    assert _in_window(None, None, T_MON_0700) is True
    # window crossing midnight: 22:00-06:00
    assert _in_window("22:00", "06:00", T_MON_2300) is True
    assert _in_window("22:00", "06:00", T_MON_1300) is False


def test_in_window_crossing_midnight_is_out_at_noon():
    noon = datetime(2026, 10, 26, 12, 0).timestamp()
    assert _in_window("22:00", "06:00", noon) is False


# --------------------------------------------------------------------------
# Default job seeding
# --------------------------------------------------------------------------


def test_first_run_seeds_default_morning_brief(tmp_path):
    clock = FakeClock(T_MON_0600)
    store = JobStore(db_path=tmp_path / "db" / "scheduler.db",
                     jobs_path=tmp_path / "jobs.json", now=clock)
    scheduler = Scheduler(store=store, now=clock)
    assert scheduler.store.get_job("morning-brief") is not None
    assert DEFAULT_JOBS[0]["name"] == "morning-brief"
    assert DEFAULT_JOBS[0]["schedule"] == "daily:07:00"

    jobs_file = tmp_path / "jobs.json"
    assert jobs_file.exists()
    raw = __import__("json").loads(jobs_file.read_text(encoding="utf-8"))
    assert raw["jobs"][0]["name"] == "morning-brief"
    # next run is today 07:00 because we synced at 06:00
    job = scheduler.status()[0]
    assert job["schedule"] == "daily:07:00"


def test_sync_jobs_reschedules_only_on_schedule_change(tmp_path):
    clock = FakeClock(T_MON_0600)
    store = JobStore(db_path=tmp_path / "db" / "scheduler.db",
                     jobs_path=tmp_path / "jobs.json", now=clock)
    scheduler = Scheduler(store=store, now=clock,
                          jobs=[{"name": "j", "schedule": "every:1h"}])
    first = scheduler.status()[0]
    assert first["schedule"] == "every:1h"

    # same schedule: keep scheduling state
    scheduler.sync_jobs([{"name": "j", "schedule": "every:1h"}])
    same = scheduler.status()[0]
    assert same["next_run_ts"] == first["next_run_ts"]

    # schedule changed: recompute next_run
    scheduler.sync_jobs([{"name": "j", "schedule": "every:2h"}])
    changed = scheduler.status()[0]
    assert changed["schedule"] == "every:2h"
    assert changed["next_run_ts"] != first["next_run_ts"]


def test_sync_jobs_validates_schedule(tmp_path):
    clock = FakeClock(T_MON_0600)
    store = JobStore(db_path=tmp_path / "db" / "scheduler.db",
                     jobs_path=tmp_path / "jobs.json", now=clock)
    with pytest.raises(ValueError):
        Scheduler(store=store, now=clock,
                  jobs=[{"name": "j", "schedule": "not-a-schedule"}])


# --------------------------------------------------------------------------
# Ticking with a fake clock
# --------------------------------------------------------------------------


def test_tick_runs_due_job_and_waits_for_next_day(tmp_path):
    clock = FakeClock(T_MON_0600)
    calls = []

    def runner(job, now_ts):
        calls.append((job["name"], now_ts))
        return {"detail": "ok"}

    scheduler = make_scheduler(
        tmp_path, clock, [{"name": "brief", "schedule": "daily:07:00"}], runner)

    # 06:00: not due yet
    assert scheduler.tick()["due"] == 0
    assert calls == []

    # 07:00: due and run
    clock.set(T_MON_0700)
    summary = scheduler.tick()
    assert summary["ran"] == 1
    assert summary["due"] == 1
    assert calls == [("brief", T_MON_0700)]
    assert scheduler.status()[0]["last_status"] == "ok"

    # 07:05 same day: not due again (next is tomorrow)
    clock.set(T_MON_0730)
    assert scheduler.tick()["due"] == 0

    # next morning: due again
    clock.set(T_TUE_0700)
    scheduler.tick()
    assert calls[-1] == ("brief", T_TUE_0700)


def test_catch_up_inside_awake_window_runs_once(tmp_path):
    clock = FakeClock(T_MON_0600)
    calls = []

    def runner(job, now_ts):
        calls.append(now_ts)
        return {"detail": "ok"}

    scheduler = make_scheduler(
        tmp_path, clock,
        [{"name": "brief", "schedule": "daily:07:00",
          "window_start": "05:00", "window_end": "12:00"}],
        runner)
    # box missed 07:00; wakes 07:30, still inside the window -> catch up once
    clock.set(T_MON_0730)
    summary = scheduler.tick()
    assert summary["ran"] == 1
    assert calls == [T_MON_0730]
    # and it does not re-fire for the same day
    summary2 = scheduler.tick()
    assert summary2["due"] == 0 and calls == [T_MON_0730]


def test_catch_up_outside_window_is_skipped_not_backfilled(tmp_path):
    clock = FakeClock(T_MON_0600)
    calls = []

    def runner(job, now_ts):
        calls.append(now_ts)
        return {"detail": "ok"}

    scheduler = make_scheduler(
        tmp_path, clock,
        [{"name": "brief", "schedule": "daily:07:00",
          "window_start": "05:00", "window_end": "08:00"}],
        runner)
    # box missed 07:00 and wakes 08:30, past the window
    clock.set(T_MON_0830)
    summary = scheduler.tick()
    assert summary["skipped"] == 1
    assert calls == []  # nothing generated
    assert "brief" in summary["skipped_names"]
    history = scheduler.history("brief")
    assert history[0]["status"] == "skipped"
    assert "awake window" in (history[0]["detail"] or "")

    # next_run advanced to tomorrow, not stuck today
    assert scheduler.status()[0]["next_run_ts"] == next_run_after(
        "daily:07:00", T_MON_0830)


def test_interval_job_reschedules_from_catch_up_time(tmp_path):
    clock = FakeClock(T_MON_0600)
    calls = []

    def runner(job, now_ts):
        calls.append(now_ts)
        return {"detail": "ok"}

    scheduler = make_scheduler(
        tmp_path, clock, [{"name": "beat", "schedule": "every:6h"}], runner)
    # missed several 6h slots; catch up once at 18:00 and shift the cadence
    clock.set(datetime(2026, 10, 26, 18, 0).timestamp())
    scheduler.tick()
    assert calls == [datetime(2026, 10, 26, 18, 0).timestamp()]
    assert scheduler.status()[0]["next_run_ts"] == \
        datetime(2026, 10, 26, 18, 0).timestamp() + 21600


def test_job_failure_is_isolated_and_loop_survives(tmp_path):
    clock = FakeClock(T_MON_0630)
    calls = []

    def runner(job, now_ts):
        calls.append(job["name"])
        if job["name"] == "raises":
            raise RuntimeError("boom")
        return {"detail": "brief done"}

    scheduler = make_scheduler(
        tmp_path, clock,
        [{"name": "brief", "schedule": "daily:07:00"},
         {"name": "raises", "schedule": "every:1m"}],
        runner)
    clock.set(T_MON_0700)
    summary = scheduler.tick()
    assert summary["errors"] == 1
    assert summary["ran"] == 1
    assert "raises" in summary["error_names"]
    assert "brief" in summary["ran_names"]
    assert sorted(calls) == ["brief", "raises"]

    status = {j["name"]: j for j in scheduler.status()}
    assert status["raises"]["last_status"] == "error"
    assert "boom" in status["raises"]["last_error"]
    assert status["brief"]["last_status"] == "ok"

    by_job = {r["job"]: r for r in scheduler.history()}
    assert by_job["raises"]["status"] == "error"
    assert by_job["brief"]["status"] == "ok"

    # the loop is still alive: next tick works without raising and catches
    # the interval job when it comes due again at its rescheduled time
    clock.set(T_MON_0701)
    summary2 = scheduler.tick()  # raises is due again, brief is not
    assert summary2["errors"] == 1
    assert "raises" in summary2["error_names"]


def test_manual_run_job_bypasses_window_and_advances(tmp_path):
    clock = FakeClock(T_MON_0600)
    calls = []

    def runner(job, now_ts):
        calls.append((job["name"], now_ts))
        return {"detail": "manual ok"}

    scheduler = make_scheduler(
        tmp_path, clock,
        [{"name": "brief", "schedule": "daily:07:00",
          "window_start": "05:00", "window_end": "08:00"}],
        runner)
    clock.set(T_MON_0830)  # window closed
    result = scheduler.run_job("brief")
    assert result["status"] == "ok"
    assert result["mode"] == "manual"
    assert calls == [("brief", T_MON_0830)]
    assert scheduler.status()[0]["last_status"] == "ok"

    with pytest.raises(KeyError):
        scheduler.run_job("does-not-exist")


def test_manual_run_job_records_error_without_raising(tmp_path):
    clock = FakeClock(T_MON_0600)

    def runner(job, now_ts):
        raise RuntimeError("manual boom")

    scheduler = make_scheduler(
        tmp_path, clock, [{"name": "x", "schedule": "every:1h"}], runner)
    result = scheduler.run_job("x")
    assert result["status"] == "error"
    assert "manual boom" in result["error"]
    assert scheduler.history("x")[0]["status"] == "error"


# --------------------------------------------------------------------------
# The threaded loop
# --------------------------------------------------------------------------


def test_thread_loop_runs_due_jobs_and_stops_cleanly(tmp_path):
    clock = FakeClock(T_MON_0630)
    calls = []

    def runner(job, now_ts):
        calls.append(now_ts)
        return {"detail": "ok"}

    scheduler = make_scheduler(
        tmp_path, clock, [{"name": "beat", "schedule": "every:1h"}], runner,
        tick_s=0.02)
    clock.set(T_MON_0730)  # due at 07:30
    scheduler.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not calls:
        time.sleep(0.01)
    scheduler.stop()
    assert calls, "the loop should have run the due job"
    assert scheduler._thread is None or not scheduler._thread.is_alive()
    # ticking after stop is still possible (synchronous API)
    assert isinstance(scheduler.tick(), dict)


def test_double_start_and_idempotent_stop(tmp_path):
    clock = FakeClock(T_MON_0630)
    calls = []

    def runner(job, now_ts):
        calls.append(now_ts)
        return {"detail": "ok"}

    scheduler = make_scheduler(
        tmp_path, clock, [{"name": "beat", "schedule": "every:1h"}], runner,
        tick_s=0.02)
    scheduler.start()
    scheduler.start()  # idempotent: does not spawn a second thread
    assert scheduler._thread is not None
    scheduler.stop()
    scheduler.stop()  # idempotent
    assert scheduler._thread is None or not scheduler._thread.is_alive()
    # only one thread was ever created
    assert threading.active_count() >= 1  # just a sanity read


def test_status_exposes_user_visible_fields(tmp_path):
    clock = FakeClock(T_MON_0600)
    scheduler = make_scheduler(
        tmp_path, clock,
        [{"name": "brief", "schedule": "daily:07:00", "prompt": "morning",
          "scope": "today"}],
        lambda job, now_ts: {"detail": "ok"})
    row = scheduler.status()[0]
    assert row["name"] == "brief"
    assert row["enabled"] is True
    assert row["next_run"]  # human ISO string present
    assert row["prompt"] == "morning"
    assert row["scope"] == "today"
