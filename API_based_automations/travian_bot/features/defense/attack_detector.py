import time
import threading
import logging
import io
import random
from typing import Optional

try:
    import pyautogui  # type: ignore
    from PIL import Image  # type: ignore
except Exception:
    pyautogui = None  # type: ignore
    Image = None  # type: ignore


def _load_easyocr(langs: list[str], use_gpu: bool):
    try:
        import easyocr  # type: ignore
        reader = easyocr.Reader(langs, gpu=use_gpu)
        return reader
    except Exception as e:
        logging.warning(f"[AttackDetector] easyocr not available: {e}")
        return None


def _send_discord(webhook_url: str, message: str, image: Optional["Image.Image"]) -> bool:
    import requests, json
    try:
        if image is not None and Image is not None:
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            buf.seek(0)
            files = {"file": ("screenshot.png", buf, "image/png")}
            data = {"payload_json": json.dumps({"content": message})}
            res = requests.post(webhook_url, data=data, files=files, timeout=20)
        else:
            res = requests.post(webhook_url, json={"content": message}, timeout=20)
        ok = res.status_code in (200, 204)
        if not ok:
            logging.warning(f"[AttackDetector] Discord post failed: {res.status_code} {getattr(res,'text','')[:200]}")
        return ok
    except Exception as e:
        logging.warning(f"[AttackDetector] Discord post error: {e}")
        return False


def _maybe_press_hotkeys(use_hotkeys: bool, refresh_combo: str | None) -> None:
    if not use_hotkeys or pyautogui is None:
        return
    try:
        # Support combos like "ctrl+r", "command+r", or "f5"
        combo = (refresh_combo or "").strip().lower()
        if not combo:
            return
        if "+" in combo:
            keys = [k.strip() for k in combo.split("+") if k.strip()]
            pyautogui.hotkey(*keys)
        else:
            pyautogui.press(combo)
    except Exception:
        pass


def _text_has_attack_markers(texts: list[str]) -> bool:
    if not texts:
        return False
    low = " ".join(texts).lower()
    markers_any = [
        "incoming attack",
        "attack incoming",
        "incoming",
        "attack",
        "aanval",
        "angriff",
        "eingehend",
    ]
    return any(m in low for m in markers_any)


def run_attack_detector_thread(config) -> threading.Thread:
    """Start a daemon thread that monitors the screen for 'incoming attack' and notifies Discord.

    Config keys expected on `config` (settings):
    - ATTACK_DETECTOR_ENABLE: bool
    - ATTACK_DETECTOR_DISCORD_WEBHOOK: str
    - ATTACK_DETECTOR_INTERVAL_BASE: float
    - ATTACK_DETECTOR_INTERVAL_JITTER: float
    - ATTACK_DETECTOR_COOLDOWN_SEC: int
    - ATTACK_DETECTOR_OCR_LANGS: list[str]
    - ATTACK_DETECTOR_GPU: bool
    - ATTACK_DETECTOR_SEND_SCREENSHOT: bool
    - ATTACK_DETECTOR_USE_HOTKEYS: bool
    - ATTACK_DETECTOR_REFRESH_COMBO: str (e.g., "f5" or "ctrl+r" or "command+r")
    """

    def _worker():
        if not getattr(config, "ATTACK_DETECTOR_ENABLE", False):
            logging.info("[AttackDetector] disabled by config")
            return
        webhook = getattr(config, "ATTACK_DETECTOR_DISCORD_WEBHOOK", "")
        if not webhook:
            logging.warning("[AttackDetector] no webhook configured; stopping thread")
            return
        if pyautogui is None or Image is None:
            logging.warning("[AttackDetector] pyautogui/PIL not available; stopping thread")
            return

        base = float(getattr(config, "ATTACK_DETECTOR_INTERVAL_BASE", 2.0))
        jit = float(getattr(config, "ATTACK_DETECTOR_INTERVAL_JITTER", 6.0))
        cooldown = int(getattr(config, "ATTACK_DETECTOR_COOLDOWN_SEC", 3600))
        langs = getattr(config, "ATTACK_DETECTOR_OCR_LANGS", ["en"]) or ["en"]
        use_gpu = bool(getattr(config, "ATTACK_DETECTOR_GPU", False))
        send_shot = bool(getattr(config, "ATTACK_DETECTOR_SEND_SCREENSHOT", True))
        use_hotkeys = bool(getattr(config, "ATTACK_DETECTOR_USE_HOTKEYS", False))
        refresh_combo = getattr(config, "ATTACK_DETECTOR_REFRESH_COMBO", "")

        reader = _load_easyocr(langs, use_gpu)
        if reader is None:
            logging.warning("[AttackDetector] OCR disabled (easyocr not loaded)")
            return

        logging.info("[AttackDetector] started")
        last_sent = 0.0
        while True:
            try:
                time.sleep(base + random.random() * max(0.0, jit))
                _maybe_press_hotkeys(use_hotkeys, refresh_combo)
                # small wait after refresh
                if use_hotkeys:
                    time.sleep(1.0 + random.random() * 2.0)

                shot = pyautogui.screenshot() if pyautogui else None
                if shot is None:
                    continue
                # OCR
                try:
                    results = reader.readtext(shot)
                    texts = [t for (_b, t, _c) in results]
                except Exception:
                    texts = []

                if _text_has_attack_markers(texts):
                    now = time.time()
                    if now - last_sent >= cooldown:
                        ok = _send_discord(webhook, "⚠️ Travian: incoming attack detected", shot if send_shot else None)
                        if ok:
                            last_sent = now
                            logging.info("[AttackDetector] Discord notification sent")
                        else:
                            logging.warning("[AttackDetector] Discord notification failed")
                # else: peaceful; no-op
            except Exception:
                # keep running; log lightly to avoid spam
                time.sleep(1.0)

    th = threading.Thread(target=_worker, name="AttackDetector", daemon=True)
    th.start()
    return th

