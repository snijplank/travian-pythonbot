import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from identity_handling.identity_helper import load_villages_from_identity
from features.build import new_village_preset as preset


RESOURCE_TYPES = ("wood", "clay", "iron", "crop")
GID_TO_TYPE = {
    1: "wood",
    2: "clay",
    3: "iron",
    4: "crop",
}
PROFILE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "database" / "resource_fields" / "village_profiles.json"

DEFAULT_PROFILE_DEFS: dict[str, dict[str, Any]] = {
    "balanced": {
        "description": "Houd alle resourcevelden binnen hetzelfde niveau.",
        "weights": {"wood": 1.0, "clay": 1.0, "iron": 1.0, "crop": 1.0},
        "allowed_types": RESOURCE_TYPES,
        "min_resource_buffer": {"wood": 800, "clay": 800, "iron": 800, "crop": 800},
        "max_actions_per_cycle": 1,
        "queue_allowance": 0,
        "min_level_gap": 1,
    },
    "crop_focus": {
        "description": "Zorg dat cropvelden voorop lopen zonder de rest te verwaarlozen.",
        "weights": {"wood": 1.15, "clay": 1.1, "iron": 1.2, "crop": 0.55},
        "allowed_types": RESOURCE_TYPES,
        "min_resource_buffer": {"wood": 700, "clay": 700, "iron": 700, "crop": 1200},
        "max_actions_per_cycle": 1,
        "queue_allowance": 0,
        "min_level_gap": 1,
    },
    "wood_clay": {
        "description": "Boost hout en klei vroeg in het spel; ijzer/crop volgen later.",
        "weights": {"wood": 0.6, "clay": 0.6, "iron": 1.4, "crop": 1.45},
        "allowed_types": RESOURCE_TYPES,
        "min_resource_buffer": {"wood": 600, "clay": 600, "iron": 900, "crop": 900},
        "max_actions_per_cycle": 1,
        "queue_allowance": 0,
        "min_level_gap": 1,
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


def _collect_fields(api, allowed_types: Iterable[str]) -> list[tuple[str, int, int]]:
    allowed = [rtype for rtype in allowed_types if rtype in RESOURCE_TYPES]
    if not allowed:
        allowed = list(RESOURCE_TYPES)

    fields: list[tuple[str, int, int]] = []
    snapshot = preset._snapshot_resource_field_levels(api)
    if snapshot:
        allowed_set = set(allowed)
        for slot_id, gid, level in snapshot:
            rtype = GID_TO_TYPE.get(gid)
            if rtype and rtype in allowed_set:
                fields.append((rtype, slot_id, int(level)))
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


def _parse_resource_bar(soup) -> dict[str, int]:
    result = {rt: 0 for rt in RESOURCE_TYPES}
    mapping = {"wood": "l1", "clay": "l2", "iron": "l3", "crop": "l4"}
    for rtype, element_id in mapping.items():
        el = soup.find(id=element_id)
        if el is None:
            continue
        try:
            result[rtype] = preset._sanitize_numeric(el.get_text())
        except Exception:
            continue
    free_crop_el = soup.find(id="stockBarFreeCrop")
    if free_crop_el is not None:
        try:
            result["free_crop"] = preset._sanitize_numeric(free_crop_el.get_text())
        except Exception:
            result["free_crop"] = 0
    return result


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


def _load_village_state(api) -> tuple[dict[str, int], int]:
    res = api.session.get(f"{api.server_url}/dorf1.php")
    res.raise_for_status()
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(res.text, "html.parser")
    resources = _parse_resource_bar(soup)
    queue_depth = _count_active_queue_items(soup)
    return resources, queue_depth


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


def run_resource_balancer_cycle(api, include_crop: bool = True) -> list[tuple[str, bool, str, str]]:
    registry = _load_profile_registry(include_crop)
    villages = load_villages_from_identity()
    if not villages:
        logging.info("[ResourceBalancer] Geen dorpen in identity; niets te doen.")
        return []

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

        try:
            resources, queue_depth = _load_village_state(api)
        except Exception as exc:
            logging.warning(
                "[ResourceBalancer] Kon dorf1 niet laden voor dorp %s: %s",
                village_id,
                exc,
            )
            results.append((str(village_id), False, "Kon dorpsoverzicht niet laden.", village_name))
            continue

        if queue_depth > profile.queue_allowance:
            results.append((
                str(village_id),
                False,
                "Bouwqueue actief; wacht totdat lopende upgrade klaar is.",
                village_name,
            ))
            continue

        success = False
        message = "Geen upgrade uitgevoerd."
        for attempt in range(profile.max_actions_per_cycle):
            if attempt > 0:
                try:
                    resources, queue_depth = _load_village_state(api)
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
                break
            # Abort further attempts if there is nothing meaningful left to try
            lowered = msg.lower()
            if any(keyword in lowered for keyword in ("queue", "buffer", "geen", "overgeslagen")):
                break

        if success:
            _human_pause()
        prefixed_message = f"[{profile.name}] {message}"
        results.append((str(village_id), success, prefixed_message, village_name))

    return results
