import logging
import time
from identity_handling.identity_helper import load_villages_from_identity
from core.database_helpers import load_latest_unoccupied_oases
from core.unit_catalog import resolve_label_u
from features.oasis.raider import run_raid_batch
from core.database_raid_config import load_saved_raid_plan
from core.learning_store import LearningStore


def run_empty_oasis_raids(api, server_url: str, multi_village: bool = True, priority_only: bool = False) -> None:
    """Run ONLY empty-oasis raids (no farm lists, no hero).

    - Iterates selected villages (or all when multi_village=True)
    - Loads saved raid plan per village
    - Loads unoccupied oases from scan DB
    - Sends raids via features.oasis.raider.run_raid_batch
    """
    villages = load_villages_from_identity() or []
    if not villages:
        logging.error("[EmptyOasisRaider] No villages found in identity. Exiting.")
        return

    phase = "priority" if priority_only else "standard"
    logging.info(f"\n[EmptyOasisRaider] Starting empty-oasis raids ({phase}).")
    for idx, v in enumerate(villages):
        village_name = v.get("village_name")
        village_id = v.get("village_id")
        vx, vy = v.get("x"), v.get("y")
        logging.info(
            f"[EmptyOasisRaider] Processing village {idx+1}/{len(villages)}: {village_name} ({vx}, {vy})"
        )

        # Ensure correct village context
        try:
            api.switch_village(village_id)
        except Exception:
            pass

        troops_info = api.get_troops_in_village() or {}
        if not troops_info:
            logging.info("[EmptyOasisRaider] No troop info available; skipping village.")
            continue
        logging.info("[EmptyOasisRaider] Current troops in village:")
        for unit_code, amount in troops_info.items():
            try:
                label = resolve_label_u(v.get("tribe_id", 4), unit_code)
            except Exception:
                label = unit_code
            logging.info(f"    {label}: {amount} units")

        # Load saved raid plan
        saved = load_saved_raid_plan(idx)
        if not (saved and saved.get("server") == server_url):
            logging.warning(
                f"[EmptyOasisRaider] ⚠️ No saved raid plan found for {village_name}. Skipping oasis raids."
            )
            continue

        # Load unoccupied oases near village
        oases = load_latest_unoccupied_oases(f"({vx}_{vy})")
        if not oases:
            logging.info(
                f"[EmptyOasisRaider] No unoccupied oases found for {village_name}. Skipping."
            )
            continue

        if priority_only:
            ls = LearningStore()
            now = time.time()
            has_priority = False
            for coords in oases.keys():
                x_str, y_str = coords.split("_")
                key = f"({int(x_str)},{int(y_str)})"
                until = ls.get_priority_until(key)
                if until and float(until) > now:
                    has_priority = True
                    break
            if not has_priority:
                logging.debug(
                    f"[EmptyOasisRaider] No priority targets for {village_name}; skipping in priority phase."
                )
                continue

        # Send raids using the extracted batch runner
        faction = v.get("faction") or v.get("tribe_name") or "Unknown"
        sent = run_raid_batch(
            api,
            saved,
            faction,
            village_id,
            oases,
            hero_raiding=False,
            hero_available=False,
            priority_only=priority_only,
        )
        logging.info(
            f"[EmptyOasisRaider] Village {village_name}: sent {sent} empty-oasis raid(s)."
        )

    logging.info(f"\n[EmptyOasisRaider] ✅ Finished all villages ({phase}).")
