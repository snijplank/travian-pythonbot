import json
import os
from pathlib import Path

from core.unit_catalog import FACTION_TO_TRIBE


_IDENTITY_PATH = Path(__file__).resolve().parent.parent / "database" / "identity.json"


def _load_identity_dict() -> dict:
    path = _IDENTITY_PATH
    if not path.exists():
        raise FileNotFoundError("identity.json not found; run identity setup first.")
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_tribe_id(travian_identity: dict) -> int:
    """Derive the numeric tribe id from faction text (preferred) or legacy id."""
    faction = str(travian_identity.get("faction", "")).strip()
    if faction:
        lookup = {str(name).strip().lower(): tid for name, tid in FACTION_TO_TRIBE.items()}
        tribe = lookup.get(faction.lower())
        if tribe:
            return int(tribe)

    tribe_val = travian_identity.get("tribe_id")
    try:
        tribe_int = int(tribe_val)
        if tribe_int > 0:
            return tribe_int
    except Exception:
        pass

    # Final fallback → Huns (4) to stay consistent with previous defaults
    return 4


def get_account_tribe_id() -> int:
    identity = _load_identity_dict()
    travian_identity = identity.get("travian_identity", {})
    return _resolve_tribe_id(travian_identity)


def load_villages_from_identity():
    """Load all villages with enriched tribe/faction metadata."""
    identity = _load_identity_dict()

    travian_identity = identity.get("travian_identity") or {}
    servers = (travian_identity.get("servers") or [])
    if not servers:
        raise Exception("❌ No servers found in identity!")

    villages = servers[0].get("villages") or []
    if not villages:
        raise Exception("❌ No villages found for the server!")

    tribe_id = _resolve_tribe_id(travian_identity)
    faction = travian_identity.get("faction")

    # Enrich each village dict so downstream code has consistent metadata
    for village in villages:
        village.setdefault("tribe_id", tribe_id)
        if faction and not village.get("faction"):
            village["faction"] = faction

    # Keep the resolved tribe_id available for callers relying on the top-level dict
    travian_identity["tribe_id"] = tribe_id

    return villages

def choose_village_to_scan(villages):
    """Prompt user to pick a village to center the scan around."""
    print("\n🏡 Available villages to scan from:")
    for idx, village in enumerate(villages):
        print(f"{idx}: {village['village_name']} ({village['x']},{village['y']})")

    while True:
        try:
            choice = int(input("\n✏️ Enter the number of the village to scan around: ").strip())
            if 0 <= choice < len(villages):
                selected = villages[choice]
                print(f"\n✅ Selected village: {selected['village_name']} at ({selected['x']},{selected['y']})")
                return selected["x"], selected["y"]
            else:
                print("❌ Invalid selection, please try again.")
        except ValueError:
            print("❌ Please enter a valid number.")
