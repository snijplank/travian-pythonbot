import json
import time
from pathlib import Path
from typing import Any, Optional


def _now() -> float:
    try:
        return time.time()
    except Exception:
        return float(int(time.time()))


def load_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def atomic_write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


class JsonKvCache:
    """Very small JSON key-value cache with timestamp support.

    Stored format: { key: {"value": ..., "ts": epoch_seconds} }
    """

    def __init__(self, file_path: str | Path):
        self.path = Path(file_path)
        self._data = load_json(self.path)

    def get(self, key: str) -> Optional[dict]:
        try:
            v = self._data.get(key)
            if isinstance(v, dict) and ("value" in v or "ts" in v):
                return v
        except Exception:
            pass
        return None

    def set(self, key: str, value: Any, ts: Optional[float] = None) -> None:
        try:
            if ts is None:
                ts = _now()
            self._data[str(key)] = {"value": value, "ts": float(ts)}
            atomic_write_json(self.path, self._data)
        except Exception:
            pass

    def purge_older_than(self, max_age_sec: int) -> None:
        try:
            now = _now()
            keys = list(self._data.keys())
            changed = False
            for k in keys:
                try:
                    ts = float(self._data[k].get("ts", 0))
                except Exception:
                    ts = 0
                if max_age_sec > 0 and (now - ts) > max_age_sec:
                    self._data.pop(k, None)
                    changed = True
            if changed:
                atomic_write_json(self.path, self._data)
        except Exception:
            pass

