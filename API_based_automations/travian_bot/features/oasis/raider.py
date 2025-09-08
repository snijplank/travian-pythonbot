import logging
import json, time
from random import uniform
from features.oasis.validator import is_valid_unoccupied_oasis
from core.learning_store import LearningStore
try:
    from config.config import settings as _cfg
except Exception:
    class _CfgFallback:
        LEARNING_ENABLE = True
    _cfg = _CfgFallback()
from core.metrics import add_sent, add_skip
from core.unit_catalog import FACTION_TO_TRIBE, resolve_label_t, t_to_u
from pathlib import Path

def resolve_unit_name(tribe_id: int, unit_code: str) -> str:
    # Use central catalog label with local slot code
    return resolve_label_t(tribe_id, unit_code)

def get_units_for_distance(distance, distance_ranges):
    """Get the appropriate unit combination for a given distance."""
    for range_data in distance_ranges:
        if range_data["start"] <= distance < range_data["end"]:
            return range_data["units"]
    return None

def _append_pending(oasis_key: str, unit_code: str, recommended: int, sent: int) -> None:
    path = Path("database/learning/pending.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except Exception:
        data = []
    entry = {
        "oasis": oasis_key,             # "(x,y)"
        "ts_sent": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "unit": unit_code,
        "recommended": int(recommended),
        "sent": int(sent),
        "_epoch": time.time(),
    }
    data.append(entry)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)

