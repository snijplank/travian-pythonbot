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


def _flatten_cfg(d: dict) -> dict:
    """Recursively flatten a nested YAML dict so that leaf keys appear at the top level.
    Later duplicates overwrite earlier ones (nested keys win over root when traversed in-order).
    """
    flat: dict = {}

    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, dict):
                    _walk(v)
                else:
                    flat[k] = v
        # ignore lists at top-level for flatten purposes

    _walk(d or {})
    # Also keep top-level keys (in case of non-dict values at root)
    for k, v in (d or {}).items():
        if not isinstance(v, dict):
            flat.setdefault(k, v)
    return flat


def _get(cfg: dict, key: str, default):
    # Only read from YAML (flattened), do not consider environment variables.
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
    DAILY_VARIANCE_PCT: float = 0.0        # e.g., 0.1 for ±10% variance on daily cap

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"

    ESCORT_UNIT_PRIORITY: list[str] | None = None

    RESERVED: dict | None = None
    NEW_VILLAGE_PRESET_ENABLE: bool = False

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

    # Humanizer (anti-bot cadence)
    HUMAN_MIN_DELAY: float = 0.6      # seconds, min think-time between requests
    HUMAN_MAX_DELAY: float = 2.2      # seconds, max think-time between requests
    HUMAN_LONG_PAUSE_EVERY: int = 7   # after N requests add a longer pause
    HUMAN_LONG_PAUSE_MIN: float = 3.0
    HUMAN_LONG_PAUSE_MAX: float = 6.0

    # Operation jitter (between high-level steps)
    OP_JITTER_MIN_SEC: float = 0.5
    OP_JITTER_MAX_SEC: float = 2.0

    # Additional human-like behavior toggles
    HUMAN_LONG_PAUSE_PROB: float = 0.12   # probability for a long pause after a request
    SHUFFLE_VILLAGE_ORDER: bool = True    # randomize village order per cycle
    FARM_LIST_RANDOM_SKIP_PROB: float = 0.15  # chance to skip farm lists for a village this cycle
    OP_COFFEE_BREAK_PROB: float = 0.10    # chance to add an extra break between cycles
    OP_COFFEE_BREAK_MIN_MINUTES: int = 2
    OP_COFFEE_BREAK_MAX_MINUTES: int = 6

    # Map view probes (neutral page views) and farm list subset
    MAPVIEW_PRE_ACTION_PROB: float = 0.35     # chance to open dorf1/dorf2 before big actions
    MAPVIEW_FARM_LIST_PROB: float = 0.25      # chance to open dorf page before a farm list launch
    FARM_LIST_SUBSET_MIN: int = 1             # minimum number of farm lists to send per village
    FARM_LIST_SUBSET_MAX: int = 3             # maximum number of farm lists to send per village

    # Limiter randomization & quiet windows
    BLOCK_SIZE_MIN: int = 45                  # minutes; if >0, use random block size in [min,max]
    BLOCK_SIZE_MAX: int = 200
    REST_MIN_MINUTES: int = 30                # random rest minutes between blocks
    REST_MAX_MINUTES: int = 90
    QUIET_WINDOWS: list[str] | None = None    # e.g., ["01:00-06:00", "13:15-14:00"]
    SKIP_CYCLE_PROB: float = 0.0              # chance (0..1) to skip an entire cycle

    # Attack detector (screen OCR → Discord)
    ATTACK_DETECTOR_ENABLE: bool = False
    ATTACK_DETECTOR_DISCORD_WEBHOOK: str = ""
    ATTACK_DETECTOR_INTERVAL_BASE: float = 2.0
    ATTACK_DETECTOR_INTERVAL_JITTER: float = 6.0
    ATTACK_DETECTOR_COOLDOWN_SEC: int = 3600
    ATTACK_DETECTOR_OCR_LANGS: list[str] | None = None
    ATTACK_DETECTOR_GPU: bool = False
    ATTACK_DETECTOR_SEND_SCREENSHOT: bool = True
    ATTACK_DETECTOR_USE_HOTKEYS: bool = False
    ATTACK_DETECTOR_REFRESH_COMBO: str = ""

    # Reports debugging
    REPORT_DEBUG_LOG: bool = False
    REPORT_DEBUG_DUMP: bool = False
    PROCESS_REPORTS_IN_AUTOMATION: bool = True

    # Hero adventures
    HERO_ADVENTURE_ENABLE: bool = True
    HERO_ADVENTURE_MIN_HEALTH: int = 40       # % health required to go on adventure
    HERO_ADVENTURE_MAX_DURATION_MIN: int = 180  # skip adventures longer than this (minutes), 0 = no limit
    HERO_ADVENTURE_ALLOW_DANGER: bool = True  # if False, skip adventures marked as dangerous
    HERO_ADVENTURE_POLL_INTERVAL_SEC: int = 90
    HERO_ADVENTURE_RANDOM_JITTER_SEC: int = 45

    # Build guard (wait/retry upgrades instead of skipping)
    BUILD_GUARD_ENABLE: bool = True
    BUILD_GUARD_MAX_RETRIES: int = 5
    BUILD_GUARD_WAIT_MIN_SEC: int = 60
    BUILD_GUARD_WAIT_MAX_SEC: int = 180

    def as_dict(self) -> dict:
        return {
            "WAIT_BETWEEN_CYCLES_MINUTES": self.WAIT_BETWEEN_CYCLES_MINUTES,
            "JITTER_MINUTES": self.JITTER_MINUTES,
            "SERVER_SELECTION": self.SERVER_SELECTION,
            "ENABLE_CYCLE_LIMITER": self.ENABLE_CYCLE_LIMITER,
            "DAILY_MAX_RUNTIME_MINUTES": self.DAILY_MAX_RUNTIME_MINUTES,
            "DAILY_BLOCKS": self.DAILY_BLOCKS,
            "DAILY_VARIANCE_PCT": self.DAILY_VARIANCE_PCT,
            "LOG_LEVEL": self.LOG_LEVEL,
            "LOG_DIR": self.LOG_DIR,
            "ESCORT_UNIT_PRIORITY": self.ESCORT_UNIT_PRIORITY or [],
            "RESERVED": self.RESERVED or {},
            "NEW_VILLAGE_PRESET_ENABLE": self.NEW_VILLAGE_PRESET_ENABLE,
            "LEARNING_MIN_MUL": self.LEARNING_MIN_MUL,
            "LEARNING_MAX_MUL": self.LEARNING_MAX_MUL,
            "LEARNING_LOSS_THRESHOLD_LOW": self.LEARNING_LOSS_THRESHOLD_LOW,
            "LEARNING_LOSS_THRESHOLD_HIGH": self.LEARNING_LOSS_THRESHOLD_HIGH,
            "LEARNING_STEP_UP_ON_LOST": self.LEARNING_STEP_UP_ON_LOST,
            "LEARNING_STEP_UP_ON_HIGH_LOSS": self.LEARNING_STEP_UP_ON_HIGH_LOSS,
            "LEARNING_STEP_DOWN_ON_LOW_LOSS": self.LEARNING_STEP_DOWN_ON_LOW_LOSS,
            "TRAVIAN_EMAIL": self.TRAVIAN_EMAIL,
            "TRAVIAN_PASSWORD": "***" if self.TRAVIAN_PASSWORD else "",
            "HUMAN_MIN_DELAY": self.HUMAN_MIN_DELAY,
            "HUMAN_MAX_DELAY": self.HUMAN_MAX_DELAY,
            "HUMAN_LONG_PAUSE_EVERY": self.HUMAN_LONG_PAUSE_EVERY,
            "HUMAN_LONG_PAUSE_MIN": self.HUMAN_LONG_PAUSE_MIN,
            "HUMAN_LONG_PAUSE_MAX": self.HUMAN_LONG_PAUSE_MAX,
            "OP_JITTER_MIN_SEC": self.OP_JITTER_MIN_SEC,
            "OP_JITTER_MAX_SEC": self.OP_JITTER_MAX_SEC,
            "HUMAN_LONG_PAUSE_PROB": self.HUMAN_LONG_PAUSE_PROB,
            "SHUFFLE_VILLAGE_ORDER": self.SHUFFLE_VILLAGE_ORDER,
            "FARM_LIST_RANDOM_SKIP_PROB": self.FARM_LIST_RANDOM_SKIP_PROB,
            "OP_COFFEE_BREAK_PROB": self.OP_COFFEE_BREAK_PROB,
            "OP_COFFEE_BREAK_MIN_MINUTES": self.OP_COFFEE_BREAK_MIN_MINUTES,
            "OP_COFFEE_BREAK_MAX_MINUTES": self.OP_COFFEE_BREAK_MAX_MINUTES,
            "MAPVIEW_PRE_ACTION_PROB": self.MAPVIEW_PRE_ACTION_PROB,
            "MAPVIEW_FARM_LIST_PROB": self.MAPVIEW_FARM_LIST_PROB,
            "FARM_LIST_SUBSET_MIN": self.FARM_LIST_SUBSET_MIN,
            "FARM_LIST_SUBSET_MAX": self.FARM_LIST_SUBSET_MAX,
            "BLOCK_SIZE_MIN": self.BLOCK_SIZE_MIN,
            "BLOCK_SIZE_MAX": self.BLOCK_SIZE_MAX,
            "REST_MIN_MINUTES": self.REST_MIN_MINUTES,
            "REST_MAX_MINUTES": self.REST_MAX_MINUTES,
            "QUIET_WINDOWS": self.QUIET_WINDOWS or [],
            "SKIP_CYCLE_PROB": self.SKIP_CYCLE_PROB,
            "ATTACK_DETECTOR_ENABLE": self.ATTACK_DETECTOR_ENABLE,
            "ATTACK_DETECTOR_DISCORD_WEBHOOK": bool(self.ATTACK_DETECTOR_DISCORD_WEBHOOK),
            "ATTACK_DETECTOR_INTERVAL_BASE": self.ATTACK_DETECTOR_INTERVAL_BASE,
            "ATTACK_DETECTOR_INTERVAL_JITTER": self.ATTACK_DETECTOR_INTERVAL_JITTER,
            "ATTACK_DETECTOR_COOLDOWN_SEC": self.ATTACK_DETECTOR_COOLDOWN_SEC,
            "ATTACK_DETECTOR_OCR_LANGS": self.ATTACK_DETECTOR_OCR_LANGS or [],
            "ATTACK_DETECTOR_GPU": self.ATTACK_DETECTOR_GPU,
            "ATTACK_DETECTOR_SEND_SCREENSHOT": self.ATTACK_DETECTOR_SEND_SCREENSHOT,
            "ATTACK_DETECTOR_USE_HOTKEYS": self.ATTACK_DETECTOR_USE_HOTKEYS,
            "ATTACK_DETECTOR_REFRESH_COMBO": self.ATTACK_DETECTOR_REFRESH_COMBO,
            "REPORT_DEBUG_LOG": self.REPORT_DEBUG_LOG,
            "REPORT_DEBUG_DUMP": self.REPORT_DEBUG_DUMP,
            "PROCESS_REPORTS_IN_AUTOMATION": self.PROCESS_REPORTS_IN_AUTOMATION,
            "HERO_ADVENTURE_ENABLE": self.HERO_ADVENTURE_ENABLE,
            "HERO_ADVENTURE_MIN_HEALTH": self.HERO_ADVENTURE_MIN_HEALTH,
            "HERO_ADVENTURE_MAX_DURATION_MIN": self.HERO_ADVENTURE_MAX_DURATION_MIN,
            "HERO_ADVENTURE_ALLOW_DANGER": self.HERO_ADVENTURE_ALLOW_DANGER,
            "HERO_ADVENTURE_POLL_INTERVAL_SEC": self.HERO_ADVENTURE_POLL_INTERVAL_SEC,
            "HERO_ADVENTURE_RANDOM_JITTER_SEC": self.HERO_ADVENTURE_RANDOM_JITTER_SEC,
            "BUILD_GUARD_ENABLE": self.BUILD_GUARD_ENABLE,
            "BUILD_GUARD_MAX_RETRIES": self.BUILD_GUARD_MAX_RETRIES,
            "BUILD_GUARD_WAIT_MIN_SEC": self.BUILD_GUARD_WAIT_MIN_SEC,
            "BUILD_GUARD_WAIT_MAX_SEC": self.BUILD_GUARD_WAIT_MAX_SEC,
        }


