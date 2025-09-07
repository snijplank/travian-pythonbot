from __future__ import annotations
import os
from dataclasses import dataclass

# YAML is the single source of truth for config.
try:
    import yaml  # type: ignore
except Exception:
    yaml = None



def _strtobool(v: str) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _load_yaml(filename: str) -> dict:
    """Load YAML relative to the package root, independent of current working directory."""
    # config.py path: .../travian_bot/config/config.py → root is two parents up
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    path = os.path.join(base_dir, filename)
    if not os.path.exists(path) or yaml is None:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _get(cfg: dict, key: str, default):
    # Only read from YAML, do not consider environment variables.
    return cfg.get(key, default)


def _as_int(val, default: int) -> int:
    try:
        return int(str(val))
    except Exception:
        return default


def _as_str(val, default: str) -> str:
    try:
        return str(val)
    except Exception:
        return default


def _as_bool(val, default: bool) -> bool:
    try:
        return _strtobool(str(val))
    except Exception:
        return default


@dataclass
class Settings:
    # Core cadence
    WAIT_BETWEEN_CYCLES_MINUTES: int = 10
    JITTER_MINUTES: int = 10
    SERVER_SELECTION: int = 0

    # Daily limiter
    ENABLE_CYCLE_LIMITER: bool = False
    DAILY_MAX_RUNTIME_MINUTES: int = 600   # 10h
    DAILY_BLOCKS: int = 3                  # split across the day

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"

    ESCORT_UNIT_PRIORITY: list[str] | None = None

    RESERVED: dict | None = None

    # Learning loop (configurable)
    LEARNING_MIN_MUL: float = 0.8
    LEARNING_MAX_MUL: float = 2.5
    LEARNING_LOSS_THRESHOLD_LOW: float = 0.20
    LEARNING_LOSS_THRESHOLD_HIGH: float = 0.50
    LEARNING_STEP_UP_ON_LOST: float = 0.25
    LEARNING_STEP_UP_ON_HIGH_LOSS: float = 0.10
    LEARNING_STEP_DOWN_ON_LOW_LOSS: float = 0.10

    # Credentials (optional; can be prompted interactively if empty)
    TRAVIAN_EMAIL: str = ""
    TRAVIAN_PASSWORD: str = ""

    def as_dict(self) -> dict:
        return {
            "WAIT_BETWEEN_CYCLES_MINUTES": self.WAIT_BETWEEN_CYCLES_MINUTES,
            "JITTER_MINUTES": self.JITTER_MINUTES,
            "SERVER_SELECTION": self.SERVER_SELECTION,
            "ENABLE_CYCLE_LIMITER": self.ENABLE_CYCLE_LIMITER,
            "DAILY_MAX_RUNTIME_MINUTES": self.DAILY_MAX_RUNTIME_MINUTES,
            "DAILY_BLOCKS": self.DAILY_BLOCKS,
            "LOG_LEVEL": self.LOG_LEVEL,
            "LOG_DIR": self.LOG_DIR,
            "ESCORT_UNIT_PRIORITY": self.ESCORT_UNIT_PRIORITY or [],
            "RESERVED": self.RESERVED or {},
            "LEARNING_MIN_MUL": self.LEARNING_MIN_MUL,
            "LEARNING_MAX_MUL": self.LEARNING_MAX_MUL,
            "LEARNING_LOSS_THRESHOLD_LOW": self.LEARNING_LOSS_THRESHOLD_LOW,
            "LEARNING_LOSS_THRESHOLD_HIGH": self.LEARNING_LOSS_THRESHOLD_HIGH,
            "LEARNING_STEP_UP_ON_LOST": self.LEARNING_STEP_UP_ON_LOST,
            "LEARNING_STEP_UP_ON_HIGH_LOSS": self.LEARNING_STEP_UP_ON_HIGH_LOSS,
            "LEARNING_STEP_DOWN_ON_LOW_LOSS": self.LEARNING_STEP_DOWN_ON_LOW_LOSS,
            "TRAVIAN_EMAIL": self.TRAVIAN_EMAIL,
            "TRAVIAN_PASSWORD": "***" if self.TRAVIAN_PASSWORD else "",
        }


