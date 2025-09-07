# Lightweight troop attack table per tribe for Travian T4.6
# Tribe ids: 1=Romans, 2=Teutons, 3=Gauls, 4=Huns, 5=Egyptians

TROOP_ATTACK = {
    1: {  # Romans
        "t1": 40, "t2": 30, "t3": 70, "t4": 0,  "t5": 120, "t6": 180,
        "t7": 60,  "t8": 75,  "t9": 50,  "t10": 0,
    },
    2: {  # Teutons (Germans)
        "t1": 40, "t2": 10, "t3": 60, "t4": 0,  "t5": 55,  "t6": 150,
        "t7": 65,  "t8": 50,  "t9": 40,  "t10": 0,
    },
    3: {  # Gauls
        "t1": 15, "t2": 65, "t3": 0,  "t4": 90, "t5": 45,  "t6": 140,
        "t7": 50,  "t8": 65,  "t9": 30,  "t10": 0,
    },
    4: {  # Huns
        "t1": 40, "t2": 30, "t3": 105, "t4": 115, "t5": 180, "t6": 170,
        "t7": 60,  "t8": 70,  "t9": 50,  "t10": 0,
    },
    5: {  # Egyptians
        "t1": 10, "t2": 20, "t3": 30,  "t4": 0,   "t5": 120, "t6": 150,
        "t7": 30,  "t8": 55,  "t9": 40,  "t10": 0,
    },
}

DEFAULT_TRIBE_ID = 4  # default to Huns if unknown (matches your use case)


def get_unit_attack(tribe_id: int, unit_code: str) -> int:
    """Return base attack for given tribe and unit code like 't5'. Fallbacks sensibly."""
    tribe = TROOP_ATTACK.get(int(tribe_id) if tribe_id else DEFAULT_TRIBE_ID, TROOP_ATTACK[DEFAULT_TRIBE_ID])
    return int(tribe.get(unit_code, 0))


def estimate_escort_units(required_attack: float, unit_attack: int,
                          min_units: int = 1, max_units: int = 999) -> int:
    """Compute number of escort units needed to meet/exceed required_attack.
    Clamps to [min_units, max_units]. If unit_attack is 0, returns min_units.
    """
    try:
        if unit_attack <= 0:
            return max(1, int(min_units))
        from math import ceil
        needed = ceil(max(0.0, float(required_attack)) / float(unit_attack))
        return int(max(min_units, min(needed, max_units)))
    except Exception:
        return max(1, int(min_units))