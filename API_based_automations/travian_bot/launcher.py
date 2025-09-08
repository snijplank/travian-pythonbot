import time
import random
import json
import os
import threading
import logging
import sys
from identity_handling.login import login
from core.travian_api import TravianAPI
from core.database_helpers import load_latest_unoccupied_oases
from oasis_raiding_from_scan_list_main import run_raid_planner
from raid_list_main import run_one_farm_list_burst
from features.raiding.reset_raid_plan import reset_saved_raid_plan
from features.raiding.setup_interactive_plan import setup_interactive_raid_plan
from identity_handling.identity_manager import handle_identity_management
from identity_handling.identity_helper import load_villages_from_identity
from features.hero.hero_operations import run_hero_operations as run_hero_ops, print_hero_status_summary
from features.hero.hero_raiding_thread import run_hero_raiding_thread
from features.hero.hero_adventure_thread import run_hero_adventure_thread
from features.hero.hero_adventure import maybe_start_adventure
from core.hero_manager import HeroManager
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from logging.handlers import RotatingFileHandler
from core.report_checker import process_ready_pendings_once
from features.defense.attack_detector import run_attack_detector_thread

# === CONFIG (centralized) ===
try:
    from config.config import settings
except Exception:
    class _Fallback:
        pass
    settings = _Fallback()
    settings.WAIT_BETWEEN_CYCLES_MINUTES = 10
    settings.JITTER_MINUTES = 10
    settings.SERVER_SELECTION = 0
    settings.LOG_LEVEL = "INFO"
    settings.LOG_DIR = "logs"

 

# --- Logging setup ---
LOGGER = None

