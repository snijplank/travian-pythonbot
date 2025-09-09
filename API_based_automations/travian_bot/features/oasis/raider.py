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
from core.unit_catalog import FACTION_TO_TRIBE, resolve_label_t, t_to_u, u_to_t
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
    # Always keep a scheduling store for last_sent timestamps, independent of learning
    ls = LearningStore()

    # Get current troops
    troops_info = api.get_troops_in_village()
    if not troops_info:
        logging.error("Could not fetch troops. Exiting.")
        return sent_raids

    # Helper: can current troop bank satisfy at least one distance range combo?
    def _can_satisfy_any_range_with_bank(bank: dict) -> bool:
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
                    have = int(bank.get(key_u, bank.get(uc, 0)) or 0)
                    if have < need:
                        ok = False
                        break
                if ok:
                    return True
            except Exception:
                continue
        return False

    # Optional early exit: if troop bank cannot satisfy any distance range composition
    try:
        early_exit_on_insufficient = bool(getattr(_cfg, 'OASIS_EARLY_EXIT_IF_INSUFFICIENT', True))
    except Exception:
        early_exit_on_insufficient = True
    if early_exit_on_insufficient:
        if not _can_satisfy_any_range_with_bank(troops_info):
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
        # Ensure numeric distance for sorting
        try:
            dist = float(tile.get("distance", float("inf")))
        except Exception:
            dist = float("inf")
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
        # Scheduler: last_sent tracking is independent of learning
        last_sent = ls.get_last_sent(key)
        # Next due; negative means overdue, 0 means due now; None (never) treated as due now
        if last_sent is None:
            due_val = 0.0
        else:
            due_val = float(last_sent + tgt_interval - now)
        sched.append((due_val, coords, dist))
    # Sort by due first (overdue first), then by distance
    sched.sort(key=lambda t: (t[0], t[2]))
    ordered_targets = [(coords, oases[coords]) for _, coords, _ in sched]

    # Persist the earliest upcoming due time for a simple external countdown
    try:
        import time as _t
        from pathlib import Path as _P
        upcoming = [max(0.0, float(d)) for (d, _, _) in sched if isinstance(d, (int, float))]
        next_due_sec = min(upcoming) if upcoming else 0.0
        payload = {
            "village": {"x": village_x, "y": village_y, "id": village_id},
            "generated": int(_t.time()),
            "next_due_sec": float(next_due_sec),
            "next_due_epoch": int(_t.time() + max(0.0, float(next_due_sec)))
        }
        p = _P("database/runtime_next_oasis_due.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        # Also log a human-friendly hint for when the next EmptyOasisRaider batch is expected
        if next_due_sec and next_due_sec > 0:
            try:
                eta_epoch = int(_t.time() + int(next_due_sec))
                mm, ss = divmod(int(next_due_sec), 60)
                # show H:MM:SS if long
                if mm >= 60:
                    hh, mm = divmod(mm, 60)
                    eta_txt = f"{hh:d}:{mm:02d}:{ss:02d}"
                else:
                    eta_txt = f"{mm:02d}:{ss:02d}"
                eta_clock = _t.strftime("%H:%M:%S", _t.localtime(eta_epoch))
                logging.info(f"[EmptyOasisRaider] Next oasis becomes due in {eta_txt} (~{eta_clock}).")
            except Exception:
                pass
    except Exception:
        pass
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
        elif not use_learning:
            # Even without learning, we still show scheduler context
            try:
                def _fmt_sec(s: float) -> str:
                    s = max(0, int(s))
                    m, s = divmod(s, 60)
                    h, m = divmod(m, 60)
                    if h > 0:
                        return f"{h}h{m:02d}m{s:02d}s"
                    return f"{m}m{s:02d}s"
                last_sent_ts = ls.get_last_sent(key)
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
            uc = str(au["unit_code"])  # may be 'tX' or legacy 'uNN'
            uc_local = u_to_t(uc) or uc  # normalize to local slot if given as uNN
            key_u = t_to_u(tribe_id, uc_local)
            need = int(au["adj_group"])
            have = int(troops_info.get(key_u, troops_info.get(uc, 0)))
            if need > have:
                can_raid = False
                logging.info(f"Not enough {resolve_unit_name(tribe_id, uc_local)} (need {need}, have {have}) for distance {distance:.1f}. Skipping.")
                add_skip("insufficient_troops")
                break
        if not can_raid:
            insufficient_skips += 1
            # Dynamic early-exit: if the remaining troop bank cannot satisfy any defined range, stop immediately
            if early_exit_on_insufficient and not _can_satisfy_any_range_with_bank(troops_info):
                logging.info("[Oasis] No viable unit combo with remaining troops; ending oasis loop early.")
                break
            # Otherwise, respect configured cap to avoid long streaks of insufficient checks
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
            uc = str(au["unit_code"])  # plan code
            uc_local = u_to_t(uc) or uc
            # payload must be a global uNN for this tribe
            upayload = t_to_u(tribe_id, uc_local)
            raid_setup[upayload] = au["adj_group"]
            unit_name = resolve_unit_name(tribe_id, uc_local)
            logging.info(f"Adding {au['adj_group']} {unit_name} to raid (base {au['base_group']})")

        logging.info(f"Launching raid on oasis at ({x}, {y})... Distance: {distance:.1f} tiles")
        try:
            attack_info = api.prepare_oasis_attack(None, x, y, raid_setup)
        except Exception as e:
            logging.error(f"❌ Failed to prepare raid to ({x}, {y}) — {e}")
            add_skip("send_failed")
            # Conservative: do not mutate local troop bank on failure
            time.sleep(uniform(0.3, 0.8))
            continue
        try:
            success = api.confirm_oasis_attack(attack_info, x, y, raid_setup, village_id)
        except Exception as e:
            logging.error(f"❌ Failed to send raid to ({x}, {y}) — {e}")
            add_skip("send_failed")
            time.sleep(uniform(0.3, 0.8))
            continue

        if success:
            logging.info(f"✅ Raid sent to ({x}, {y}) - Distance: {distance:.1f} tiles")
            add_sent(1)
            # Record last sent timestamp for scheduler (independent of learning)
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
