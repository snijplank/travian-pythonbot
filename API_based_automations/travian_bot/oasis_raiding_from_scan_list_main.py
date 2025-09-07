import os
import json
import logging
from random import uniform
import time

from identity_handling.login import login
from identity_handling.identity_helper import load_villages_from_identity
from core.travian_api import TravianAPI
from analysis.number_to_unit_mapping import get_unit_name
from core.unit_catalog import resolve_label_u
from core.unit_catalog import resolve_label_t
from core.database_helpers import load_latest_unoccupied_oases
from core.database_raid_config import load_saved_raid_plan, save_raid_plan
from features.oasis.raider import run_raid_batch
from core.hero_runner import try_send_hero_to_oasis  # ✅ Hero logic
from identity_handling.faction_utils import get_faction_name

# --- Unit name mapping (per tribe) ---
UNIT_NAME_MAP = {
    1: {  # Romans
        "t1": "Legionnaire", "t2": "Praetorian", "t3": "Imperian", "t4": "Equites Legati",
        "t5": "Equites Imperatoris", "t6": "Equites Caesaris", "t7": "Battering Ram", "t8": "Fire Catapult", "t9": "Senator", "t10": "Settler",
    },
    2: {  # Teutons
        "t1": "Clubswinger", "t2": "Spearman", "t3": "Axeman", "t4": "Scout",
        "t5": "Paladin", "t6": "Teutonic Knight", "t7": "Ram", "t8": "Catapult", "t9": "Chief", "t10": "Settler",
    },
    3: {  # Gauls
        "t1": "Phalanx", "t2": "Swordsman", "t3": "Pathfinder", "t4": "Theutates Thunder",
        "t5": "Druidrider", "t6": "Haeduan", "t7": "Ram", "t8": "Trebuchet", "t9": "Chieftain", "t10": "Settler",
    },
    4: {  # Huns
        "t1": "Mercenary", "t2": "Bowman", "t3": "Spotter", "t4": "Steppe Rider",
        "t5": "Marksman", "t6": "Marauder", "t7": "Ram", "t8": "Catapult", "t9": "Logades", "t10": "Settler",
    },
    5: {  # Egyptians
        "t1": "Slave Militia", "t2": "Ash Warden", "t3": "Khopesh Warrior", "t4": "Sopdu Explorer",
        "t5": "Anhur Guard", "t6": "Resheph Chariot", "t7": "Ram", "t8": "Stone Catapult", "t9": "Nomarch", "t10": "Settler",
    },
}


def _u_to_t(u_code: str) -> str | None:
    """Map global unit id like 'u61' to local slot 't1'..'t10'."""
    try:
        if not (u_code and u_code.startswith("u") and u_code[1:].isdigit()):
            return None
        n = int(u_code[1:])
        if 1 <= n <= 10:
            return f"t{n}"
        if 11 <= n <= 20:
            return f"t{n-10}"
        if 21 <= n <= 30:
            return f"t{n-20}"
        if 31 <= n <= 40:
            return f"t{n-30}"
        if 41 <= n <= 50:
            return f"t{n-40}"
        if 61 <= n <= 70:
            return f"t{n-60}"
    except Exception:
        return None
    return None


def resolve_unit_name(tribe_id: int, unit_code: str) -> str:
    """Return readable unit name with local code in parentheses, or fallback to Unknown."""
    tcode = _u_to_t(unit_code) if unit_code.startswith("u") else unit_code if unit_code.startswith("t") else None
    if tcode and tribe_id in UNIT_NAME_MAP:
        return f"{UNIT_NAME_MAP[tribe_id].get(tcode, tcode)} ({tcode})"
    return f"Unknown Unit ({unit_code})"

class NoTimestampFormatter(logging.Formatter):
    def format(self, record):
        return f"[{record.levelname}] {record.getMessage()}"

console_handler = logging.StreamHandler()
console_handler.setFormatter(NoTimestampFormatter())
logging.basicConfig(level=logging.INFO, handlers=[console_handler])

def save_raid_plan(raid_plan, server_url, village_index):
    """Save the raid plan to a JSON file."""
    try:
        # Create a directory for raid plans if it doesn't exist
        os.makedirs("database/raid_plans", exist_ok=True)
        
        # Save the raid plan with village index in the filename
        filename = f"database/raid_plans/raid_plan_village_{village_index}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(raid_plan, f, indent=4)
        logging.info(f"✅ Raid plan saved to {filename}")
    except Exception as e:
        logging.error(f"❌ Failed to save raid plan: {e}")

def load_saved_raid_plan(village_index):
    """Load the saved raid plan from JSON file."""
    try:
        filename = f"database/raid_plans/raid_plan_village_{village_index}.json"
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logging.warning(f"⚠️ No saved raid plan found for village {village_index}")
        return None
    except Exception as e:
        logging.error(f"❌ Failed to load raid plan: {e}")
        return None