def _setup_logging():
    global LOGGER
    LOGGER = logging.getLogger("travian")
    if LOGGER.handlers:
        return

    level_name = getattr(settings, "LOG_LEVEL", "INFO")
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    LOGGER.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)
    LOGGER.addHandler(ch)

    log_dir = Path(getattr(settings, "LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(log_dir / "bot.log", maxBytes=5*1024*1024, backupCount=5, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    LOGGER.addHandler(fh)

# Ensure unbuffered prints for interactive runs
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

def _log_info(msg): 
    if LOGGER: LOGGER.info(msg)
def _log_warn(msg): 
    if LOGGER: LOGGER.warning(msg)
def _log_error(msg, exc=None):
    if LOGGER:
        if exc:
            LOGGER.exception(msg)
        else:
            LOGGER.error(msg)

# --- Daily limiter helpers ---
RUNTIME_TRACK_PATH = Path("database/runtime_track.json")

def _today_str(): return datetime.now().strftime("%Y-%m-%d")

def _load_runtime_state():
    import json
    try:
        if RUNTIME_TRACK_PATH.exists():
            with open(RUNTIME_TRACK_PATH, "r", encoding="utf-8") as f:
                st = json.load(f)
                if isinstance(st, dict) and "date" in st and "minutes_used" in st:
                    return st
    except Exception: pass
    return {"date": _today_str(), "minutes_used": 0}

def _save_runtime_state(state):
    import json
    try:
        RUNTIME_TRACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(RUNTIME_TRACK_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[Limiter] ⚠️ Could not save runtime state: {e}")

def _minutes_until_tomorrow():
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return int((tomorrow - now).total_seconds() // 60)

def _sleep_minutes(minutes):
    if minutes <= 0: return
    print(f"[Limiter] ⏸️ Sleeping {minutes} minutes...")
    _log_info(f"Sleeping {minutes} minutes...")
    time.sleep(minutes*60)

def _compute_limiter_params():
    import random
    base_total = max(0, int(getattr(settings, "DAILY_MAX_RUNTIME_MINUTES", 600)))
    variance = float(getattr(settings, "DAILY_VARIANCE_PCT", 0.0))
    if variance > 0:
        delta = int(base_total * random.uniform(-variance, variance))
    else:
        delta = 0
    total = max(0, base_total + delta)
    blocks = max(1, int(getattr(settings, "DAILY_BLOCKS", 3)))
    # Random block size if configured
    bmin = int(getattr(settings, "BLOCK_SIZE_MIN", 0))
    bmax = int(getattr(settings, "BLOCK_SIZE_MAX", 0))
    if bmax > 0 and bmax >= bmin > 0:
        block_size = random.randint(bmin, bmax)
    else:
        block_size = max(1, total // blocks)
    # Random rest between blocks if configured
    rmin = int(getattr(settings, "REST_MIN_MINUTES", 0))
    rmax = int(getattr(settings, "REST_MAX_MINUTES", 0))
    rest_between_blocks = (rmin, rmax) if (rmax >= rmin > 0) else (0, 0)
    return total, blocks, block_size, rest_between_blocks

def _parse_quiet_windows() -> list[tuple[dtime,dtime]]:
    out = []
    try:
        wins = getattr(settings, "QUIET_WINDOWS", []) or []
        for w in wins:
            try:
                a,b = [p.strip() for p in str(w).split('-')]
                h1,m1 = [int(x) for x in a.split(':')]
                h2,m2 = [int(x) for x in b.split(':')]
                out.append((dtime(h1,m1), dtime(h2,m2)))
            except Exception:
                continue
    except Exception:
        pass
    return out

def _in_quiet_window(now: datetime, windows: list[tuple[dtime,dtime]]) -> timedelta | None:
    for (s,e) in windows:
        start = now.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0)
        end = now.replace(hour=e.hour, minute=e.minute, second=0, microsecond=0)
        if end <= start:
            end += timedelta(days=1)
        if start <= now < end:
            return end - now
    return None

def view_identity():
    """Display the current identity information."""
    try:
        with open("database/identity.json", "r", encoding="utf-8") as f:
            identity = json.load(f)
        
        travian_identity = identity.get("travian_identity", {})
        faction = travian_identity.get("faction", "unknown").title()
        tribe_id = travian_identity.get("tribe_id", "unknown")
        
        print("\n👤 Current Identity:")
        print(f"Faction: {faction} (ID: {tribe_id})")
        print("\n🏰 Villages:")
        
        for server in travian_identity.get("servers", []):
            for village in server.get("villages", []):
                name = village.get("village_name", "Unknown")
                vid = village.get("village_id", "?")
                x = village.get("x", "?")
                y = village.get("y", "?")
                print(f"- {name} (ID: {vid}) at ({x}|{y})")
    
    except FileNotFoundError:
        print("\n❌ No identity file found. Please set up your identity first.")
    except json.JSONDecodeError:
        print("\n❌ Identity file is corrupted. Please set up your identity again.")
    except Exception as e:
        print(f"\n❌ Error reading identity: {e}")

def update_village_coordinates():
    """Update coordinates for existing villages."""
    try:
        # Read current identity
        with open("database/identity.json", "r", encoding="utf-8") as f:
            identity = json.load(f)
        
        travian_identity = identity.get("travian_identity", {})
        servers = travian_identity.get("servers", [])
        
        if not servers:
            print("\n❌ No servers found in identity file.")
            return
        
        # For each server's villages
        for server in servers:
            villages = server.get("villages", [])
            print("\n🏰 Your villages:")
            for i, village in enumerate(villages):
                name = village.get("village_name", "Unknown")
                current_x = village.get("x", "?")
                current_y = village.get("y", "?")
                print(f"[{i}] {name} - Current coordinates: ({current_x}|{current_y})")
            
            while True:
                try:
                    choice = input("\nEnter village number to update (or 'q' to quit): ").strip()
                    if choice.lower() == 'q':
                        break
                    
                    village_idx = int(choice)
                    if village_idx < 0 or village_idx >= len(villages):
                        print("❌ Invalid village number.")
                        continue
                    
                    coords = input(f"Enter new coordinates for {villages[village_idx]['village_name']} (format: x y): ").strip().split()
                    if len(coords) != 2:
                        print("❌ Invalid format. Please enter two numbers separated by space.")
                        continue
                    
                    x, y = map(int, coords)
                    villages[village_idx]["x"] = x
                    villages[village_idx]["y"] = y
                    print(f"✅ Updated coordinates to ({x}|{y})")
                
                except ValueError:
                    print("❌ Invalid input. Please enter valid numbers.")
                except Exception as e:
                    print(f"❌ Error: {e}")
        
        # Save updated identity
        with open("database/identity.json", "w", encoding="utf-8") as f:
            json.dump(identity, f, indent=4, ensure_ascii=False)
        print("\n✅ Successfully saved updated coordinates.")
    
    except FileNotFoundError:
        print("\n❌ No identity file found. Please set up your identity first.")
    except Exception as e:
        print(f"\n❌ Error: {e}")

def handle_identity_management():
    """Handle identity management sub-menu."""
    print("""
👤 Identity Management
[1] Set up new identity
[2] View current identity
[3] Update village coordinates
[4] Back to main menu
""")
    choice = input("Select an option: ").strip()
    
    if choice == "1":
        print("\nℹ️ Running identity setup...")
        os.system("python3 setup_identity.py")
    elif choice == "2":
        view_identity()
    elif choice == "3":
        update_village_coordinates()
    elif choice == "4":
        return
    else:
        print("❌ Invalid choice.")


def _write_config_yaml(updates: dict):
    """Naive updater for config.yaml keys (top-level)."""
    import yaml
    path = Path("config.yaml")
    try:
        data = {}
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data.update(updates or {})
        path.write_text(yaml.dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        print("✅ Saved changes to config.yaml")
    except Exception as e:
        print(f"❌ Could not write config.yaml: {e}")


def tools_menu(api: TravianAPI):
    """Menu for tools & detectors (e.g., attack detector)."""
    while True:
        print("""
🛠️ Tools & Detectors
[1] Toggle Attack Detector (enable/disable)
[2] Set Discord Webhook URL
[3] Test Discord Notification (with screenshot)
[4] Process reports now (one pass)
[5] Back to main menu
""")
        sel = input("Select an option: ").strip()
        if sel == "1":
            cur = bool(getattr(settings, "ATTACK_DETECTOR_ENABLE", False))
            new = not cur
            _write_config_yaml({"ATTACK_DETECTOR_ENABLE": new})
            print(f"Attack Detector is now {'ENABLED' if new else 'DISABLED'}")
            # reflect in current session settings
            try:
                settings.ATTACK_DETECTOR_ENABLE = new
            except Exception:
                pass
        elif sel == "2":
            url = input("Enter Discord webhook URL: ").strip()
            if url:
                _write_config_yaml({"ATTACK_DETECTOR_DISCORD_WEBHOOK": url})
                try:
                    settings.ATTACK_DETECTOR_DISCORD_WEBHOOK = url
                except Exception:
                    pass
        elif sel == "3":
            # Attempt to send a test to webhook (with optional screenshot)
            url = getattr(settings, "ATTACK_DETECTOR_DISCORD_WEBHOOK", "")
            if not url:
                print("❌ No webhook configured.")
                continue
            try:
                from features.defense.attack_detector import _send_discord  # type: ignore
                shot = None
                # Try to capture a screenshot if pyautogui is available; else send text-only
                try:
                    import pyautogui  # type: ignore
                    shot = pyautogui.screenshot()
                except Exception:
                    shot = None
                ok = _send_discord(url, "🔔 Test notification from Travian bot", shot)
                print("✅ Sent" if ok else "❌ Failed to send")
            except Exception as e:
                print(f"❌ Error: {e}")
        elif sel == "4":
            try:
                # Verbose one-pass processing with progress output
                n = process_ready_pendings_once(api, verbose=True)
                print(f"✅ ReportChecker processed {n} pending(s).")
            except Exception as e:
                print(f"❌ Failed to process reports: {e}")
        elif sel == "5":
            return
        else:
            print("❌ Invalid option.")

def run_hero_operations(api: TravianAPI):
    """Run hero-specific operations including checking status and sending to suitable oases."""
    run_hero_ops(api)

def setup_interactive_raid_plan(api, server_url):
    """Set up a raid plan interactively."""
    print("\n🎯 Interactive Raid Plan Creator")
    print("[1] Set up new raid plan")
    print("[2] Use saved configuration")
    
    choice = input("\nSelect an option: ").strip()
    
    if choice == "1":
        from features.raiding.setup_interactive_plan import setup_interactive_raid_plan
        setup_interactive_raid_plan(api, server_url)
    elif choice == "2":
        # Load saved configuration
        try:
            with open("database/saved_raid_plan.json", "r", encoding="utf-8") as f:
                saved_config = json.load(f)
            
            # Create raid plans for all villages
            from features.raiding.setup_interactive_plan import create_raid_plan_from_saved
            from identity_handling.identity_helper import load_villages_from_identity
            
            villages = load_villages_from_identity()
            if not villages:
                print("❌ No villages found in identity. Exiting.")
                return
            
            for i, village in enumerate(villages):
                print(f"\nSetting up raid plan for {village['village_name']}...")
                create_raid_plan_from_saved(api, server_url, i, saved_config)
            
            print("\n✅ Finished setting up raid plans for all villages.")
        except FileNotFoundError:
            print("❌ No saved raid plan found. Please set up a new raid plan first.")
        except Exception as e:
            print(f"❌ Error: {e}")
    else:
        print("❌ Invalid option.")

def run_map_scan(api: TravianAPI):
    """Run map scanning operations."""
    print("\n🗺️ Map Scanning")
    print("[1] Scan for unoccupied oases")
    print("[2] View latest scan results")
    print("[3] Back to main menu")
    
    choice = input("\nSelect an option: ").strip()
    
    if choice == "1":
        from features.map_scanning.scan_map import scan_map_for_oases
        print("\n🔍 Starting map scan...")
        scan_map_for_oases(api)
        print("✅ Map scan complete!")
    elif choice == "2":
        from core.database_helpers import load_latest_unoccupied_oases
        villages = load_villages_from_identity()
        if not villages:
            print("❌ No villages found in identity. Exiting.")
            return
        
        print("\nAvailable villages:")
        for idx, v in enumerate(villages):
            print(f"[{idx}] {v['village_name']} at ({v['x']}, {v['y']})")
        
        try:
            village_idx = int(input("\nSelect village to view oases for: ").strip())
            selected_village = villages[village_idx]
            oases = load_latest_unoccupied_oases(f"({selected_village['x']}_{selected_village['y']})")
            
            if not oases:
                print("❌ No oases found in latest scan.")
                return
            
            print(f"\n📊 Found {len(oases)} unoccupied oases near {selected_village['village_name']}:")
            for coord_key, oasis_data in oases.items():
                x_str, y_str = coord_key.split("_")
                print(f"- Oasis at ({x_str}, {y_str})")
        except (ValueError, IndexError):
            print("❌ Invalid village selection.")
    elif choice == "3":
        return
    else:
        print("❌ Invalid choice.")

def main():
    _setup_logging()
    _log_info("Launcher started.")
    # Fingerprint banner removed for cleaner console output
    print("\n" + "="*40)
    print("🎮 TRAVIAN AUTOMATION LAUNCHER")
    print("="*40)
    
    print("\n🌾 FARM LIST:")
    print("1) Farm burst")
    print("2) Configure farm lists")
    print("3) Run farm from config")
    
    print("\n🏰 OASIS RAID:")
    print("4) Setup raid plan")
    print("5) Reset raid plan")
    print("6) Test raid (single village)")
    
    print("\n🤖 AUTOMATION:")
    print("7) 👑 FULL AUTO MODE 👑")
    print("   • Farm lists + Oasis raids")
    print("   • Multi-village loop")
    
    print("\n🗺️ MAP SCANNING:")
    print("8) Scan & View Oases")
    
    print("\n👤 ACCOUNT:")
    print("9) Hero Operations")
    print("10) Identity & Villages")
    print("11) Test Hero Raiding Thread (Standalone)")
    print("12) Tools & Detectors")
    
    print("\n" + "="*40)

    choice = input("\n👉 Select an option: ").strip()

    # Preflight: show masked email and server index
    try:
        from config.config import settings as _cfg
        _email = (getattr(_cfg, "TRAVIAN_EMAIL", "") or "").strip()
        _server = getattr(_cfg, "SERVER_SELECTION", None)
        def _mask_email(e: str) -> str:
            if not e or "@" not in e:
                return "<unset>"
            name, dom = e.split("@", 1)
            if len(name) <= 2:
                masked = name[0] + "*"
            else:
                masked = name[:2] + "***"
            return masked + "@" + dom
        print(f"\n[Preflight] Email: {_mask_email(_email)} | Server index: {_server}")
    except Exception:
        pass

    # Login first (use YAML credentials; avoid interactive prompt)
    print("\n🔐 Logging into Travian...")
    try:
        from config.config import settings as _cfg
        email = (getattr(_cfg, "TRAVIAN_EMAIL", "") or "").strip()
        password = (getattr(_cfg, "TRAVIAN_PASSWORD", "") or "").strip()
        server_sel = getattr(_cfg, "SERVER_SELECTION", None)
        if not email or not password:
            raise RuntimeError("Missing credentials in config.yaml")
        session, server_url = login(email=email, password=password, server_selection=server_sel, interactive=False)
    except Exception as _e:
        print(f"❌ Login failed: {_e}")
        print("Hint: set TRAVIAN_EMAIL and TRAVIAN_PASSWORD in config.yaml")
        return
    api = TravianAPI(session, server_url)

    if choice == "1":
        run_one_farm_list_burst(api)
    elif choice == "2":
        from features.farm_lists.manage_farm_lists import update_farm_lists
        update_farm_lists(api, server_url)
    elif choice == "3":
        from features.farm_lists.farm_list_raider import run_farm_list_raids
        villages = load_villages_from_identity()
        if not villages:
            print("❌ No villages found in identity. Exiting.")
            return
        for village in villages:
            run_farm_list_raids(api, server_url, village["village_id"])
    elif choice == "4":
        setup_interactive_raid_plan(api, server_url)
    elif choice == "5":
        reset_saved_raid_plan()
    elif choice == "6":
        print("\n🎯 Starting single-village oasis raiding (testing mode)...")
        run_raid_planner(api, server_url, multi_village=False, run_farm_lists=False)
    elif choice == "7":
        print("\n🤖 Starting full automation mode...")
        _log_info("Starting Full Auto Mode.")
        
        # Read configurable toggle for first cycle farm-lists behavior
        skip_farm_lists_first_run = bool(getattr(settings, "SKIP_FARM_LISTS_FIRST_RUN", False))
        # Reuse existing logged-in API and server_url from earlier login

        # Start hero raiding thread (non-blocking, defensive)
        try:
            hero_thread = threading.Thread(
                target=run_hero_raiding_thread,
                args=(api,),
                name="HeroRaiderThread",
                daemon=True,
            )
            hero_thread.start()
            _log_info(f"Hero raiding thread started (daemon, alive={hero_thread.is_alive()}).")
            # Also start the adventure watcher
            adv_thread = threading.Thread(
                target=run_hero_adventure_thread,
                args=(api,),
                name="HeroAdventureThread",
                daemon=True,
            )
            adv_thread.start()
            _log_info(f"Hero adventure thread started (daemon, alive={adv_thread.is_alive()}).")
        except Exception as e:
            print(f"[Main] ⚠️ Could not start hero thread: {e}", flush=True)
            _log_warn(f"Could not start hero thread: {e}")
        
        # ReportChecker niet parallel starten: we verwerken pendings sequentieel per cycle
        # Start attack detector if enabled
        try:
            if bool(getattr(settings, "ATTACK_DETECTOR_ENABLE", False)):
                run_attack_detector_thread(settings)
                print("[Main] AttackDetector started (daemon).", flush=True)
                _log_info("AttackDetector started (daemon).")
        except Exception as e:
            print(f"[Main] ⚠️ Could not start AttackDetector: {e}", flush=True)
            _log_warn(f"Could not start AttackDetector: {e}")
        first_cycle = True
        print("[Main] ✅ Entering Full Auto cycle loop…", flush=True)

        # Daily limiter init
        limiter_enabled = bool(getattr(settings, "ENABLE_CYCLE_LIMITER", False))
        total_allowed, blocks, block_size, rest_between_blocks = _compute_limiter_params()
        runtime_state = _load_runtime_state()
        if runtime_state["date"] != _today_str():
            runtime_state = {
                "date": _today_str(),
                "minutes_used": 0,
                "total_allowed": int(total_allowed),
                "block_size": int(block_size),
            }
            _save_runtime_state(runtime_state)
        else:
            # reuse chosen values for today
            total_allowed = int(runtime_state.get("total_allowed", total_allowed))
            block_size = int(runtime_state.get("block_size", block_size))

        
        while True:
            try:
                _log_info("Loop tick")
                print(f"\n[Main] Starting cycle at {time.strftime('%H:%M:%S')}")
                _log_info("Cycle started.")
                cycle_start_ts = time.time()

                # --- Place your farming/oasis logic here (existing repo code) ---
                # Try to start a hero adventure if conditions allow (low-latency)
                try:
                    if bool(getattr(settings, 'HERO_ADVENTURE_ENABLE', True)):
                        started = maybe_start_adventure(api)
                        if started:
                            print("[Main] 🦸 Hero adventure started.")
                except Exception as _adv_e:
                    _log_warn(f"Hero adventure attempt failed: {_adv_e}")

                # Small human-like pause before starting planner
                try:
                    import random, time as _t
                    from config.config import settings as _cfg
                    _t.sleep(random.uniform(float(getattr(_cfg, 'OP_JITTER_MIN_SEC', 0.5)), float(getattr(_cfg, 'OP_JITTER_MAX_SEC', 2.0))))
                    # Occasional neutral map view before heavy actions
                    if random.random() < float(getattr(_cfg, 'MAPVIEW_PRE_ACTION_PROB', 0.35)):
                        try:
                            page = random.choice((1, 2))
                            _ = api.session.get(f"{api.server_url}/dorf{page}.php")
                            _t.sleep(random.uniform(0.4, 1.2))
                        except Exception:
                            pass
                except Exception:
                    pass
                print("[Main] Running raid planner...", flush=True)
                if first_cycle and skip_farm_lists_first_run:
                    # Skip farm lists only on the first run if requested
                    run_raid_planner(api, server_url, reuse_saved=True, multi_village=True, run_farm_lists=False)
                else:
                    run_raid_planner(api, server_url, reuse_saved=True, multi_village=True, run_farm_lists=True)
                first_cycle = False

                # Hero summary + record to metrics
                _log_info("Fetching hero status summary…")
                hero_manager = HeroManager(api)
                status = hero_manager.fetch_hero_status()
                if status:
                    print_hero_status_summary(status)
                    try:
                        from core.metrics import set_hero_status_summary
                        set_hero_status_summary({
                            "present": bool(status.is_present),
                            "health": status.health,
                            "level": getattr(status, "level", None),
                            "village": status.current_village_name,
                        })
                    except Exception:
                        pass
                    _log_info("Hero status summary fetched.")
                else:
                    print("❌ Could not fetch hero status summary.")
                    _log_warn("Could not fetch hero status summary.")

                # Report checker – sequential pass (avoid simultaneous activity)
                try:
                    from config.config import settings as _cfg
                    if bool(getattr(_cfg, 'PROCESS_REPORTS_IN_AUTOMATION', True)):
                        import random as _rnd, time as _t
                        _t.sleep(_rnd.uniform(0.3, 1.0))
                        processed = process_ready_pendings_once(api)
                        _log_info(f"ReportChecker pass processed {processed} pending(s) this cycle.")
                    else:
                        _log_info("ReportChecker pass skipped (disabled in config).")
                except Exception as _e:
                    _log_warn(f"ReportChecker pass failed: {_e}")

                # Cycle report (metrics snapshot)
                try:
                    from core.metrics import snapshot_and_reset
                    snap = snapshot_and_reset()
                    ctr = snap.get("counters", {})
                    skips = snap.get("skip_reasons", {})
                    changes = snap.get("learning_changes", [])
                    print("\n📈 Cycle Report")
                    print("- Raids sent:", ctr.get("raids_sent", 0))
                    print("- Raids skipped:", ctr.get("raids_skipped", 0))
                    if skips:
                        print("  Reasons:")
                        for k, v in sorted(skips.items(), key=lambda x: -x[1]):
                            print(f"    • {k}: {v}")
                    hs = snap.get("hero_status")
                    if hs:
                        print("- Hero:", ("present" if hs.get("present") else "away"), f"health={hs.get('health')}%", f"level={hs.get('level')}")
                    if changes:
                        print("- Learning changes:")
                        for ch in changes[-5:]:
                            loss = ch.get("loss_pct")
                            loss_txt = f", loss={int(loss*100)}%" if isinstance(loss, float) else ""
                            print(f"    • {ch['oasis']}: {ch['old']:.2f} → {ch['new']:.2f} ({ch['dir']}{loss_txt})")
                    print()
                except Exception:
                    pass

                # === Limiter accounting ===
                elapsed_minutes = max(1, int((time.time() - cycle_start_ts)//60))
                if limiter_enabled:
                    if runtime_state["date"] != _today_str():
                        runtime_state = {"date": _today_str(), "minutes_used": 0, "total_allowed": int(total_allowed), "block_size": int(block_size)}
                    before = runtime_state["minutes_used"]
                    runtime_state["minutes_used"] = before + elapsed_minutes
                    _save_runtime_state(runtime_state)

                    prev_block = before // block_size
                    new_block = runtime_state["minutes_used"] // block_size
                    # pick random rest between blocks if configured
                    if new_block > prev_block and new_block < blocks:
                        rest_min, rest_max = 0, 0
                        try:
                            rest_min, rest_max = rest_between_blocks
                        except Exception:
                            pass
                        rest_len = 0
                        if rest_max >= rest_min and rest_max > 0:
                            import random as _rnd
                            rest_len = int(_rnd.randint(int(rest_min), int(rest_max)))
                        elif isinstance(rest_between_blocks, int) and rest_between_blocks > 0:
                            rest_len = int(rest_between_blocks)
                        if rest_len > 0:
                            print(f"[Limiter] ✅ Block {prev_block+1} completed ({runtime_state['minutes_used']} / {total_allowed} min).")
                            _log_info(f"Block {prev_block+1} completed.")
                            _sleep_minutes(rest_len)

                    if runtime_state["minutes_used"] >= total_allowed:
                        mins_to_tomorrow = _minutes_until_tomorrow()
                        print(f"[Limiter] 🛑 Daily cap reached ({runtime_state['minutes_used']} / {total_allowed} min).")
                        _log_warn("Daily cap reached.")
                        if mins_to_tomorrow > 0:
                            print(f"[Limiter] 🌙 Sleeping until tomorrow (+{mins_to_tomorrow} minutes).")
                            _log_info(f"Sleeping until tomorrow: {mins_to_tomorrow} minutes.")
                            _sleep_minutes(mins_to_tomorrow)
                            runtime_state = {"date": _today_str(), "minutes_used": 0}
                            _save_runtime_state(runtime_state)

                # === Cycle wait ===
                jitter = random.randint(-settings.JITTER_MINUTES, settings.JITTER_MINUTES)
                total_wait_minutes = settings.WAIT_BETWEEN_CYCLES_MINUTES + jitter
                # Occasional coffee break for human-like idle time
                try:
                    import random as _rnd
                    if _rnd.random() < float(getattr(settings, 'OP_COFFEE_BREAK_PROB', 0.1)):
                        extra = _rnd.randint(int(getattr(settings, 'OP_COFFEE_BREAK_MIN_MINUTES', 2)), int(getattr(settings, 'OP_COFFEE_BREAK_MAX_MINUTES', 6)))
                        total_wait_minutes += extra
                        print(f"[Humanizer] ☕ Taking a short break (+{extra} min)")
                except Exception:
                    pass
                print(f"[Main] Cycle complete. Waiting {total_wait_minutes} minutes...", flush=True)
                _log_info(f"Cycle complete. Waiting {total_wait_minutes} minutes.")
                time.sleep(max(0, total_wait_minutes) * 60)

            except Exception as e:
                print(f"[Main] ⚠️ Error during cycle: {e}")
                _log_error("Error during cycle", e)
                print("[Main] 🔁 Attempting re-login and retry...")
                _log_warn("Re-login after error.")
                session, server_url = login()
                api = TravianAPI(session, server_url)
                print("[Main] ✅ Re-login successful.")
                continue
    elif choice == "8":
        run_map_scan(api)
    elif choice == "9":
        run_hero_operations(api)
    elif choice == "10":
        handle_identity_management()
    elif choice == "11":
        print("\n🦸 Testing Hero Raiding Thread (Standalone)...")
        run_hero_raiding_thread(api)
    elif choice == "12":
        tools_menu(api)
    else:
        print("❌ Invalid choice.")

if __name__ == "__main__":
    main()
