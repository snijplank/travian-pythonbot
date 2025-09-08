# core/report_checker.py
from __future__ import annotations
import json, time, re
from pathlib import Path
from typing import Optional, Dict, Any
import logging

from core.learning_store import LearningStore
from core.metrics import add_learning_change
try:
    from config.config import settings
except Exception:
    class _Fallback:
        LEARNING_MIN_MUL = 0.8
        LEARNING_MAX_MUL = 2.5
        LEARNING_LOSS_THRESHOLD_LOW = 0.20
        LEARNING_LOSS_THRESHOLD_HIGH = 0.50
        LEARNING_STEP_UP_ON_LOST = 0.25
        LEARNING_STEP_UP_ON_HIGH_LOSS = 0.10
        LEARNING_STEP_DOWN_ON_LOW_LOSS = 0.10
    settings = _Fallback()

PENDING_PATH = Path("database/learning/pending.json")

def _load_pending() -> list[dict]:
    if not PENDING_PATH.exists():
        return []
    try:
        return json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []

def _save_pending(p: list[dict]) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PENDING_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PENDING_PATH)

def _parse_loss_pct_from_report_html(html: str) -> Optional[float]:
    """
    Best-effort: zoek een tabel met 'Troepen'/'Units' met 2 kolommen: gestuurd / overlevend.
    We pakken de ESCORT-eenheden bij elkaar en schatten verlies% (0..1).
    Als niet te parsen: None.
    """
    try:
        # Pak alle getallenparen "X / Y" (gestuurd/over); kies hoogste X als escort-schatting
        pairs = re.findall(r"(\d+)\s*/\s*(\d+)", html)
        if not pairs:
            return None
        sent_total = 0
        survived_total = 0
        for s, r in pairs:
            s, r = int(s), int(r)
            # Heuristiek: vermijd Held/katapult/etc door alleen rijen met s>0 mee te nemen
            if s > 0:
                sent_total += s
                survived_total += r
        if sent_total <= 0:
            return None
        loss_pct = max(0.0, min(1.0, (sent_total - survived_total) / sent_total))
        return loss_pct
    except Exception:
        return None

def _parse_bounty_from_report_html(html: str) -> Optional[dict]:
    """Parse bounty/resources looted from a report detail HTML.

    Looks for the Bounty row in the additionalInformation table and extracts
    wood, clay, iron, crop amounts in that order.
    Returns a dict {wood, clay, iron, crop, total} or None if not found.
    """
    try:
        # Remove bi-directional marks (unicode and HTML entities)
        clean = html.replace("\u202d", "").replace("\u202c", "")
        clean = clean.replace("&#x202d;", "").replace("&#x202c;", "")
        import re as _re
        # Narrow to a block containing 'Bounty'
        mtbl = _re.search(r"<table[^>]*additionalInformation[^>]*>[\s\S]*?</table>", clean, _re.IGNORECASE)
        block = mtbl.group(0) if mtbl else clean
        if "Bounty" not in block and "bounty" not in block.lower():
            return None
        # Find the row with <th>Bounty</th> and capture numbers in the following cells
        mrow = _re.search(r"<tr[\s\S]*?<th[^>]*>\s*Bounty\s*</th>([\s\S]*?)</tr>", block, _re.IGNORECASE)
        row = mrow.group(1) if mrow else block
        # Extract the first four integers which correspond to wood, clay, iron, crop
        nums = _re.findall(r">\s*([0-9]{1,7})\s*<", row)
        if len(nums) < 4:
            # Try a more lenient approach: pull digit groups from text between <td>..</td>
            cells = _re.findall(r"<td[^>]*>([\s\S]*?)</td>", row, _re.IGNORECASE)
            nums = []
            for c in cells:
                k = _re.findall(r"([0-9]{1,7})", c)
                nums.extend(k)
        if len(nums) >= 4:
            w, c, i, r = [int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3])]
            tot = w + c + i + r
            return {"wood": w, "clay": c, "iron": i, "crop": r, "total": tot}
    except Exception:
        pass
    return None

def _extract_result_from_report_html(html: str) -> Optional[str]:
    """
    Herken 'won'/'lost' op basis van bekende labels/klassen in report.
    """
    text = html
    low = text.lower()
    if any(k in low for k in ("victory", "overwinning", "gewonnen", "won")):
        return "won"
    if any(k in low for k in ("defeat", "nederlaag", "verloren", "lost")):
        return "lost"
    return None

