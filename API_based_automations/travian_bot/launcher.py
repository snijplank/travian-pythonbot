import time
import random
import json
import os
import threading
import logging
import sys
import shutil
from identity_handling.login import login
from core.travian_api import TravianAPI
from core.database_helpers import load_latest_unoccupied_oases
from oasis_raiding_from_scan_list_main import run_raid_planner
from raid_list_main import run_one_farm_list_burst
from features.raiding.empty_oasis_raider import run_empty_oasis_raids
from features.farm_lists.farm_list_runner import run_farmlists_for_villages
from features.raiding.reset_raid_plan import reset_saved_raid_plan
from features.raiding.setup_interactive_plan import setup_interactive_raid_plan
from identity_handling.identity_manager import handle_identity_management
from identity_handling.identity_helper import load_villages_from_identity, get_account_tribe_id
from features.hero.hero_operations import run_hero_operations as run_hero_ops, print_hero_status_summary
from features.hero.hero_raiding_thread import run_hero_raiding_thread
from features.hero.hero_adventure_thread import run_hero_adventure_thread
from features.build.new_village_preset import run_new_village_preset_if_new
from features.hero.hero_adventure import maybe_start_adventure
from core.hero_manager import HeroManager
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from logging.handlers import RotatingFileHandler
from core.rally_tracker import get_pending_count, process_pending_returns
from features.defense.attack_detector import run_attack_detector_thread
from features.tasks.progressive_tasks import collect_rewards_for_all_villages, count_collectible_rewards

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

    # Avoid double-printing via root logger handlers
    LOGGER.propagate = False

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


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def display_status_snapshot() -> None:
    base_dir = Path(__file__).resolve().parent
    learning_path = base_dir / "database/learning/raid_targets_stats.json"
    pendings_path = base_dir / "database/learning/pending_rally.json"
    runtime_path = base_dir / "database/runtime_next_oasis_due.json"
    hero_eta_path = base_dir / "database/hero_mission_eta.json"

    learning_store = _load_json(learning_path, {}) or {}
    pendings = _load_json(pendings_path, []) or []
    runtime_hint = _load_json(runtime_path, {}) or {}
    hero_eta = _load_json(hero_eta_path, {}) or {}

    print("\n===== STATUS SNAPSHOT =====")

    total_targets = len(learning_store)
    now = time.time()
    paused = []
    priority = []
    for key, entry in learning_store.items():
        try:
            pause_until = float(entry.get("pause_until", 0) or 0)
            if pause_until > now:
                paused.append((key, pause_until))
        except Exception:
            pass
        try:
            priority_until = float(entry.get("priority_until", 0) or 0)
            if priority_until > now:
                priority.append((key, priority_until))
        except Exception:
            pass

    print(f"Learning targets: {total_targets}")
    if paused:
        paused.sort(key=lambda x: x[1])
        print(f"  Paused ({len(paused)}):")
        for key, ts in paused[:5]:
            mins = max(0, int((ts - now) / 60))
            print(f"    • {key} (resume in ~{mins} min)")
        if len(paused) > 5:
            print("    …")
    else:
        print("  Paused: none")

    if priority:
        priority.sort(key=lambda x: x[1])
        print(f"  Priority ({len(priority)}):")
        for key, ts in priority[:5]:
            secs = max(0, int(ts - now))
            print(f"    • {key} (expires in ~{secs//60}m{secs%60:02d}s)")
        if len(priority) > 5:
            print("    …")
    else:
        print("  Priority: none")

    print(f"Pending rally entries: {len(pendings)}")
    for entry in pendings[:5]:
        target = entry.get("target", "?")
        depart = entry.get("depart_epoch")
        if isinstance(depart, (int, float)):
            age = max(0, int(now - depart))
            age_txt = f"{age//60}m{age%60:02d}s ago"
        else:
            age_txt = "unknown"
        print(f"  • {target} (sent {age_txt})")
    if len(pendings) > 5:
        print("    …")

    next_due_epoch = runtime_hint.get("next_due_epoch")
    if isinstance(next_due_epoch, (int, float)) and next_due_epoch > now:
        remaining = max(0, int(next_due_epoch - now))
        due_time = datetime.fromtimestamp(next_due_epoch).strftime("%H:%M:%S")
        print(f"Next oasis due in {remaining//60}m{remaining%60:02d}s (~{due_time})")
    else:
        print("Next oasis due: n/a")

    return_epoch = hero_eta.get("return_epoch")
    if isinstance(return_epoch, (int, float)) and return_epoch > now:
        remain = max(0, int(return_epoch - now))
        eta_time = datetime.fromtimestamp(return_epoch).strftime("%H:%M:%S")
        print(f"Hero return ETA: {remain//60}m{remain%60:02d}s (~{eta_time})")
    else:
        print("Hero return ETA: n/a")

    print("===========================\n")


