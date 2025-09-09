from __future__ import annotations
import json
import time
from pathlib import Path
from datetime import datetime, timedelta

_PATH = Path("database/metrics.json")
_ACTIVITY_PATH = Path("database/activity.json")


def _load() -> dict:
    try:
        if _PATH.exists():
            return json.loads(_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save(data: dict) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_PATH)
    except Exception:
        pass


def inc_counter(name: str, n: int = 1) -> None:
    d = _load()
    ctrs = d.setdefault("counters", {})
    ctrs[name] = int(ctrs.get(name, 0)) + int(n)
    d["_ts"] = time.time()
    _save(d)


def add_sent(n: int = 1) -> None:
    inc_counter("raids_sent", n)


def add_skip(reason: str, n: int = 1) -> None:
    d = _load()
    ctrs = d.setdefault("counters", {})
    ctrs["raids_skipped"] = int(ctrs.get("raids_skipped", 0)) + int(n)
    reasons = d.setdefault("skip_reasons", {})
    reasons[reason] = int(reasons.get(reason, 0)) + int(n)
    d["_ts"] = time.time()
    _save(d)


def add_learning_change(oasis_key: str, old: float, new: float, direction: str, loss_pct: float | None = None) -> None:
    d = _load()
    changes = d.setdefault("learning_changes", [])
    changes.append({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "oasis": oasis_key,
        "old": round(float(old), 3),
        "new": round(float(new), 3),
        "dir": direction,
        "loss_pct": None if loss_pct is None else round(float(loss_pct), 3),
    })
    # keep only last 100 changes
    if len(changes) > 100:
        d["learning_changes"] = changes[-100:]
    d["_ts"] = time.time()
    _save(d)


def set_hero_status_summary(status: dict) -> None:
    d = _load()
    d["hero_status"] = status
    d["_ts"] = time.time()
    _save(d)


def snapshot_and_reset() -> dict:
    """Return current metrics snapshot and reset counters and changes.
    Leaves hero_status intact to show last known status.
    """
    d = _load()
    snap = {
        "counters": d.get("counters", {}).copy(),
        "skip_reasons": d.get("skip_reasons", {}).copy(),
        "learning_changes": d.get("learning_changes", []).copy(),
        "hero_status": d.get("hero_status", None),
    }
    # reset
    d["counters"] = {}
    d["skip_reasons"] = {}
    d["learning_changes"] = []
    d["_ts"] = time.time()
    _save(d)
    return snap


# === Activity tracking (per-day windows) ===

def _act_load() -> dict:
    try:
        if _ACTIVITY_PATH.exists():
            return json.loads(_ACTIVITY_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _act_save(data: dict) -> None:
    try:
        _ACTIVITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ACTIVITY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_ACTIVITY_PATH)
    except Exception:
        pass


def activity_init(session_start_ts: float | None = None) -> None:
    """Initialize a session start for activity accounting.

    Stores a `session_start` timestamp if not present, and keeps `first_seen` as
    the earliest ever start we observed in the file.
    """
    d = _act_load()
    now_ts = time.time()
    ss = float(session_start_ts) if session_start_ts is not None else now_ts
    if not d.get("session_start_ts"):
        d["session_start_ts"] = ss
        d["session_start_iso"] = datetime.fromtimestamp(ss).isoformat(timespec="seconds")
    if not d.get("first_seen_ts"):
        d["first_seen_ts"] = ss
        d["first_seen_iso"] = datetime.fromtimestamp(ss).isoformat(timespec="seconds")
    d.setdefault("per_day", {})
    d.setdefault("totals", {}).setdefault("all_time_active_sec", 0)
    d.setdefault("sessions", [])
    # Start a new session record if last one is closed or absent
    sessions = d["sessions"]
    if not sessions or sessions[-1].get("end_ts"):
        sessions.append({
            "start_ts": ss,
            "start_iso": datetime.fromtimestamp(ss).isoformat(timespec="seconds"),
        })
    _act_save(d)


def _split_by_day(s_ts: float, e_ts: float) -> list[tuple[str, float, float]]:
    """Split an interval into day-bounded segments.
    Returns [(date_str, start_ts, end_ts), ...] in local time.
    """
    out = []
    cur = s_ts
    while cur < e_ts:
        cur_dt = datetime.fromtimestamp(cur)
        day_end = datetime(cur_dt.year, cur_dt.month, cur_dt.day) + timedelta(days=1)
        seg_end = min(e_ts, day_end.timestamp())
        out.append((cur_dt.strftime("%Y-%m-%d"), cur, seg_end))
        cur = seg_end
    return out


def activity_record_window(start_ts: float, end_ts: float | None = None) -> None:
    """Record an active window (e.g., one cycle processing time, excluding waits)."""
    try:
        e = float(end_ts) if end_ts is not None else time.time()
        s = float(start_ts)
        if e <= s:
            return
        d = _act_load()
        d.setdefault("per_day", {})
        d.setdefault("totals", {}).setdefault("all_time_active_sec", 0)
        for day, a, b in _split_by_day(s, e):
            sec = int(max(0, b - a))
            pd = d["per_day"].setdefault(day, {"active_sec": 0, "windows": []})
            pd["active_sec"] = int(pd.get("active_sec", 0)) + sec
            pd["windows"].append({
                "start_ts": a,
                "end_ts": b,
                "start_iso": datetime.fromtimestamp(a).isoformat(timespec="seconds"),
                "end_iso": datetime.fromtimestamp(b).isoformat(timespec="seconds"),
                "sec": sec,
            })
            d["totals"]["all_time_active_sec"] = int(d["totals"].get("all_time_active_sec", 0)) + sec
        # Update current session end
        sessions = d.setdefault("sessions", [])
        if sessions:
            sessions[-1]["end_ts"] = e
            sessions[-1]["end_iso"] = datetime.fromtimestamp(e).isoformat(timespec="seconds")
        _act_save(d)
    except Exception:
        pass


def activity_summary(now: float | None = None) -> dict:
    """Return a compact summary: today/yesterday active seconds and session uptime.
    """
    now_ts = float(now) if now is not None else time.time()
    d = _act_load()
    per_day = d.get("per_day", {})
    today = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d")
    yday = (datetime.fromtimestamp(now_ts) - timedelta(days=1)).strftime("%Y-%m-%d")
    today_sec = int(per_day.get(today, {}).get("active_sec", 0))
    yday_sec = int(per_day.get(yday, {}).get("active_sec", 0))
    ss = float(d.get("session_start_ts", now_ts))
    session_uptime_sec = int(max(0, now_ts - ss))
    # Windows count for today
    today_windows = int(len(per_day.get(today, {}).get("windows", [])))
    return {
        "today_active_sec": today_sec,
        "yesterday_active_sec": yday_sec,
        "today_windows": today_windows,
        "session_start_iso": d.get("session_start_iso"),
        "session_uptime_sec": session_uptime_sec,
        "all_time_active_sec": int(d.get("totals", {}).get("all_time_active_sec", 0)),
    }


def _fmt_hm(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h{m:02d}m"


def render_activity_lines() -> list[str]:
    """Human-readable lines for end-of-cycle reporting."""
    s = activity_summary()
    lines = []
    lines.append(f"- Activity today: {_fmt_hm(s['today_active_sec'])} in {s['today_windows']} window(s)")
    lines.append(f"- Activity yesterday: {_fmt_hm(s['yesterday_active_sec'])}")
    if s.get("session_start_iso"):
        lines.append(f"- Session uptime: {_fmt_hm(s['session_uptime_sec'])} since {s['session_start_iso']}")
    return lines

