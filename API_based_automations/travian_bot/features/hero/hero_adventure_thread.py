import time
import random
import threading
import logging

from core.hero_manager import HeroManager

try:
    from config.config import settings
except Exception:
    class _F: pass
    settings = _F(); settings.HERO_ADVENTURE_ENABLE = True; settings.HERO_ADVENTURE_MIN_HEALTH = 40; settings.HERO_ADVENTURE_MAX_DURATION_MIN = 180; settings.HERO_ADVENTURE_ALLOW_DANGER = True


def run_hero_adventure_thread(api):
    """Background thread that starts hero adventures as soon as they appear."""
    logging.info("[HeroAdv] Adventure thread started.")
    while True:
        try:
            if not bool(getattr(settings, "HERO_ADVENTURE_ENABLE", True)):
                time.sleep(60)
                continue

            hero = HeroManager(api)
            st = hero.fetch_hero_status()
            if not st or not st.is_present:
                # Hero missing → wait longer
                time.sleep(120)
                continue
            if st.is_on_mission:
                # On adventure/mission already
                time.sleep(90)
                continue

            min_health = int(getattr(settings, "HERO_ADVENTURE_MIN_HEALTH", 40))
            if isinstance(st.health, (int, float)) and st.health < min_health:
                time.sleep(180)
                continue

            advs = api.list_hero_adventures() or []
            if advs:
                allow_danger = bool(getattr(settings, "HERO_ADVENTURE_ALLOW_DANGER", True))
                max_dur = int(getattr(settings, "HERO_ADVENTURE_MAX_DURATION_MIN", 0) or 0)
                # Filter per config
                def _ok(a):
                    dang = a.get("is_dangerous")
                    if dang is True and not allow_danger:
                        return False
                    dmin = a.get("duration_min")
                    if max_dur > 0 and isinstance(dmin, int):
                        return dmin <= max_dur
                    return True
                cand = [a for a in advs if _ok(a)]
                cand.sort(key=lambda a: (a.get("duration_min") or 10_000))
                if cand:
                    chosen = cand[0]
                    if api.start_hero_adventure(chosen):
                        logging.info("[HeroAdv] ✅ Adventure started by thread.")
                        # Let the hero depart; sleep a bit longer
                        time.sleep(120)
                        continue
                    else:
                        logging.info("[HeroAdv] ❌ Could not start adventure.")

            # No adventures or not eligible → sleep
            base = int(getattr(settings, "HERO_ADVENTURE_POLL_INTERVAL_SEC", 90))
            jit = int(getattr(settings, "HERO_ADVENTURE_RANDOM_JITTER_SEC", 45))
            time.sleep(max(30, base + random.randint(-jit, jit)))
        except Exception as e:
            logging.error(f"[HeroAdv] Thread exception: {e}")
            time.sleep(120)

