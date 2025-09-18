import logging
import time
from random import uniform
from features.oasis.validator import is_valid_unoccupied_oasis
from core.learning_store import LearningStore
from core.rally_tracker import enqueue_pending_raid
try:
    from config.config import settings as _cfg
except Exception:
    class _CfgFallback:
        LEARNING_ENABLE = True
    _cfg = _CfgFallback()
from core.metrics import add_sent, add_skip
from core.unit_catalog import FACTION_TO_TRIBE, resolve_label_t, t_to_u, u_to_t

def resolve_unit_name(tribe_id: int, unit_code: str) -> str:
    # Use central catalog label with local slot code
    return resolve_label_t(tribe_id, unit_code)

def get_units_for_distance(distance, distance_ranges):
    """Get the appropriate unit combination for a given distance."""
    for range_data in distance_ranges:
        if range_data["start"] <= distance < range_data["end"]:
            return range_data["units"]
    return None

# Helper: get index of the range that contains a given distance
# Used to decide promotion to the *next* range only when the current target distance
# actually lies within that next range (keeps 0–10 Mercs-only)

def get_range_index_for_distance(distance, distance_ranges):
    """Return the index of the first range that contains the distance, else -1."""
    for i, range_data in enumerate(distance_ranges):
        try:
            if float(range_data.get("start", 0)) <= float(distance) < float(range_data.get("end", float("inf"))):
                return i
        except Exception:
            continue
    return -1

