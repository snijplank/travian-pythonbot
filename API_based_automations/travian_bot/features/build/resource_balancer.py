import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from config.config import settings
except Exception:
    class _Cfg:
        FUNDAMENTAL_BUILDING_ENABLE = False
        FUNDAMENTAL_OVERFLOW_THRESHOLD_PCT = 0.9
        FUNDAMENTAL_MAIN_BUILDING_TARGET = 10
        FUNDAMENTAL_BUILDING_COOLDOWN_SEC = 1800
        FUNDAMENTAL_QUEUE_ALLOWANCE = 0

    settings = _Cfg()

from identity_handling.identity_helper import load_villages_from_identity
from features.build import new_village_preset as preset


RESOURCE_TYPES = ("wood", "clay", "iron", "crop")
_DEFAULT_GID_MAP = {
    1: "wood",
    2: "clay",
    3: "iron",
    4: "crop",
}

if not hasattr(preset, "GID_TO_TYPE") or not getattr(preset, "GID_TO_TYPE"):
    try:
        preset.GID_TO_TYPE = dict(_DEFAULT_GID_MAP)
    except Exception:
        pass

GID_TO_TYPE = getattr(preset, "GID_TO_TYPE", dict(_DEFAULT_GID_MAP))
PROFILE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "database" / "resource_fields" / "village_profiles.json"
STATE_TRACK_PATH = Path(__file__).resolve().parents[2] / "database" / "resource_fields" / "last_upgrade_state.json"

DEFAULT_PROFILE_DEFS: dict[str, dict[str, Any]] = {
    "balanced": {
        "description": "Houd alle resourcevelden binnen hetzelfde niveau.",
        "weights": {"wood": 1.0, "clay": 1.0, "iron": 1.0, "crop": 1.0},
        "allowed_types": RESOURCE_TYPES,
        "min_resource_buffer": {"wood": 800, "clay": 800, "iron": 800, "crop": 800},
        "max_actions_per_cycle": 1,
        "queue_allowance": 0,
        "min_level_gap": 1,
        "cooldown_seconds": 900,
    },
    "crop_focus": {
        "description": "Zorg dat cropvelden voorop lopen zonder de rest te verwaarlozen.",
        "weights": {"wood": 1.15, "clay": 1.1, "iron": 1.2, "crop": 0.55},
        "allowed_types": RESOURCE_TYPES,
        "min_resource_buffer": {"wood": 700, "clay": 700, "iron": 700, "crop": 1200},
        "max_actions_per_cycle": 1,
        "queue_allowance": 0,
        "min_level_gap": 1,
        "cooldown_seconds": 900,
    },
    "wood_clay": {
        "description": "Boost hout en klei vroeg in het spel; ijzer/crop volgen later.",
        "weights": {"wood": 0.6, "clay": 0.6, "iron": 1.4, "crop": 1.45},
        "allowed_types": RESOURCE_TYPES,
        "min_resource_buffer": {"wood": 600, "clay": 600, "iron": 900, "crop": 900},
        "max_actions_per_cycle": 1,
        "queue_allowance": 0,
        "min_level_gap": 1,
        "cooldown_seconds": 900,
    },
}