def _load_identity_servers() -> dict[str, list[dict]]:
    path = Path(__file__).resolve().parent / "database/identity.json"
    data = _load_json(path, {}) or {}
    servers = data.get("travian_identity", {}).get("servers", [])
    mapping: dict[str, list[dict]] = {}
    for server in servers:
        url = str(server.get("server_url") or server.get("server_name") or "").strip()
        if url:
            mapping[url.rstrip('/')] = server.get("villages", []) or []
    return mapping


def _parse_coord_key(key: str) -> tuple[int, int] | None:
    try:
        key = key.strip()
        if key.startswith('(') and key.endswith(')'):
            key = key[1:-1]
        x_str, y_str = [part.strip() for part in key.split(',')]
        return int(x_str), int(y_str)
    except Exception:
        return None


def display_raid_plan_stats() -> None:
    base_dir = Path(__file__).resolve().parent
    raid_plan_dir = base_dir / "database/raid_plans"
    learning_path = base_dir / "database/learning/raid_targets_stats.json"
    learning_store = _load_json(learning_path, {}) or {}
    servers = _load_identity_servers()

    plan_files = sorted(raid_plan_dir.glob("raid_plan_village_*.json"))
    if not plan_files:
        print("\n❌ No raid plan files found in database/raid_plans.")
        return

    if not learning_store:
        print("\n⚠️ Learning store empty (no stats yet). Showing plan metadata only.")

    print("\n===== RAID PLAN STATS =====")
    now = time.time()

    for path in plan_files:
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"\n{path.name}: failed to load ({exc})")
            continue

        server_url = str(plan.get("server") or "").rstrip('/')
        village_index = int(plan.get("village_index", 0))
        max_dist = int(plan.get("max_raid_distance", 60))
        villages = servers.get(server_url, [])
        village_info = villages[village_index] if 0 <= village_index < len(villages) else None
        village_name = village_info.get("village_name") if village_info else f"IDX {village_index}"
        vx = int(village_info.get("x")) if village_info else 0
        vy = int(village_info.get("y")) if village_info else 0

        print(f"\n📌 {path.name} — {village_name} ({vx}|{vy}) [max {max_dist} tiles]")

        summary = {
            "targets": 0,
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "loot": {"wood": 0, "clay": 0, "iron": 0, "crop": 0, "total": 0},
            "avg_loss_pct": [],
        }
        details = []

        for coord_key, entry in learning_store.items():
            coord = _parse_coord_key(coord_key)
            if coord is None:
                continue
            dx = abs(coord[0] - vx)
            dy = abs(coord[1] - vy)
            distance = (dx ** 2 + dy ** 2) ** 0.5
            if distance > max_dist:
                continue

            attempts = int(entry.get("attempts", 0) or 0)
            successes = int(entry.get("successes", 0) or 0)
            failures = int(entry.get("failures", attempts - successes))
            loot = entry.get("total_loot") or {}
            loss = entry.get("avg_loss_pct")

            summary["targets"] += 1
            summary["attempts"] += attempts
            summary["successes"] += successes
            summary["failures"] += failures
            if isinstance(loss, (int, float)):
                summary["avg_loss_pct"].append(float(loss))
            for resource in ("wood", "clay", "iron", "crop", "total"):
                summary["loot"][resource] += int(loot.get(resource, 0) or 0)

            last = entry.get("last") or {}
            last_ts = last.get("ts") or entry.get("last_ts")
            pause_until = entry.get("pause_until")
            priority_until = entry.get("priority_until")

            detail = {
                "coord": coord_key,
                "attempts": attempts,
                "successes": successes,
                "failures": failures,
                "avg_loss": loss,
                "loot_total": int(loot.get("total", 0) or 0),
                "last_ts": last_ts,
                "pause": pause_until,
                "priority": priority_until,
            }
            details.append(detail)

        if summary["targets"] == 0:
            print("  • No learning data yet for this plan.")
            continue

        ratio = (summary["successes"] / summary["attempts"]) if summary["attempts"] else 0.0
        avg_loss = sum(summary["avg_loss_pct"])/len(summary["avg_loss_pct"]) if summary["avg_loss_pct"] else 0.0

        print(f"  Targets with data: {summary['targets']}")
        print(f"  Raids: {summary['attempts']} total | {summary['successes']} successes | {summary['failures']} failures")
        print(f"  Success ratio: {ratio*100:.1f}% | Avg loss: {avg_loss*100:.1f}%")
        print(f"  Loot total: {summary['loot']['total']} (wood={summary['loot']['wood']}, clay={summary['loot']['clay']}, iron={summary['loot']['iron']}, crop={summary['loot']['crop']})")

        details.sort(key=lambda x: x['attempts'], reverse=True)
        print("  Top targets:")
        for item in details[:5]:
            pause_txt = priority_txt = ""
            if item['pause'] and item['pause'] > now:
                mins = max(0, int((item['pause'] - now)/60))
                pause_txt = f", pause {mins}m"
            if item['priority'] and item['priority'] > now:
                secs = max(0, int(item['priority'] - now))
                priority_txt = f", priority {secs}s"
            print(f"    • {item['coord']} → {item['attempts']} attempts, loot {item['loot_total']}, loss={item['avg_loss'] or 0:.2f}{pause_txt}{priority_txt}")
        if len(details) > 5:
            print("    …")

    print("\n===========================\n")
