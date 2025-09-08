# core/learning_store.py
from __future__ import annotations
import json, time
from pathlib import Path
from typing import Optional

class LearningStore:
    def __init__(self, path: str = "database/learning/oasis_stats.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def get_multiplier(self, key: str) -> float:
        return float(self.data.get(key, {}).get("multiplier", 1.0))

    def record_attempt(self, key: str, unit: str, recommended: int, sent: int, result: str, loss_pct: Optional[float] = None) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        s = self.data.setdefault(key, {"multiplier": 1.0, "attempts": 0, "successes": 0, "failures": 0})
        s["attempts"] = int(s.get("attempts", 0)) + 1
        if result in ("won", "accepted"):
            s["successes"] = int(s.get("successes", 0)) + 1
        elif result in ("skipped", "failed", "lost"):
            s["failures"] = int(s.get("failures", 0)) + 1
        # Snapshot of last attempt
        s["last"] = {"ts": now, "unit": unit, "recommended": recommended, "sent": sent, "result": result, "loss_pct": loss_pct}
        s["last_ts"] = now
        s["last_result"] = result
        s["last_loss_pct"] = loss_pct
        # Append to short rolling history for baseline decisions
        hist = s.setdefault("history", [])
        hist.append({
            "ts": now,
            "unit": unit,
            "recommended": recommended,
            "sent": sent,
            "result": result,
            "loss_pct": loss_pct,
        })
        # Cap history length
        try:
            max_len = 20
            if isinstance(hist, list) and len(hist) > max_len:
                s["history"] = hist[-max_len:]
        except Exception:
            pass
        # Compute average loss pct over history (excluding None)
        try:
            vals = [h.get("loss_pct") for h in s.get("history", []) if isinstance(h, dict) and isinstance(h.get("loss_pct"), (int, float))]
            if vals:
                s["avg_loss_pct"] = round(sum(vals)/len(vals), 4)
        except Exception:
            pass
        self._save()

    def nudge_multiplier(self, key: str, direction: str, step: float = 0.1, min_mul: float = 0.8, max_mul: float = 2.5) -> float:
        s = self.data.setdefault(key, {"multiplier": 1.0})
        m = float(s.get("multiplier", 1.0))
        if direction == "up":
            m = min(max_mul, m * (1.0 + step))
        elif direction == "down":
            m = max(min_mul, m * (1.0 - step))
        s["multiplier"] = round(m, 3)
        self._save()
        return m

    def get_baseline(self, key: str) -> dict:
        """Return a baseline snapshot for an oasis coordinate key '(x,y)'.

        Includes attempts, successes, failures, last_result/ts/loss_pct,
        average loss_pct over recent history, and multiplier.
        """
        s = self.data.get(key, {}) or {}
        return {
            "multiplier": float(s.get("multiplier", 1.0)),
            "attempts": int(s.get("attempts", 0)),
            "successes": int(s.get("successes", 0)),
            "failures": int(s.get("failures", 0)),
            "last_result": s.get("last_result"),
            "last_ts": s.get("last_ts"),
            "last_loss_pct": s.get("last_loss_pct"),
            "avg_loss_pct": s.get("avg_loss_pct"),
        }