def run_raid_planner(
    api,
    server_url,
    reuse_saved=True,
    selected_village_index=None,
    units_to_use=None,
    enable_hero_raiding=True,
    interactive=False,
    multi_village=False,  # New parameter to control multi-village mode
    run_farm_lists=False  # New parameter to control whether to run farm lists
):
    villages = load_villages_from_identity()
    if not villages:
        logging.error("No villages found in identity. Exiting.")
        return

    # Load faction from identity using the helper function
    try:
        current_dir = os.path.dirname(__file__)
        database_dir = os.path.join(current_dir, 'database')
        identity_path = os.path.join(database_dir, 'identity.json')
        identity_path = os.path.abspath(identity_path)

        with open(identity_path, "r", encoding="utf-8") as f:
            identity = json.load(f)
            tribe_id = identity["travian_identity"]["tribe_id"]
            faction = get_faction_name(tribe_id)
            logging.info(f"Detected faction: {faction.title()}")
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        logging.error(f"❌ Error loading identity: {e}")
        return

    # Determine which villages to process
    if multi_village:
        # Optionally shuffle village order to avoid fixed patterns
        try:
            from config.config import settings as _cfg
            import random as _rnd
            vlist = list(villages)
            if bool(getattr(_cfg, 'SHUFFLE_VILLAGE_ORDER', True)):
                _rnd.shuffle(vlist)
            villages_to_process = list(enumerate(vlist))
        except Exception:
            villages_to_process = list(enumerate(villages))
        logging.info(f"Running in multi-village mode. Will process {len(villages)} villages.")
    else:
        village_index = 0
        villages_to_process = [(village_index, villages[village_index])]
        logging.info("Running in single-village mode.")

    # Process selected villages
    for village_index, selected_village in villages_to_process:
        logging.info(f"\n{'='*50}")
        logging.info(f"Processing village {village_index + 1}/{len(villages)}: {selected_village['village_name']}")
        logging.info(f"{'='*50}")

        village_id = selected_village["village_id"]
        village_x = selected_village["x"]
        village_y = selected_village["y"]
        logging.info(f"Village coordinates: ({village_x}, {village_y})")

        # Switch to the correct village
        logging.info(f"\nSwitching to village {village_id}")
        url = f"{api.server_url}/dorf1.php?newdid={village_id}"
        response = api.session.get(url)
        response.raise_for_status()

        # Get troops info for this village
        troops_info = api.get_troops_in_village()
        if not troops_info:
            logging.error("Could not fetch troops. Skipping village.")
            continue

        logging.info("Current troops in village:")
        for unit_code, amount in troops_info.items():
            try:
                label = resolve_label_u(tribe_id, unit_code)
            except Exception:
                label = unit_code
            logging.info(f"    {label}: {amount} units")

        # Run farm lists only if explicitly requested
        if run_farm_lists:
            logging.info("\nRunning farm lists...")
            # Small human-like jitter before launching farm lists + random skip
            try:
                import random, time as _t
                from config.config import settings as _cfg
                _t.sleep(random.uniform(float(getattr(_cfg, 'OP_JITTER_MIN_SEC', 0.5)), float(getattr(_cfg, 'OP_JITTER_MAX_SEC', 2.0))))
                skip_prob = float(getattr(_cfg, 'FARM_LIST_RANDOM_SKIP_PROB', 0.0))
                if skip_prob > 0 and random.random() < skip_prob:
                    logging.info("[humanizer] Skipping farm lists this cycle for this village.")
                    run_farm_lists = False
            except Exception:
                pass
            if run_farm_lists:
                from features.farm_lists.farm_list_raider import run_farm_list_raids
                run_farm_list_raids(api, server_url, village_id)
        else:
            logging.info("\nSkipping farm lists as requested.")

        # Then check if this village has a raid plan for oases
        saved_data = load_saved_raid_plan(village_index)
        if reuse_saved and saved_data and saved_data["server"] == server_url:
            # This village has a saved raid plan
            raid_plan = saved_data  # Use the entire saved data as the raid plan
        else:
            # No raid plan for this village
            logging.warning(f"⚠️ No raid plan found for {selected_village['village_name']}. Skipping oasis raids.")
            continue

        oases = load_latest_unoccupied_oases(f"({village_x}_{village_y})")
        if not oases:
            logging.info("No unoccupied oases found for this village. Skipping oasis raids.")
            continue

        # --- HERO LOGIC START ---
        hero_available = troops_info.get("uhero", 0) >= 1
        hero_sent = False

        if enable_hero_raiding and hero_available:
            for coord_key, oasis_data in oases.items():
                x_str, y_str = coord_key.split("_")
                oasis = {"x": int(x_str), "y": int(y_str)}
                if try_send_hero_to_oasis(api, selected_village, oasis):
                    hero_sent = True
                    break
        # --- HERO LOGIC END ---

        run_raid_batch(api, raid_plan, faction, village_id, oases)

    logging.info("\n✅ Finished processing all selected villages.")