def load_settings(env_prefix: str = "") -> Settings:
    s = Settings()
    cfg = _load_yaml("config.yaml")

    def g(name: str, default):
        key = (env_prefix + name) if env_prefix else name
        return _get(cfg, key, default)

    s.WAIT_BETWEEN_CYCLES_MINUTES = _as_int(g("WAIT_BETWEEN_CYCLES_MINUTES", s.WAIT_BETWEEN_CYCLES_MINUTES), s.WAIT_BETWEEN_CYCLES_MINUTES)
    s.JITTER_MINUTES = _as_int(g("JITTER_MINUTES", s.JITTER_MINUTES), s.JITTER_MINUTES)
    s.SERVER_SELECTION = _as_int(g("SERVER_SELECTION", s.SERVER_SELECTION), s.SERVER_SELECTION)

    s.ENABLE_CYCLE_LIMITER = _as_bool(g("ENABLE_CYCLE_LIMITER", s.ENABLE_CYCLE_LIMITER), s.ENABLE_CYCLE_LIMITER)
    s.DAILY_MAX_RUNTIME_MINUTES = _as_int(g("DAILY_MAX_RUNTIME_MINUTES", s.DAILY_MAX_RUNTIME_MINUTES), s.DAILY_MAX_RUNTIME_MINUTES)
    s.DAILY_BLOCKS = _as_int(g("DAILY_BLOCKS", s.DAILY_BLOCKS), s.DAILY_BLOCKS)

    s.LOG_LEVEL = _as_str(g("LOG_LEVEL", s.LOG_LEVEL), s.LOG_LEVEL)
    s.LOG_DIR = _as_str(g("LOG_DIR", s.LOG_DIR), s.LOG_DIR)

    # Escort priority (comma separated in .env, or list in YAML)
    prio = g("ESCORT_UNIT_PRIORITY", "t5,t3,t1,t2,t4,t6,t7,t8,t9,t10")
    if isinstance(prio, str):
        s.ESCORT_UNIT_PRIORITY = [p.strip() for p in prio.split(",") if p.strip()]
    elif isinstance(prio, list):
        s.ESCORT_UNIT_PRIORITY = [str(p).strip() for p in prio if str(p).strip()]
    else:
        s.ESCORT_UNIT_PRIORITY = ["t5","t3","t1","t2","t4","t6","t7","t8","t9","t10"]

    reserved = g("RESERVED", {}) or {}
    s.RESERVED = reserved if isinstance(reserved, dict) else {}

    # Learning loop parameters
    def _as_float(val, default: float) -> float:
        try:
            return float(str(val))
        except Exception:
            return default

    s.LEARNING_MIN_MUL = _as_float(g("LEARNING_MIN_MUL", s.LEARNING_MIN_MUL), s.LEARNING_MIN_MUL)
    s.LEARNING_MAX_MUL = _as_float(g("LEARNING_MAX_MUL", s.LEARNING_MAX_MUL), s.LEARNING_MAX_MUL)
    s.LEARNING_LOSS_THRESHOLD_LOW = _as_float(g("LEARNING_LOSS_THRESHOLD_LOW", s.LEARNING_LOSS_THRESHOLD_LOW), s.LEARNING_LOSS_THRESHOLD_LOW)
    s.LEARNING_LOSS_THRESHOLD_HIGH = _as_float(g("LEARNING_LOSS_THRESHOLD_HIGH", s.LEARNING_LOSS_THRESHOLD_HIGH), s.LEARNING_LOSS_THRESHOLD_HIGH)
    s.LEARNING_STEP_UP_ON_LOST = _as_float(g("LEARNING_STEP_UP_ON_LOST", s.LEARNING_STEP_UP_ON_LOST), s.LEARNING_STEP_UP_ON_LOST)
    s.LEARNING_STEP_UP_ON_HIGH_LOSS = _as_float(g("LEARNING_STEP_UP_ON_HIGH_LOSS", s.LEARNING_STEP_UP_ON_HIGH_LOSS), s.LEARNING_STEP_UP_ON_HIGH_LOSS)
    s.LEARNING_STEP_DOWN_ON_LOW_LOSS = _as_float(g("LEARNING_STEP_DOWN_ON_LOW_LOSS", s.LEARNING_STEP_DOWN_ON_LOW_LOSS), s.LEARNING_STEP_DOWN_ON_LOW_LOSS)

    # Credentials
    s.TRAVIAN_EMAIL = _as_str(g("TRAVIAN_EMAIL", s.TRAVIAN_EMAIL), s.TRAVIAN_EMAIL)
    s.TRAVIAN_PASSWORD = _as_str(g("TRAVIAN_PASSWORD", s.TRAVIAN_PASSWORD), s.TRAVIAN_PASSWORD)
    return s


settings = load_settings()
