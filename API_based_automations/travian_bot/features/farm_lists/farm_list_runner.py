import logging
import time
import random
from identity_handling.identity_helper import load_villages_from_identity


def run_farmlists_for_villages(api, server_url: str, multi_village: bool = True) -> None:
    """Run ONLY farm list raids across villages.

    Respects small jitter before launch. Does not touch hero/oasis logic.
    """
    try:
        from features.farm_lists.farm_list_raider import run_farm_list_raids
    except Exception:
        logging.error("[FarmListRaider] farm_list_raider module not available.")
        return

    villages = load_villages_from_identity() or []
    if not villages:
        logging.error("[FarmListRaider] No villages found in identity. Exiting.")
        return

    logging.info("\n[FarmListRaider] Starting farm-list raids…")
    for v in villages:
        vid = v.get("village_id")
        vname = v.get("village_name")
        try:
            api.switch_village(vid)
        except Exception:
            pass
        # jitter
        try:
            from config.config import settings as _cfg
            time.sleep(random.uniform(float(getattr(_cfg, 'OP_JITTER_MIN_SEC', 0.5)), float(getattr(_cfg, 'OP_JITTER_MAX_SEC', 2.0))))
        except Exception:
            pass
        logging.info(f"[FarmListRaider] Running for village {vname}…")
        try:
            run_farm_list_raids(api, server_url, vid)
        except Exception as e:
            logging.info(f"[FarmListRaider] Error in village {vname}: {e}")

    logging.info("\n[FarmListRaider] ✅ Finished all villages.")

