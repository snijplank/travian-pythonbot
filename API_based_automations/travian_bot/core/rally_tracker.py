from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

from core.learning_store import LearningStore
from core.metrics import add_learning_change

try:  # pragma: no cover - config loading fallback
    from config.config import settings
except Exception:  # pragma: no cover
    class _Fallback:
        LEARNING_ENABLE = True
        RALLY_MATCH_TOLERANCE_SEC = 120.0
        RALLY_RETURN_TIMEOUT_SEC = 900.0

    settings = _Fallback()

LOG = logging.getLogger("travian")

PENDING_FILE = Path("database/learning/pending_rally.json")


def _load_pending() -> List[Dict[str, Any]]:
    if not PENDING_FILE.exists():
        return []
    try:
        return json.loads(PENDING_FILE.read_text(encoding="utf-8")) or []
    except Exception:
        return []


def _save_pending(entries: List[Dict[str, Any]]) -> None:
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PENDING_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PENDING_FILE)


def enqueue_pending_raid(
    *,
    village_id: int,
    target: str,
    recommended: int,
    sent_total: int,
    sent_units: Dict[str, int],
    depart_epoch: float,
    travel_time_sec: Optional[float],
    source: str = "oasis",
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist a pending raid entry that will be matched via the rally point overview."""
    entry = {
        "id": f"{int(village_id)}:{target}:{int(depart_epoch)}",
        "village_id": int(village_id),
        "target": target,
        "recommended": int(recommended),
        "sent_total": int(sent_total),
        "sent_units": {str(k): int(v) for k, v in (sent_units or {}).items()},
        "depart_epoch": float(depart_epoch),
        "travel_time_sec": float(travel_time_sec) if travel_time_sec is not None else None,
        "expected_return_epoch": (
            float(depart_epoch) + 2.0 * float(travel_time_sec)
            if travel_time_sec is not None
            else None
        ),
        "created": float(time.time()),
        "unit_code": "mixed",
        "source": source,
    }
    if meta:
        entry["meta"] = meta
    data = _load_pending()
    data.append(entry)
    _save_pending(data)


def _schedule_immediate_retry(
    api,
    ls: LearningStore,
    target: str,
    *,
    village_id: Optional[int] = None,
) -> None:
    """Mark a target as due now and update the next-oasis hint after a full-haul return."""
    norm_target = _normalize_key(target)
    if not norm_target:
        return

    def _coords_from_target(token: str) -> Optional[tuple[int, int]]:
        match = re.match(r"\((-?\d+)\s*,\s*(-?\d+)\)", token)
        if not match:
            return None
        try:
            return int(match.group(1)), int(match.group(2))
        except Exception:
            return None

    def _is_friendly_occupied(api_obj, token: str) -> bool:
        coords = _coords_from_target(token)
        if not coords or api_obj is None:
            return False
        try:
            from analysis.tile_analysis import analyze_tile
            from features.oasis.validator import _get_own_alliance_tag
        except Exception:
            return False
        x, y = coords
        try:
            html = api_obj.get_tile_html(x, y)
            info = analyze_tile(html, (x, y)) or {}
            if info.get("type") != "occupied_oasis":
                return False
            owner = info.get("owner_info") or {}
            owner_alliance = str(owner.get("alliance") or "").strip()
            own = _get_own_alliance_tag(api_obj)
            if own and owner_alliance and owner_alliance.lower() == own.lower():
                return True
        except Exception:
            return False
        return False

    if _is_friendly_occupied(api, norm_target):
        LOG.info("[RallyTracker] Full haul for %s maar oase is vriendelijk bezet; geen immediate retry.", norm_target)
        return

    try:
        interval = float(getattr(settings, "OASIS_TARGET_INTERVAL_MIN_SEC", 600.0))
    except Exception:
        interval = 600.0

    try:
        priority_window = float(getattr(settings, "LEARNING_PRIORITY_RETRY_SEC", 300.0))
    except Exception:
        priority_window = 300.0

    now = time.time()
    try:
        offset = interval if interval > 0 else 60.0
        ls.set_last_sent(norm_target, ts=now - offset - 1.0)
    except Exception as exc:  # pragma: no cover - defensive logging only
        LOG.debug("[RallyTracker] Failed to mark %s due immediately: %s", norm_target, exc)

    if priority_window > 0:
        try:
            ls.set_priority(norm_target, priority_window)
        except Exception as exc:
            LOG.debug("[RallyTracker] Failed to mark %s priority: %s", norm_target, exc)

    try:
        hint_path = Path("database/runtime_next_oasis_due.json")
        payload: Dict[str, Any] = {}
        if hint_path.exists():
            payload = json.loads(hint_path.read_text(encoding="utf-8")) or {}

        village_info = payload.get("village")
        if not isinstance(village_info, dict):
            village_info = {}
        if village_id:
            village_info.setdefault("id", int(village_id))
            if "x" not in village_info or "y" not in village_info:
                try:
                    from identity_handling.identity_helper import load_villages_from_identity
                    villages = load_villages_from_identity() or []
                    match = next((v for v in villages if int(v.get("village_id", 0)) == int(village_id)), None)
                    if match:
                        village_info.setdefault("x", int(match.get("x")))
                        village_info.setdefault("y", int(match.get("y")))
                except Exception:
                    pass
        payload["village"] = village_info
        payload["generated"] = int(now)
        payload["next_due_sec"] = 0.0
        payload["next_due_epoch"] = int(now)

        tmp = hint_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(hint_path)
    except Exception as exc:  # pragma: no cover - defensive logging only
        LOG.debug("[RallyTracker] Failed to update next-due hint for %s: %s", norm_target, exc)

    LOG.info("[RallyTracker] Full haul for %s — scheduling immediate retry.", norm_target)


@dataclass
class RallyReturn:
    target: str
    arrival_epoch: float
    troops: Dict[str, int]
    troops_total: int
    bounty_detail: Dict[str, int]
    bounty_total: int
    carry_full: bool


def _parse_coordinates(node: Any) -> Optional[str]:
    if node is None:
        return None
    text = "".join(node.stripped_strings) if hasattr(node, "stripped_strings") else str(node)
    text = (
        text.replace("\u202d", "")
        .replace("\u202c", "")
        .replace("\u2212", "-")
        .replace("\u2013", "-")
    )
    match = re.search(r"([-+]?\d{1,3})\s*\|\s*([-+]?\d{1,3})", text)
    if not match:
        return None
    x, y = int(match.group(1)), int(match.group(2))
    return f"({x},{y})"


def _parse_time_to_seconds(text: str) -> Optional[int]:
    text = (
        text.replace("\u202d", "")
        .replace("\u202c", "")
        .replace("\u2212", "-")
        .replace("\u2013", "-")
    )
    match = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", text)
    if not match:
        return None
    h, m, s = map(int, match.groups())
    return h * 3600 + m * 60 + s


def _extract_server_epoch(soup: BeautifulSoup) -> Optional[int]:
    timer = soup.select_one('#servertime span.timer[value]')
    if timer and timer.has_attr("value"):
        try:
            return int(float(timer["value"]))
        except Exception:
            return None
    return None


def _parse_return_table(table: Any, server_epoch: float) -> Optional[RallyReturn]:
    try:
        headline = table.find("td", class_="troopHeadline")
        target = _parse_coordinates(headline)
        if not target:
            return None

        units_row = table.find("tbody", class_="units")
        if not units_row:
            return None
        unit_codes: List[str] = []
        for td in units_row.find_all("td", class_=lambda c: c and "uniticon" in c):
            img = td.find("img")
            code = None
            if img:
                for cls in img.get("class", []):
                    if cls.startswith("u"):
                        code = cls
                        break
            if code:
                unit_codes.append(code)

        counts_row = table.find("tbody", class_="units last")
        counts: List[int] = []
        if counts_row:
            for td in counts_row.find_all("td", class_=lambda c: c and "unit" in c):
                txt = (
                    td.get_text(strip=True)
                    .replace("\u202d", "")
                    .replace("\u202c", "")
                    .replace("\u2212", "-")
                    .replace("\u2013", "-")
                )
                try:
                    counts.append(int(txt))
                except Exception:
                    counts.append(0)
        troops: Dict[str, int] = {}
        troops_total = 0
        for code, amount in zip(unit_codes, counts):
            if code == "uhero":
                continue
            if amount > 0:
                troops[code] = amount
            troops_total += amount

        bounty_total = 0
        bounty_detail: Dict[str, int] = {}
        carry_full = False
        arrival_in: Optional[int] = None
        for info_body in table.find_all("tbody", class_="infos"):
            th = info_body.find("th")
            if not th:
                continue
            label = th.get_text(strip=True)
            if "Bounty" in label:
                numbers = re.findall(r"([0-9]{1,7})", info_body.get_text())
                if len(numbers) >= 4:
                    wood, clay, iron, crop = map(int, numbers[:4])
                    bounty_detail = {
                        "wood": wood,
                        "clay": clay,
                        "iron": iron,
                        "crop": crop,
                    }
                    bounty_total = wood + clay + iron + crop
                icon = info_body.find("i", class_=lambda c: c and "carry" in c)
                if icon:
                    classes = icon.get("class", [])
                    carry_full = any("full" == cls or cls.endswith("full") for cls in classes)
            elif "Arrival" in label:
                timer = info_body.find("span", class_="timer")
                if timer and timer.has_attr("value"):
                    try:
                        arrival_in = int(float(timer["value"]))
                    except Exception:
                        arrival_in = None
                if arrival_in is None:
                    arrival_in = _parse_time_to_seconds(info_body.get_text())

        if arrival_in is None:
            return None
        return RallyReturn(
            target=target,
            arrival_epoch=float(server_epoch) + float(arrival_in),
            troops=troops,
            troops_total=troops_total,
            bounty_detail=bounty_detail,
            bounty_total=bounty_total,
            carry_full=carry_full,
        )
    except Exception:
        return None


def _fetch_rally_returns(api, village_id: int) -> Dict[str, Any]:
    url = f"{api.server_url}/build.php?newdid={village_id}&id=39&gid=16&tt=1"
    res = api.session.get(url)
    res.raise_for_status()
    html = res.text
    soup = BeautifulSoup(html, "html.parser")
    server_epoch = _extract_server_epoch(soup)
    if server_epoch is None:
        server_epoch = time.time()
    returns: List[RallyReturn] = []
    for table in soup.select("table.troop_details.inReturn"):
        entry = _parse_return_table(table, server_epoch)
        if entry:
            returns.append(entry)
    return {"server_epoch": float(server_epoch), "returns": returns}


def get_pending_count() -> int:
    return len(_load_pending())


def process_pending_returns(api, *, verbose: bool = False) -> int:
    """Match pending raids against rally overview returns and update learning."""
    if not bool(getattr(settings, "LEARNING_ENABLE", True)):
        return 0

    pendings = _load_pending()
    if not pendings:
        return 0

    tolerance = float(getattr(settings, "RALLY_MATCH_TOLERANCE_SEC", 120.0))
    expiry = float(getattr(settings, "RALLY_RETURN_TIMEOUT_SEC", 900.0))

    remaining: List[Dict[str, Any]] = []
    processed = 0
    cache: Dict[int, Dict[str, Any]] = {}
    ls = LearningStore()

    for item in pendings:
        village_id = int(item.get("village_id", 0) or 0)
        if village_id <= 0:
            continue
        if village_id not in cache:
            try:
                cache[village_id] = _fetch_rally_returns(api, village_id)
            except Exception as exc:
                LOG.warning(f"[RallyTracker] Failed to fetch rally overview for village {village_id}: {exc}")
                cache[village_id] = {"server_epoch": time.time(), "returns": []}
        data = cache.get(village_id) or {"server_epoch": time.time(), "returns": []}
        server_epoch = float(data.get("server_epoch", time.time()))
        returns = data.get("returns", [])
        match = _match_pending(item, returns, tolerance, server_epoch)
        if match:
            processed += 1
            info = _apply_learning(ls, item, match, verbose=verbose)
            source = str(item.get("source") or (item.get("meta", {}) if isinstance(item.get("meta"), dict) else {}).get("source") or "oasis").lower()
            if info.get("carry_full") and source == "oasis":
                _schedule_immediate_retry(api, ls, match.target, village_id=village_id)
            pause_until = info.get("pause_until")
            if pause_until:
                try:
                    remain = max(0.0, float(pause_until) - time.time())
                    logging.info(
                        "[RallyTracker] Paused %s for %.1f minute(s) after losses.",
                        match.target,
                        remain / 60.0,
                    )
                except Exception:
                    logging.info("[RallyTracker] Paused %s after losses.", match.target)
            match._matched = True  # mark for future filtering
            continue
        expected = item.get("expected_return_epoch")
        if expected and server_epoch >= float(expected) + expiry:
            processed += 1
            _handle_timeout(ls, item, verbose=verbose)
            continue
        remaining.append(item)

    _save_pending(remaining)
    return processed


def _normalize_key(key: Optional[str]) -> Optional[str]:
    return LearningStore._normalize_key(key) if key is not None else None


def _match_pending(
    item: Dict[str, Any],
    returns: List[RallyReturn],
    tolerance: float,
    server_epoch: float,
) -> Optional[RallyReturn]:
    norm_target = _normalize_key(item.get("target"))
    expected = item.get("expected_return_epoch")
    candidate: Optional[RallyReturn] = None
    best_diff: Optional[float] = None

    # Filter returns that are not already matched and share same target if available
    candidates = []
    for entry in returns:
        if getattr(entry, "_matched", False):
            continue
        if norm_target and entry.target != norm_target:
            continue
        candidates.append(entry)

    if not candidates:
        return None

    if expected:
        for entry in candidates:
            diff = abs(float(entry.arrival_epoch) - float(expected))
            if best_diff is None or diff < best_diff:
                best_diff = diff
                candidate = entry
        if candidate and best_diff is not None and best_diff <= tolerance:
            return candidate
        return None

    # No expected arrival recorded → try to match by unit composition and departure timing.
    def _normalize_units(obj: Dict[str, Any] | None) -> Dict[str, int]:
        out: Dict[str, int] = {}
        if not isinstance(obj, dict):
            return out
        for k, v in obj.items():
            try:
                val = int(v)
            except Exception:
                continue
            if val > 0:
                out[str(k)] = val
        return out

    sent_units = _normalize_units(item.get("sent_units"))
    depart_epoch = float(item.get("depart_epoch", 0) or 0)
    best_match: Optional[RallyReturn] = None
    best_metric: Optional[float] = None
    for entry in candidates:
        entry_units = _normalize_units(entry.troops)
        if sent_units and entry_units != sent_units:
            continue
        if depart_epoch > 0:
            metric = float(entry.arrival_epoch) - depart_epoch
            if metric < 0:
                continue
        else:
            metric = float(entry.arrival_epoch) - server_epoch
        if best_match is None or metric < (best_metric or float("inf")):
            best_match = entry
            best_metric = metric

    if best_match:
        return best_match

    # Fallback: only match if single candidate remains
    if len(candidates) == 1:
        return candidates[0]
    return None


def _apply_learning(ls: LearningStore, item: Dict[str, Any], entry: RallyReturn, *, verbose: bool = False) -> Dict[str, Any]:
    key = _normalize_key(item.get("target")) or item.get("target")
    if not key:
        return {}
    recommended = int(item.get("recommended", 0) or 0)
    sent_total = int(item.get("sent_total", 0) or 0)
    sent_total = max(sent_total, 1)

    returned = max(0, int(entry.troops_total))
    loss_pct = max(0.0, min(1.0, 1.0 - (returned / sent_total)))
    result = "won" if returned > 0 else "lost"

    haul_detail = entry.bounty_detail or {}
    if entry.bounty_total:
        haul_detail = dict(haul_detail)
        haul_detail.setdefault("total", entry.bounty_total)

    ls.record_attempt(
        key,
        item.get("unit_code", "mixed"),
        recommended=recommended,
        sent=sent_total,
        result=result,
        loss_pct=loss_pct,
        haul=haul_detail,
    )

    current = float(ls.get_multiplier(key))
    direction = None
    pause_seconds = float(getattr(settings, "LEARNING_PAUSE_ON_LOSS_SEC", 3600.0))
    if loss_pct > 0.0:
        direction = None
        m = current
        if pause_seconds > 0:
            ls.set_pause(key, pause_seconds)
    elif entry.carry_full:
        step_full = float(getattr(settings, "LEARNING_STEP_UP_ON_FULL_LOOT", 1.0))
        if step_full > 0.0:
            direction = "up"
            m = ls.nudge_multiplier(
                key,
                "up",
                step=step_full,
                min_mul=float(getattr(settings, "LEARNING_MIN_MUL", 0.8)),
                max_mul=float(getattr(settings, "LEARNING_MAX_MUL", 2.5)),
            )
        else:
            m = current
        ls.clear_pause(key)
    else:
        m = current
        ls.clear_pause(key)

    if direction:
        add_learning_change(key, old=current, new=m, direction=direction, loss_pct=loss_pct)
        if verbose:
            LOG.info("[RallyTracker] %s → multiplier %s to %.3f (loss %.0f%%, haul %s)", key, direction, m, loss_pct * 100, entry.bounty_total)
    info = {
        "result": result,
        "loss_pct": loss_pct,
        "bounty_total": entry.bounty_total,
        "carry_full": entry.carry_full,
        "multiplier": m,
    }
    pause_until = ls.get_pause_until(key)
    if pause_until is not None:
        info["pause_until"] = float(pause_until)
    priority_until = ls.get_priority_until(key)
    if priority_until is not None:
        info["priority_until"] = float(priority_until)
    return info


def _handle_timeout(ls: LearningStore, item: Dict[str, Any], *, verbose: bool = False) -> None:
    key = _normalize_key(item.get("target")) or item.get("target")
    if not key:
        return
    recommended = int(item.get("recommended", 0) or 0)
    sent_total = int(item.get("sent_total", 0) or 0)
    sent_total = max(sent_total, 1)

    ls.record_attempt(
        key,
        item.get("unit_code", "mixed"),
        recommended=recommended,
        sent=sent_total,
        result="lost",
        loss_pct=1.0,
        haul=None,
    )
    current = float(ls.get_multiplier(key))
    m = ls.nudge_multiplier(
        key,
        "up",
        step=float(getattr(settings, "LEARNING_STEP_UP_ON_LOST", 0.25)),
        min_mul=float(getattr(settings, "LEARNING_MIN_MUL", 0.8)),
        max_mul=float(getattr(settings, "LEARNING_MAX_MUL", 2.5)),
    )
    add_learning_change(key, old=current, new=m, direction="up", loss_pct=1.0)
    if verbose:
        LOG.warning("[RallyTracker] %s timed out without return → multiplier up to %.3f", key, m)