def _backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak_{ts}")
    try:
        shutil.copy2(path, backup)
        print(f"[Reset] Backup created: {backup}")
        return backup
    except Exception as exc:
        print(f"[Reset] ⚠️ Failed to backup {path.name}: {exc}")
        return None


def _write_json(path: Path, payload) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[Reset] ⚠️ Failed to write {path}: {exc}")


def reset_environment() -> None:
    base_dir = Path(__file__).resolve().parent
    templates_dir = base_dir / "templates"
    config_template = templates_dir / "config.template.yaml"
    identity_template = templates_dir / "identity.template.json"
    config_path = base_dir / "config.yaml"
    identity_path = base_dir / "database/identity.json"
    learning_dir = base_dir / "database/learning"
    runtime_files = [
        base_dir / "database/runtime_next_oasis_due.json",
        base_dir / "database/hero_mission_eta.json",
    ]

    print("\n⚠️ Reset / Clean Install")
    print("This will backup the current config/identity and clear learning data.")
    print("You will need to edit config.yaml and rerun setup_identity afterwards.")
    confirm = input("Type 'reset' to continue: ").strip().lower()
    if confirm != "reset":
        print("[Reset] Aborted.")
        return

    _backup_file(config_path)
    _backup_file(identity_path)

    try:
        if config_template.exists():
            shutil.copy2(config_template, config_path)
            print(f"[Reset] Config reset using template: {config_template.name}")
        else:
            print("[Reset] ⚠️ Config template missing; keeping existing config.yaml")
    except Exception as exc:
        print(f"[Reset] ⚠️ Failed to reset config.yaml: {exc}")

    try:
        if identity_template.exists():
            shutil.copy2(identity_template, identity_path)
            print(f"[Reset] Identity reset using template: {identity_template.name}")
        else:
            _write_json(identity_path, {"travian_identity": {"created_at": "", "tribe_id": None, "faction": "", "servers": []}})
            print("[Reset] Identity template missing; wrote empty identity.json")
    except Exception as exc:
        print(f"[Reset] ⚠️ Failed to reset identity.json: {exc}")

    try:
        learning_dir.mkdir(parents=True, exist_ok=True)
        _write_json(learning_dir / "raid_targets_stats.json", {})
        _write_json(learning_dir / "pending_rally.json", [])
        print("[Reset] Cleared learning store")
    except Exception as exc:
        print(f"[Reset] ⚠️ Failed to reset learning data: {exc}")

    for rf in runtime_files:
        if rf.name.endswith("hero_mission_eta.json"):
            payload = {"return_epoch": None}
        else:
            payload = {}
        _write_json(rf, payload)

    logs_dir = base_dir / "logs"
    try:
        for log_file in logs_dir.glob("*.log*"):
            log_file.unlink(missing_ok=True)
        print("[Reset] Cleared log files")
    except Exception:
        pass

    print(
        "\n[Reset] Done. Next steps:\n"
        "- Update config.yaml with credentials & server selection.\n"
        "- Run setup_identity.py to populate identity.json.\n"
        "- Restart the bot.\n"
    )

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
        try:
            tribe_id = get_account_tribe_id()
        except Exception:
            tribe_id = "unknown"
        
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
        try:
            from config.config import settings as _cfg
        except Exception:
            class _Tmp:
                LEARNING_ENABLE = True
            _cfg = _Tmp()
        learning_enabled = bool(getattr(_cfg, 'LEARNING_ENABLE', True))

        opt4 = "[4] Process reports now (one pass)"
        opt7 = "[7] Show learned oasis multipliers"
        if not learning_enabled:
            opt4 += " (disabled)"
            opt7 += " (disabled)"

        print(f"""
🛠️ Tools & Detectors
[1] Toggle Attack Detector (enable/disable)
[2] Set Discord Webhook URL
[3] Test Discord Notification (with screenshot)
{opt4}
[5] Collect task rewards now (one pass)
[6] Show unread reports (IDs + coords)
{opt7}
[8] Back to main menu
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
                from config.config import settings as _cfg
                if not bool(getattr(_cfg, 'LEARNING_ENABLE', True)):
                    print("ℹ️ Learning is disabled; report processing is not active.")
                else:
                    # Verbose one-pass processing with progress output
                    n = process_ready_pendings_once(api, verbose=True)
                    print(f"✅ ReportChecker processed {n} pending(s).")
            except Exception as e:
                print(f"❌ Failed to process reports: {e}")
        elif sel == "5":
            try:
                cnt = collect_rewards_for_all_villages(api, verbose=True)
                print(f"✅ Collected {cnt} progressive reward(s).")
            except Exception as e:
                print(f"❌ Failed to collect rewards: {e}")
        elif sel == "6":
            try:
                items = api.list_unread_reports(max_items=50)
                if not items:
                    print("No unread reports found.")
                else:
                    print(f"Unread reports ({len(items)}):")
                    for it in items:
                        cid = it.get('id') or '?'
                        crd = it.get('coords')
                        when = it.get('time') or ''
                        crd_txt = f"{crd[0]}|{crd[1]}" if isinstance(crd, tuple) else "?"
                        print(f"- id={cid} at ({crd_txt}) {when}")
            except Exception as e:
                print(f"❌ Failed to list unread reports: {e}")
        elif sel == "7":
            try:
                from config.config import settings as _cfg
                if not bool(getattr(_cfg, 'LEARNING_ENABLE', True)):
                    print("ℹ️ Learning is disabled; no multipliers to show.")
                else:
                    from core.learning_store import LearningStore  # type: ignore
                    ls = LearningStore()
                    data = getattr(ls, 'data', {}) or {}
                    if not data:
                        print("No learned multipliers yet.")
                    else:
                        print("Learned multipliers (oasis → mul, last):")
                        # Show up to 20 most recent by 'last.ts' if available
                        items = []
                        for k, v in data.items():
                            ts = ((v or {}).get('last') or {}).get('ts') or ''
                            items.append((ts, k, v))
                        items.sort(reverse=True)
                        for ts, k, v in items[:20]:
                            m = v.get('multiplier')
                            last = v.get('last')
                            lp = last.get('loss_pct') if isinstance(last, dict) else None
                            lp_txt = f", loss={lp:.0%}" if isinstance(lp, (int, float)) else ''
                            print(f"- {k}: {m:.2f}{lp_txt} (last={ts})")
            except Exception as e:
                print(f"❌ Failed to show multipliers: {e}")
        elif sel == "8":
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
    print("[1] Full scan for unoccupied oases")
    print("[2] Quick scan (small radius)")
    print("[3] View latest scan results")
    print("[4] Back to main menu")
    
    choice = input("\nSelect an option: ").strip()
    
    if choice == "1":
        from features.map_scanning.scan_map import scan_map_for_oases
        print("\n🔍 Starting map scan...")
        scan_map_for_oases(api)
        print("✅ Map scan complete!")
    elif choice == "2":
        from features.map_scanning.scan_map import quick_scan_for_oases
        quick_scan_for_oases(api)
        print("✅ Quick scan complete!")
    elif choice == "3":
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
    elif choice == "4":
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
    print("13) Reset / Clean Install")
    print("14) Status Snapshot")

    print("\n" + "="*40)

    choice = input("\n👉 Select an option: ").strip()

    if choice == "13":
        reset_environment()
        return
    if choice == "14":
        display_status_snapshot()
        return

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
        try:
            # Show key feature toggles at startup for clarity
            feat_rally = bool(getattr(settings, 'PROCESS_RALLY_RETURNS', True))
            feat_adv = bool(getattr(settings, 'HERO_ADVENTURE_ENABLE', True))
            feat_tasks = bool(getattr(settings, 'PROGRESSIVE_TASKS_ENABLE', True))
            # New independent raiders (robust toggle reader supports nested 'raiding' section)
            def _cfg_bool(name: str, legacy: str | None = None, default: bool = True) -> bool:
                v = getattr(settings, name, None)
                if isinstance(v, bool):
                    return v
                try:
                    ra = getattr(settings, 'raiding', None)
                    # Support both dict and object-like containers
                    if ra is not None:
                        if isinstance(ra, dict):
                            rv = ra.get(name)
                        else:
                            rv = getattr(ra, name, None)
                        if isinstance(rv, bool):
                            return rv
                except Exception:
                    pass
                # Fallback: read directly from YAML for nested keys
                try:
                    from pathlib import Path as _P
                    import yaml as _yaml
                    cfg_path = _P(__file__).resolve().parent / 'config.yaml'
                    if cfg_path.exists():
                        data = _yaml.safe_load(cfg_path.read_text(encoding='utf-8')) or {}
                        ra = (data.get('raiding') or {})
                        yv = ra.get(name)
                        if isinstance(yv, bool):
                            return yv
                except Exception:
                    pass
                if legacy is not None:
                    lv = getattr(settings, legacy, None)
                    if isinstance(lv, bool):
                        return lv
                return bool(default)

            fl_enabled = _cfg_bool('FARM_LIST_RAIDER_ENABLE', legacy='FARM_LISTS_ENABLE', default=True)
            empty_oasis_enabled = _cfg_bool('EMPTY_OASIS_RAIDER_ENABLE', legacy='ENABLE_EMPTY_OASIS_RAIDER', default=True)
            hero_clear_enabled = _cfg_bool('HERO_OASIS_CLEAR_ENABLE', legacy='ENABLE_HERO_OASIS_CLEAR', default=True)
            print("\n⚙️  Feature Toggles:")
            print(f"- Rally tracker:     {'ENABLED' if feat_rally else 'DISABLED'}")
            print(f"- Progressive tasks: {'ENABLED' if feat_tasks else 'DISABLED'}")
            print(f"- Hero adventures:   {'ENABLED' if feat_adv else 'DISABLED'}")
            print(f"- FarmListRaider:    {'ENABLED' if fl_enabled else 'DISABLED'}")
            print(f"- EmptyOasisRaider:  {'ENABLED' if empty_oasis_enabled else 'DISABLED'}")
            print(f"- HeroOasisClear:    {'ENABLED' if hero_clear_enabled else 'DISABLED'}\n")
        except Exception:
            pass
        
        # Read configurable toggle for first cycle farm-lists behavior
        skip_farm_lists_first_run = bool(getattr(settings, "SKIP_FARM_LISTS_FIRST_RUN", False))
        # Reuse existing logged-in API and server_url from earlier login

        # Initialize activity tracking session
        try:
            from core.metrics import activity_init
            activity_init(time.time())
        except Exception:
            pass

        # Start hero raiding thread (non-blocking, defensive) only if enabled
        try:
            # Use the same robust reader for starting threads
            def _cfg_bool(name: str, legacy: str | None = None, default: bool = True) -> bool:
                v = getattr(settings, name, None)
                if isinstance(v, bool):
                    return v
                try:
                    ra = getattr(settings, 'raiding', None)
                    if ra is not None:
                        if isinstance(ra, dict):
                            rv = ra.get(name)
                        else:
                            rv = getattr(ra, name, None)
                        if isinstance(rv, bool):
                            return rv
                except Exception:
                    pass
                try:
                    from pathlib import Path as _P
                    import yaml as _yaml
                    cfg_path = _P(__file__).resolve().parent / 'config.yaml'
                    if cfg_path.exists():
                        data = _yaml.safe_load(cfg_path.read_text(encoding='utf-8')) or {}
                        ra = (data.get('raiding') or {})
                        yv = ra.get(name)
                        if isinstance(yv, bool):
                            return yv
                except Exception:
                    pass
                if legacy is not None:
                    lv = getattr(settings, legacy, None)
                    if isinstance(lv, bool):
                        return lv
                return bool(default)

            hero_clear_enabled = _cfg_bool('HERO_OASIS_CLEAR_ENABLE', legacy='ENABLE_HERO_OASIS_CLEAR', default=True)
            if hero_clear_enabled:
                import requests as _requests
                hero_session = _requests.Session()
                try:
                    hero_session.headers.update(getattr(api.session, 'headers', {}) or {})
                    if hasattr(api.session, 'cookies'):
                        hero_session.cookies.update(api.session.cookies.get_dict())
                except Exception:
                    pass
                hero_api = TravianAPI(hero_session, api.server_url)

                hero_thread = threading.Thread(
                    target=run_hero_raiding_thread,
                    args=(hero_api,),
                    name="HeroOasisClearThread",
                    daemon=True,
                )
                hero_thread.start()
                _log_info(f"HeroOasisClear thread started (daemon, alive={hero_thread.is_alive()}).")
            # Adventure watcher independent toggle
            adv_enabled = bool(getattr(settings, 'HERO_ADVENTURE_ENABLE', True))
            if adv_enabled:
                adv_thread = threading.Thread(
                    target=run_hero_adventure_thread,
                    args=(api,),
                    name="HeroAdventureThread",
                    daemon=True,
                )
                adv_thread.start()
                _log_info(f"Hero adventure thread started (daemon, alive={adv_thread.is_alive()}).")
        except Exception as e:
            print(f"[Main] ⚠️ Could not start hero/adventure thread: {e}", flush=True)
            _log_warn(f"Could not start hero/adventure thread: {e}")
        
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
                quiet_windows = _parse_quiet_windows()
                quiet_remaining = _in_quiet_window(datetime.now(), quiet_windows) if quiet_windows else None
                if quiet_remaining:
                    jitter_min = float(getattr(settings, "QUIET_WINDOW_RESUME_JITTER_MIN", 60.0))
                    jitter_max = float(getattr(settings, "QUIET_WINDOW_RESUME_JITTER_MAX", 240.0))
                    if jitter_max < jitter_min:
                        jitter_max = jitter_min
                    try:
                        extra = random.uniform(jitter_min, jitter_max) if jitter_max > 0 else 0.0
                    except Exception:
                        extra = 0.0
                    wait_seconds = max(0.0, quiet_remaining.total_seconds() + extra)
                    resume_at = datetime.now() + quiet_remaining + timedelta(seconds=extra)
                    mins = int(wait_seconds // 60)
                    secs = int(wait_seconds % 60)
                    msg = (
                        f"[Main] 💤 Quiet window active. Sleeping {mins}m {secs}s "
                        f"(resume around {resume_at.strftime('%H:%M')})."
                    )
                    print(msg)
                    _log_info(msg)
                    time.sleep(wait_seconds)
                    continue

                _log_info("Loop tick")
                print(f"\n[Main] Starting cycle at {time.strftime('%H:%M:%S')}")
                _log_info("Cycle started.")
                cycle_start_ts = time.time()

                # Cycle status: unread reports and task rewards available
                try:
                    unread = 0
                    try:
                        unread = int(api.get_unread_report_count())
                    except Exception:
                        unread = 0

                    parts = [f"📬 Unread reports: {unread}"]

                    # Progressive task rewards available count (+ top-bar indicator fallback)
                    if bool(getattr(settings, 'PROGRESSIVE_TASKS_ENABLE', True)):
                        try:
                            avail = int(count_collectible_rewards(api))
                        except Exception:
                            avail = 0
                        # Be conservative: do not claim 1+ on bubble; show indicator status alongside 0
                        try:
                            bubble = bool(api.has_task_reward_indicator())
                        except Exception:
                            bubble = False
                        if avail > 0:
                            parts.append(f"🎁 Task rewards: {avail}")
                        else:
                            parts.append("🎁 Task rewards: 0" + (" (indicator)" if bubble else ""))

                    # Open hero adventures count
                    if bool(getattr(settings, 'HERO_ADVENTURE_ENABLE', True)):
                        try:
                            advs = api.list_hero_adventures() or []
                            parts.append(f"🗺️ Adventures: {len(advs)}")
                        except Exception:
                            pass

                    print("[Main] " + " | ".join(parts))
                except Exception:
                    pass

                # --- Place your farming/oasis logic here (existing repo code) ---
                # Optional: auto-run new village preset if explicitly toggled in YAML
                try:
                    if bool(getattr(settings, 'NEW_VILLAGE_PRESET_ENABLE', False)):
                        # Run preset only upon detection of a newly founded village (once per id)
                        run_new_village_preset_if_new(api)
                except Exception as _p_e:
                    _log_warn(f"New village preset run failed: {_p_e}")
                # Try to start a hero adventure if conditions allow (low-latency)
                try:
                    if bool(getattr(settings, 'HERO_ADVENTURE_ENABLE', True)):
                        started = maybe_start_adventure(api)
                        if started:
                            print("[Main] 🦸 Hero adventure started.")
                except Exception as _adv_e:
                    _log_warn(f"Hero adventure attempt failed: {_adv_e}")

                # Collect progressive task rewards (per village)
                try:
                    if bool(getattr(settings, 'PROGRESSIVE_TASKS_ENABLE', True)):
                        import random as _rnd, time as _t
                        # Small jitter before interacting with tasks page
                        _t.sleep(_rnd.uniform(0.2, 0.9))
                        got = collect_rewards_for_all_villages(api, verbose=False)
                        if got:
                            print(f"[Main] 🎁 Collected {got} task reward(s).")
                except Exception as _tasks_e:
                    _log_warn(f"Progressive tasks collection failed: {_tasks_e}")

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
                # Independent raiders
                def _cfg_bool(name: str, legacy: str | None = None, default: bool = True) -> bool:
                    v = getattr(settings, name, None)
                    if isinstance(v, bool):
                        return v
                    try:
                        ra = getattr(settings, 'raiding', None)
                        if ra is not None:
                            if isinstance(ra, dict):
                                rv = ra.get(name)
                            else:
                                rv = getattr(ra, name, None)
                            if isinstance(rv, bool):
                                return rv
                    except Exception:
                        pass
                    try:
                        from pathlib import Path as _P
                        import yaml as _yaml
                        cfg_path = _P(__file__).resolve().parent / 'config.yaml'
                        if cfg_path.exists():
                            data = _yaml.safe_load(cfg_path.read_text(encoding='utf-8')) or {}
                            ra = (data.get('raiding') or {})
                            yv = ra.get(name)
                            if isinstance(yv, bool):
                                return yv
                    except Exception:
                        pass
                    if legacy is not None:
                        lv = getattr(settings, legacy, None)
                        if isinstance(lv, bool):
                            return lv
                    return bool(default)

                fl_enabled = _cfg_bool('FARM_LIST_RAIDER_ENABLE', legacy='FARM_LISTS_ENABLE', default=True)
                empty_oasis_enabled = _cfg_bool('EMPTY_OASIS_RAIDER_ENABLE', legacy='ENABLE_EMPTY_OASIS_RAIDER', default=True)
                learning_enabled = bool(getattr(settings, 'LEARNING_ENABLE', True))

                if empty_oasis_enabled and learning_enabled:
                    run_empty_oasis_raids(api, server_url, multi_village=True, priority_only=True)
                if fl_enabled:
                    # first-cycle skip honored if configured
                    if not (first_cycle and skip_farm_lists_first_run):
                        run_farmlists_for_villages(api, server_url, multi_village=True)
                if empty_oasis_enabled:
                    run_empty_oasis_raids(api, server_url, multi_village=True)
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

                # Rally tracker – sequential pass (avoid simultaneous activity)
                try:
                    from config.config import settings as _cfg
                    learning_on = bool(getattr(_cfg, 'LEARNING_ENABLE', True))
                    tracking_on = bool(getattr(_cfg, 'PROCESS_RALLY_RETURNS', True))
                    status_txt = None
                    if learning_on and tracking_on:
                        pendings_cnt = 0
                        try:
                            pendings_cnt = get_pending_count()
                        except Exception:
                            pendings_cnt = 0
                        import random as _rnd, time as _t
                        _t.sleep(_rnd.uniform(0.3, 1.0))
                        if pendings_cnt <= 0:
                            _log_info("RallyTracker pass skipped (no pending raids).")
                            status_txt = "no pendings"
                        else:
                            processed = process_pending_returns(api, verbose=True)
                            _log_info(f"RallyTracker processed {processed} pending raid(s) this cycle.")
                            status_txt = f"processed {processed}" if processed else "processed 0"
                    else:
                        status_txt = "disabled"
                        _log_info("RallyTracker pass skipped (disabled in config).")
                    if status_txt:
                        print(f"[Main] 📨 Rally tracker: {status_txt}")
                except Exception as _e:
                    _log_warn(f"RallyTracker pass failed: {_e}")

                # Cycle report (metrics snapshot)
                try:
                    from core.metrics import snapshot_and_reset, activity_record_window, render_activity_lines
                    # Record this cycle's active window (exclude waiting time)
                    try:
                        activity_record_window(cycle_start_ts, time.time())
                    except Exception:
                        pass
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
                    # Activity lines
                    try:
                        for line in render_activity_lines():
                            print(line)
                    except Exception:
                        pass
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

                # Event-driven wait: if next oasis due is earlier than base cycle wait, prefer waking up earlier
                base_wait_sec = max(0, int(total_wait_minutes) * 60)
                event_wait_sec = None
                try:
                    from pathlib import Path as _P
                    import json as _json
                    p = _P("database/runtime_next_oasis_due.json")
                    if p.exists():
                        j = _json.loads(p.read_text(encoding='utf-8')) or {}
                        next_epoch = int(j.get('next_due_epoch', 0))
                        if next_epoch > 0:
                            event_wait_sec = max(0, next_epoch - int(time.time()))
                except Exception:
                    event_wait_sec = None

                use_event = bool(getattr(settings, 'OASIS_EVENT_DRIVEN_WAIT_ENABLE', True))
                wait_total = base_wait_sec
                if use_event and isinstance(event_wait_sec, int) and event_wait_sec > 0:
                    # Wake up earlier if the next oasis becomes due sooner than the base cycle
                    wait_total = min(base_wait_sec, max(15, event_wait_sec))

                # Announce wait with optional event-driven hint
                # Use global console lock to avoid interleaving with other threads' prints
                try:
                    from core.console import print_line as _print_line
                except Exception:
                    _print_line = None
                if use_event and isinstance(event_wait_sec, int) and event_wait_sec > 0:
                    mm, ss = divmod(max(0, event_wait_sec), 60)
                    msg = f"[Main] Cycle complete. Waiting {max(0, wait_total//60)} minute(s)... (event-driven: next oasis in {mm:02d}:{ss:02d})"
                else:
                    msg = f"[Main] Cycle complete. Waiting {max(0, wait_total//60)} minute(s)..."
                if _print_line:
                    _print_line(msg)
                else:
                    print(msg, flush=True)
                _log_info(f"Cycle complete. Waiting {max(0, wait_total//60)} minute(s).")

                # Progress bar countdown for next cycle (and show next oasis ETA when available)
                start_ts = time.time()
                def _read_next_oasis_eta() -> int | None:
                    try:
                        import json as _json
                        from pathlib import Path as _P
                        p = _P("database/runtime_next_oasis_due.json")
                        if not p.exists():
                            return None
                        j = _json.loads(p.read_text(encoding='utf-8'))
                        eta = int(j.get("next_due_epoch", 0) - time.time())
                        return eta if eta > 0 else None
                    except Exception:
                        return None

                def _render_bar(prefix: str, remain: int, total: int) -> str:
                    total = max(total, 1)
                    remain = max(0, remain)
                    done = total - remain
                    width = 24
                    filled = int(width * done / total)
                    bar = "#" * filled + "." * (width - filled)
                    mm, ss = divmod(remain, 60)
                    return f"{prefix} [{bar}] {mm:02d}:{ss:02d}"

                # Update once per second; if stdout is not a TTY, fall back to timestamped lines
                last_line = None
                is_tty = False
                try:
                    is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
                except Exception:
                    is_tty = False
                newline_fallback_every = 10  # seconds
                last_newline = 0
                while True:
                    elapsed = int(time.time() - start_ts)
                    remain = max(0, wait_total - elapsed)
                    if remain <= 0:
                        break
                    oasis_eta = _read_next_oasis_eta()
                    if oasis_eta is not None:
                        # Show oasis ETA alongside cycle countdown (minimum displayed)
                        line = _render_bar("Next cycle", remain, wait_total)
                        line2 = _render_bar("Next oasis", oasis_eta, max(oasis_eta, 1))
                        out = line + " | " + line2
                    else:
                        out = _render_bar("Next cycle", remain, wait_total)
                    if is_tty:
                        if out != last_line:
                            try:
                                from core.console import print_status as _print_status
                            except Exception:
                                _print_status = None
                            if _print_status:
                                _print_status(out)
                            else:
                                print("\r" + out, end="", flush=True)
                            last_line = out
                    else:
                        # Non‑TTY: print a timestamped line every few seconds so it shows up in logs
                        if (elapsed - last_newline) >= newline_fallback_every:
                            last_newline = elapsed
                            _log_info(out)
                    time.sleep(1)
                if is_tty and last_line:
                    try:
                        from core.console import end_status as _end_status
                    except Exception:
                        _end_status = None
                    if _end_status:
                        _end_status()
                    else:
                        print()

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