def setup_raid_plan_interactive(api, server_url, selected_village_index=None):
    """Set up a raid plan interactively."""
    villages = load_villages_from_identity()
    if not villages:
        logging.error("No villages found in identity. Exiting.")
        return

    # Load faction from identity using the helper function
    try:
        current_dir = os.path.dirname(__file__)
        database_dir = os.path.join(current_dir, '..', 'database')
        identity_path = os.path.join(database_dir, 'identity.json')
        identity_path = os.path.abspath(identity_path)

        with open(identity_path, "r", encoding="utf-8") as f:
            identity = json.load(f)
            tribe_id = identity["travian_identity"]["tribe_id"]
            faction = get_faction_name(tribe_id)
            logging.info(f"Detected faction: {faction.title()}")
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        logging.error(f"❌ Error loading identity: {e}")
        return

    # Determine which village to use
    if selected_village_index is None:
        # Show available villages
        print("\nAvailable villages:")
        for i, village in enumerate(villages):
            print(f"[{i}] {village['village_name']} ({village['x']}, {village['y']})")
        
        # Get village selection
        while True:
            try:
                selected_village_index = int(input("\nSelect a village (enter number): "))
                if 0 <= selected_village_index < len(villages):
                    break
                print("Invalid selection. Please try again.")
            except ValueError:
                print("Please enter a valid number.")

    selected_village = villages[selected_village_index]
    village_id = selected_village["village_id"]
    village_x = selected_village["x"]
    village_y = selected_village["y"]
    logging.info(f"Selected village: {selected_village['village_name']} ({village_x}, {village_y})")

    # Switch to the correct village
    logging.info(f"\nSwitching to village {village_id}")
    url = f"{api.server_url}/dorf1.php?newdid={village_id}"
    response = api.session.get(url)
    response.raise_for_status()

    # Get troops info
    troops_info = api.get_troops_in_village()
    if not troops_info:
        logging.error("Could not fetch troops. Exiting.")
        return

    logging.info("Current troops in village:")
    for unit_code, amount in troops_info.items():
        unit_name = resolve_label_t(tribe_id, unit_code)
        logging.info(f"    {unit_name}: {amount} units")

    # Get max raid distance
    while True:
        try:
            max_raid_distance = int(input("\nEnter maximum raid distance (1-15): "))
            if 1 <= max_raid_distance <= 15:
                break
            print("Distance must be between 1 and 15.")
        except ValueError:
            print("Please enter a valid number.")

    # Get distance ranges and unit combinations
    distance_ranges = []
    current_start = 0

    while current_start < max_raid_distance:
        print(f"\nSetting up raid group for distance {current_start}+")
        
        # Get end distance for this range
        while True:
            try:
                end_distance = int(input(f"Enter end distance for this group (max {max_raid_distance}): "))
                if current_start < end_distance <= max_raid_distance:
                    break
                print(f"End distance must be between {current_start + 1} and {max_raid_distance}.")
            except ValueError:
                print("Please enter a valid number.")

        # Get unit combinations for this range
        units = []
        while True:
            print("\nAvailable units:")
            for unit_code, amount in troops_info.items():
                unit_name = resolve_unit_name(tribe_id, unit_code)
                print(f"    {unit_name}: {amount} units")

            unit_code = input("\nEnter unit code to add (or press Enter to finish this range): ").strip()
            if not unit_code:
                break

            if unit_code not in troops_info:
                print("Invalid unit code. Please try again.")
                continue

            while True:
                try:
                    group_size = int(input(f"Enter group size for {resolve_unit_name(tribe_id, unit_code)}: "))
                    if 1 <= group_size <= troops_info[unit_code]:
                        break
                    print(f"Group size must be between 1 and {troops_info[unit_code]}.")
                except ValueError:
                    print("Please enter a valid number.")

            units.append({
                "unit_code": unit_code,
                "unit_payload_code": unit_code,
                "group_size": group_size
            })

        if units:
            distance_ranges.append({
                "start": current_start,
                "end": end_distance,
                "units": units
            })
            current_start = end_distance
        else:
            print("No units added for this range. Please add at least one unit.")

    # Create the raid plan
    raid_plan = {
        "server": server_url,
        "village_index": selected_village_index,
        "max_raid_distance": max_raid_distance,
        "distance_ranges": distance_ranges,
        "raid_plan": []
    }

    # Save the raid plan
    save_raid_plan(raid_plan, server_url, selected_village_index)
    logging.info("✅ Raid plan setup complete!")

    return raid_plan

def main():
    """Main entry point for the oasis raiding script (YAML-config, no .env)."""
    # Login to server using configured identity
    print("\n🔐 Logging into Travian...")
    session, server_url = login()
    api = TravianAPI(session, server_url)

    print("\n🎯 Starting raid planner for all villages (single pass)...")
    run_raid_planner(api, server_url, reuse_saved=True, multi_village=True, run_farm_lists=True)

if __name__ == "__main__":
    main()
