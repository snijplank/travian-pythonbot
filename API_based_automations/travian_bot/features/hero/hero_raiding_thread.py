import time
import random
import threading
from datetime import datetime
from core.hero_manager import HeroManager
from core.database_helpers import load_latest_unoccupied_oases
from core.hero_runner import try_send_hero_to_oasis
from identity_handling.identity_helper import load_villages_from_identity
from core.console import CONSOLE_LOCK, print_line

def _ts() -> str:
    try:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return time.strftime("%Y-%m-%d %H:%M:%S")


def safe_print(message):
    """Thread-safe printing function with timestamp prefix for CLI output."""
    # Use console.print_line to ensure a pending status/progress line is ended first
    print_line(f"{_ts()} {message}")

def run_hero_raiding_thread(api):
    """Background thread for adaptive hero raiding."""
    # Early exit if disabled in config (supports nested raiding section)
    try:
        from config.config import settings as _cfg
        def _cfg_bool(name: str, legacy: str | None = None, default: bool = True) -> bool:
            v = getattr(_cfg, name, None)
            if isinstance(v, bool):
                return v
            try:
                ra = getattr(_cfg, 'raiding', None)
                if ra is not None:
                    if isinstance(ra, dict):
                        rv = ra.get(name)
                    else:
                        rv = getattr(ra, name, None)
                    if isinstance(rv, bool):
                        return rv
            except Exception:
                pass
            # YAML fallback
            try:
                from pathlib import Path as _P
                import yaml as _yaml
                cfg_path = _P(__file__).resolve().parent.parent / 'config.yaml'
                if cfg_path.exists():
                    data = _yaml.safe_load(cfg_path.read_text(encoding='utf-8')) or {}
                    ra = (data.get('raiding') or {})
                    yv = ra.get(name)
                    if isinstance(yv, bool):
                        return yv
            except Exception:
                pass
            if legacy is not None:
                lv = getattr(_cfg, legacy, None)
                if isinstance(lv, bool):
                    return lv
            return bool(default)
        if not _cfg_bool('HERO_OASIS_CLEAR_ENABLE', legacy='ENABLE_HERO_OASIS_CLEAR', default=True):
            safe_print("[HeroOasisClear] Disabled via config; exiting thread.")
            return
    except Exception:
        pass
    safe_print("[HeroOasisClear] Hero raiding thread started.")
    safe_print("[HeroOasisClear] Thread ID: " + str(threading.get_ident()))
    
    # Persisted ETA path for mission return (shared across restarts)
    from pathlib import Path
    eta_path = Path("database/hero_mission_eta.json")

    while True:
        try:
            safe_print("[HeroOasisClear] Checking hero status...")
            hero_manager = HeroManager(api)
            status = hero_manager.fetch_hero_status()
            
            if not status:
                safe_print("[HeroOasisClear] ❌ Failed to fetch hero status")
                wait_time = 300 + random.randint(-30, 30)
                safe_print(f"[HeroOasisClear] Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
                continue

            if not status.is_present:
                safe_print("[HeroOasisClear] ❌ Hero is not present.")
                wait_time = 300 + random.randint(-30, 30)
                safe_print(f"[HeroOasisClear] Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
                continue

            if status.health is not None and status.health < 20:
                safe_print(f"[HeroOasisClear] ⚠️ Hero health too low ({status.health}%)")
                wait_time = 300 + random.randint(-30, 30)
                safe_print(f"[HeroOasisClear] Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
                continue

            if status.is_on_mission:
                # If we know the return ETA from a previous send, sleep until then
                try:
                    eta = None
                    if eta_path.exists():
                        import json as _json
                        j = _json.loads(eta_path.read_text(encoding='utf-8')) or {}
                        eta = float(j.get('return_epoch')) if j.get('return_epoch') else None
                    # If no persisted ETA, try to parse it live from rally point
                    if not eta:
                        remain = api.get_hero_return_eta()
                        if isinstance(remain, int) and remain > 0:
                            eta = time.time() + remain
                    if eta and eta > time.time():
                        wait_time = int(eta - time.time())
                        safe_print(f"[HeroOasisClear] ❌ On mission. Sleeping until ETA (~{wait_time} sec)…")
                        time.sleep(max(30, wait_time))
                        continue
                except Exception:
                    pass
                safe_print("[HeroOasisClear] ❌ Hero is on a mission.")
                wait_time = 600 + random.randint(-60, 60)
                safe_print(f"[HeroOasisClear] Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
                continue

            if not status.current_village_id:
                safe_print("[HeroOasisClear] ❌ No current village information.")
                wait_time = 300 + random.randint(-30, 30)
                safe_print(f"[HeroRaider] Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
                continue

            safe_print("[HeroOasisClear] Loading villages from identity...")
            villages = load_villages_from_identity()
            current_village = None
            for village in villages:
                if str(village["village_id"]) == str(status.current_village_id):
                    current_village = village
                    break

            if not current_village:
                safe_print(f"[HeroOasisClear] ⚠️ Hero is in village {status.current_village_id} which is not in your identity.")
                safe_print("[HeroOasisClear] Available villages in identity:")
                for v in villages:
                    safe_print(f"[HeroOasisClear] - {v['village_name']} (ID: {v['village_id']})")
                wait_time = 300 + random.randint(-30, 30)
                safe_print(f"[HeroOasisClear] Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
                continue

            # Debug: Print hero status and current village details
            safe_print(f"[HeroOasisClear] DEBUG: Hero status: {status.__dict__}")
            safe_print(f"[HeroOasisClear] DEBUG: Current village: {current_village}")

            safe_print("[HeroOasisClear] Loading unoccupied oases...")
            oases = load_latest_unoccupied_oases(f"({current_village['x']}_{current_village['y']})")
            if not oases:
                safe_print("[HeroOasisClear] ❌ No unoccupied oases found in latest scan.")
                wait_time = 300 + random.randint(-30, 30)
                safe_print(f"[HeroOasisClear] Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
                continue

            safe_print("[HeroOasisClear] Ordering candidates by distance (nearest first)…")
            # Build nearest-first candidate list using stored distances; fallback to Euclidean if missing
            def _euclid(ax, ay, bx, by):
                try:
                    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
                except Exception:
                    return 9999.0
            candidates = []
            for coord_key, tile in (oases or {}).items():
                try:
                    x_str, y_str = coord_key.split("_")
                    x_i, y_i = int(x_str), int(y_str)
                except Exception:
                    continue
                d = tile.get("distance")
                if not isinstance(d, (int, float)):
                    d = _euclid(current_village['x'], current_village['y'], x_i, y_i)
                candidates.append((d, {"x": x_i, "y": y_i}))
            candidates = [c for c in candidates if c[0] < 20]
            candidates.sort(key=lambda t: t[0])
            # Log a short preview of nearest distances
            try:
                preview = ", ".join([f"({c[1]['x']},{c[1]['y']}) {c[0]:.1f}t" for c in candidates[:5]])
                safe_print(f"[HeroOasisClear] Candidates (nearest): {preview}")
            except Exception:
                pass

            sent = False
            for dist, oasis in candidates:
                # Quick power-gate using live oasis info; also ensure unoccupied
                try:
                    info = api.get_oasis_info(oasis["x"], oasis["y"])
                except Exception:
                    continue
                if info.get("is_occupied"):
                    continue
                power = info.get("attack_power", 0)
                # Distance-gated max power
                max_power = 2000
                if dist < 3:
                    max_power = 500
                elif dist < 6:
                    max_power = 1000
                if not (50 <= int(power) <= int(max_power)):
                    continue

                safe_print(f"[HeroOasisClear] Trying nearest oasis at ({oasis['x']}, {oasis['y']}) — d={dist:.1f}t, power={power}…")
                if try_send_hero_to_oasis(api, current_village, oasis):
                    safe_print(f"[HeroOasisClear] ✅ Hero sent to oasis at ({oasis['x']}, {oasis['y']})")
                    # Verify on rally point that hero is on mission; avoid false positives where only escort left
                    time.sleep(2)
                    remain = None
                    try:
                        remain = api.get_hero_return_eta()
                    except Exception:
                        remain = None
                    if not (isinstance(remain, int) and remain > 0):
                        safe_print("[HeroOasisClear] ⚠️ Send confirmed but hero not detected on mission; likely only escort sent. Skipping ETA.")
                        sent = True
                        break
                    # Persist ETA to avoid frequent polling while on mission
                    try:
                        eta = time.time() + remain + random.randint(60, 120)
                        eta_path.parent.mkdir(parents=True, exist_ok=True)
                        import json as _json
                        eta_path.write_text(_json.dumps({"return_epoch": int(eta)}, ensure_ascii=False, indent=2), encoding='utf-8')
                    except Exception:
                        pass
                    safe_print(f"[HeroOasisClear] Hero will return in {remain / 3600:.2f} hours.")
                    time.sleep(remain + random.randint(60, 120))
                    sent = True
                    break
                # If send failed (incl. onvoldoende escorts), just fall back to next nearest automatically
            if not sent:
                safe_print("[HeroOasisClear] ❌ Failed to send hero — no feasible oasis with current escorts.")
                wait_time = 300 + random.randint(-30, 30)
                safe_print(f"[HeroOasisClear] Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
        except Exception as e:
            safe_print(f"[HeroOasisClear] Exception: {e}")
            safe_print("[HeroOasisClear] Waiting 300 seconds before retry...")
            time.sleep(300) 