def run_raid_batch(api, raid_plan, faction, village_id, oases, hero_raiding=False, hero_available=False, priority_only: bool = False):
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

    # Resolve village coordinates from identity using provided village_id
    try:
        from identity_handling.identity_helper import load_villages_from_identity
        _villages = load_villages_from_identity() or []
        _match = next((v for v in _villages if str(v.get("village_id")) == str(village_id)), None)
        if _match is not None:
            village_x, village_y = int(_match.get("x")), int(_match.get("y"))
        else:
            # Fallback: best-effort derive from scan payload (pick nearest oasis and assume distances are relative)
            first = next(iter(oases.keys()))
            vx, vy = first.split("_")
            village_x, village_y = int(vx), int(vy)
    except Exception:
        first = next(iter(oases.keys()))
        vx, vy = first.split("_")
        village_x, village_y = int(vx), int(vy)
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
    def _min_required_for_unit(unit_code: str, group: int, base_group: int) -> int:
        norm = u_to_t(unit_code) or unit_code
        if norm == "t1" and group < 2:
            target = max(2, base_group)
            return target
        return group

    for coords, tile in oases.items():
        x_str, y_str = coords.split("_")
        x_i, y_i = int(x_str), int(y_str)
        key = f"({x_i},{y_i})"
        pause_until = ls.get_pause_until(key) if ls else None
        priority_until = ls.get_priority_until(key) if ls else None
        if priority_only:
            if not priority_until or float(priority_until) <= now:
                continue
        if pause_until and float(pause_until) > now:
            try:
                remain = max(0, float(pause_until) - now)
                logging.info(
                    f"[Oasis] Paused target {key} for another {remain/60:.1f} minute(s); skipping.")
            except Exception:
                logging.info(f"[Oasis] Paused target {key}; skipping.")
            continue
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
    # Snapshot full schedule BEFORE filtering (used to persist next upcoming due time)
    sched_all = list(sched)

    # Optionally force focus on the nearest oasis only
    try:
        from config.config import settings as _sched_cfg  # type: ignore
        always_nearest_only = bool(getattr(_sched_cfg, 'OASIS_ALWAYS_NEAREST_ONLY', False))
    except Exception:
        always_nearest_only = False

    # In nearest-only mode, we now sort all due candidates by distance and
    # fall back to the next one if the nearest gets skipped (e.g., animals present).
    # We will still stop after the first successful send to preserve cadence semantics.
    stop_after_first_success = False
    if always_nearest_only:
        due_candidates = [t for t in sched if (isinstance(t[0], (int, float)) and t[0] <= 0)]
        due_candidates.sort(key=lambda t: (t[2]))  # nearest first
        sched = due_candidates
        try:
            sample = ", ".join([f"{c} {d:.1f}t" for (_, c, d) in sched[:5]])
            logging.info(f"[Oasis] Ordering due targets by distance (nearest first). Count={len(sched)}. Nearest: {sample}")
        except Exception:
            pass
        stop_after_first_success = True
    else:
        # Keep only due or overdue items; order strictly by distance (nearest first),
        # ignoring how long an oasis is overdue. This matches the desired behavior:
        # always handle the shortest distance first among due targets.
        sched = [t for t in sched if (isinstance(t[0], (int, float)) and t[0] <= 0)]
        sched.sort(key=lambda t: t[2])
        try:
            sample = ", ".join([f"{c} {d:.1f}t" for (_, c, d) in sched[:5]])
            logging.info(f"[Oasis] Ordering due targets by distance (nearest first). Count={len(sched)}. Nearest: {sample}")
        except Exception:
            pass

        # === PATCH: filter due targets op ranges die haalbaar zijn met huidige troepen ===
        def _range_satisfiable(dr: dict, bank: dict, tribe_id: int) -> bool:
            try:
                for u in (dr.get("units") or []):
                    uc = str(u.get("unit_code"))
                    need = int(u.get("group_size", 0))
                    if need <= 0:
                        continue
                    from core.unit_catalog import t_to_u, u_to_t
                    uc_local = u_to_t(uc) or uc  # accepteer zowel uNN als tX codes
                    key_u = t_to_u(tribe_id, uc_local)
                    have = int(bank.get(key_u, bank.get(uc, 0)) or 0)
                    if have < need:
                        return False
                return True
            except Exception:
                return False

        satisfiable_ranges = []
        for dr in (distance_ranges or []):
            if _range_satisfiable(dr, troops_info, tribe_id):
                satisfiable_ranges.append((float(dr.get("start", 0)), float(dr.get("end", 0))))

        if satisfiable_ranges:
            try:
                _parts = []
                for (s, e) in satisfiable_ranges:
                    if e == float("inf"):
                        _parts.append(f"{s:.0f}+")
                    else:
                        _parts.append(f"{s:.0f}–{e:.0f}")
                logging.info(f"[Oasis] Ranges satisfiable with current troop bank: {', '.join(_parts)}")
            except Exception:
                pass
            before = len(sched)
            sched = [t for t in sched if any(s <= float(t[2]) < e for (s, e) in satisfiable_ranges)]
            after = len(sched)
            logging.info(f"[Oasis] Filtered due targets by satisfiable ranges: {before} → {after}")
        else:
            logging.info("[Oasis] No ranges satisfiable with current troop bank.")
        # === END PATCH

    ordered_targets = [(coords, oases[coords]) for _, coords, _ in sched]

    # Persist the earliest upcoming due time for a simple external countdown
    try:
        import time as _t
        from pathlib import Path as _P
        # Use the full, unfiltered schedule to compute the NEXT due time (>0 only),
        # so that even when some are due now (<=0) we still expose the upcoming future due.
        upcoming = [float(d) for (d, _, _) in sched_all if isinstance(d, (int, float)) and float(d) > 0.0]
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
        # Adjust per-unit composition with multiplier, enforce floors per unit type
        adjusted_units = []
        for u in units:
            base_g = int(u.get("group_size", 0))
            adj_g = int(round(base_g * mul)) if base_g > 0 else 0
            if base_g > 0 and adj_g <= 0:
                adj_g = 1
            adj_g = _min_required_for_unit(u["unit_code"], adj_g, base_g)
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
            # Optionally promote to the next distance range, but ONLY if this target's
            # distance actually lies within that next range. Keeps ranges intact:
            # 0–10 stays Mercs-only; for <10 we never send Steppe.
            try:
                enable_promote = bool(getattr(_cfg, 'OASIS_PROMOTE_TO_NEXT_RANGE', True))
            except Exception:
                enable_promote = True

            promoted_used = False
            if enable_promote and distance_ranges:
                idx = get_range_index_for_distance(distance, distance_ranges)
                if idx >= 0:
                    for next_idx in range(idx + 1, len(distance_ranges)):
                        next_range = distance_ranges[next_idx]
                        try:
                            nr_start = float(next_range.get('start', 0))
                            nr_end = float(next_range.get('end', float('inf')))
                        except Exception:
                            nr_start, nr_end = 0.0, float('inf')
                        if not (nr_start <= float(distance) < nr_end):
                            continue  # target not in next range; do not promote for this target
                        next_units = next_range.get('units') or []
                        if not next_units:
                            continue
                        # Build adjusted units for the next range using the same multiplier
                        next_adjusted = []
                        for u in next_units:
                            base_g = int(u.get('group_size', 0))
                            adj_g = int(round(base_g * mul)) if base_g > 0 else 0
                            if base_g > 0 and adj_g <= 0:
                                adj_g = 1
                            adj_g = _min_required_for_unit(u['unit_code'], adj_g, base_g)
                            next_adjusted.append({"unit_code": u['unit_code'], "base_group": base_g, "adj_group": adj_g})
                        # Check availability for promotion composition
                        can_promote = True
                        for au in next_adjusted:
                            uc = str(au['unit_code'])
                            uc_local = u_to_t(uc) or uc
                            key_u = t_to_u(tribe_id, uc_local)
                            need = int(au['adj_group'])
                            have = int(troops_info.get(key_u, troops_info.get(uc, 0)))
                            if need > have:
                                can_promote = False
                                break
                        if can_promote:
                            logging.info(f"[Promote] Current range insufficient → switching to next range {next_range.get('start')}-{next_range.get('end')} for distance {distance:.1f} (e.g., Steppe if defined).")
                            adjusted_units = next_adjusted
                            units = next_units
                            promoted_used = True
                            break
            if not promoted_used:
                insufficient_skips += 1
                # Dynamic early-exit: if the remaining troop bank cannot satisfy any defined range, stop immediately
                if early_exit_on_insufficient and not _can_satisfy_any_range_with_bank(troops_info):
                    logging.info("[Oasis] No viable unit combo with remaining troops; ending oasis loop early.")
                    break
                # Respect configured cap to avoid long streaks of insufficient checks
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
        travel_time_sec = attack_info.get("travel_time_sec") if isinstance(attack_info, dict) else None
        depart_epoch = time.time()
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
            try:
                ls.clear_priority(key)
            except Exception:
                pass
            # Log één pending entry voor deze raid zodat de report checker de multiplier kan bijstellen.
            # Omdat we hier een combinatie van units sturen, labelen we dit als 'mixed'.
            adj_total = sum(int(a["adj_group"]) for a in adjusted_units)
            if use_learning:
                try:
                    sent_units: dict[str, int] = {}
                    for au in adjusted_units:
                        uc = str(au["unit_code"])
                        local_code = u_to_t(uc) or uc
                        global_code = t_to_u(tribe_id, local_code)
                        sent_units[global_code] = sent_units.get(global_code, 0) + int(au["adj_group"])
                    enqueue_pending_raid(
                        village_id=village_id,
                        target=key,
                        recommended=int(base_total),
                        sent_total=int(adj_total),
                        sent_units=sent_units,
                        depart_epoch=depart_epoch,
                        travel_time_sec=float(travel_time_sec) if travel_time_sec else None,
                    )
                except Exception as exc:
                    logging.debug(f"[Learning] Failed to queue rally tracker pending for {key}: {exc}")
            # Update available troops
            for au in adjusted_units:
                uc = au["unit_code"]
                key_u = t_to_u(tribe_id, uc)
                if key_u in troops_info:
                    troops_info[key_u] = max(0, int(troops_info.get(key_u, 0)) - int(au["adj_group"]))
                else:
                    troops_info[uc] = max(0, int(troops_info.get(uc, 0)) - int(au["adj_group"]))
            sent_raids += 1
            if stop_after_first_success:
                # In nearest-only mode, end the loop after the first successful send.
                break
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
