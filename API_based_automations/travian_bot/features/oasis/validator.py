from analysis.tile_analysis import analyze_tile
import logging
from pathlib import Path
from core.simple_cache import JsonKvCache

try:
    from config.config import settings as _cfg
except Exception:
    class _Cfg: pass
    _cfg = _Cfg(); _cfg.OASIS_ANIMALS_CACHE_TTL_SEC = 600

def is_valid_unoccupied_oasis(api, x, y, distance: float | None = None):
    """
    Checks if the oasis at (x, y) is unoccupied and has no animals.
    
    :param api: TravianAPI instance
    :param x: X coordinate
    :param y: Y coordinate
    :return: True if oasis is valid for raiding (unoccupied and no animals), False otherwise
    """
    # Small TTL cache to avoid re-fetching the same tile HTML repeatedly
    ttl = int(getattr(_cfg, 'OASIS_ANIMALS_CACHE_TTL_SEC', 600) or 0)
    cache = None
    key = f"({int(x)},{int(y)})"
    animals_cached = None
    if ttl > 0:
        try:
            cache = JsonKvCache(Path("database/cache/oasis_animals_cache.json"))
            ent = cache.get(key)
            if ent:
                ts = float(ent.get("ts", 0))
                if ts and (ts > 0):
                    import time as _t
                    if (_t.time() - ts) <= ttl:
                        animals_cached = (ent.get("value") or {}).get("animals")
                        # Use cached decision when present
                        if animals_cached and any((animals_cached or {}).values()):
                            suffix = f" — Distance: {distance:.1f} tiles" if isinstance(distance, (int, float)) else ""
                            logging.info(f"[cache] Skipping oasis at ({x}, {y}) — Animals present: {animals_cached}{suffix}")
                            return False, "animals_present"
                        if isinstance(animals_cached, dict) and not any(animals_cached.values()):
                            # Cached empty animals → still need to confirm type is unoccupied oasis
                            pass
        except Exception:
            pass

    html = api.get_tile_html(x, y)
    tile_info = analyze_tile(html, (x, y))
    
    # Must be an unoccupied oasis
    if tile_info['type'] != 'unoccupied_oasis':
        suffix = f" — Distance: {distance:.1f} tiles" if isinstance(distance, (int, float)) else ""
        logging.info(f"Skipping tile at ({x}, {y}) — Not an unoccupied oasis{suffix}")
        return False, "not_unoccupied"
        
    # Check for animals (handle case where animals might be None)
    animals = tile_info.get('animals', {})
    # Update cache with latest animals info
    try:
        if ttl > 0 and cache is not None:
            cache.set(key, {"animals": animals})
            # Occasionally purge outdated entries
            cache.purge_older_than(ttl * 3)
    except Exception:
        pass
    if animals and any(animals.values()):
        suffix = f" — Distance: {distance:.1f} tiles" if isinstance(distance, (int, float)) else ""
        logging.info(f"Skipping oasis at ({x}, {y}) — Animals present: {animals}{suffix}")
        return False, "animals_present"
        
    return True, "ok"
