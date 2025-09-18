#!/usr/bin/env python3
from __future__ import annotations
import sys
from pathlib import Path

try:
    import yaml  # type: ignore
except Exception as e:
    print(f"❌ PyYAML not installed: {e}")
    sys.exit(1)


SECTIONS = {
    "cadence": [
        "WAIT_BETWEEN_CYCLES_MINUTES", "JITTER_MINUTES", "SERVER_SELECTION",
    ],
    "limiter": [
        "ENABLE_CYCLE_LIMITER", "DAILY_MAX_RUNTIME_MINUTES", "DAILY_BLOCKS",
    ],
    "logging": [
        "LOG_LEVEL", "LOG_DIR",
    ],
    "humanizer": [
        "HUMAN_MIN_DELAY", "HUMAN_MAX_DELAY", "HUMAN_LONG_PAUSE_EVERY",
        "HUMAN_LONG_PAUSE_MIN", "HUMAN_LONG_PAUSE_MAX",
    ],
    "operation": [
        "OP_JITTER_MIN_SEC", "OP_JITTER_MAX_SEC",
    ],
    "map_and_farms": [
        "MAPVIEW_PRE_ACTION_PROB", "MAPVIEW_FARM_LIST_PROB",
        "FARM_LIST_SUBSET_MIN", "FARM_LIST_SUBSET_MAX",
    ],
    "raiding": [
        "SKIP_FARM_LISTS_FIRST_RUN", "ESCORT_UNIT_PRIORITY",
    ],
    "learning": [
        "LEARNING_MIN_MUL", "LEARNING_MAX_MUL", "LEARNING_LOSS_THRESHOLD_LOW",
        "LEARNING_LOSS_THRESHOLD_HIGH", "LEARNING_STEP_UP_ON_LOST",
        "LEARNING_STEP_UP_ON_HIGH_LOSS", "LEARNING_STEP_UP_ON_FULL_LOOT",
        "LEARNING_PAUSE_ON_LOSS_SEC", "LEARNING_PRIORITY_RETRY_SEC",
    ],
    "credentials": [
        "TRAVIAN_EMAIL", "TRAVIAN_PASSWORD",
    ],
}


def is_attack_detector_key(k: str) -> bool:
    return k.startswith("ATTACK_DETECTOR_")


def format_config(path: Path) -> None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # If already nested (has our top sections), just rewrite with nice ordering
    flat: dict = {}
    def _flatten(d):
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, dict):
                    _flatten(v)
                else:
                    flat[k] = v
    _flatten(raw)
    # Build new nested structure
    out: dict = {}
    out["cadence"] = {k: flat[k] for k in SECTIONS["cadence"] if k in flat}
    out["limiter"] = {k: flat[k] for k in SECTIONS["limiter"] if k in flat}
    out["logging"] = {k: flat[k] for k in SECTIONS["logging"] if k in flat}
    out["humanizer"] = {k: flat[k] for k in SECTIONS["humanizer"] if k in flat}
    out["operation"] = {k: flat[k] for k in SECTIONS["operation"] if k in flat}
    out["map_and_farms"] = {k: flat[k] for k in SECTIONS["map_and_farms"] if k in flat}
    out["raiding"] = {k: flat[k] for k in SECTIONS["raiding"] if k in flat}
    out["learning"] = {k: flat[k] for k in SECTIONS["learning"] if k in flat}
    out["credentials"] = {k: flat[k] for k in SECTIONS["credentials"] if k in flat}
    # Attack detector block
    atk = {k: v for k, v in flat.items() if is_attack_detector_key(k)}
    if atk:
        out["attack_detector"] = atk
    # Any remaining top-level keys (unlikely) preserved under 'other'
    used = set().union(*[set(v) for v in SECTIONS.values()])
    used |= set(atk.keys())
    other = {k: v for k, v in flat.items() if k not in used}
    if other:
        out["other"] = other

    # Write back with preserved order
    path.write_text(yaml.dump(out, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"✅ Reformatted {path}")


def main():
    cfg_path = Path(__file__).resolve().parents[1] / "config.yaml"
    if not cfg_path.exists():
        print(f"❌ config.yaml not found at {cfg_path}")
        return 1
    format_config(cfg_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