def run_raid_batch(api, raid_plan, faction, village_id, oases, hero_raiding=False, hero_available=False):
    """
    Execute a batch of raids on oases based on the raid plan.
    
    :param api: TravianAPI instance
    :param raid_plan: Dictionary containing raid configuration
    :param faction: Player's faction (Romans, Gauls, etc.)
    :param village_id: ID of the village sending raids
    :param oases: Dictionary of oases to raid
    :param hero_raiding: Whether hero raiding is enabled
    :param hero_available: Whether hero is available
    :return: Number of successful raids sent
    """
    sent_raids = 0
    max_raid_distance = raid_plan.get("max_raid_distance", float("inf"))
    distance_ranges = raid_plan.get("distance_ranges", [])

    # Get village coordinates from the first oasis's parent folder name
    village_coords = next(iter(oases.keys())).split("_")
    village_x, village_y = int(village_coords[0]), int(village_coords[1])
    tribe_id = FACTION_TO_TRIBE.get(str(faction), 4)
    logging.info(f"Raid origin village at ({village_x}, {village_y})")
    logging.info(f"Maximum raid distance: {max_raid_distance} tiles")
    use_learning = bool(getattr(_cfg, 'LEARNING_ENABLE', True))
    ls = LearningStore() if use_learning else None

    # Get current troops
    troops_info = api.get_troops_in_village()
    if not troops_info:
        logging.error("Could not fetch troops. Exiting.")
        return sent_raids

    # Process targets starting from the closest oases first
    ordered_targets = sorted(oases.items(), key=lambda it: it[1].get("distance", float("inf")))
    for coords, tile in ordered_targets:
        # Check distance from stored value
        distance = tile["distance"]
        if distance > max_raid_distance:
            logging.info(f"Reached maximum raid distance ({max_raid_distance} tiles). Stopping raids.")
            break

        # Get appropriate unit combination for this distance
        units = get_units_for_distance(distance, distance_ranges)
        if not units:
            logging.info(f"No unit combination defined for distance {distance:.1f}. Skipping.")
            add_skip("no_unit_combo")
            continue

        x_str, y_str = coords.split("_")
        x, y = int(x_str), int(y_str)
        key = f"({x},{y})"

        # Apply learning multiplier per oasis to adjust suggested group sizes
        mul = float(ls.get_multiplier(key)) if use_learning and ls else 1.0
        # Optional: log baseline snapshot for visibility and future decisions
        if use_learning and ls:
            try:
                base = ls.get_baseline(key)
                avg = base.get("avg_loss_pct")
                last_r = base.get("last_result")
                if avg is not None or last_r:
                avg_txt = f", avg_loss={avg:.0%}" if isinstance(avg, (int, float)) else ""
                loot_total = base.get("total_loot_total")
                loot_txt = f", loot_total={loot_total}" if isinstance(loot_total, int) and loot_total > 0 else ""
                logging.info(f"[Baseline] {key}: last={last_r}{avg_txt}{loot_txt}, mul={mul:.2f}")
            except Exception:
                pass
        base_total = sum(int(u.get("group_size", 0)) for u in units)
        # Adjust per-unit composition with multiplier, minimum 1 if base > 0
        adjusted_units = []
        for u in units:
            base_g = int(u.get("group_size", 0))
            adj_g = int(round(base_g * mul)) if base_g > 0 else 0
            if base_g > 0 and adj_g <= 0:
                adj_g = 1
            adjusted_units.append({"unit_code": u["unit_code"], "base_group": base_g, "adj_group": adj_g})

        # Check if we have enough troops for adjusted combination
        can_raid = True
        for au in adjusted_units:
            uc = au["unit_code"]
            key_u = t_to_u(tribe_id, uc)
            need = int(au["adj_group"])
            have = int(troops_info.get(key_u, troops_info.get(uc, 0)))
            if need > have:
                can_raid = False
                logging.info(f"Not enough {resolve_unit_name(tribe_id, uc)} (need {need}, have {have}) for distance {distance:.1f}. Skipping.")
                add_skip("insufficient_troops")
                break
        if not can_raid:
            continue

        # Validate oasis is raidable
        ok, why = is_valid_unoccupied_oasis(api, x, y, distance)
        if not ok:
            add_skip(f"invalid_oasis:{why}")
            continue

        # Prepare raid setup with all units in the combination
        raid_setup = {}
        logging.info(f"Using multiplier {mul:.2f} for oasis {key}")
        for au in adjusted_units:
            raid_setup[au["unit_code"]] = au["adj_group"]
            unit_name = resolve_unit_name(tribe_id, au["unit_code"])
            logging.info(f"Adding {au['adj_group']} {unit_name} to raid (base {au['base_group']})")

        logging.info(f"Launching raid on oasis at ({x}, {y})... Distance: {distance:.1f} tiles")
        attack_info = api.prepare_oasis_attack(None, x, y, raid_setup)
        success = api.confirm_oasis_attack(attack_info, x, y, raid_setup, village_id)

        if success:
            logging.info(f"✅ Raid sent to ({x}, {y}) - Distance: {distance:.1f} tiles")
            add_sent(1)
            # Log één pending entry voor deze raid zodat de report checker de multiplier kan bijstellen.
            # Omdat we hier een combinatie van units sturen, labelen we dit als 'mixed'.
            if use_learning:
                try:
                    adj_total = sum(int(a["adj_group"]) for a in adjusted_units)
                    _append_pending(key, "mixed", recommended=int(base_total), sent=int(adj_total))
                except Exception:
                    # Logging naar pending is niet kritisch voor het versturen; fouten hier mogen geen crash veroorzaken.
                    pass
            # Update available troops
            for au in adjusted_units:
                uc = au["unit_code"]
                key_u = t_to_u(tribe_id, uc)
                if key_u in troops_info:
                    troops_info[key_u] = max(0, int(troops_info.get(key_u, 0)) - int(au["adj_group"]))
                else:
                    troops_info[uc] = max(0, int(troops_info.get(uc, 0)) - int(au["adj_group"]))
            sent_raids += 1
        else:
            logging.error(f"❌ Failed to send raid to ({x}, {y}) - Distance: {distance:.1f} tiles")
            add_skip("send_failed")

        time.sleep(uniform(0.5, 1.2))

    logging.info(f"\n✅ Finished sending {sent_raids} raids.")
    logging.info("Troops remaining:")
    for unit_code, amount in troops_info.items():
        if amount > 0 and unit_code != "uhero":
            unit_name = resolve_unit_name(tribe_id, unit_code)
            logging.info(f"    {unit_name}: {amount} left")
            
    return sent_raids 