@dataclass
class ProfileDefinition:
    name: str
    weights: dict[str, float]
    allowed_types: tuple[str, ...]
    min_resource_buffer: dict[str, int]
    max_actions_per_cycle: int
    queue_allowance: int
    min_level_gap: int
    cooldown_seconds: int
    description: str | None = None

    def clone(self) -> "ProfileDefinition":
        return ProfileDefinition(
            name=self.name,
            weights=dict(self.weights),
            allowed_types=tuple(self.allowed_types),
            min_resource_buffer=dict(self.min_resource_buffer),
            max_actions_per_cycle=self.max_actions_per_cycle,
            queue_allowance=self.queue_allowance,
            min_level_gap=self.min_level_gap,
            cooldown_seconds=self.cooldown_seconds,
            description=self.description,
        )

    def with_updates(self, data: Mapping[str, Any] | None) -> "ProfileDefinition":
        updated = self.clone()
        if not isinstance(data, Mapping):
            return updated

        weights_override = data.get("weights") or data.get("bias")
        if isinstance(weights_override, Mapping):
            for rtype, val in weights_override.items():
                if rtype in updated.weights:
                    try:
                        updated.weights[rtype] = float(val)
                    except Exception:
                        continue

        allowed_types = data.get("allowed_types") or data.get("resource_types")
        if isinstance(allowed_types, Iterable) and not isinstance(allowed_types, (str, bytes)):
            allowed: list[str] = []
            for item in allowed_types:
                rt = str(item).strip().lower()
                if rt in RESOURCE_TYPES and rt not in allowed:
                    allowed.append(rt)
            if allowed:
                updated.allowed_types = tuple(allowed)

        buffer_override = (
            data.get("min_resource_buffer")
            or data.get("resource_buffer")
            or data.get("buffer")
        )
        if isinstance(buffer_override, Mapping):
            for rtype, val in buffer_override.items():
                if rtype in updated.min_resource_buffer:
                    try:
                        updated.min_resource_buffer[rtype] = max(0, int(val))
                    except Exception:
                        continue

        if "max_actions_per_cycle" in data:
            try:
                updated.max_actions_per_cycle = max(0, int(data["max_actions_per_cycle"]))
            except Exception:
                pass
        elif "max_upgrades_per_cycle" in data:
            try:
                updated.max_actions_per_cycle = max(0, int(data["max_upgrades_per_cycle"]))
            except Exception:
                pass

        if "queue_allowance" in data:
            try:
                updated.queue_allowance = max(0, int(data["queue_allowance"]))
            except Exception:
                pass
        elif "max_queue_depth" in data:
            try:
                updated.queue_allowance = max(0, int(data["max_queue_depth"]))
            except Exception:
                pass

        if "min_level_gap" in data:
            try:
                updated.min_level_gap = max(0, int(data["min_level_gap"]))
            except Exception:
                pass

        if "cooldown_seconds" in data:
            try:
                updated.cooldown_seconds = max(0, int(data["cooldown_seconds"]))
            except Exception:
                pass
        elif "min_seconds_between_actions" in data:
            try:
                updated.cooldown_seconds = max(0, int(data["min_seconds_between_actions"]))
            except Exception:
                pass
        elif "cooldown" in data:
            try:
                updated.cooldown_seconds = max(0, int(data["cooldown"]))
            except Exception:
                pass

        if "description" in data:
            try:
                updated.description = str(data["description"]).strip() or None
            except Exception:
                pass
        return updated

    @property
    def allows_crop(self) -> bool:
        return "crop" in self.allowed_types


@dataclass
class ProfileRegistry:
    default_profile: str
    profiles: dict[str, ProfileDefinition]
    overrides: dict[str, dict[str, Any]]

    def for_village(self, village_id: int | str) -> ProfileDefinition:
        village_key = str(village_id)
        cfg = self.overrides.get(village_key) or {}
        profile_name = str(cfg.get("profile") or self.default_profile or "balanced")
        base = self.profiles.get(profile_name)
        if base is None:
            logging.debug(
                "[ResourceBalancer] Onbekend profiel '%s' voor dorp %s; val terug op default.",
                profile_name,
                village_key,
            )
            base = self.profiles.get(self.default_profile)
        if base is None and self.profiles:
            base = next(iter(self.profiles.values()))
        if base is None:
            raise RuntimeError("Geen profielen beschikbaar voor resource balancer.")
        overrides = {k: v for k, v in cfg.items() if k != "profile"}
        profile = base.with_updates(overrides)
        profile.name = base.name  # preserve canonical profile name
        return profile


