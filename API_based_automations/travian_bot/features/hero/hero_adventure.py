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
        # 1) First check if any adventures are available at all to avoid misleading logs
        adventures = api.list_hero_adventures() or []
        if not adventures:
            logging.info("[HeroAdv] No adventures available to start.")
            return False

        # 2) Then check hero availability/health only if there is something to start
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

        # Prefer the JSON API contract observed in HAR: /api/v1/troop/send
        # Falls back to existing GraphQL/form flow if unavailable.
        try:
            map_id = chosen.get("gql_map_id") or chosen.get("id")
            if map_id is not None:
                res = api.send_hero_to_adventure(int(map_id), method="POST")
                if res.get("ok"):
                    eta = res.get("eta")
                    coords = res.get("coords")
                    tgt_player = ((res.get("raw", {}).get("targetPlayer") or {}).get("name"))
                    logging.info(f"[HeroAdv] ✅ Adventure started. ETA(s)={eta}, coords={coords}, targetPlayer={tgt_player}")
                    # Optional HUD refresh to reflect new mission status promptly
                    try:
                        api.refresh_hero_hud()
                    except Exception:
                        pass
                    return True
                else:
                    logging.info("[HeroAdv] JSON send did not validate (t11/eventType). Falling back.")
        except Exception as e:
            try:
                import requests
                if isinstance(e, requests.HTTPError):
                    code = getattr(getattr(e, 'response', None), 'status_code', None)
                    if code in (401, 403):
                        logging.error("[HeroAdv] Auth expired during adventure send (401/403). Re-login required.")
                    elif code == 429:
                        logging.warning("[HeroAdv] Rate limited (429) on adventure send. Try again later.")
            except Exception:
                pass
            logging.info(f"[HeroAdv] JSON send path failed: {e}. Falling back.")

        # Fallback to legacy starter when JSON route not available or failed validation
        ok = api.start_hero_adventure(chosen)
        if ok:
            logging.info("[HeroAdv] ✅ Adventure started (fallback path).")
        else:
            logging.info("[HeroAdv] ❌ Failed to start adventure (fallback path).")
        return bool(ok)
    except Exception as e:
        logging.error(f"[HeroAdv] Exception while attempting adventure: {e}")
        return False
