import logging
import random
import time
from typing import Optional


def collect_rewards_for_all_villages(api, verbose: bool = False) -> int:
    """Iterate villages, parse /tasks for collectible items, and collect rewards.

    Returns the number of successful collect operations.
    """
    successes = 0
    try:
        pinfo = api.get_player_info() or {}
        villages = (pinfo.get("villages") or [])
    except Exception as e:
        logging.error(f"[Tasks] Could not fetch villages: {e}")
        return 0

    # Randomize order a bit to avoid fixed patterns
    try:
        random.shuffle(villages)
    except Exception:
        pass

    for v in villages:
        vid = v.get("id")
        vname = v.get("name") or str(vid)
        try:
            api.switch_village(vid)
            items = api.list_collectible_progressive_tasks() or []
            if verbose:
                print(f"[Tasks] {vname}: found {len(items)} collectible item(s).")
            # Randomize order
            try:
                random.shuffle(items)
            except Exception:
                pass
            for it in items:
                # Defensive: ensure mandatory keys
                if not it.get("questType") or not it.get("scope"):
                    continue
                # Add hero level if missing
                if "heroLevel" not in it:
                    lvl = api.get_hero_level()
                    if lvl is not None:
                        it["heroLevel"] = int(lvl)
                res = api.collect_progressive_reward(it)
                if res and res.get("success"):
                    successes += 1
                    try:
                        qt = it.get("questType")
                        b = it.get("buildingId")
                        tl = it.get("targetLevel")
                        logging.info(f"[Tasks] ✅ Collected reward in {vname} (questType={qt}, buildingId={b}, targetLevel={tl}).")
                    except Exception:
                        logging.info(f"[Tasks] ✅ Collected reward in {vname}.")
                    # Optionally refresh HUD (UI does this)
                    try:
                        from config.config import settings as _cfg
                        if bool(getattr(_cfg, 'PROGRESSIVE_TASKS_REFRESH_HUD', True)):
                            api.refresh_hero_hud()
                    except Exception:
                        pass
                    # small jitter between collects
                    try:
                        time.sleep(random.uniform(0.3, 1.2))
                    except Exception:
                        pass
                else:
                    logging.info(f"[Tasks] Collect maybe not successful (payload={it}, resp={res}).")
        except Exception as e:
            logging.warning(f"[Tasks] Error processing village {vname}: {e}")

    return successes


def count_collectible_rewards(api) -> int:
    """Return total number of collectible progressive task rewards across villages.

    Lightweight counter used for cycle status; does not perform any collect.
    """
    total = 0
    try:
        pinfo = api.get_player_info() or {}
        villages = (pinfo.get("villages") or [])
    except Exception:
        return 0
    for v in villages:
        vid = v.get("id")
        try:
            api.switch_village(vid)
            items = api.list_collectible_progressive_tasks() or []
            total += len(items)
        except Exception:
            continue
    return total