def process_ready_pendings(api, interval_sec: int = 60) -> None:
    """
    Poll periodiek: voor pendings met ts_sent + buffer, haal nieuwste report voor
    die oase op en update LearningStore.
    """
    try:
        from config.config import settings as _cfg
    except Exception:
        class _CfgFallback:
            LEARNING_ENABLE = True
        _cfg = _CfgFallback()
    if not bool(getattr(_cfg, 'LEARNING_ENABLE', True)):
        # Learning disabled → do nothing
        if verbose:
            print("[RC] Learning disabled; skipping.")
        return 0
    ls = LearningStore()
    while True:
        pendings = _load_pending()
        changed = False

        now_ts = time.time()
        keep: list[dict] = []
        for item in pendings:
            # Generic key per target (coords like "(x,y)"), supports legacy 'oasis' field
            key = item.get("target") or item.get("oasis")            # "(x,y)"
            code = item.get("unit", "mixed")   # "t1" etc
            base = int(item.get("recommended", item.get("sent", 0)) or 0)
            sent = int(item.get("sent", 0) or 0)
            ts_sent = item.get("ts_sent")      # ISO
            # Wacht een minimale periode (configureerbaar) na verzending voordat we zoeken
            ts_epoch = item.get("_epoch", None)
            if ts_epoch is None:
                # voeg epoch toe de eerste keer
                try:
                    item["_epoch"] = now_ts
                except Exception:
                    pass
                keep.append(item)
                continue
            age = now_ts - float(ts_epoch)
            min_wait = float(getattr(settings, "REPORT_MIN_WAIT_SEC", 60.0))
            if age < min_wait:
                logging.debug(f"[ReportChecker] Pending {item.get('oasis')} too fresh (age={age:.0f}s < {min_wait:.0f}s)")
                keep.append(item)
                continue

            # Zoek recent rapport voor deze oase
            try:
                x, y = eval(key) if isinstance(key, str) else (None, None)
            except Exception:
                x = y = None

            report = api.find_latest_report_by_coords(x, y)
            if not report:
                # nog niet beschikbaar, later opnieuw proberen
                logging.info(f"[ReportChecker] No report found yet for {key}; keep pending (age={age:.0f}s)")
                keep.append(item)
                continue

            html = report.get("html", "")
            result = _extract_result_from_report_html(html) or "won"
            loss_pct = _parse_loss_pct_from_report_html(html)
            ls.record_attempt(key, code, recommended=base, sent=sent, result=result, loss_pct=loss_pct)

            # Nudge multiplier op basis van verlies
            current = float(ls.get_multiplier(key))
            if result == "lost":
                m = ls.nudge_multiplier(
                    key,
                    "up",
                    step=float(getattr(settings, "LEARNING_STEP_UP_ON_LOST", 0.25)),
                    min_mul=float(getattr(settings, "LEARNING_MIN_MUL", 0.8)),
                    max_mul=float(getattr(settings, "LEARNING_MAX_MUL", 2.5)),
                )
                add_learning_change(key, old=current, new=m, direction="up", loss_pct=1.0)
                logging.info(f"[ReportChecker] {key} lost → mul ↑ to {m}")
            elif loss_pct is not None:
                low = float(getattr(settings, "LEARNING_LOSS_THRESHOLD_LOW", 0.20))
                high = float(getattr(settings, "LEARNING_LOSS_THRESHOLD_HIGH", 0.50))
                if loss_pct <= low:
                    m = ls.nudge_multiplier(
                        key,
                        "down",
                        step=float(getattr(settings, "LEARNING_STEP_DOWN_ON_LOW_LOSS", 0.10)),
                        min_mul=float(getattr(settings, "LEARNING_MIN_MUL", 0.8)),
                        max_mul=float(getattr(settings, "LEARNING_MAX_MUL", 2.5)),
                    )
                    add_learning_change(key, old=current, new=m, direction="down", loss_pct=loss_pct)
                    logging.info(f"[ReportChecker] {key} won with {loss_pct:.0%} losses → mul ↓ to {m}")
                elif loss_pct > high:
                    m = ls.nudge_multiplier(
                        key,
                        "up",
                        step=float(getattr(settings, "LEARNING_STEP_UP_ON_HIGH_LOSS", 0.10)),
                        min_mul=float(getattr(settings, "LEARNING_MIN_MUL", 0.8)),
                        max_mul=float(getattr(settings, "LEARNING_MAX_MUL", 2.5)),
                    )
                    add_learning_change(key, old=current, new=m, direction="up", loss_pct=loss_pct)
                    logging.info(f"[ReportChecker] {key} won with {loss_pct:.0%} losses → mul ↑ to {m}")
                else:
                    logging.info(f"[ReportChecker] {key} won with {loss_pct:.0%} losses → mul stays")

            changed = True
            # niet opnieuw bewaren → processed

        if changed:
            _save_pending(keep)

        time.sleep(interval_sec)


