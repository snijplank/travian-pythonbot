"""Resource router: skim overflowed resources via marketplace between own villages."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup

from features.build.resource_balancer import RESOURCE_TYPES

try:
    from config.config import settings
except Exception:  # pragma: no cover - fallback when config import fails
    class _Cfg:
        RESOURCE_ROUTER_ENABLE = False
        RESOURCE_ROUTER_OVERFLOW_THRESHOLD = 0.95
        RESOURCE_ROUTER_TARGET_RATIO = 0.8
        RESOURCE_ROUTER_COOLDOWN_SEC = 900
        RESOURCE_ROUTER_MIN_TRANSFER = 600
        RESOURCE_ROUTER_MAX_BATCHES = 1

    settings = _Cfg()

from identity_handling.identity_helper import load_villages_from_identity
from features.build import resource_balancer as balancer


LOG = logging.getLogger("travian")
STATE_PATH = Path("database/resource_fields/router_state.json")


@dataclass
class VillageState:
    village_id: int
    name: str
    x: int
    y: int
    resources: dict[str, int]
    capacities: dict[str, int]
    merchants_total: int
    merchants_available: int
    merchant_capacity: int

    def free_capacity(self, resource: str, target_ratio: float) -> int:
        cap = int(self.capacities.get(resource, 0) or 0)
        current = int(self.resources.get(resource, 0) or 0)
        if cap <= 0:
            return 0
        target = int(cap * target_ratio)
        return max(0, target - current)


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception as exc:  # pragma: no cover - defensive logging only
        LOG.debug("[ResourceRouter] Failed to persist state: %s", exc)


def _extract_int(text: str | None) -> int:
    if not text:
        return 0
    m = re.search(r"(-?\d+)", text)
    return int(m.group(1)) if m else 0


def _parse_market_data(html: str, soup: BeautifulSoup) -> tuple[int, int, int]:
    available = 0
    total = 0
    capacity = 0

    for pattern in (r"\"availableMerchants\"\s*:\s*(\d+)", r"availableMerchants\s*=\s*(\d+)"):
        m = re.search(pattern, html)
        if m:
            available = int(m.group(1))
            break

    for pattern in (r"\"totalMerchants\"\s*:\s*(\d+)", r"totalMerchants\s*=\s*(\d+)"):
        m = re.search(pattern, html)
        if m:
            total = int(m.group(1))
            break

    for pattern in (
        r"\"merchantCapacity\"\s*:\s*(\d+)",
        r"capacityPerMerchant\s*[:=]\s*(\d+)",
    ):
        m = re.search(pattern, html)
        if m:
            capacity = int(m.group(1))
            break

    if not available:
        node = soup.find(id="merchantsAvailable") or soup.find(class_=re.compile("merchantsAvailable"))
        if node:
            available = _extract_int(node.get_text(" ", strip=True))

    if not total:
        node = soup.find(id="merchantsTotal") or soup.find(class_=re.compile("merchantsTotal"))
        if node:
            total = _extract_int(node.get_text(" ", strip=True))

    if not capacity:
        text_node = soup.find(string=re.compile("per merchant", re.IGNORECASE))
        if text_node:
            capacity = _extract_int(str(text_node))

    capacity = max(0, capacity)
    available = max(0, available)
    total = max(available, total)
    return available, total, capacity


def _fetch_market_page(api):
    res = api.session.get(f"{api.server_url}/build.php?gid=17")
    res.raise_for_status()
    html = res.text
    soup = BeautifulSoup(html, "html.parser")
    return (*_parse_market_data(html, soup), soup, html)


def _send_resources(api, target_x: int, target_y: int, send_amounts: dict[str, int]) -> bool:
    try:
        available, _total, merchant_capacity, soup, _html = _fetch_market_page(api)
    except Exception as exc:
        LOG.debug("[ResourceRouter] Marketplace fetch failed: %s", exc)
        return False

    if available <= 0:
        LOG.debug("[ResourceRouter] Geen kooplieden beschikbaar.")
        return False
    if merchant_capacity <= 0:
        LOG.debug("[ResourceRouter] Onbekende merchant-capaciteit; afgebroken.")
        return False

    send_form = None
    for form in soup.find_all("form"):
        if form.find("input", {"name": "r1"}):
            send_form = form
            break
    if send_form is None:
        LOG.debug("[ResourceRouter] Marketplace send-form niet gevonden.")
        return False

    extra_payload = {
        "x": str(target_x),
        "y": str(target_y),
        "r1": str(send_amounts.get("wood", 0)),
        "r2": str(send_amounts.get("clay", 0)),
        "r3": str(send_amounts.get("iron", 0)),
        "r4": str(send_amounts.get("crop", 0)),
    }

    action = send_form.get("action") or "build.php?gid=17"
    if not action.startswith("http"):
        if not action.startswith("/"):
            action = "/" + action
        action = f"{api.server_url}{action}"
    method = (send_form.get("method") or "POST").upper()

    payload: dict[str, str] = {}
    for inp in send_form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        value = inp.get("value") or ""
        if inp.get("type") == "checkbox" and not inp.has_attr("checked"):
            continue
        payload[name] = value
    payload.update(extra_payload)

    try:
        first = api.session.post(action, data=payload) if method == "POST" else api.session.get(action, params=payload)
        first.raise_for_status()
    except Exception as exc:
        LOG.debug("[ResourceRouter] Marktpre-send mislukt: %s", exc)
        return False

    confirm_soup = BeautifulSoup(first.text, "html.parser")
    confirm_form = None
    for form in confirm_soup.find_all("form"):
        button = form.find("button") or form.find("input", {"type": "submit"})
        if not button:
            continue
        if any(name in (button.get("name") or "") for name in ("send", "deliver", "submit")):
            confirm_form = form
            break
        if form.find("input", {"name": "send"}):
            confirm_form = form
            break

    if confirm_form is None:
        if "class=\"error" in first.text:
            LOG.debug("[ResourceRouter] Marktconfirmatie gaf error.")
            return False
        # If no confirmation form, success might already be final (older servers)
        LOG.info("[ResourceRouter] Resources verstuurd zonder bevestigingsstap.")
        return True

    confirm_payload: dict[str, str] = {}
    for inp in confirm_form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        value = inp.get("value") or ""
        if inp.get("type") == "checkbox" and not inp.has_attr("checked"):
            continue
        confirm_payload[name] = value

    confirm_action = confirm_form.get("action") or "build.php?gid=17"
    if not confirm_action.startswith("http"):
        if not confirm_action.startswith("/"):
            confirm_action = "/" + confirm_action
        confirm_action = f"{api.server_url}{confirm_action}"

    try:
        second = api.session.post(confirm_action, data=confirm_payload)
        second.raise_for_status()
    except Exception as exc:
        LOG.debug("[ResourceRouter] Marktconfirmatie faalde: %s", exc)
        return False

    if "class=\"error" in second.text.lower():
        LOG.debug("[ResourceRouter] Markttransactie gaf foutmelding.")
        return False

    LOG.info(
        "[ResourceRouter] Resources verzonden naar (%s,%s): wood=%s clay=%s iron=%s crop=%s",
        target_x,
        target_y,
        send_amounts.get("wood", 0),
        send_amounts.get("clay", 0),
        send_amounts.get("iron", 0),
        send_amounts.get("crop", 0),
    )
    return True


def _gather_village_states(api) -> list[VillageState]:
    villages = load_villages_from_identity()
    states: list[VillageState] = []
    for village in villages:
        village_id = int(village.get("village_id", 0) or 0)
        if village_id <= 0:
            continue
        try:
            api.switch_village(village_id)
        except Exception:
            continue
        try:
            resources, capacities, _queue = balancer._load_village_state(api)
            available, total, capacity, _soup, _html = _fetch_market_page(api)
        except Exception as exc:
            LOG.debug("[ResourceRouter] Kon staat niet ophalen voor dorp %s: %s", village_id, exc)
            continue
        states.append(
            VillageState(
                village_id=village_id,
                name=str(village.get("village_name") or village_id),
                x=int(village.get("x", 0)),
                y=int(village.get("y", 0)),
                resources=resources,
                capacities=capacities,
                merchants_total=total,
                merchants_available=available,
                merchant_capacity=capacity,
            )
        )
    return states


def _total_overflow(state: VillageState, threshold: float) -> int:
    overflow = 0
    for rtype in RESOURCE_TYPES:
        cap = int(state.capacities.get(rtype, 0) or 0)
        cur = int(state.resources.get(rtype, 0) or 0)
        if cap <= 0:
            continue
        limit = int(cap * threshold)
        overflow += max(0, cur - limit)
    return overflow


def _find_best_target(source: VillageState, states: Iterable[VillageState], resource: str, target_ratio: float) -> tuple[VillageState, int] | tuple[None, int]:
    best = None
    best_capacity = 0
    for candidate in states:
        if candidate.village_id == source.village_id:
            continue
        free = candidate.free_capacity(resource, target_ratio)
        if free > best_capacity:
            best = candidate
            best_capacity = free
    return (best, best_capacity) if best else (None, 0)


def run_resource_router_cycle(api) -> list[tuple[str, bool, str, str]]:
    enable = bool(getattr(settings, "RESOURCE_ROUTER_ENABLE", False))
    if not enable:
        return []

    try:
        threshold = float(getattr(settings, "RESOURCE_ROUTER_OVERFLOW_THRESHOLD", 0.95))
    except Exception:
        threshold = 0.95
    threshold = max(0.5, min(threshold, 0.99))

    try:
        target_ratio = float(getattr(settings, "RESOURCE_ROUTER_TARGET_RATIO", threshold - 0.1))
    except Exception:
        target_ratio = threshold - 0.1
    target_ratio = max(0.35, min(target_ratio, threshold - 0.01))

    try:
        cooldown = int(getattr(settings, "RESOURCE_ROUTER_COOLDOWN_SEC", 900))
    except Exception:
        cooldown = 900
    cooldown = max(0, cooldown)

    try:
        min_transfer = int(getattr(settings, "RESOURCE_ROUTER_MIN_TRANSFER", 600))
    except Exception:
        min_transfer = 600
    min_transfer = max(1, min_transfer)

    try:
        max_batches = int(getattr(settings, "RESOURCE_ROUTER_MAX_BATCHES", 1))
    except Exception:
        max_batches = 1
    max_batches = max(0, max_batches)

    states = _gather_village_states(api)
    if not states:
        return []

    def _status_tuple(state: VillageState) -> tuple[bool, str]:
        parts = []
        for rtype in RESOURCE_TYPES:
            cap = int(state.capacities.get(rtype, 0) or 0)
            cur = int(state.resources.get(rtype, 0) or 0)
            pct = int(round((cur / cap) * 100)) if cap > 0 else 0
            parts.append(f"{rtype}:{cur}/{cap} ({pct}%)")
        status = " | ".join(parts)
        is_over = any(
            cap > 0 and cur > int(cap * threshold)
            for rtype in RESOURCE_TYPES
            for cap, cur in [(int(state.capacities.get(rtype, 0) or 0), int(state.resources.get(rtype, 0) or 0))]
        )
        return is_over, status

    status_results: list[tuple[str, bool, str, str]] = []
    for st in states:
        over, status = _status_tuple(st)
        icon = "🔺" if over else "ℹ️"
        status_results.append((str(st.village_id), over, f"{icon} {status}", st.name))

    state_map = {state.village_id: state for state in states}
    router_state = _load_state()
    now = time.time()
    results: list[tuple[str, bool, str, str]] = []

    sources = sorted(states, key=lambda st: _total_overflow(st, threshold), reverse=True)

    for source in sources:
        batches_used = 0
        merchant_capacity = source.merchant_capacity or 0
        if merchant_capacity <= 0:
            continue
        if source.merchants_available <= 0:
            continue
        for rtype in RESOURCE_TYPES:
            if batches_used >= max_batches or source.merchants_available <= 0:
                break
            cap = int(source.capacities.get(rtype, 0) or 0)
            if cap <= 0:
                continue
            cur = int(source.resources.get(rtype, 0) or 0)
            overflow_limit = int(cap * threshold)
            overflow_amount = max(0, cur - overflow_limit)
            if overflow_amount < min_transfer:
                continue

            target, free_capacity = _find_best_target(source, states, rtype, target_ratio)
            if not target or free_capacity < min_transfer:
                continue

            amount = min(overflow_amount, free_capacity, cur)
            max_possible = source.merchants_available * merchant_capacity
            amount = min(amount, max_possible)
            if amount < min_transfer:
                continue

            merchants_needed = math.ceil(amount / merchant_capacity)
            if merchants_needed <= 0:
                continue

            key = f"{source.village_id}->{target.village_id}:{rtype}"
            last_ts = float(router_state.get(key, 0.0) or 0.0)
            if cooldown > 0 and (now - last_ts) < cooldown:
                continue

            send_amounts = {res: 0 for res in RESOURCE_TYPES}
            send_amounts[rtype] = amount
            success = _send_resources(api, target.x, target.y, send_amounts)
            if success:
                router_state[key] = time.time()
                source.resources[rtype] -= amount
                target.resources[rtype] += amount
                source.merchants_available -= merchants_needed
                batches_used += 1
                results.append((
                    str(source.village_id),
                    True,
                    f"[{rtype}] {amount} verstuurd naar {target.name}",
                    source.name,
                ))
            else:
                results.append((
                    str(source.village_id),
                    False,
                    f"[{rtype}] Versturen naar {target.name} mislukt.",
                    source.name,
                ))

    if router_state:
        _save_state(router_state)
    return status_results + results
