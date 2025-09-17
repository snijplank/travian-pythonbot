from identity_handling.login import login
from identity_handling.identity_helper import load_villages_from_identity, choose_village_to_scan
from core.travian_api import TravianAPI
from core.full_map_scanner import full_map_scan
from analysis.full_scan_oasis_analysis import extract_unoccupied_oases
import time


def scan_map_for_oases(
    api: TravianAPI,
    *,
    default_radius: int = 25,
    prompt_radius: bool = True,
    disable_humanizer: bool = False,
) -> None:
    """Run a map scan for unoccupied oases.

    Args:
        api: Logged-in Travian API client.
        default_radius: Radius to use when the user skips input (tiles).
        prompt_radius: Whether to ask the user for a custom radius.
    """
    # Try to load villages, else inform user to run identity setup
    try:
        villages = load_villages_from_identity()
    except (FileNotFoundError, Exception) as e:
        print(f"[!] Issue loading identity: {e}")
        print("[!] Please run 'python identity_handling/setup_identity.py' first to set up your identity")
        return

    # Let user choose from villages
    village_x, village_y = choose_village_to_scan(villages)

    # Ask for scan radius (optional for quick scans)
    scan_radius = default_radius
    if prompt_radius:
        try:
            user_input = input(f"\n🗺️  Enter scan radius around the village (default = {default_radius}): ").strip()
            if user_input:
                scan_radius = int(user_input)
        except ValueError:
            scan_radius = default_radius

    total_tiles = (scan_radius * 2 + 1) ** 2
    print(f"[i] This scan will request {total_tiles} tiles.")

    humanizer_toggled = False
    if disable_humanizer and hasattr(api, "set_humanizer"):
        try:
            api.set_humanizer(False)
            humanizer_toggled = True
            print("[i] Humanizer disabled for faster scanning.")
        except Exception:
            pass

    start_time = time.time()
    try:
        # Full map scan
        print(f"[+] Starting full map scan around ({village_x}, {village_y}) with radius {scan_radius}...")
        scan_path = full_map_scan(api, village_x, village_y, scan_radius)

        # Oasis extraction
        print("[+] Extracting unoccupied oases from scan data...")
        extract_unoccupied_oases(scan_path)
    finally:
        if humanizer_toggled:
            try:
                api.set_humanizer(True)
                print("[i] Humanizer restored to previous state.")
            except Exception:
                pass

    elapsed = time.time() - start_time
    print(f"\n✅ Map scanning and oasis extraction complete in {elapsed:.1f}s")


def quick_scan_for_oases(api: TravianAPI, *, radius: int = 15) -> None:
    """Run a quick map scan with a small fixed radius for faster results."""
    print(f"\n⚡ Starting quick scan (radius {radius})...")
    scan_map_for_oases(
        api,
        default_radius=radius,
        prompt_radius=False,
        disable_humanizer=True,
    )