def _load_profile_config() -> dict[str, Any]:
    if not PROFILE_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(PROFILE_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.warning("[ResourceBalancer] Profielconfig kon niet worden gelezen: %s", exc)
        return {}


def _default_profile_definitions(include_crop: bool) -> dict[str, ProfileDefinition]:
    definitions: dict[str, ProfileDefinition] = {}
    for name, data in DEFAULT_PROFILE_DEFS.items():
        weights = {rtype: float(data["weights"].get(rtype, 1.0)) for rtype in RESOURCE_TYPES}
        allowed_tuple = tuple(data.get("allowed_types") or RESOURCE_TYPES)
        if name == "balanced" and not include_crop:
            allowed_tuple = tuple(rt for rt in allowed_tuple if rt != "crop") or tuple(RESOURCE_TYPES[:3])
        buffer_map = {rtype: int(data["min_resource_buffer"].get(rtype, 0)) for rtype in RESOURCE_TYPES}
        definitions[name] = ProfileDefinition(
            name=name,
            weights=weights,
            allowed_types=allowed_tuple,
            min_resource_buffer=buffer_map,
            max_actions_per_cycle=int(data.get("max_actions_per_cycle", 1)),
            queue_allowance=int(data.get("queue_allowance", 0)),
            min_level_gap=int(data.get("min_level_gap", 1)),
            cooldown_seconds=int(data.get("cooldown_seconds", 0)),
            description=data.get("description"),
        )
    return definitions


def _load_profile_registry(include_crop: bool) -> ProfileRegistry:
    base_defs = _default_profile_definitions(include_crop)
    raw_cfg = _load_profile_config()

    profiles_cfg = raw_cfg.get("profiles")
    if isinstance(profiles_cfg, Mapping):
        for name, override in profiles_cfg.items():
            if not isinstance(override, Mapping):
                continue
            base = base_defs.get(name)
            if base is None:
                template = base_defs.get("balanced") or next(iter(base_defs.values()))
                base = template.clone()
                base.name = name
            updated = base.with_updates(override)
            updated.name = name
            base_defs[name] = updated

    default_profile = str(raw_cfg.get("default_profile") or "balanced")
    if default_profile not in base_defs:
        logging.debug(
            "[ResourceBalancer] Default-profiel '%s' niet gevonden; gebruik 'balanced'.",
            default_profile,
        )
        default_profile = "balanced"

    village_cfg: dict[str, dict[str, Any]] = {}
    villages = raw_cfg.get("villages")
    if isinstance(villages, Mapping):
        for vid, cfg in villages.items():
            if isinstance(cfg, Mapping):
                village_cfg[str(vid)] = dict(cfg)

    return ProfileRegistry(default_profile=default_profile, profiles=base_defs, overrides=village_cfg)


def _load_runtime_state() -> dict[str, dict[str, float]]:
    if not STATE_TRACK_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_TRACK_PATH.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logging.warning("[ResourceBalancer] Kon runtime-state niet lezen: %s", exc)
        return {}


def _save_runtime_state(state: Mapping[str, dict[str, float]]) -> None:
    try:
        STATE_TRACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_TRACK_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        logging.warning("[ResourceBalancer] Kon runtime-state niet opslaan: %s", exc)


def _format_remaining(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _collect_fields(api, allowed_types: Iterable[str]) -> list[tuple[str, int, int]]:
    allowed = [rtype for rtype in allowed_types if rtype in RESOURCE_TYPES]
    if not allowed:
        allowed = list(RESOURCE_TYPES)

    gid_map = getattr(preset, "GID_TO_TYPE", None) or globals().get("GID_TO_TYPE") or dict(_DEFAULT_GID_MAP)

    fields: list[tuple[str, int, int]] = []
    snapshot = preset._snapshot_resource_field_levels(api)
    snapshot_fields = []
    if isinstance(snapshot, dict):
        snapshot_fields = snapshot.get("fields") or []
    elif isinstance(snapshot, (list, tuple)):
        snapshot_fields = snapshot

    if snapshot_fields:
        allowed_set = set(allowed)
        for item in snapshot_fields:
            try:
                if isinstance(item, Mapping):
                    slot_id = int(item.get("slot"))
                    gid = int(item.get("gid"))
                    level = int(item.get("level"))
                else:
                    slot_id, gid, level = item[:3]
                    slot_id = int(slot_id)
                    gid = int(gid)
                    level = int(level)
            except Exception:
                continue
            rtype = gid_map.get(gid)
            if rtype and rtype in allowed_set:
                fields.append((rtype, slot_id, level))
        if fields:
            return fields

    logging.debug("[ResourceBalancer] Snapshot empty; fallback naar individuele slot-scan.")
    for rtype in allowed:
        slots = preset._find_resource_slots_with_levels(api, rtype)
        for slot_id, lvl in slots:
            try:
                level = int(lvl)
            except Exception:
                try:
                    level = int(str(lvl).strip())
                except Exception:
                    continue
            fields.append((rtype, slot_id, level))
    return fields


def _select_candidate_for_profile(
    fields: list[tuple[str, int, int]],
    profile: ProfileDefinition,
) -> tuple[str, int, int] | None:
    if not fields:
        return None
    per_type: dict[str, list[int]] = {}
    for rtype, _slot, level in fields:
        per_type.setdefault(rtype, []).append(level)

    best_type = None
    best_score = None
    for rtype, levels in per_type.items():
        avg = sum(levels) / max(1, len(levels))
        weight = profile.weights.get(rtype, 1.0)
        score = avg * weight
        if best_score is None or score < best_score - 1e-6:
            best_score = score
            best_type = rtype
        elif best_type and abs(score - best_score) <= 1e-6:
            # Tie-breaker: prefer type with lower weight (higher priority)
            if profile.weights.get(rtype, 1.0) < profile.weights.get(best_type, 1.0):
                best_type = rtype
    if not best_type:
        return None

    type_fields = [f for f in fields if f[0] == best_type]
    if not type_fields:
        return None
    lowest_level = min(level for _rtype, _slot, level in type_fields)
    candidates = [f for f in type_fields if f[2] == lowest_level]
    if not candidates:
        return None
    random.shuffle(candidates)
    return sorted(candidates, key=lambda item: item[1])[0]


def _human_pause() -> None:
    try:
        time.sleep(random.uniform(0.4, 1.0))
    except Exception:
        time.sleep(0.5)


def _upgrade_main_building_once(api) -> bool:
    gid = getattr(preset, "BUILDING_GID", {}).get("main_building")
    if not gid:
        return False
    try:
        sid, level = preset._get_building_level_by_gid(api, gid)
    except Exception:
        sid, level = None, None
    if sid is None:
        try:
            sid = preset._construct_building_if_missing(api, gid)
        except Exception:
            sid = None
        if sid is None:
            return False
        return True
    if level is None:
        return False
    try:
        return preset._try_upgrade_with_guard(api, sid)
    except Exception:
        return False


def _parse_resource_bar(soup) -> tuple[dict[str, int], dict[str, int]]:
    current = {rt: 0 for rt in RESOURCE_TYPES}
    capacities: dict[str, int] = {}
    mapping = {"wood": "l1", "clay": "l2", "iron": "l3", "crop": "l4"}
    for rtype, element_id in mapping.items():
        el = soup.find(id=element_id)
        if el is None:
            continue
        try:
            current[rtype] = preset._sanitize_numeric(el.get_text())
        except Exception:
            continue
    free_crop_el = soup.find(id="stockBarFreeCrop")
    if free_crop_el is not None:
        try:
            current["free_crop"] = preset._sanitize_numeric(free_crop_el.get_text())
        except Exception:
            current["free_crop"] = 0
    script = soup.find("script", string=re.compile(r"var\s+resources"))
    if script and script.string:
        text = script.string
        for key, name in (("l1", "wood"), ("l2", "clay"), ("l3", "iron")):
            m = re.search(rf"maxStorage\s*:\s*\{{[^}}]*\"{key}\"\s*:\s*(\d+)", text)
            if m:
                try:
                    capacities[name] = int(m.group(1))
                except Exception:
                    continue
        m_crop = re.search(r"maxCropStorage\s*:\s*(\d+)", text)
        if m_crop:
            try:
                capacities["crop"] = int(m_crop.group(1))
            except Exception:
                pass
    return current, capacities


def _count_active_queue_items(soup) -> int:
    container = soup.find("div", id="build")
    if not container:
        return 0
    selectors = [
        ".underConstruction",
        ".timer",
        ".buildDuration",
        ".queueSlot",
        ".slotRow[data-duration]",
    ]
    seen: set[int] = set()
    count = 0
    for selector in selectors:
        for node in container.select(selector):
            key = id(node)
            if key in seen:
                continue
            seen.add(key)
            count += 1
    return count


def _load_village_state(api) -> tuple[dict[str, int], dict[str, int], int]:
    res = api.session.get(f"{api.server_url}/dorf1.php")
    res.raise_for_status()
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(res.text, "html.parser")
    resources, capacities = _parse_resource_bar(soup)
    queue_depth = _count_active_queue_items(soup)
    return resources, capacities, queue_depth


def _check_resource_buffer(
    resources: Mapping[str, int],
    costs: Mapping[str, int],
    min_buffer: Mapping[str, int],
) -> tuple[bool, str | None]:
    for rtype in RESOURCE_TYPES:
        try:
            current = int(resources.get(rtype, 0))
            cost = int(costs.get(rtype, 0))
            buffer = int(min_buffer.get(rtype, 0))
        except Exception:
            continue
        if current < cost + buffer:
            missing = (cost + buffer) - current
            return False, (
                f"Buffer vereist voor {rtype}: tekort van {missing}. "
                "Upgrade overgeslagen om reserves te beschermen."
            )
    return True, None


def _attempt_upgrade(
    api,
    profile: ProfileDefinition,
    resources: dict[str, int],
) -> tuple[bool, str, dict[str, int]]:
    fields = _collect_fields(api, profile.allowed_types)
    if not fields:
        return False, "Geen resourcevelden gevonden (controleer dorp).", resources

    highest = max(level for _rtype, _slot, level in fields)
    lowest = min(level for _rtype, _slot, level in fields)
    if profile.min_level_gap > 0 and highest - lowest < profile.min_level_gap:
        return False, "Velden zijn al in balans voor dit profiel.", resources

    candidate = _select_candidate_for_profile(fields, profile)
    if not candidate:
        return False, "Geen geschikte veldkandidaat gevonden voor dit profiel.", resources

    rtype, slot_id, level = candidate
    build_info = preset.get_build_page_info(api, slot_id)
    blocked = build_info.get("blocked")
    if blocked in {"queue", "queue_full"}:
        return False, "Bouwqueue is actief; upgrade uitgesteld.", resources

    costs = build_info.get("costs") or {}
    has_buffer, buffer_msg = _check_resource_buffer(resources, costs, profile.min_resource_buffer)
    if not has_buffer:
        return False, buffer_msg or "Onvoldoende buffer om upgrade veilig uit te voeren.", resources

    _human_pause()
    ok, reason = preset._click_upgrade_button(api, slot_id, soup=build_info.get("soup"))
    guard_used = False
    if not ok and reason in {"freecrop", "granaryfull", "warehousefull", "storage"}:
        guard_used = bool(preset._try_upgrade_with_guard(api, slot_id))
        if guard_used:
            ok = True
            reason = "ok"

    if ok:
        logging.info(
            "[ResourceBalancer] Upgrade gestart (%s slot=%s level=%s profile=%s).",
            rtype,
            slot_id,
            level,
            profile.name,
        )
        for res_type, amount in costs.items():
            try:
                resources[res_type] = max(0, int(resources.get(res_type, 0)) - int(amount))
            except Exception:
                continue
        message = f"Upgrade gestart voor {rtype}-veld (slot {slot_id}) vanaf level {level}."
        if guard_used:
            message += " Guard heeft opslagen gecontroleerd."
        return True, message, resources

    logging.debug(
        "[ResourceBalancer] Upgrade niet gestart (slot=%s type=%s level=%s reason=%s profile=%s).",
        slot_id,
        rtype,
        level,
        reason,
        profile.name,
    )
    reason_map = {
        "notenough": "Onvoldoende grondstoffen (buiten buffer).",
        "queue": "Bouwqueue is vol.",
        "queue_full": "Bouwqueue is vol.",
        "freecrop": "Niet genoeg vrije crop beschikbaar.",
        "granaryfull": "Graanschuur zit vol.",
        "warehousefull": "Pakhuis zit vol.",
        "storage": "Opslag zit vol.",
        "no_form": "Upgradeknop niet gevonden.",
        "http": "HTTP-fout tijdens upgrade.",
    }
    msg = reason_map.get(reason, f"Upgrade niet gestart (reden: {reason or 'onbekend'}).").strip()
    return False, msg, resources


def _run_fundamental_building_upgrades(
    api,
    entry: dict,
    resources: Mapping[str, int],
    capacities: Mapping[str, int],
    queue_depth: int,
    now: float,
) -> tuple[list[tuple[bool, str]], bool]:
    actions: list[tuple[bool, str]] = []
    state_changed = False

    enable = bool(getattr(settings, "FUNDAMENTAL_BUILDING_ENABLE", False))
    if not enable:
        return actions, state_changed

    queue_allow = max(0, int(getattr(settings, "FUNDAMENTAL_QUEUE_ALLOWANCE", 0)))
    if queue_depth > queue_allow:
        logging.debug(
            "[Fundamentals] Queue depth %s hoger dan allowance %s; geen upgrade.",
            queue_depth,
            queue_allow,
        )
        return actions, state_changed

    cooldown = max(0, int(getattr(settings, "FUNDAMENTAL_BUILDING_COOLDOWN_SEC", 0)))
    last_build_val = entry.get("last_building_upgrade_ts", entry.get("last_building_ts"))
    last_build_ts = 0.0
    try:
        if last_build_val is not None:
            last_build_ts = float(last_build_val)
    except Exception:
        last_build_ts = 0.0
    if cooldown > 0 and last_build_ts > 0 and (now - last_build_ts) < cooldown:
        logging.debug("[Fundamentals] Cooldown actief; geen upgrade.")
        return actions, state_changed

    try:
        threshold = float(getattr(settings, "FUNDAMENTAL_OVERFLOW_THRESHOLD_PCT", 0.9))
    except Exception:
        threshold = 0.9
    threshold = max(0.0, min(threshold, 0.99))

    def _calc_ratio(keys: Iterable[str]) -> float:
        ratios: list[float] = []
        for key in keys:
            cap = capacities.get(key)
            if not cap or cap <= 0:
                continue
            cur = resources.get(key, 0)
            try:
                ratios.append(float(cur) / float(cap))
            except Exception:
                continue
        return max(ratios) if ratios else -1.0

    storage_candidates: list[tuple[str, float]] = []
    if threshold > 0 and capacities:
        warehouse_ratio = _calc_ratio(("wood", "clay", "iron"))
        if warehouse_ratio >= threshold:
            storage_candidates.append(("warehouse", warehouse_ratio))
        granary_ratio = _calc_ratio(("crop",))
        if granary_ratio >= threshold:
            storage_candidates.append(("granary", granary_ratio))

    storage_candidates.sort(key=lambda item: item[1], reverse=True)
    for stype, ratio in storage_candidates:
        pct = min(100, int(round(max(0.0, ratio) * 100)))
        if stype == "warehouse":
            ok = bool(preset._upgrade_warehouse_once(api))
            label = "Pakhuis"
        else:
            ok = bool(preset._upgrade_granary_once(api))
            label = "Graanschuur"
        if ok:
            ts = time.time()
            entry["last_building_upgrade_ts"] = ts
            entry.pop("last_building_ts", None)
            actions.append((True, f"{label} upgrade gestart (opslag {pct}%)."))
            state_changed = True
            return actions, state_changed
        logging.debug("[Fundamentals] %s-upgrade mislukt of overgeslagen (ratio %.2f).", stype, ratio)

    try:
        target_mb = int(getattr(settings, "FUNDAMENTAL_MAIN_BUILDING_TARGET", 10))
    except Exception:
        target_mb = 10
    target_mb = max(0, target_mb)

    if target_mb > 0:
        gid = getattr(preset, "BUILDING_GID", {}).get("main_building")
        level_before = None
        if gid:
            try:
                _sid, level_before = preset._get_building_level_by_gid(api, gid)
            except Exception:
                level_before = None
        if isinstance(level_before, (int, float)) and int(level_before) < target_mb:
            ok = _upgrade_main_building_once(api)
            if ok:
                ts = time.time()
                entry["last_building_upgrade_ts"] = ts
                entry.pop("last_building_ts", None)
                actions.append((True, f"Hoofdgebouw upgrade gestart (van level {int(level_before)})."))
                state_changed = True
            else:
                logging.debug(
                    "[Fundamentals] Hoofdgebouw-upgrade niet gestart (huidig level=%s).",
                    level_before,
                )

    return actions, state_changed


def run_resource_balancer_cycle(api, include_crop: bool = True) -> list[tuple[str, bool, str, str]]:
    registry = _load_profile_registry(include_crop)
    villages = load_villages_from_identity()
    if not villages:
        logging.info("[ResourceBalancer] Geen dorpen in identity; niets te doen.")
        return []

    runtime_state = _load_runtime_state()
    state_dirty = False
    now = time.time()
    results: list[tuple[str, bool, str, str]] = []
    for village in villages:
        village_id = village.get("village_id")
        village_name = str(village.get("village_name") or village_id)
        if village_id is None:
            continue
        try:
            api.switch_village(village_id)
        except Exception:
            logging.debug("[ResourceBalancer] Kon niet wisselen naar dorp %s.", village_id)
            continue

        try:
            profile = registry.for_village(village_id)
        except Exception as exc:
            logging.warning(
                "[ResourceBalancer] Geen profiel voor dorp %s (%s); sla over.",
                village_id,
                exc,
            )
            results.append((str(village_id), False, "Geen profiel beschikbaar voor dit dorp.", village_name))
            continue

        if profile.max_actions_per_cycle <= 0:
            results.append((str(village_id), False, "Profiel staat ingesteld op 0 upgrades per cycle.", village_name))
            continue

        state_key = str(village_id)
        entry = runtime_state.get(state_key)
        if not isinstance(entry, dict):
            entry = {}
            runtime_state[state_key] = entry

        try:
            resources, capacities, queue_depth = _load_village_state(api)
        except Exception as exc:
            logging.warning(
                "[ResourceBalancer] Kon dorf1 niet laden voor dorp %s: %s",
                village_id,
                exc,
            )
            results.append((str(village_id), False, "Kon dorpsoverzicht niet laden.", village_name))
            continue

        now = time.time()
        last_ts_val = entry.get("last_resource_upgrade_ts", entry.get("last_upgrade_ts"))
        last_ts = 0.0
        try:
            if last_ts_val is not None:
                last_ts = float(last_ts_val)
        except Exception:
            last_ts = 0.0

        cooldown_active = False
        if profile.cooldown_seconds > 0 and last_ts > 0:
            elapsed = now - last_ts
            if elapsed < profile.cooldown_seconds:
                cooldown_active = True
                remaining = profile.cooldown_seconds - elapsed
                prefixed = f"[{profile.name}] Wachttijd actief; probeer later opnieuw ({_format_remaining(remaining)})."
                results.append((state_key, False, prefixed, village_name))

        if queue_depth > profile.queue_allowance:
            results.append((
                str(village_id),
                False,
                "Bouwqueue actief; wacht totdat lopende upgrade klaar is.",
                village_name,
            ))
            resource_attempted = False
        else:
            resource_attempted = not cooldown_active

        success = False
        message = "Geen upgrade uitgevoerd."
        if resource_attempted:
            for attempt in range(profile.max_actions_per_cycle):
                if attempt > 0:
                    try:
                        resources, capacities, queue_depth = _load_village_state(api)
                    except Exception as exc:
                        logging.warning(
                            "[ResourceBalancer] Kon dorpsoverzicht niet verversen voor dorp %s: %s",
                            village_id,
                            exc,
                        )
                        message = (
                            "Kon dorpsoverzicht niet verversen. "
                            "Upgrade overgeslagen."
                        )
                        break
                    if queue_depth > profile.queue_allowance:
                        message = "Bouwqueue inmiddels gevuld; geen extra upgrade."
                        break

                ok, msg, resources = _attempt_upgrade(api, profile, resources)
                message = msg
                if ok:
                    success = True
                    ts = time.time()
                    entry["last_resource_upgrade_ts"] = ts
                    entry.pop("last_upgrade_ts", None)
                    state_dirty = True
                    break
                lowered = msg.lower()
                if any(keyword in lowered for keyword in ("queue", "buffer", "geen", "overgeslagen")):
                    break

            prefixed_message = f"[{profile.name}] {message}"
            results.append((str(village_id), success, prefixed_message, village_name))
            if success:
                _human_pause()

        # After resource balancing (or skip), attempt fundamental building upgrades
        try:
            resources, capacities, queue_depth = _load_village_state(api)
        except Exception as exc:
            logging.debug(
                "[ResourceBalancer] Kon fundamentals niet evalueren voor dorp %s: %s",
                village_id,
                exc,
            )
        else:
            actions, changed = _run_fundamental_building_upgrades(
                api,
                entry,
                resources,
                capacities,
                queue_depth,
                time.time(),
            )
            if changed:
                state_dirty = True
            for ok_flag, msg in actions:
                if ok_flag:
                    _human_pause()
                results.append((str(village_id), ok_flag, f"[fundamentals] {msg}", village_name))

    if state_dirty:
        _save_runtime_state(runtime_state)

    return results
