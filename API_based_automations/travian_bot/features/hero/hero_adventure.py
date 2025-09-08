import logging
from typing import Optional

from core.hero_manager import HeroManager

try:
    from config.config import settings
except Exception:
    class _F: pass
    settings = _F(); settings.HERO_ADVENTURE_ENABLE = True; settings.HERO_ADVENTURE_MIN_HEALTH = 40; settings.HERO_ADVENTURE_MAX_DURATION_MIN = 180; settings.HERO_ADVENTURE_ALLOW_DANGER = True


def maybe_start_adventure(api) -> bool:
    """Try to start a hero adventure based on config thresholds.

    Returns True if an adventure was started, False otherwise.
    """
    if not bool(getattr(settings, "HERO_ADVENTURE_ENABLE", True)):
        return False

    try:
        hero = HeroManager(api)
        status = hero.fetch_hero_status()
        if not status:
            logging.info("[HeroAdv] No hero status available.")
            return False

        if not status.is_present:
            logging.info("[HeroAdv] Hero not present; cannot start an adventure.")
            return False

        if status.is_on_mission:
            logging.info("[HeroAdv] Hero currently on mission; skipping adventures.")
            return False

        min_health = int(getattr(settings, "HERO_ADVENTURE_MIN_HEALTH", 40))
        if isinstance(status.health, (int, float)) and status.health < min_health:
            logging.info(f"[HeroAdv] Hero health too low ({status.health}%) < {min_health}%.")
            return False

        adventures = api.list_hero_adventures() or []
        if not adventures:
            logging.info("[HeroAdv] No adventures available to start.")
            return False

        # Filter by duration and danger level per config
        allow_danger = bool(getattr(settings, "HERO_ADVENTURE_ALLOW_DANGER", True))
        max_dur = int(getattr(settings, "HERO_ADVENTURE_MAX_DURATION_MIN", 0) or 0)

        def _ok(adv: dict) -> bool:
            # danger filter
            dang = adv.get("is_dangerous")
            if dang is True and not allow_danger:
                return False
            # duration filter if configured (>0)
            dmin = adv.get("duration_min")
            if max_dur > 0 and isinstance(dmin, int):
                return dmin <= max_dur
            # if duration unknown and max_dur set, still allow (be optimistic)
            return True

        candidates = [a for a in adventures if _ok(a)]
        if not candidates:
            logging.info("[HeroAdv] Adventures found, but none meet filters.")
            return False

        # Prefer the shortest known duration
        candidates.sort(key=lambda a: (a.get("duration_min") or 10_000))
        chosen = candidates[0]
        dtxt = f"~{chosen.get('duration_min')}min" if isinstance(chosen.get("duration_min"), int) else "unknown"
        logging.info(f"[HeroAdv] Starting adventure (duration {dtxt}, dangerous={chosen.get('is_dangerous')}).")
        ok = api.start_hero_adventure(chosen)
        if ok:
            logging.info("[HeroAdv] ✅ Adventure started.")
        else:
            logging.info("[HeroAdv] ❌ Failed to start adventure.")
        return bool(ok)
    except Exception as e:
        logging.error(f"[HeroAdv] Exception while attempting adventure: {e}")
        return False

