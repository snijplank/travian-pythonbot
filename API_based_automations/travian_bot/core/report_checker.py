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
    ls = LearningStore()
    while True:
        pendings = _load_pending()
        changed = False

        now_ts = time.time()
        keep: list[dict] = []
        for item in pendings:
            key = item.get("oasis")            # "(x,y)"
            code = item.get("unit", "mixed")   # "t1" etc
            base = int(item.get("recommended", item.get("sent", 0)) or 0)
            sent = int(item.get("sent", 0) or 0)
            ts_sent = item.get("ts_sent")      # ISO
            # Wacht minstens 5 minuten na verzending voordat we zoeken
            ts_epoch = item.get("_epoch", None)
            if ts_epoch is None:
                # voeg epoch toe de eerste keer
                try:
                    item["_epoch"] = now_ts
                except Exception:
                    pass
                keep.append(item)
                continue
            if now_ts - float(ts_epoch) < 300:
                keep.append(item)
                continue

            # Zoek recent rapport voor deze oase
            try:
                x, y = eval(key) if isinstance(key, str) else (None, None)
            except Exception:
                x = y = None

            report = api.find_latest_oasis_report(x, y)
            if not report:
                # nog niet beschikbaar, later opnieuw proberen
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