def load_settings(env_prefix: str = "") -> Settings:
    s = Settings()
    cfg_raw = _load_yaml("config.yaml")
    cfg = _flatten_cfg(cfg_raw)

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
    s.NEW_VILLAGE_PRESET_ENABLE = _as_bool(g("NEW_VILLAGE_PRESET_ENABLE", s.NEW_VILLAGE_PRESET_ENABLE), s.NEW_VILLAGE_PRESET_ENABLE)

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

    # Humanizer
    def _as_float(val, default: float) -> float:
        try:
            return float(str(val))
        except Exception:
            return default
    s.HUMAN_MIN_DELAY = _as_float(g("HUMAN_MIN_DELAY", s.HUMAN_MIN_DELAY), s.HUMAN_MIN_DELAY)
    s.HUMAN_MAX_DELAY = _as_float(g("HUMAN_MAX_DELAY", s.HUMAN_MAX_DELAY), s.HUMAN_MAX_DELAY)
    s.HUMAN_LONG_PAUSE_EVERY = _as_int(g("HUMAN_LONG_PAUSE_EVERY", s.HUMAN_LONG_PAUSE_EVERY), s.HUMAN_LONG_PAUSE_EVERY)
    s.HUMAN_LONG_PAUSE_MIN = _as_float(g("HUMAN_LONG_PAUSE_MIN", s.HUMAN_LONG_PAUSE_MIN), s.HUMAN_LONG_PAUSE_MIN)
    s.HUMAN_LONG_PAUSE_MAX = _as_float(g("HUMAN_LONG_PAUSE_MAX", s.HUMAN_LONG_PAUSE_MAX), s.HUMAN_LONG_PAUSE_MAX)

    # Operation jitter
    s.OP_JITTER_MIN_SEC = _as_float(g("OP_JITTER_MIN_SEC", s.OP_JITTER_MIN_SEC), s.OP_JITTER_MIN_SEC)
    s.OP_JITTER_MAX_SEC = _as_float(g("OP_JITTER_MAX_SEC", s.OP_JITTER_MAX_SEC), s.OP_JITTER_MAX_SEC)

    # Human-like behavior options
    s.HUMAN_LONG_PAUSE_PROB = _as_float(g("HUMAN_LONG_PAUSE_PROB", s.HUMAN_LONG_PAUSE_PROB), s.HUMAN_LONG_PAUSE_PROB)
    s.SHUFFLE_VILLAGE_ORDER = _as_bool(g("SHUFFLE_VILLAGE_ORDER", s.SHUFFLE_VILLAGE_ORDER), s.SHUFFLE_VILLAGE_ORDER)
    s.FARM_LIST_RANDOM_SKIP_PROB = _as_float(g("FARM_LIST_RANDOM_SKIP_PROB", s.FARM_LIST_RANDOM_SKIP_PROB), s.FARM_LIST_RANDOM_SKIP_PROB)
    s.OP_COFFEE_BREAK_PROB = _as_float(g("OP_COFFEE_BREAK_PROB", s.OP_COFFEE_BREAK_PROB), s.OP_COFFEE_BREAK_PROB)
    s.OP_COFFEE_BREAK_MIN_MINUTES = _as_int(g("OP_COFFEE_BREAK_MIN_MINUTES", s.OP_COFFEE_BREAK_MIN_MINUTES), s.OP_COFFEE_BREAK_MIN_MINUTES)
    s.OP_COFFEE_BREAK_MAX_MINUTES = _as_int(g("OP_COFFEE_BREAK_MAX_MINUTES", s.OP_COFFEE_BREAK_MAX_MINUTES), s.OP_COFFEE_BREAK_MAX_MINUTES)

    # Map view + farm list subset
    s.MAPVIEW_PRE_ACTION_PROB = _as_float(g("MAPVIEW_PRE_ACTION_PROB", s.MAPVIEW_PRE_ACTION_PROB), s.MAPVIEW_PRE_ACTION_PROB)
    s.MAPVIEW_FARM_LIST_PROB = _as_float(g("MAPVIEW_FARM_LIST_PROB", s.MAPVIEW_FARM_LIST_PROB), s.MAPVIEW_FARM_LIST_PROB)
    s.FARM_LIST_SUBSET_MIN = _as_int(g("FARM_LIST_SUBSET_MIN", s.FARM_LIST_SUBSET_MIN), s.FARM_LIST_SUBSET_MIN)
    s.FARM_LIST_SUBSET_MAX = _as_int(g("FARM_LIST_SUBSET_MAX", s.FARM_LIST_SUBSET_MAX), s.FARM_LIST_SUBSET_MAX)

    # Attack detector
    s.ATTACK_DETECTOR_ENABLE = _as_bool(g("ATTACK_DETECTOR_ENABLE", s.ATTACK_DETECTOR_ENABLE), s.ATTACK_DETECTOR_ENABLE)
    s.ATTACK_DETECTOR_DISCORD_WEBHOOK = _as_str(g("ATTACK_DETECTOR_DISCORD_WEBHOOK", s.ATTACK_DETECTOR_DISCORD_WEBHOOK), s.ATTACK_DETECTOR_DISCORD_WEBHOOK)
    s.ATTACK_DETECTOR_INTERVAL_BASE = _as_float(g("ATTACK_DETECTOR_INTERVAL_BASE", s.ATTACK_DETECTOR_INTERVAL_BASE), s.ATTACK_DETECTOR_INTERVAL_BASE)
    s.ATTACK_DETECTOR_INTERVAL_JITTER = _as_float(g("ATTACK_DETECTOR_INTERVAL_JITTER", s.ATTACK_DETECTOR_INTERVAL_JITTER), s.ATTACK_DETECTOR_INTERVAL_JITTER)
    s.ATTACK_DETECTOR_COOLDOWN_SEC = _as_int(g("ATTACK_DETECTOR_COOLDOWN_SEC", s.ATTACK_DETECTOR_COOLDOWN_SEC), s.ATTACK_DETECTOR_COOLDOWN_SEC)
    langs = g("ATTACK_DETECTOR_OCR_LANGS", s.ATTACK_DETECTOR_OCR_LANGS or ["en"]) or ["en"]
    if isinstance(langs, str):
        s.ATTACK_DETECTOR_OCR_LANGS = [p.strip() for p in langs.split(',') if p.strip()]
    elif isinstance(langs, list):
        s.ATTACK_DETECTOR_OCR_LANGS = [str(p).strip() for p in langs if str(p).strip()]
    else:
        s.ATTACK_DETECTOR_OCR_LANGS = ["en"]
    s.ATTACK_DETECTOR_GPU = _as_bool(g("ATTACK_DETECTOR_GPU", s.ATTACK_DETECTOR_GPU), s.ATTACK_DETECTOR_GPU)
    s.ATTACK_DETECTOR_SEND_SCREENSHOT = _as_bool(g("ATTACK_DETECTOR_SEND_SCREENSHOT", s.ATTACK_DETECTOR_SEND_SCREENSHOT), s.ATTACK_DETECTOR_SEND_SCREENSHOT)
    s.ATTACK_DETECTOR_USE_HOTKEYS = _as_bool(g("ATTACK_DETECTOR_USE_HOTKEYS", s.ATTACK_DETECTOR_USE_HOTKEYS), s.ATTACK_DETECTOR_USE_HOTKEYS)
    s.ATTACK_DETECTOR_REFRESH_COMBO = _as_str(g("ATTACK_DETECTOR_REFRESH_COMBO", s.ATTACK_DETECTOR_REFRESH_COMBO), s.ATTACK_DETECTOR_REFRESH_COMBO)
    # Reports debugging
    s.REPORT_DEBUG_LOG = _as_bool(g("REPORT_DEBUG_LOG", s.REPORT_DEBUG_LOG), s.REPORT_DEBUG_LOG)
    s.REPORT_DEBUG_DUMP = _as_bool(g("REPORT_DEBUG_DUMP", s.REPORT_DEBUG_DUMP), s.REPORT_DEBUG_DUMP)
    s.PROCESS_REPORTS_IN_AUTOMATION = _as_bool(g("PROCESS_REPORTS_IN_AUTOMATION", s.PROCESS_REPORTS_IN_AUTOMATION), s.PROCESS_REPORTS_IN_AUTOMATION)

    # Hero adventures
    s.HERO_ADVENTURE_ENABLE = _as_bool(g("HERO_ADVENTURE_ENABLE", s.HERO_ADVENTURE_ENABLE), s.HERO_ADVENTURE_ENABLE)
    s.HERO_ADVENTURE_MIN_HEALTH = _as_int(g("HERO_ADVENTURE_MIN_HEALTH", s.HERO_ADVENTURE_MIN_HEALTH), s.HERO_ADVENTURE_MIN_HEALTH)
    s.HERO_ADVENTURE_MAX_DURATION_MIN = _as_int(g("HERO_ADVENTURE_MAX_DURATION_MIN", s.HERO_ADVENTURE_MAX_DURATION_MIN), s.HERO_ADVENTURE_MAX_DURATION_MIN)
    s.HERO_ADVENTURE_ALLOW_DANGER = _as_bool(g("HERO_ADVENTURE_ALLOW_DANGER", s.HERO_ADVENTURE_ALLOW_DANGER), s.HERO_ADVENTURE_ALLOW_DANGER)
    s.HERO_ADVENTURE_POLL_INTERVAL_SEC = _as_int(g("HERO_ADVENTURE_POLL_INTERVAL_SEC", s.HERO_ADVENTURE_POLL_INTERVAL_SEC), s.HERO_ADVENTURE_POLL_INTERVAL_SEC)
    s.HERO_ADVENTURE_RANDOM_JITTER_SEC = _as_int(g("HERO_ADVENTURE_RANDOM_JITTER_SEC", s.HERO_ADVENTURE_RANDOM_JITTER_SEC), s.HERO_ADVENTURE_RANDOM_JITTER_SEC)
    # Build guard
    s.BUILD_GUARD_ENABLE = _as_bool(g("BUILD_GUARD_ENABLE", s.BUILD_GUARD_ENABLE), s.BUILD_GUARD_ENABLE)
    s.BUILD_GUARD_MAX_RETRIES = _as_int(g("BUILD_GUARD_MAX_RETRIES", s.BUILD_GUARD_MAX_RETRIES), s.BUILD_GUARD_MAX_RETRIES)
    s.BUILD_GUARD_WAIT_MIN_SEC = _as_int(g("BUILD_GUARD_WAIT_MIN_SEC", s.BUILD_GUARD_WAIT_MIN_SEC), s.BUILD_GUARD_WAIT_MIN_SEC)
    s.BUILD_GUARD_WAIT_MAX_SEC = _as_int(g("BUILD_GUARD_WAIT_MAX_SEC", s.BUILD_GUARD_WAIT_MAX_SEC), s.BUILD_GUARD_WAIT_MAX_SEC)
    return s


settings = load_settings()
