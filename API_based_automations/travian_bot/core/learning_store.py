# core/learning_store.py
from __future__ import annotations
import json, time
from pathlib import Path
from typing import Optional

class LearningStore:
    def __init__(self, path: str = "database/learning/raid_targets_stats.json") -> None:
        # Use a generic filename; migrate seamlessly from legacy if present
        self.path = Path(path)
        self.legacy_path = Path("database/learning/oasis_stats.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {}
        self._migrated_from_legacy = False
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                return
            # Legacy migration path: load old file if present
            if self.legacy_path.exists():
                self.data = json.loads(self.legacy_path.read_text(encoding="utf-8"))
                self._migrated_from_legacy = True
                return
        except Exception:
            self.data = {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
        # After successful save to new path, remove legacy file to avoid confusion
        try:
            if self._migrated_from_legacy and self.legacy_path.exists():
                self.legacy_path.unlink()
                self._migrated_from_legacy = False
        except Exception:
            pass

    def get_multiplier(self, key: str) -> float:
        return float(self.data.get(key, {}).get("multiplier", 1.0))

    def record_attempt(self, key: str, unit: str, recommended: int, sent: int, result: str, loss_pct: Optional[float] = None, haul: Optional[dict] = None) -> None:
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
        # Aggregate haul (resources looted) if provided
        if isinstance(haul, dict):
            try:
                tw = int(haul.get("wood", 0) or 0)
                tc = int(haul.get("clay", 0) or 0)
                ti = int(haul.get("iron", 0) or 0)
                tr = int(haul.get("crop", 0) or 0)
                tot = max(0, tw + tc + ti + tr)
                agg = s.setdefault("total_loot", {"wood": 0, "clay": 0, "iron": 0, "crop": 0, "total": 0})
                agg["wood"] = int(agg.get("wood", 0)) + tw
                agg["clay"] = int(agg.get("clay", 0)) + tc
                agg["iron"] = int(agg.get("iron", 0)) + ti
                agg["crop"] = int(agg.get("crop", 0)) + tr
                agg["total"] = int(agg.get("total", 0)) + tot
                s["last_haul"] = {"ts": now, "wood": tw, "clay": tc, "iron": ti, "crop": tr, "total": tot}
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
        """Return a baseline snapshot for a raid target key '(x,y)'.

        Includes attempts, successes, failures, last_result/ts/loss_pct,
        average loss_pct over recent history, multiplier and total loot aggregate.
        """
        s = self.data.get(key, {}) or {}
        out = {
            "multiplier": float(s.get("multiplier", 1.0)),
            "attempts": int(s.get("attempts", 0)),
            "successes": int(s.get("successes", 0)),
            "failures": int(s.get("failures", 0)),
            "last_result": s.get("last_result"),
            "last_ts": s.get("last_ts"),
            "last_loss_pct": s.get("last_loss_pct"),
            "avg_loss_pct": s.get("avg_loss_pct"),
        }
        try:
            agg = s.get("total_loot", {}) or {}
            out["total_loot_total"] = int(agg.get("total", 0))
            out["total_loot"] = {
                "wood": int(agg.get("wood", 0)),
                "clay": int(agg.get("clay", 0)),
                "iron": int(agg.get("iron", 0)),
                "crop": int(agg.get("crop", 0)),
            }
        except Exception:
            pass
        return out

    # --- Scheduling helpers (optional) ---
    def set_last_sent(self, key: str, ts: Optional[float] = None) -> None:
        try:
            if ts is None:
                ts = time.time()
            s = self.data.setdefault(key, {"multiplier": 1.0})
            s["last_sent_ts"] = float(ts)
            self._save()
        except Exception:
            pass

    def get_last_sent(self, key: str) -> Optional[float]:
        try:
            v = self.data.get(key, {}).get("last_sent_ts")
            return float(v) if v is not None else None
        except Exception:
            return None
