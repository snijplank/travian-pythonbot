import logging
import json
from pathlib import Path

try:
    from config.config import settings
except Exception:
    class _F: pass
    settings = _F(); settings.HERO_ATTACK_ESTIMATE = 0; settings.ESCORT_SAFETY_FACTOR = 1.0

from core.combat_stats import get_unit_attack, estimate_escort_units

def try_send_hero_to_oasis(api, village, oasis, min_power=50, max_power=2000, help=True):
    """
    Sends the hero to the given oasis if its attack power is under the threshold.
    Power thresholds are based on distance:
    - Extra low distance (< 3 tiles): Power < 500
    - Low distance (< 6 tiles): Power < 1000
    - Medium distance (< 20 tiles): Power < 2000

    Args:
        api (TravianAPI): the game API object
        village (dict): the current village dict (must contain 'village_id', 'x', 'y')
        oasis (dict): a single oasis dict (must contain 'x', 'y')
        min_power (int): minimum acceptable power (default: 50)
        max_power (int): maximum acceptable power (default: 2000)
        help (bool): whether to send help troops (default: True)

    Returns:
        bool: True if hero was sent, False otherwise
    """
    # Preflight: ensure hero is available in the current village and not on a mission
    def _hero_available_here() -> bool:
        try:
            import re as _re
            res = api.session.get(
                f"{api.server_url}/api/v1/hero/dataForHUD",
                headers=api._headers_ajax("/hero"),
            )
            res.raise_for_status()
            j = res.json() or {}
            # hero must be alive and not on a mission
            alive = (j.get("healthStatus") == "alive")
            on_mission = ("heroHome" not in (j.get("statusInlineIcon", "") or ""))
            if not alive or on_mission:
                return False
            # verify hero is in this village
            url = j.get("url", "") or ""
            m = _re.search(r"newdid=(\d+)", url)
            current_vid = str(m.group(1)) if m else None
            expected_vid = str(village.get("village_id") if isinstance(village, dict) else getattr(village, "village_id", ""))
            return current_vid is not None and expected_vid and current_vid == expected_vid
        except Exception:
            return False

    if not _hero_available_here():
        try:
            logging.info("[HeroOasisClear] ❌ Hero not available in this village (or on mission). Skipping send.")
        except Exception:
            pass
        return False

    # Calculate distance
    distance = abs(village['x'] - oasis['x']) + abs(village['y'] - oasis['y'])
    
    # Set power threshold based on distance
    if distance < 3:
        max_power = 500  # Extra low distance
    elif distance < 6:
        max_power = 1000  # Low distance
    elif distance < 20:
        max_power = 2000  # Medium distance
    else:
        logging.info(f"⚠️ Distance too far ({distance} tiles). Skipping.")
        return False

    oasis_info = api.get_oasis_info(oasis["x"], oasis["y"])
    power = oasis_info["attack_power"]
    logging.debug(f"Checking oasis at ({oasis['x']}, {oasis['y']}) → Power: {power}, Distance: {distance}")
    
    if power > max_power or power < min_power:
        logging.info(
            f"⚠️ Skipping oasis at ({oasis['x']},{oasis['y']}) — Power {power} outside range {min_power}-{max_power} — Distance: {distance}"
        )
        return False

    # Determine tribe_id from identity (fallback to 4=Huns if missing)
    tribe_id = None
    try:
        ident_path = Path("database/identity.json")
        if ident_path.exists():
            data = json.loads(ident_path.read_text(encoding="utf-8"))
            tribe_id = data.get("travian_identity", {}).get("tribe_id")
    except Exception:
        tribe_id = None
    # Normalize tribe_id into known range 1..5; default to 4 (Huns)
    try:
        tribe_id = int(tribe_id) if tribe_id is not None else None
    except Exception:
        tribe_id = None
    if tribe_id not in (1, 2, 3, 4, 5):
        tribe_id = 4

    # Use live hero attack estimate when possible; fallback to config
    live_hero_atk = None
    try:
        live_hero_atk = api.get_hero_attack_estimate()
    except Exception:
        live_hero_atk = None
    hero_atk = live_hero_atk if isinstance(live_hero_atk, int) else getattr(settings, "HERO_ATTACK_ESTIMATE", 0)
    safety = float(getattr(settings, "ESCORT_SAFETY_FACTOR", 1.0))

    # Dynamically choose an escort unit based on availability (default priority starts with t5)
    # 1) Convert available troop keys (uXX) to local t1..t10 slots
    try:
        available_u = api.get_troops_in_village() or {}
    except Exception:
        available_u = {}

    def _u_to_t(u_code: str) -> str | None:
        try:
            if not (u_code and u_code.startswith("u") and u_code[1:].isdigit()):
                return None
            n = int(u_code[1:])
            # Map global ids to local slots
            if 1 <= n <= 10:
                return f"t{n}"
            if 11 <= n <= 20:
                return f"t{n-10}"
            if 21 <= n <= 30:
                return f"t{n-20}"
            if 31 <= n <= 40:
                return f"t{n-30}"
            if 41 <= n <= 50:
                return f"t{n-40}"
            if 61 <= n <= 70:
                return f"t{n-60}"
        except Exception:
            return None
        return None

    available_by_t: dict[str, int] = {}
    for k, v in (available_u.items() if isinstance(available_u, dict) else []):
        tcode = _u_to_t(k)
        if not tcode:
            continue
        try:
            v = int(v)
        except Exception:
            v = 0
        if v <= 0:
            continue
        available_by_t[tcode] = available_by_t.get(tcode, 0) + v

    # Priority for escort unit (configurable via settings, else sensible default)
    priority = list(getattr(settings, "ESCORT_UNIT_PRIORITY", [
        "t5", "t3", "t1", "t2", "t4", "t6", "t7", "t8", "t9", "t10"
    ]))

    escort_unit = None
    for code in priority:
        if available_by_t.get(code, 0) > 0:
            escort_unit = code
            break

    if help:
        if not escort_unit:
            logging.info(
                f"[HeroRaider] ❌ Hero raid skipped — geen escort beschikbaar (all zero). — Distance: {distance}, Power: {power}"
            )
            return False
        # 2) Recompute unit attack and recommendation for the chosen unit
        unit_atk = get_unit_attack(tribe_id, escort_unit)
        required = max(0.0, power * safety - float(hero_atk))
        recommended = estimate_escort_units(required_attack=required, unit_attack=unit_atk, min_units=1, max_units=50)
        available_count = int(available_by_t.get(escort_unit, 0))
        if available_count < recommended:
            logging.info(
                f"[HeroRaider] ❌ Hero raid skipped — onvoldoende escorts: nodig={recommended}, beschikbaar={available_count}. — Distance: {distance}, Power: {power}"
            )
            return False
        escort_count = int(recommended)
        logging.info(
            f"[HeroRaider] Escort planning: tribe={tribe_id} unit={escort_unit} unit_atk={unit_atk} "
            f"hero_atk≈{hero_atk} oasis_power={power} safety×{safety} ⇒ recommended={recommended}; available={available_count}; chosen={escort_count}"
        )
        if escort_count <= 0:
            logging.info(
                f"[HeroRaider] ❌ Hero raid skipped — geen escort beschikbaar voor {escort_unit} (available={available_count})."
            )
            return False
    else:
        # help == False → hero only (no escort sizing). Keep behaviour
        escort_unit = None
        escort_count = 0

    logging.info(f"🚀 Sending hero to oasis at ({oasis['x']},{oasis['y']}) — Power: {power}, Distance: {distance}")
    raid_setup = {}
    if help:
        raid_setup = {escort_unit: int(escort_count), "t11": 1}
        logging.info(
            f"[HeroRaider] Sending hero + escort: {escort_unit}={escort_count} "
            f"(available={available_by_t.get(escort_unit, 0)}, recommended={recommended})"
        )
    else:
        raid_setup = {"t11": 1}  # Hero only
    
    attack_info = api.prepare_oasis_attack(None, oasis["x"], oasis["y"], raid_setup)
    success = api.confirm_oasis_attack(attack_info, oasis["x"], oasis["y"], raid_setup, village["village_id"])
    return success