def process_ready_pendings_once(api, verbose: bool = False) -> int:
    """Verwerk alle pendings éénmaal (geen polling-loop). Retourneert aantal verwerkte items.

    Als verbose=True, print voortgang per pending (handig via Tools-menu).
    """
    try:
        from config.config import settings as _cfg
    except Exception:
        class _CfgFallback:
            LEARNING_ENABLE = True
        _cfg = _CfgFallback()
    if not bool(getattr(_cfg, 'LEARNING_ENABLE', True)):
        if verbose:
            print("[RC] Learning disabled; skipping.")
        return 0
    ls = LearningStore()
    pendings = _load_pending()
    changed = False
    processed = 0

    now_ts = time.time()
    keep: list[dict] = []
    # Optional early-exit if geen ongelezen reports in navbar
    try:
        from config.config import settings as _cfg
        if bool(getattr(_cfg, 'REPORT_USE_INDICATOR', True)):
            unread = int(api.get_unread_report_count())
            if verbose:
                print(f"[RC] Unread reports indicator: {unread}")
            if unread <= 0 and pendings:
                if verbose:
                    print("[RC] No unread reports; skipping scan this pass.")
                return 0
    except Exception:
        pass
    if verbose:
        print(f"[RC] Starting report processing: {len(pendings)} pending item(s)…")
    for item in pendings:
        # Generic key per target (coords like "(x,y)")
        key = item.get("target") or item.get("oasis")            # "(x,y)"
        code = item.get("unit", "mixed")   # "t1" etc
        base = int(item.get("recommended", item.get("sent", 0)) or 0)
        sent = int(item.get("sent", 0) or 0)
        ts_epoch = item.get("_epoch")
        if ts_epoch is None:
            try:
                item["_epoch"] = now_ts
            except Exception:
                pass
            keep.append(item)
            continue
        age = now_ts - float(ts_epoch)
        min_wait = float(getattr(settings, "REPORT_MIN_WAIT_SEC", 60.0))
        if age < min_wait:
            if verbose:
                print(f"[RC] {key}: too fresh (age={age:.0f}s < {min_wait:.0f}s), keep")
            keep.append(item)
            continue

        try:
            x, y = eval(key) if isinstance(key, str) else (None, None)
        except Exception:
            x = y = None

        report = api.find_latest_report_by_coords(x, y)
        if not report:
            keep.append(item)
            if verbose:
                print(f"[RC] {key}: no report found yet, keep (age={age:.0f}s)")
            continue

        html = report.get("html", "")
        result = _extract_result_from_report_html(html) or "won"
        loss_pct = _parse_loss_pct_from_report_html(html)
        haul = _parse_bounty_from_report_html(html)
        ls.record_attempt(key, code, recommended=base, sent=sent, result=result, loss_pct=loss_pct, haul=haul)

        current = float(ls.get_multiplier(key))
        if result == "lost":
            m = ls.nudge_multiplier(
                key,
                "up",
                step=float(getattr(settings, "LEARNING_STEP_UP_ON_LOST", 0.25)),
                min_mul=float(getattr(settings, "LEARNING_MIN_MUL", 0.8)),
                max_mul=float(getattr(settings, "LEARNING_MAX_MUL", 2.5)),
            )
            add_learning_change(key, old=current, new=m, direction="up", loss_pct=1.0)
            if verbose:
                print(f"[RC] {key}: LOST → mul {current:.2f} → {m:.2f}")
        elif loss_pct is not None:
            low = float(getattr(settings, "LEARNING_LOSS_THRESHOLD_LOW", 0.20))
            high = float(getattr(settings, "LEARNING_LOSS_THRESHOLD_HIGH", 0.50))
            if loss_pct <= low:
                m = ls.nudge_multiplier(
                    key,
                    "down",
                    step=float(getattr(settings, "LEARNING_STEP_DOWN_ON_LOW_LOSS", 0.10)),
                    min_mul=float(getattr(settings, "LEARNING_MIN_MUL", 0.8)),
                    max_mul=float(getattr(settings, "LEARNING_MAX_MUL", 2.5)),
                )
                add_learning_change(key, old=current, new=m, direction="down", loss_pct=loss_pct)
                if verbose:
                    print(f"[RC] {key}: WON ({loss_pct:.0%} losses) → mul {current:.2f} → {m:.2f}")
            elif loss_pct > high:
                m = ls.nudge_multiplier(
                    key,
                    "up",
                    step=float(getattr(settings, "LEARNING_STEP_UP_ON_HIGH_LOSS", 0.10)),
                    min_mul=float(getattr(settings, "LEARNING_MIN_MUL", 0.8)),
                    max_mul=float(getattr(settings, "LEARNING_MAX_MUL", 2.5)),
                )
                add_learning_change(key, old=current, new=m, direction="up", loss_pct=loss_pct)
                if verbose:
                    print(f"[RC] {key}: WON ({loss_pct:.0%} losses) → mul {current:.2f} → {m:.2f}")
            else:
                if verbose:
                    print(f"[RC] {key}: WON (loss unknown) → mul stays {current:.2f}")
        else:
            if verbose:
                print(f"[RC] {key}: WON (loss unknown) → mul stays {current:.2f}")
        processed += 1
        changed = True

    if changed:
        _save_pending(keep)
    if verbose:
        print(f"[RC] Done. Processed {processed} pending(s). Remaining: {len(keep)}")
    return processed
