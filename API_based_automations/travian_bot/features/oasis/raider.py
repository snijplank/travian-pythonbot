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

def _append_pending(target_key: str, unit_code: str, recommended: int, sent: int) -> None:
    path = Path("database/learning/pending.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except Exception:
        data = []
    entry = {
        # Generic key for downstream processing; keep legacy 'oasis' for back-compat
        "target": target_key,           # "(x,y)"
        "oasis": target_key,            # legacy field; remove in a future cleanup
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

    # Optional early exit: if troop bank cannot satisfy any distance range composition
    try:
        early_exit_on_insufficient = bool(getattr(_cfg, 'OASIS_EARLY_EXIT_IF_INSUFFICIENT', True))
    except Exception:
        early_exit_on_insufficient = True
    if early_exit_on_insufficient:
        def _can_satisfy_any_range() -> bool:
            for dr in (distance_ranges or []):
                reqs = {}
                try:
                    for u in (dr.get("units") or []):
                        uc = str(u.get("unit_code"))
                        gs = int(u.get("group_size", 0))
                        if gs <= 0:
                            continue
                        reqs[uc] = reqs.get(uc, 0) + gs
                    if not reqs:
                        continue
                    ok = True
                    for uc, need in reqs.items():
                        key_u = t_to_u(tribe_id, uc)
                        have = int(troops_info.get(key_u, troops_info.get(uc, 0)) or 0)
                        if have < need:
                            ok = False
                            break
                    if ok:
                        return True
                except Exception:
                    continue
            return False
        if not _can_satisfy_any_range():
            try:
                # Determine a minimal required sum for messaging only
                min_need = None
                for dr in (distance_ranges or []):
                    total = 0
                    for u in (dr.get("units") or []):
                        gs = int(u.get("group_size", 0))
                        if gs > 0:
                            total += gs
                    if total > 0:
                        min_need = total if min_need is None else min(min_need, total)
                bank = sum(int(v) for v in troops_info.values() if isinstance(v, int))
                if min_need is not None:
                    logging.info(f"[Oasis] Insufficient troop bank: need ≥ {min_need}, have {bank}. Skipping oasis loop.")
                else:
                    logging.info("[Oasis] Insufficient troop bank for any configured range. Skipping oasis loop.")
            except Exception:
                logging.info("[Oasis] Insufficient troop bank. Skipping oasis loop.")
            return sent_raids

    # Build scheduling view: due based on last_sent and interval+jitter
    try:
        # Use a different local name to avoid shadowing the module-level _cfg
        from config.config import settings as _sched_cfg  # type: ignore
        tgt_interval = int(getattr(_sched_cfg, 'OASIS_TARGET_INTERVAL_MIN_SEC', 600))
        jitter = int(getattr(_sched_cfg, 'OASIS_INTERVAL_JITTER_SEC', 60))
        cooldown_lost = int(getattr(_sched_cfg, 'OASIS_COOLDOWN_ON_LOST_SEC', 1800))
    except Exception:
        tgt_interval, jitter, cooldown_lost = 600, 60, 1800

    now = time.time()
    sched = []
    for coords, tile in oases.items():
        x_str, y_str = coords.split("_")
        x_i, y_i = int(x_str), int(y_str)
        key = f"({x_i},{y_i})"
        dist = tile.get("distance", float("inf"))
        # Cooldown on recent loss
        if use_learning and ls:
            try:
                base = ls.get_baseline(key)
                last_r = (base or {}).get("last_result")
                last_ts = (base or {}).get("last_ts")
                if last_r == 'lost' and isinstance(last_ts, str):
                    # Convert ISO to epoch
                    try:
                        import datetime as _dt
                        t = _dt.datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ")
                        if (now - t.timestamp()) < cooldown_lost:
                            # Skip for now
                            continue
                    except Exception:
                        pass
            except Exception:
                pass
        last_sent = ls.get_last_sent(key) if (use_learning and ls) else None
        # Next due with jitter window centered around tgt_interval
        due_ts = (last_sent or 0) + tgt_interval
        sched.append((max(0, due_ts - now), coords, dist))
    # Sort by due first (overdue first), then by distance
    sched.sort(key=lambda t: (t[0], t[2]))
    ordered_targets = [(coords, oases[coords]) for _, coords, _ in sched]
    # Cap excessive insufficient skips to avoid noisy cycles
    try:
        insufficient_cap = int(getattr(_cfg, 'OASIS_MAX_INSUFFICIENT_SKIPS', 10))
    except Exception:
        insufficient_cap = 10
    insufficient_skips = 0

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
                # Also log scheduling context: when last sent and due in
                last_sent_ts = ls.get_last_sent(key)
                def _fmt_sec(s: float) -> str:
                    s = max(0, int(s))
                    m, s = divmod(s, 60)
                    h, m = divmod(m, 60)
                    if h > 0:
                        return f"{h}h{m:02d}m{s:02d}s"
                    return f"{m}m{s:02d}s"
                if last_sent_ts:
                    elapsed = now - float(last_sent_ts)
                    due_in = (float(last_sent_ts) + tgt_interval) - now
                    logging.info(
                        f"[Schedule] {key}: last_sent {_fmt_sec(elapsed)} ago; target_interval={_fmt_sec(tgt_interval)}; due in {_fmt_sec(due_in)}"
                    )
                else:
                    logging.info(f"[Schedule] {key}: last_sent=never; target_interval={_fmt_sec(tgt_interval)}; due now")
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
            insufficient_skips += 1
            if insufficient_cap > 0 and insufficient_skips >= insufficient_cap:
                logging.info(f"[Oasis] Reached insufficient skip cap ({insufficient_cap}); breaking oasis loop.")
                break
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
            # Record last sent timestamp for scheduler
            if use_learning and ls:
                try:
                    ls.set_last_sent(key)
                except Exception:
                    pass
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
