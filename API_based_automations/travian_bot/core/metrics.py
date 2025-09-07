from __future__ import annotations
import json
import time
from pathlib import Path

_PATH = Path("database/metrics.json")


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

