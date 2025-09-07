from __future__ import annotations

# Central catalog for tribe/unit mappings and helpers

# Map common faction strings to tribe IDs
FACTION_TO_TRIBE = {
    "Roman": 1, "Romans": 1,
    "Teuton": 2, "Teutons": 2, "German": 2, "Germans": 2,
    "Gaul": 3, "Gauls": 3,
    "Hun": 4, "Huns": 4,
    "Egyptian": 5, "Egyptians": 5,
}

# Global unit id block base per tribe for uXX → local slot t1..t10 mapping
TRIBE_BASE = {
    1: 0,   # Romans: u1..u10
    2: 10,  # Teutons: u11..u20
    3: 20,  # Gauls: u21..u30
    5: 40,  # Egyptians: u41..u50
    4: 60,  # Huns: u61..u70
}

# Unit names per tribe (local codes t1..t10)
UNIT_NAME_MAP = {
    1: {"t1": "Legionnaire", "t2": "Praetorian", "t3": "Imperian", "t4": "Equites Legati", "t5": "Equites Imperatoris", "t6": "Equites Caesaris", "t7": "Battering Ram", "t8": "Fire Catapult", "t9": "Senator", "t10": "Settler"},
    2: {"t1": "Clubswinger", "t2": "Spearman", "t3": "Axeman", "t4": "Scout", "t5": "Paladin", "t6": "Teutonic Knight", "t7": "Ram", "t8": "Catapult", "t9": "Chief", "t10": "Settler"},
    3: {"t1": "Phalanx", "t2": "Swordsman", "t3": "Pathfinder", "t4": "Theutates Thunder", "t5": "Druidrider", "t6": "Haeduan", "t7": "Ram", "t8": "Trebuchet", "t9": "Chieftain", "t10": "Settler"},
    4: {"t1": "Mercenary", "t2": "Bowman", "t3": "Spotter", "t4": "Steppe Rider", "t5": "Marksman", "t6": "Marauder", "t7": "Ram", "t8": "Catapult", "t9": "Logades", "t10": "Settler"},
    5: {"t1": "Slave Militia", "t2": "Ash Warden", "t3": "Khopesh Warrior", "t4": "Sopdu Explorer", "t5": "Anhur Guard", "t6": "Resheph Chariot", "t7": "Ram", "t8": "Stone Catapult", "t9": "Nomarch", "t10": "Settler"},
}


def u_to_t(code: str) -> str | None:
    """Map a global unit id like 'u61' or '61' to local slot 't1'..'t10'. Accepts already 'tX'."""
    try:
        if code is None:
            return None
        s = str(code).strip()
        if s.startswith("t") and s[1:].isdigit():
            return s
        if s.startswith("u") and s[1:].isdigit():
            n = int(s[1:])
        elif s.isdigit():
            n = int(s)
        else:
            return None
        if 1 <= n <= 10:  return f"t{n}"
        if 11 <= n <= 20: return f"t{n-10}"
        if 21 <= n <= 30: return f"t{n-20}"
        if 31 <= n <= 40: return f"t{n-30}"
        if 41 <= n <= 50: return f"t{n-40}"
        if 61 <= n <= 70: return f"t{n-60}"
    except Exception:
        return None
    return None


def t_to_u(tribe_id: int, unit_code: str) -> str:
    """Map local slot 't1'..'t10' to a global 'uNN' based on tribe."""
    s = str(unit_code).strip()
    if s.startswith("u"):
        return s
    if s.startswith("t") and s[1:].isdigit():
        slot = int(s[1:])
        base = TRIBE_BASE.get(int(tribe_id), 60)
        return f"u{base + slot}"
    return s


def resolve_unit_base_name(tribe_id: int | None, unit_code: str) -> str:
    """Return readable unit name (without code suffix), or the code if unknown."""
    tcode = u_to_t(unit_code) if not (str(unit_code).startswith("t")) else unit_code
    if tcode and isinstance(tribe_id, int) and tribe_id in UNIT_NAME_MAP:
        return UNIT_NAME_MAP[tribe_id].get(tcode, str(unit_code))
    return str(unit_code)


def resolve_label_t(tribe_id: int | None, unit_code: str) -> str:
    name = resolve_unit_base_name(tribe_id, unit_code)
    tcode = u_to_t(unit_code) or unit_code
    return f"{name} ({tcode})"


def resolve_label_u(tribe_id: int | None, unit_code: str) -> str:
    name = resolve_unit_base_name(tribe_id, unit_code)
    ucode = unit_code if str(unit_code).startswith("u") else t_to_u(int(tribe_id or 4), unit_code)
    return f"{name} ({ucode})"

