import requests
import re
import random
import time
from bs4 import BeautifulSoup
from analysis.animal_to_power_mapping import get_animal_power
from core.unit_catalog import resolve_unit_base_name, resolve_label_u
from typing import Optional
import logging
from pathlib import Path


class TravianAPI:
    def __init__(self, session: requests.Session, server_url: str):
        self.session = session
        # Normalize server URL to avoid '//path' issues when concatenating endpoints
        sanitized_url = (server_url or "").strip()
        self.server_url = sanitized_url.rstrip('/') if sanitized_url else sanitized_url
        # Humanizer state
        try:
            from config.config import settings as _cfg
            self._human_min = max(0.0, float(getattr(_cfg, 'HUMAN_MIN_DELAY', 0.6)))
            self._human_max = max(self._human_min, float(getattr(_cfg, 'HUMAN_MAX_DELAY', 2.2)))
            self._human_every = max(1, int(getattr(_cfg, 'HUMAN_LONG_PAUSE_EVERY', 7)))
            self._human_long_min = max(0.0, float(getattr(_cfg, 'HUMAN_LONG_PAUSE_MIN', 3.0)))
            self._human_long_max = max(self._human_long_min, float(getattr(_cfg, 'HUMAN_LONG_PAUSE_MAX', 6.0)))
            self._human_long_prob = max(0.0, min(1.0, float(getattr(_cfg, 'HUMAN_LONG_PAUSE_PROB', 0.12))))
            self._idle_prob = max(0.0, min(1.0, float(getattr(_cfg, 'HUMAN_IDLE_LOOKAROUND_PROB', 0.25))))
            self._idle_min_interval = max(15.0, float(getattr(_cfg, 'HUMAN_IDLE_MIN_INTERVAL', 45.0)))
            self._idle_max_interval = max(self._idle_min_interval, float(getattr(_cfg, 'HUMAN_IDLE_MAX_INTERVAL', 180.0)))
            self._idle_jitter_min = max(0.0, float(getattr(_cfg, 'HUMAN_IDLE_JITTER_MIN', 0.4)))
            self._idle_jitter_max = max(self._idle_jitter_min, float(getattr(_cfg, 'HUMAN_IDLE_JITTER_MAX', 1.3)))
            self._suspicion_sleep_min = max(0.0, float(getattr(_cfg, 'HUMAN_SUSPICION_SLEEP_MIN', 90.0)))
            self._suspicion_sleep_max = max(self._suspicion_sleep_min, float(getattr(_cfg, 'HUMAN_SUSPICION_SLEEP_MAX', 240.0)))
            pages = getattr(_cfg, 'HUMAN_IDLE_LOOKAROUND_PAGES', None)
            if isinstance(pages, (list, tuple)):
                self._idle_pages = [str(p).strip() for p in pages if str(p).strip()]
            else:
                self._idle_pages = []
            self._x_version = str(getattr(_cfg, 'TRAVIAN_X_VERSION', '') or '').strip()
        except Exception:
            self._human_min, self._human_max = 0.6, 2.2
            self._human_every, self._human_long_min, self._human_long_max = 7, 3.0, 6.0
            self._human_long_prob = 0.12
            self._idle_prob = 0.25
            self._idle_min_interval, self._idle_max_interval = 45.0, 180.0
            self._idle_jitter_min, self._idle_jitter_max = 0.4, 1.3
            self._suspicion_sleep_min, self._suspicion_sleep_max = 90.0, 240.0
            self._idle_pages = []
            self._x_version = ''
        if not self._idle_pages:
            self._idle_pages = [
                "/dorf1.php",
                "/dorf2.php",
                "/report",
                "/messages",
                "/statistics/general",
                "/statistics/player",
                "/hero/adventures",
            ]
        self._idle_next_ts = time.time()  # schedule immediately to seed interval
        self._req_counter = 0
        # Set polite default headers if missing
        try:
            self.session.headers.setdefault('Accept', 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8')
            self.session.headers.setdefault('Accept-Language', 'nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7')
            self.session.headers.setdefault('Connection', 'keep-alive')
            self.session.headers.setdefault(
                'User-Agent',
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
            )
            self.session.headers.setdefault('Accept-Encoding', 'gzip, deflate, br, zstd')
        except Exception:
            pass
        # Wrap session.request to inject human-like delays globally
        self._raw_request = self.session.request

        def _human_request(method, url, **kwargs):
            import random, time as _t
            try:
                self._req_counter += 1
                # Short think time before each request
                _t.sleep(random.uniform(self._human_min, self._human_max))
                # Occasionally take a longer pause
                if self._req_counter % self._human_every == 0 or random.random() < self._human_long_prob:
                    _t.sleep(random.uniform(self._human_long_min, self._human_long_max))
            except Exception:
                pass
            resp = self._raw_request(method, url, **kwargs)
            try:
                self._monitor_response(resp)
            except Exception:
                pass
            try:
                self._maybe_idle_browse()
            except Exception:
                pass
            return resp

        try:
            self._human_request = _human_request  # type: ignore[attr-defined]
            self.session.request = self._human_request  # type: ignore
        except Exception:
            pass

        self._schedule_next_idle()

        # Ensure Travian X-Version header is present: use config override or auto-detect
        try:
            if self._x_version:
                try:
                    self.session.headers["X-Version"] = self._x_version
                except Exception:
                    pass
            else:
                v = self._detect_and_apply_x_version()
                if v:
                    self._x_version = v
        except Exception:
            pass

    def set_humanizer(self, enabled: bool) -> None:
        """Enable/disable the humanizer wrapper around session.request at runtime."""
        try:
            if enabled:
                if hasattr(self, "_human_request"):
                    self.session.request = getattr(self, "_human_request")  # type: ignore
                    self._schedule_next_idle()
            else:
                if hasattr(self, "_raw_request"):
                    self.session.request = getattr(self, "_raw_request")  # type: ignore
        except Exception:
            pass

    # --- Humanizer helpers ---
    def _schedule_next_idle(self) -> None:
        try:
            interval = random.uniform(self._idle_min_interval, self._idle_max_interval)
            self._idle_next_ts = time.time() + interval
        except Exception:
            self._idle_next_ts = float("inf")

    def _maybe_idle_browse(self) -> None:
        if not self._idle_pages or self._idle_prob <= 0:
            return
        now = time.time()
        if now < getattr(self, "_idle_next_ts", 0.0):
            return
        try:
            roll = random.random()
        except Exception:
            roll = 1.0
        # Regardless of outcome, schedule the next check to avoid tight loops
        self._schedule_next_idle()
        if roll > self._idle_prob:
            return
        try:
            page = random.choice(self._idle_pages)
        except Exception:
            return
        if not page:
            return
        try:
            delay = random.uniform(self._idle_jitter_min, self._idle_jitter_max)
        except Exception:
            delay = 0.6
        if delay > 0:
            time.sleep(delay)
        try:
            url = f"{self.server_url.rstrip('/')}/{page.lstrip('/')}"
            resp = self._raw_request("GET", url, timeout=15)
            try:
                self._monitor_response(resp)
            except Exception:
                pass
            logging.info("[Humanizer] Idle lookaround → %s (status %s)", page, getattr(resp, "status_code", "?"))
        except Exception as exc:
            logging.info("[Humanizer] Idle lookaround failed for %s: %s", page, exc)

    def _monitor_response(self, response: Optional[requests.Response]) -> None:
        if response is None:
            return
        suspicious_reasons: list[str] = []
        suspicious_details: list[str] = []
        try:
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and status in (403, 429, 503):
                suspicious_reasons.append(f"status {status}")
                suspicious_details.append(f"status={status}")
        except Exception:
            pass
        headers = getattr(response, "headers", {}) or {}
        try:
            token_pattern = re.compile(r"\b(captcha|bot|verify|attention)\b", re.IGNORECASE)
            for key, value in headers.items():
                text = f"{key}:{value}"
                if token_pattern.search(text):
                    suspicious_reasons.append(f"header {key}")
                    suspicious_details.append(f"{key}={value}")
        except Exception:
            pass
        try:
            set_cookie = headers.get("Set-Cookie", "")
            if isinstance(set_cookie, str) and "captcha" in set_cookie.lower():
                suspicious_reasons.append("Set-Cookie captcha")
                suspicious_details.append(f"Set-Cookie={set_cookie}")
        except Exception:
            pass
        try:
            cookies = getattr(response, "cookies", None)
            if cookies is not None:
                for cookie in cookies:
                    try:
                        name = getattr(cookie, "name", "")
                        if name and "captcha" in str(name).lower():
                            suspicious_reasons.append(f"cookie {name}")
                            suspicious_details.append(f"cookie {name}={getattr(cookie, 'value', '')}")
                    except Exception:
                        continue
        except Exception:
            pass
        if suspicious_reasons:
            detail_txt = ""
            if suspicious_details:
                detail_txt = " Details: " + "; ".join(suspicious_details)
            logging.warning(
                "[Humanizer] Suspicion trigger detected (%s). Initiating cool-down.%s",
                ", ".join(suspicious_reasons),
                detail_txt,
            )
            self._handle_suspicion()

    def _handle_suspicion(self) -> None:
        try:
            sleep_min = getattr(self, "_suspicion_sleep_min", 90.0)
            sleep_max = getattr(self, "_suspicion_sleep_max", max(sleep_min, 240.0))
            cooldown = random.uniform(sleep_min, sleep_max)
        except Exception:
            cooldown = 120.0
        logging.info("[Humanizer] Cooling down for %.1f seconds due to suspicion signal.", cooldown)
        if cooldown > 0:
            time.sleep(cooldown)
        self._schedule_next_idle()

    def get_player_info(self):
        payload = {
            "query": """
                query {
                    ownPlayer {
                        currentVillageId
                        villages {
                            id
                            sortIndex
                            name
                            tribeId
                            hasHarbour
                        }
                        farmLists {
                            id
                            name
                            ownerVillage {
                                id
                            }
                        }
                    }
                }
            """
        }
        response = self.session.post(
            f"{self.server_url}/api/v1/graphql",
            json=payload,
            headers=self._headers_json_api("/dorf1.php"),
        )
        response.raise_for_status()
        return response.json()["data"]["ownPlayer"]

    # --- X-Version detection from HTML ---
    def _parse_versions_from_html(self, html: str) -> list[str]:
        try:
            versions: set[str] = set()
            for m in re.finditer(r"gpack/(\d+\.\d+)/", html):
                versions.add(m.group(1))
            for m in re.finditer(r"/js/[A-Za-z\-/]+\.js\?(\d+\.\d+)", html):
                versions.add(m.group(1))
            return sorted(versions, key=lambda s: [int(p) for p in s.split('.')])
        except Exception:
            return []

    def _detect_and_apply_x_version(self) -> str | None:
        """Fetch a lightweight page and derive Travian build version (X-Version) from HTML.

        Strategy:
        - Check /dorf1.php; if not, try /hero/adventures, /tasks
        - Look for gpack/<ver>/ and js?...<ver> patterns
        Applies header globally on the session if found.
        """
        if getattr(self, "_x_version", ""):
            # Already set (e.g., from config)
            try:
                self.session.headers["X-Version"] = self._x_version
            except Exception:
                pass
            return self._x_version
        pages = ["/dorf1.php", "/hero/adventures", "/tasks", "/dorf2.php"]
        html = ""
        for p in pages:
            try:
                r = self.session.get(f"{self.server_url.rstrip('/')}{p}")
                if getattr(r, "ok", False):
                    html = getattr(r, "text", "") or ""
                    cand = self._parse_versions_from_html(html)
                    if cand:
                        ver = cand[-1]  # highest
                        try:
                            self.session.headers["X-Version"] = ver
                        except Exception:
                            pass
                        return ver
            except Exception:
                continue
        return None

    # --- Progressive Tasks & Rewards ---
    def get_hero_level(self) -> int | None:
        """Fetch hero level from HUD endpoint (fast JSON)."""
        try:
            res = self.session.get(
                f"{self.server_url}/api/v1/hero/dataForHUD",
                headers=self._headers_ajax("/hero"),
            )
            res.raise_for_status()
            j = res.json() or {}
            lvl = j.get("level")
            return int(lvl) if lvl is not None else None
        except Exception:
            return None

    def refresh_hero_hud(self) -> None:
        """Trigger a refresh of the HUD JSON (optional after collect)."""
        try:
            _ = self.session.get(
                f"{self.server_url}/api/v1/hero/dataForHUD",
                headers=self._headers_ajax("/hero"),
            )
        except Exception:
            pass

    def switch_village(self, village_id: int | str) -> None:
        """Switch current village context (affects tasks scoped to settledVillage)."""
        try:
            vid = str(village_id)
            self.session.get(f"{self.server_url}/dorf1.php?newdid={vid}")
            try:
                # Track current village id for APIs that may require it (e.g., tasks in spawnVillage scope)
                self._current_village_id = int(village_id)
            except Exception:
                self._current_village_id = None
        except Exception:
            pass

    def list_collectible_progressive_tasks(self) -> list[dict]:
        """Parse /tasks page and return a list of JSON payloads for collectReward.

        Returns a list of dicts suitable for POST /api/v1/progressive-tasks/collectReward
        Each item aims to include: questType, scope, targetLevel, heroLevel, buildingId (optional)
        """
        import re, json as _json
        payloads: list[dict] = []
        try:
            res = self.session.get(f"{self.server_url}/tasks")
            res.raise_for_status()
            html = getattr(res, "text", "") or ""
            # Save for debugging
            try:
                Path("logs").mkdir(parents=True, exist_ok=True)
                (Path("logs")/"tasks_page.html").write_text(html, encoding="utf-8")
            except Exception:
                pass
            # 1) Prefer parsing the React bootstrap JSON (robust across skins)
            #    We extract the first argument object from window.Travian.React.Tasks.render({ ... })
            if True:
                try:
                    marker = "window.Travian.React.Tasks.render"
                    idx = html.find(marker)
                    if idx != -1:
                        # Find opening brace of first argument
                        s = html.find("(", idx)
                        s = html.find("{", s)
                        # Walk and balance braces
                        depth = 0
                        e = s
                        while e < len(html):
                            ch = html[e]
                            if ch == '{':
                                depth += 1
                            elif ch == '}':
                                depth -= 1
                                if depth == 0:
                                    e += 1
                                    break
                            e += 1
                        blob = html[s:e]
                        # The first argument passed to React often uses unquoted top‑level keys
                        # (tasksData, activeTab, heroData), which is valid JS but not strict JSON.
                        # Quote those keys before json.loads to avoid parse errors while leaving
                        # the already JSON‑like nested structures untouched.
                        try:
                            fixed = blob
                            for _k in ("tasksData", "activeTab", "heroData"):
                                fixed = re.sub(rf'([{{,]\s*){_k}\s*:', r'\1"' + _k + r'":', fixed)
                            data = _json.loads(fixed)
                        except Exception:
                            # Fallback to original (in case the page already used strict JSON)
                            data = _json.loads(blob)
                        tasksData = data.get("tasksData") or {}
                        # Merge generalTasks and activeVillageTasks
                        def _iter_tasks(td):
                            for k in ("generalTasks", "activeVillageTasks"):
                                arr = td.get(k) or []
                                for t in arr:
                                    yield t
                        for t in _iter_tasks(tasksData):
                            qtype = t.get("type")
                            scope = t.get("scope")
                            meta = t.get("metadata") or {}
                            bld = meta.get("buildingId")
                            for lvl in (t.get("levels") or []):
                                if lvl.get("readyToBeCollected") and not lvl.get("wasCollected"):
                                    item = {
                                        "questType": qtype,
                                        "scope": scope,
                                        # Some endpoints expect 'level' (ordinal) while UI shows 'levelValue'.
                                        # Include both so the collector can try alternatives.
                                        "targetLevel": lvl.get("levelValue"),
                                        "level": lvl.get("level"),
                                    }
                                    qid = lvl.get("questId")
                                    if qid:
                                        item["questId"] = qid
                                    if bld is not None:
                                        item["buildingId"] = bld
                                    payloads.append(item)
                except Exception:
                    pass

            # 2) If React JSON parse failed or found nothing, try inline JSON-like fragments
            if not payloads:
                candidates = re.findall(r"\{[^{}]*?\"questType\"\s*:\s*\"[^\"]+\"[^{}]*?\}", html)
                for c in candidates:
                    try:
                        clean = re.sub(r",\s*([}\]])", r"\1", c)
                        j = _json.loads(clean)
                    except Exception:
                        def _grab(name, num=False):
                            m = re.search(rf'\"{name}\"\s*:\s*(\"([^\"]+)\"|(\d+))', c)
                            if not m:
                                return None
                            if num:
                                try:
                                    return int(m.group(3) or m.group(2))
                                except Exception:
                                    return None
                            return m.group(2)
                        j = {
                            "questType": _grab("questType"),
                            "scope": _grab("scope"),
                            "targetLevel": _grab("targetLevel", num=True),
                            "heroLevel": _grab("heroLevel", num=True),
                            "buildingId": _grab("buildingId", num=True),
                        }
                    if not j or not j.get("questType") or not j.get("scope"):
                        continue
                    if j.get("targetLevel") is None and j.get("buildingId") is None:
                        continue
                    payloads.append({k: v for k, v in j.items() if v is not None})

            # 3) Finally, look for attribute-based hints on buttons/links
            if not payloads:
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(html, "html.parser")
                    btns = soup.find_all(["button", "a"], attrs={"data-action": re.compile("collect", re.I)})
                    for b in btns:
                        q = b.get("data-quest-type") or b.get("data-questType") or b.get("questType")
                        sc = b.get("data-scope") or b.get("scope")
                        tl = b.get("data-target-level") or b.get("data-targetLevel") or b.get("targetLevel")
                        bi = b.get("data-building-id") or b.get("data-buildingId") or b.get("buildingId")
                        if not q or not sc:
                            continue
                        item = {"questType": q, "scope": sc}
                        try:
                            if tl is not None:
                                item["targetLevel"] = int(tl)
                        except Exception:
                            pass
                        try:
                            if bi is not None:
                                item["buildingId"] = int(bi)
                        except Exception:
                            pass
                        payloads.append(item)
                except Exception:
                    pass
        except Exception:
            return []

        # Ensure heroLevel is populated, as server may require it for bonus calculation
        if payloads:
            lvl = self.get_hero_level()
            if lvl is not None:
                for p in payloads:
                    p.setdefault("heroLevel", int(lvl))
        return payloads

    def collect_progressive_reward(self, payload: dict) -> dict | None:
        """POST to collect reward. Returns JSON or None on failure.

        Expected payload keys typically include:
          questType, scope, targetLevel, heroLevel, buildingId (optional)
        """
        # Respect site headers per HAR (align strictly with browser)
        from urllib.parse import urlsplit
        parts = urlsplit(self.server_url)
        origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else self.server_url
        headers = self._headers_json_api("/tasks?t=1")
        url = f"{self.server_url.rstrip('/')}/api/v1/progressive-tasks/collectReward"

        def _try_send(js: dict) -> dict | None:
            try:
                r = self.session.post(url, json=js, headers=headers)
                r.raise_for_status()
                return r.json()
            except Exception as _e:
                logging.warning(f"[Tasks] collectReward failed for {js}: {_e}")
                try:
                    Path("logs").mkdir(parents=True, exist_ok=True)
                    body = None
                    try:
                        body = getattr(getattr(_e, 'response', None), 'text', None)
                    except Exception:
                        body = None
                    (Path("logs")/"collect_reward_error.txt").write_text(
                        f"Payload: {js}\nError: {_e}\nResponse: {body}\n", encoding="utf-8"
                    )
                except Exception:
                    pass
                return None

        # Build a sequence of attempts. First: strict body exactly as observed in HAR
        # { questType, scope, targetLevel, heroLevel, buildingId }
        attempts: list[dict] = []
        base = dict(payload)
        # Derive questId from hints when absent (e.g., allResourceBuildingsHaveLevel_1_3)
        try:
            if not base.get("questId"):
                _qt = base.get("questType")
                _b = base.get("buildingId")
                _lv = base.get("level") or base.get("targetLevel")
                if _qt and _b is not None and _lv is not None:
                    base["questId"] = f"{_qt}_{int(_b)}_{int(_lv)}"
        except Exception:
            pass
        # If scope is village‑bound and we track it, add scopeVillageId hint
        try:
            if base.get("scope") and base.get("scope") != "account":
                vid = getattr(self, "_current_village_id", None)
                if isinstance(vid, int):
                    # Do NOT include in the first strict attempt; only in fallbacks below
                    pass
        except Exception:
            pass

        # Fill hero level if missing
        if "heroLevel" not in base or base.get("heroLevel") is None:
            lvl = self.get_hero_level()
            if lvl is not None:
                base["heroLevel"] = int(lvl)

        strict = {
            k: base[k]
            for k in ("questType", "scope", "targetLevel", "heroLevel", "buildingId")
            if k in base and base[k] is not None
        }
        attempts.append(strict)

        # Alt 1: use ordinal 'level' instead of 'targetLevel'
        if base.get("level") is not None:
            alt = {
                k: base[k]
                for k in ("questType", "scope", "level", "heroLevel", "buildingId")
                if k in base and base[k] is not None
            }
            attempts.append(alt)

        # Alt 2: minimal questId only
        if base.get("questId"):
            attempts.append({"questId": base["questId"]})
            # questId + scopeVillageId
            vid = getattr(self, "_current_village_id", None)
            if isinstance(vid, int):
                attempts.append({"questId": base["questId"], "scopeVillageId": vid})
            # questId + questType/scope
            attempts.append({k: base[k] for k in ("questId", "questType", "scope") if k in base})

        # Alt 3: remove buildingId if present (server may infer it)
        if base.get("buildingId") is not None:
            alt = dict(base)
            alt.pop("buildingId", None)
            attempts.append(alt)

        # Alt 4: add scopeVillageId to strict
        vid = getattr(self, "_current_village_id", None)
        if isinstance(vid, int):
            alt = dict(strict)
            alt["scopeVillageId"] = vid
            attempts.append(alt)

        # Deduplicate attempts while preserving order
        seen = set()
        uniq: list[dict] = []
        for a in attempts:
            key = tuple(sorted(a.items()))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(a)

        for js in uniq:
            res = _try_send(js)
            if res and (res.get("success") is True or res.get("status") in ("ok", True)):
                return res
            # Fallback: some servers accept PUT for questId-only variants
            try:
                if set(js.keys()) in ({"questId"}, {"questId", "scopeVillageId"}):
                    r = self.session.put(url, json=js, headers=headers)
                    if getattr(r, "ok", False):
                        j = r.json() or {}
                        if j and (j.get("success") is True or j.get("status") in ("ok", True)):
                            return j
                        # log body for diagnostics
                        try:
                            Path("logs").mkdir(parents=True, exist_ok=True)
                            (Path("logs")/"collect_reward_error.txt").write_text(
                                f"Payload(put): {js}\nStatus: {getattr(r,'status_code',None)}\nBody: {getattr(r,'text',None)}\n", encoding="utf-8"
                            )
                        except Exception:
                            pass
            except Exception:
                pass
        return None

    def get_village_farm_lists(self, village_id: int) -> list:
        """Get farm lists from the rally point page."""
        payload = {
            "query": """
                query {
                    ownPlayer {
                        farmLists {
                            id
                            name
                            slotsAmount
                            runningRaidsAmount
                            lastStartedTime
                            ownerVillage {
                                id
                            }
                        }
                    }
                }
            """
        }
        response = self.session.post(
            f"{self.server_url}/api/v1/graphql",
            json=payload,
            headers=self._headers_json_api("/dorf1.php"),
        )
        response.raise_for_status()
        
        data = response.json()
        if "data" in data and "ownPlayer" in data["data"] and "farmLists" in data["data"]["ownPlayer"]:
            # Filter farm lists to only those belonging to the specified village
            return [fl for fl in data["data"]["ownPlayer"]["farmLists"] 
                   if fl["ownerVillage"]["id"] == village_id]
        return []

    def get_farm_list_details(self, farm_list_id: int) -> dict:
        payload = {
            "query": """
                query($id: Int!, $onlyExpanded: Boolean) {
                    farmList(id: $id) {
                        id
                        name
                        slotsAmount
                        runningRaidsAmount
                        slots(onlyExpanded: $onlyExpanded) {
                            id
                            target {
                                id
                                mapId
                                x
                                y
                                name
                                type
                                population
                            }
                            troop {
                                t1 t2 t3 t4 t5 t6 t7 t8 t9 t10
                            }
                        }
                    }
                }
            """,
            "variables": {
                "id": farm_list_id,
                "onlyExpanded": False
            }
        }
        response = self.session.post(
            f"{self.server_url}/api/v1/graphql",
            json=payload,
            headers=self._headers_json_api("/dorf1.php"),
        )
        response.raise_for_status()
        return response.json()["data"]["farmList"]

    def get_oasis_info(self, x: int, y: int) -> dict:
        """Get complete information about an oasis in a single API call.
        
        Returns a dictionary containing:
        - is_occupied: bool
        - title: str
        - animals: list of (name, count) tuples
        - total_animal_count: int
        - attack_power: int
        """
        url = f"{self.server_url}/api/v1/map/tile-details"
        payload = {"x": x, "y": y}
        response = self.session.post(url, json=payload, headers=self._headers_json_api("/karte.php"))
        response.raise_for_status()
        data = response.json()
        html = data.get("html")
        if not html:
            return {
                "is_occupied": False,
                "title": "",
                "animals": [],
                "total_animal_count": 0,
                "attack_power": 0
            }

        soup = BeautifulSoup(html, "html.parser")
        
        # Get title and occupation status
        title_tag = soup.find("h1")
        title = title_tag.text.strip() if title_tag else ""
        is_occupied = not title.lower().startswith("unoccupied oasis")
        
        # Get animal information
        animals = []
        total_count = 0
        troop_table = soup.find("table", id="troop_info")
        if troop_table:
            for row in troop_table.find_all("tr"):
                img = row.find("img")
                cols = row.find_all("td")
                if img and len(cols) >= 2:
                    animal_name = img.get("alt", "").strip().lower()
                    count_text = cols[1].get_text(strip=True).replace("\u202d", "").replace("\u202c", "")
                    try:
                        count = int(count_text)
                        animals.append((animal_name, count))
                        total_count += count
                    except ValueError:
                        continue
        
        # Calculate attack power
        attack_power = sum(get_animal_power(name) * count for name, count in animals)
        
        return {
            "is_occupied": is_occupied,
            "title": title,
            "animals": animals,
            "total_animal_count": total_count,
            "attack_power": attack_power
        }

    # --- Low-level JSON endpoints: troop send ---
    def _headers_ajax(self, referer_path: str | None = None) -> dict:
        """Headers for AJAX/JSON GETs (no Content-Type).

        Adds Accept, X-Requested-With, Origin, optional Referer, and X-Version.
        """
        from urllib.parse import urlsplit
        parts = urlsplit(self.server_url)
        origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else self.server_url
        h = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": origin,
        }
        if referer_path:
            if not referer_path.startswith("/"):
                referer_path = "/" + referer_path
            h["Referer"] = f"{self.server_url.rstrip('/')}{referer_path}"
        if getattr(self, "_x_version", ""):
            h["X-Version"] = self._x_version
        return h
    def _headers_json_api(self, referer_path: str | None = None) -> dict:
        """Headers aligned with HAR-captured JSON calls.

        Includes X-Requested-With and optional X-Version (from config if set).
        """
        from urllib.parse import urlsplit
        parts = urlsplit(self.server_url)
        origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else self.server_url
        h = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": origin,
        }
        if referer_path:
            if not referer_path.startswith("/"):
                referer_path = "/" + referer_path
            h["Referer"] = f"{self.server_url.rstrip('/')}{referer_path}"
        if getattr(self, "_x_version", ""):
            h["X-Version"] = self._x_version
        return h

    def troops_send(self, payload: dict, method: str = "POST", referer_path: str | None = None) -> dict:
        """Generic wrapper for /api/v1/troop/send.

        - method: "POST" or "PUT" (both observed to work similarly)
        - payload: dict, e.g. {"action":"troopsSend","targetMapId":110407,"eventType":50,"troops":[{"t11":1}]}
        Returns parsed JSON (raises for HTTP errors). Caller may validate semantics.
        """
        url = f"{self.server_url.rstrip('/')}/api/v1/troop/send"
        m = method.upper().strip()
        res = self.session.request(m, url, json=payload, headers=self._headers_json_api(referer_path))
        res.raise_for_status()
        try:
            return res.json() or {}
        except Exception:
            return {}

    def send_hero_to_adventure(self, target_map_id: int, method: str = "POST") -> dict:
        """Send the hero to an Adventure tile via JSON API.

        Inputs
        - target_map_id: adventure tile mapId
        - method: POST or PUT

        Returns
        - { ok: bool, eta: int|None, start: int|None, arrive: int|None, coords: (x,y)|None, raw: dict }
        """
        body = {
            "action": "troopsSend",
            "targetMapId": int(target_map_id),
            "eventType": 50,  # 50 = Adventure
            "troops": [ { "t11": 1 } ],
        }
        j = self.troops_send(body, method=method, referer_path="/hero/adventures")

        # Validation (best-effort): ensure hero only and correct eventType
        ok = False
        try:
            troops = (j.get("troops") or [])
            first = troops[0] if troops else {}
            ok = (int(first.get("t11", 0)) == 1) and (int(first.get("eventType", 0)) == 50)
        except Exception:
            ok = False

        # Extract timing and coordinates when available
        eta = None
        start = None
        arrive = None
        coords = None
        try:
            first = ((j.get("troops") or [None])[0]) or {}
            eta = first.get("arrivalIn")
            start = first.get("timeStart")
            arrive = first.get("timeArrive")
        except Exception:
            pass
        try:
            t = j.get("target") or {}
            c = t.get("coordinates") or {}
            if "x" in c and "y" in c:
                coords = (int(c["x"]), int(c["y"]))
        except Exception:
            pass
        return {"ok": bool(ok), "eta": eta, "start": start, "arrive": arrive, "coords": coords, "raw": j}

    def get_oasis_animal_count(self, x: int, y: int) -> int:
        """Get total count of animals in an oasis."""
        url = f"{self.server_url}/api/v1/map/tile-details"
        payload = {"x": x, "y": y}
        response = self.session.post(url, json=payload, headers=self._headers_json_api("/karte.php"))
        response.raise_for_status()
        data = response.json()
        html = data.get("html")
        if not html:
            return 0

        soup = BeautifulSoup(html, "html.parser")
        troop_table = soup.find("table", id="troop_info")
        if not troop_table:
            return 0

        animal_count = 0
        for row in troop_table.find_all("tr"):
            cols = row.find_all("td")
            if len(cols) >= 2:
                count_text = cols[1].get_text(strip=True).replace("\u202d", "").replace("\u202c", "")
                try:
                    count = int(count_text)
                    animal_count += count
                except ValueError:
                    continue
        return animal_count

    # --- Hero Adventures (HTML-based parsing) ---
    def list_hero_adventures(self) -> list[dict]:
        """Parse the hero adventures page and return a list of available adventures.

        Each returned dict aims to include:
        - id: best-effort adventure id (if found)
        - duration_min: estimated duration in minutes (int or None)
        - is_dangerous: whether the adventure is marked as dangerous (bool or None)
        - form_action: the form action URL to start the adventure
        - form_method: POST/GET (default POST)
        - form_inputs: dict of hidden + other input fields required to submit

        The parser is resilient across Travian variants; when fields cannot be
        extracted reliably, it still attempts to capture a usable form payload
        and dumps the page to logs for debugging.
        """
        try:
            res = self.session.get(f"{self.server_url}/hero/adventures")
            res.raise_for_status()
        except Exception as e:
            logging.error(f"[HeroAdv] Failed to open adventures page: {e}")
            return []

        html = getattr(res, "text", "") or ""
        try:
            Path("logs").mkdir(parents=True, exist_ok=True)
            (Path("logs")/"hero_adventures_page.html").write_text(html, encoding="utf-8")
        except Exception:
            pass

        soup = BeautifulSoup(html, "html.parser")

        adventures: list[dict] = []

        # 0) Try to parse GraphQL stub embedded in the page (most reliable in React UI)
        try:
            import re, json as _json
            scripts = soup.find_all("script")
            for sc in scripts:
                s = sc.string or sc.get_text("\n", strip=False) or ""
                if "Travian.React.HeroAdventure.render" in s and "viewData" in s:
                    # Extract viewData JSON object
                    m = re.search(r"viewData:\s*(\{.*?\})\s*,\s*activePerspective", s, re.DOTALL)
                    if not m:
                        continue
                    jtxt = m.group(1)
                    # Fix possible trailing commas/newlines by a lenient attempt
                    # Remove newlines and JS-specific trailing commas in simple cases
                    jtxt = re.sub(r",\s*([}\]])", r"\1", jtxt)
                    data = _json.loads(jtxt)
                    hero = (((data or {}).get("data", {}) or {}).get("ownPlayer", {}) or {}).get("hero", {}) or {}
                    advs = hero.get("adventures", []) or []
                    for a in advs:
                        # travelingDuration is in seconds in the snippet; convert to minutes
                        dur_sec = a.get("travelingDuration")
                        dmin = int(round(float(dur_sec)/60.0)) if isinstance(dur_sec, (int, float)) else None
                        diff = a.get("difficulty")
                        is_danger = bool(diff >= 4) if isinstance(diff, (int, float)) else None
                        adventures.append({
                            "id": a.get("mapId"),
                            "duration_min": dmin,
                            "is_dangerous": is_danger,
                            "form_action": None,   # no form; will use GraphQL fallback
                            "form_method": "POST",
                            "form_inputs": {},
                            "gql_map_id": a.get("mapId"),
                        })
                    # If we found GraphQL data, prefer it and skip further parsing
                    if adventures:
                        return adventures
        except Exception:
            pass

        # Travian variants present adventure rows as forms or within a table/list with a submit button.
        # Strategy: find forms whose action contains 'adventure' and that include a submit button.
        forms = []
        try:
            for form in soup.find_all("form"):
                action = (form.get("action") or "").lower()
                if "adventure" in action:
                    # must have at least one submit/button
                    if form.find("button") or form.find("input", {"type": "submit"}):
                        forms.append(form)
        except Exception:
            forms = []

        # Fallback: sometimes adventures are within rows with data attributes and a single global form.
        # We'll still try to collect any button/link that triggers an adventure and reconstruct a pseudo-form.
        buttons = []
        try:
            for b in soup.find_all(["button", "a"]):
                txt = (b.get_text(" ", strip=True) or "").lower()
                # broad heuristics across locales (adventure related)
                if any(k in txt for k in ("adventure", "advent", "avontuur", "abenteuer", "aventura")):
                    buttons.append(b)
        except Exception:
            buttons = []

        def _parse_duration_minutes(container) -> int | None:
            try:
                txt = container.get_text(" ", strip=True)
            except Exception:
                txt = ""
            if not txt:
                return None
            # Patterns like '1:23:00', '1:23 h', '75 min', '75m'
            import re
            m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", txt)
            if m:
                h = int(m.group(1))
                mi = int(m.group(2))
                s = int(m.group(3)) if m.group(3) else 0
                return h*60 + mi + (1 if s>=30 else 0)
            m = re.search(r"(\d+)\s*(?:min|m)\b", txt, re.IGNORECASE)
            if m:
                return int(m.group(1))
            m = re.search(r"(\d+)\s*(?:h|uur|std)\b", txt, re.IGNORECASE)
            if m:
                return int(m.group(1)) * 60
            return None

        def _is_dangerous(container) -> bool | None:
            try:
                classes = " ".join(container.get("class", []))
                if any(k in classes.lower() for k in ("danger", "dangerous")):
                    return True
            except Exception:
                pass
            try:
                txt = container.get_text(" ", strip=True).lower()
                if any(k in txt for k in ("danger", "gevaar", "gefähr", "peligro")):
                    return True
            except Exception:
                pass
            return None

        def _collect_form_payload(form) -> dict:
            payload = {}
            try:
                for inp in form.find_all("input"):
                    name = inp.get("name")
                    if not name:
                        continue
                    val = inp.get("value") or ""
                    payload[name] = val
                # also include selected option values if any
                for sel in form.find_all("select"):
                    name = sel.get("name")
                    if not name:
                        continue
                    opt = sel.find("option", selected=True) or sel.find("option")
                    if opt:
                        payload[name] = opt.get("value") or opt.get_text(strip=True)
            except Exception:
                pass
            return payload

        # Parse form-based adventures (most reliable)
        for f in forms:
            try:
                action = f.get("action") or "/hero/adventures"
                method = (f.get("method") or "POST").upper()
                payload = _collect_form_payload(f)
                # Best effort ID
                adv_id = payload.get("adventureId") or payload.get("aId") or payload.get("id")
                # Duration/danger from nearest row/container
                container = f
                for parent in [f, f.parent, f.parent.parent]:
                    if not parent:
                        continue
                    dmin = _parse_duration_minutes(parent)
                    dang = _is_dangerous(parent)
                    if dmin is not None or dang is not None:
                        adventures.append({
                            "id": adv_id,
                            "duration_min": dmin,
                            "is_dangerous": bool(dang) if dang is not None else None,
                            "form_action": action,
                            "form_method": method,
                            "form_inputs": payload,
                        })
                        break
                else:
                    adventures.append({
                        "id": adv_id,
                        "duration_min": None,
                        "is_dangerous": None,
                        "form_action": action,
                        "form_method": method,
                        "form_inputs": payload,
                    })
            except Exception:
                continue

        # Fallback: button/link-based adventures when forms are not cleanly exposed
        if not adventures and buttons:
            for b in buttons:
                try:
                    href = b.get("href")
                    if href and ("adventure" in href.lower()):
                        adventures.append({
                            "id": None,
                            "duration_min": _parse_duration_minutes(b) or None,
                            "is_dangerous": _is_dangerous(b),
                            "form_action": href,
                            "form_method": "GET",
                            "form_inputs": {},
                        })
                except Exception:
                    continue

        return adventures

    def start_hero_adventure(self, adventure: dict) -> bool:
        """Submit the adventure start form/link obtained from list_hero_adventures().

        Returns True if the response indicates success. On failures, dumps debug HTML.
        """
        if not adventure:
            return False
        action = adventure.get("form_action") or "/hero/adventures"
        method = str(adventure.get("form_method") or "POST").upper()
        inputs = adventure.get("form_inputs") or {}

        # Normalize action to absolute URL
        if not action.startswith("http"):
            if not action.startswith("/"):
                action = "/" + action
            action = f"{self.server_url}{action}"

        try:
            # Primary: if GraphQL map id is available, attempt known mutation names
            gql_map_id = adventure.get("gql_map_id") or adventure.get("id")
            if gql_map_id is not None:
                import json as _json
                gql_headers = self._headers_json_api("/hero/adventures")
                mutations = [
                    ("startAdventure",       "mutation($mapId:Int!){ startAdventure(mapId:$mapId){ __typename } }"),
                    ("heroStartAdventure",   "mutation($mapId:Int!){ heroStartAdventure(mapId:$mapId){ __typename } }"),
                    ("startHeroAdventure",   "mutation($mapId:Int!){ startHeroAdventure(mapId:$mapId){ __typename } }"),
                ]
                for op_name, q in mutations:
                    try:
                        res = self.session.post(
                            f"{self.server_url.rstrip('/')}/api/v1/graphql",
                            json={"query": q, "variables": {"mapId": int(gql_map_id)}},
                            headers=gql_headers,
                        )
                        if not getattr(res, "ok", False):
                            continue
                        j = {}
                        try:
                            j = res.json() or {}
                        except Exception:
                            j = {}
                        # Success if no errors and data contains a non-null field for this operation
                        if j.get("errors"):
                            continue
                        data = j.get("data") or {}
                        node = data.get(op_name)
                        if node is not None:
                            return True
                    except Exception:
                        continue

                # REST-ish fallbacks observed in some variants
                rest_paths = [
                    "/api/v1/hero/startAdventure",
                    "/api/v1/hero/adventures/start",
                    "/api/v1/hero-adventures/start",
                ]
                rest_body = {"mapId": int(gql_map_id)}
                for p in rest_paths:
                    try:
                        r = self.session.post(f"{self.server_url.rstrip('/')}{p}", json=rest_body, headers=gql_headers)
                        if getattr(r, "ok", False):
                            try:
                                jj = r.json() or {}
                            except Exception:
                                jj = {}
                            if not jj.get("errors") and (jj.get("success") is True or jj.get("status") in ("ok", True) or jj.get("data")):
                                return True
                    except Exception:
                        continue

            # Fallback: submit the form/link if present
            if method == "GET":
                res = self.session.get(action, params=inputs)
            else:
                res = self.session.post(action, data=inputs)
            ok = getattr(res, "ok", False)
            if not ok:
                raise Exception(f"HTTP {getattr(res, 'status_code', '?')}")

            text = getattr(res, "text", "") or ""
            low = text.lower()
            # Be more conservative: look for explicit error markers; generic word 'error' appears in scripts
            error_markers = (
                "not enough", "insufficient", "health too low", "adventure not available",
                "cannot start adventure", "already on adventure"
            )
            success = not any(k in low for k in error_markers)
            if not success:
                raise Exception("Adventure start likely failed (page reported an error)")
            return True
        except Exception as e:
            logging.error(f"[HeroAdv] Failed to start adventure: {e}")
            try:
                Path("logs").mkdir(parents=True, exist_ok=True)
                content = None
                if 'res' in locals():
                    try:
                        content = res.text
                    except Exception:
                        content = None
                # Also dump any GraphQL error payload captured in variable 'j' or 'jj'
                dump = ""
                try:
                    if content:
                        dump = content
                except Exception:
                    dump = ""
                (Path("logs")/"hero_adventure_start_error.html").write_text(dump or "", encoding="utf-8")
            except Exception:
                pass
            return False

    def prepare_oasis_attack(self, map_id: int, x: int, y: int, troop_setup: dict) -> dict:
        """Prepare an attack on a given oasis and return action and checksum."""

        def normalize_troops_dict(troops):
            """Convert 'uX' keys to correct 't1..t10' depending on tribe block.
            Travian forms use t1..t10 for the current village tribe slots, regardless of global unit id.
            """
            normalized = {}
            for key, value in troops.items():
                if not isinstance(value, int):
                    continue
                if key.startswith("t") and key[1:].isdigit():
                    n = int(key[1:])
                    # Accept local troop slots t1..t10 and hero slot t11 as-is
                    if 1 <= n <= 11:
                        normalized[f"t{n}"] = value
                    continue
                if key.startswith("u") and key[1:].isdigit():
                    n = int(key[1:])
                    # Map global unit ids to local troop slots 1..10
                    if 1 <= n <= 10:
                        slot = n
                    elif 11 <= n <= 20:
                        slot = n - 10
                    elif 21 <= n <= 30:
                        slot = n - 20
                    elif 31 <= n <= 40:
                        slot = n - 30
                    elif 41 <= n <= 50:
                        slot = n - 40
                    elif 61 <= n <= 70:
                        # T3-style / server-variant block where tribe units appear as 61..70
                        slot = n - 60
                    else:
                        continue
                    normalized[f"t{slot}"] = value
            return normalized

        troop_setup = normalize_troops_dict(troop_setup)

        # Early sanity checks to provide clearer errors (e.g., when trying to send escort with 0 troops)
        if sum(v for v in troop_setup.values() if isinstance(v, int)) <= 0:
            raise ValueError("Geen troepen geselecteerd om te versturen (escort/aanval). Voeg minimaal één eenheid toe aan de samenstelling.")

        url = f"{self.server_url}/build.php?gid=16&tt=2&eventType=4&targetMapId={map_id}"
        res = self.session.get(url)
        res.raise_for_status()

        prepare_data = {
            "villagename": "",
            "x": x,
            "y": y,
            "eventType": 4,
            "ok": "ok",
        }

        for troop_id in range(1, 12):
            prepare_data[f"troop[t{troop_id}]"] = troop_setup.get(f"t{troop_id}", 0)

        prepare_data["troop[scoutTarget]"] = ""
        prepare_data["troop[catapultTarget1]"] = ""
        prepare_data["troop[catapultTarget2]"] = ""

        troop_preparation_res = self.session.post(f"{self.server_url}/build.php?gid=16&tt=2", data=prepare_data)
        troop_preparation_res.raise_for_status()

        soup = BeautifulSoup(troop_preparation_res.text, "html.parser")
        travel_time_sec = self._extract_duration_seconds(troop_preparation_res, soup)

        # Accept multiple possible hidden token names across Travian variants
        token_input = soup.select_one('input[name="action"], input[name="a"], input[name="k"], input[name="c"]')
        if not token_input:
            # Dump HTML for debugging, and surface any validation messages to the user.
            try:
                Path("logs").mkdir(parents=True, exist_ok=True)
                (Path("logs")/"send_hero_form_dump.html").write_text(troop_preparation_res.text, encoding="utf-8")
            except Exception:
                pass
            page_errors = self._extract_page_errors(troop_preparation_res.text)
            hint = f" Mogelijke reden: {page_errors[0]}" if page_errors else ""
            raise Exception(f"[-] Geen action/CSRF token gevonden tijdens voorbereiden.{hint} (logs/send_hero_form_dump.html)")

        action_field = token_input.get("name", "action")
        action = token_input.get("value", "")

        button = soup.find("button", id="confirmSendTroops")
        if not button:
            try:
                Path("logs").mkdir(parents=True, exist_ok=True)
                (Path("logs")/"send_hero_form_dump.html").write_text(troop_preparation_res.text, encoding="utf-8")
            except Exception:
                pass
            page_errors = self._extract_page_errors(troop_preparation_res.text)
            # Heuristics: escort without enough troops often removes the confirm button and shows a validation error.
            escort_hint = ""
            if any("escort" in e.lower() or "begeleiding" in e.lower() for e in page_errors):
                escort_hint = " (mogelijk escort geselecteerd zonder voldoende troepen)"
            hint = f" Mogelijke reden: {page_errors[0]}{escort_hint}" if page_errors else " Controleer of er voldoende troepen zijn geselecteerd (ook bij escort)."
            raise Exception(f"[-] Bevestigingsknop niet gevonden tijdens voorbereiden.{hint} (logs/send_hero_form_dump.html)")
        onclick = button.get("onclick", "")
        checksum_match = re.search(r"value\s*=\s*'([a-f0-9]+)'", onclick)
        if not checksum_match:
            try:
                Path("logs").mkdir(parents=True, exist_ok=True)
                (Path("logs")/"send_hero_form_dump.html").write_text(troop_preparation_res.text, encoding="utf-8")
            except Exception:
                pass
            page_errors = self._extract_page_errors(troop_preparation_res.text)
            hint = f" Mogelijke reden: {page_errors[0]}" if page_errors else ""
            raise Exception(f"[-] Checksum niet gevonden in onclick tijdens voorbereiden.{hint} (logs/send_hero_form_dump.html)")
        checksum = checksum_match.group(1)

        return {
            "action_field": action_field,
            "action": action,
            "checksum": checksum,
            "travel_time_sec": travel_time_sec,
        }

    def confirm_oasis_attack(self, attack_info: dict, x: int, y: int, troops: dict, village_id: int) -> bool:
        """Confirm and send the final attack based on prepared action and checksum."""

        def normalize_troops_dict(troops):
            """Convert 'uX' keys to correct 't1..t10' depending on tribe block.
            Travian forms use t1..t10 for the current village tribe slots, regardless of global unit id.
            """
            normalized = {}
            for key, value in troops.items():
                if not isinstance(value, int):
                    continue
                if key.startswith("t") and key[1:].isdigit():
                    n = int(key[1:])
                    # Accept local troop slots t1..t10 and hero slot t11 as-is
                    if 1 <= n <= 11:
                        normalized[f"t{n}"] = value
                    continue
                if key.startswith("u") and key[1:].isdigit():
                    n = int(key[1:])
                    # Map global unit ids to local troop slots 1..10
                    if 1 <= n <= 10:
                        slot = n
                    elif 11 <= n <= 20:
                        slot = n - 10
                    elif 21 <= n <= 30:
                        slot = n - 20
                    elif 31 <= n <= 40:
                        slot = n - 30
                    elif 41 <= n <= 50:
                        slot = n - 40
                    elif 61 <= n <= 70:
                        # T3-style / server-variant block where tribe units appear as 61..70
                        slot = n - 60
                    else:
                        continue
                    normalized[f"t{slot}"] = value
            return normalized

        troops = normalize_troops_dict(troops)
        if sum(v for v in troops.values() if isinstance(v, int)) <= 0:
            raise ValueError("Geen troepen geselecteerd om te versturen (escort/aanval). Voeg minimaal één eenheid toe aan de samenstelling.")

        token_field = attack_info.get("action_field", "action")
        token_value = attack_info.get("action", "")
        final_payload = {
            token_field: token_value,
            "eventType": 4,
            "villagename": "",
            "x": x,
            "y": y,
            "redeployHero": "",
            "checksum": attack_info["checksum"],
        }

        for troop_id in range(1, 12):
            final_payload[f"troops[0][t{troop_id}]"] = troops.get(f"t{troop_id}", 0)

        final_payload["troops[0][scoutTarget]"] = ""
        final_payload["troops[0][catapultTarget1]"] = ""
        final_payload["troops[0][catapultTarget2]"] = ""
        final_payload["troops[0][villageId]"] = village_id

        res = self.session.post(
            f"{self.server_url}/build.php?gid=16&tt=2",
            data=final_payload,
            headers={"Referer": f"{self.server_url}/build.php?gid=16&tt=2"},
            allow_redirects=False,
        )
        res.raise_for_status()

        ok = (res.status_code == 302 and res.headers.get("Location") == "/build.php?gid=16&tt=1")
        if not ok:
            try:
                Path("logs").mkdir(parents=True, exist_ok=True)
                content = getattr(res, "text", "")
                (Path("logs")/"send_hero_post_dump.html").write_text(content or f"Status: {res.status_code}\nHeaders: {dict(res.headers)}", encoding="utf-8")
            except Exception:
                pass
            # Attempt to extract any errors from the response body when available
            page_errors = []
            try:
                page_errors = self._extract_page_errors(getattr(res, "text", "") or "")
            except Exception:
                pass
            if page_errors:
                logging.error(f"Verzenden mislukt. Mogelijke reden: {page_errors[0]}")
        return ok

    def _extract_duration_seconds(self, response, soup: Optional[BeautifulSoup] = None) -> Optional[float]:
        """Extract travel duration in seconds from the rally confirmation page."""
        try:
            html = getattr(response, "text", "") or ""
            if soup is None:
                soup = BeautifulSoup(html, "html.parser")

            # Preferred: row with Duration label and timer value attribute
            for th in soup.find_all("th"):
                label = th.get_text(strip=True)
                if not label:
                    continue
                if "duration" in label.lower():
                    row = th.parent
                    timer = row.find("span", attrs={"class": "timer"}) if row else None
                    if timer and timer.has_attr("value"):
                        return float(timer["value"])
                    if row:
                        span_value = row.find("span", attrs={"value": True})
                        if span_value and span_value.has_attr("value"):
                            return float(span_value["value"])
                        text = row.get_text(" ", strip=True)
                        secs = self._parse_hms_to_seconds(text)
                        if secs is not None:
                            return float(secs)

            # Fallback: regex on raw HTML
            match = re.search(r"Duration[^0-9]*([0-9]{1,2}:[0-9]{2}:[0-9]{2})", html, re.IGNORECASE)
            if match:
                h, m, s = match.group(1).split(":")
                return float(int(h) * 3600 + int(m) * 60 + int(s))
        except Exception:
            return None
        return None

    @staticmethod
    def _parse_hms_to_seconds(text: str) -> Optional[int]:
        text = text.replace("\u202d", "").replace("\u202c", "")
        match = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", text)
        if not match:
            return None
        h, m, s = map(int, match.groups())
        return h * 3600 + m * 60 + s

    

    def get_tile_html(self, x, y):
        url = f"{self.server_url}/api/v1/map/tile-details"
        res = self.session.post(url, json={"x": x, "y": y})
        res.raise_for_status()
        return res.json()["html"]

    def get_hero_return_eta(self) -> int | None:
        """Best-effort: parse rally point movements page to find hero mission remaining seconds.

        Returns the remaining seconds (int), or None if not found.
        Also useful to approximate return epoch: time.time() + remaining.
        """
        try:
            import re as _re, time as _t
            res = self.session.get(f"{self.server_url}/build.php?gid=16&tt=1")
            res.raise_for_status()
            html = getattr(res, "text", "") or ""
        except Exception:
            return None

        # Look for a movement block that contains the hero unit icon, then capture a nearby timer value
        try:
            # Find segments that include the hero icon
            segments = html.split('\n')
            for i, line in enumerate(segments):
                if 'uhero' in line or 'unit uhero' in line:
                    # Search forward a small window for a timer with value attribute
                    window = '\n'.join(segments[i:i+30])
                    m = _re.search(r'class=\"timer\"[^>]*value=\"(\d+)\"', window)
                    if m:
                        val = int(m.group(1))
                        return val if val > 0 else None
            # Fallback: any timer inside an outgoing own troops block that mentions hero
            m2 = _re.search(r'(?:uhero)[\s\S]{0,400}?class=\"timer\"[^>]*value=\"(\d+)\"', html)
            if m2:
                val = int(m2.group(1))
                return val if val > 0 else None
        except Exception:
            return None
        return None




    def find_latest_oasis_report(self, x: int | None, y: int | None) -> Optional[dict]:
        """
        Best-effort: zoekt in /berichte.php naar het meest recente rapport dat (x|y) bevat,
        opent dat rapport en retourneert {'html': ...}. Retourneert None als niets gevonden.
        Robuuster gemaakt tegen RTL/Unicode mintekens en onzichtbare layout-tekens.
        """
        try:
            # Legacy debug toggles (kept default disabled)
            _dbg_log = _dbg_dump = False
            # 1) lijstpagina
            lst = self.session.get(f"{self.server_url}/berichte.php")
            lst.raise_for_status()
            html = lst.text or ""
            
            # Normaliseer unicode voor matching: verwijder directionele marks en vervang speciale mintekens
            def _normalize(s: str) -> str:
                if not s:
                    return ""
                # verwijder directionality/invisible marks
                rm = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"), None)
                s = s.translate(rm)
                # vervang diverse dash/min varianten door ASCII '-'
                for ch in ("\u2212", "\u2010", "\u2011", "\u2012", "\u2013", "\u2014"):
                    s = s.replace(ch, "-")
                return s

            norm_html = _normalize(html)
            # Try newer /report listing with simple pagination first
            try:
                import re
                pages = ["/report", "/report?page=1", "/report?page=2", "/report?page=3"]
                xs = str(int(x)) if x is not None else None
                ys = str(int(y)) if y is not None else None
                for p in pages:
                    pg = self.session.get(f"{self.server_url}{p}")
                    if not getattr(pg, "ok", False):
                        continue
                    page_html = getattr(pg, "text", "") or ""
                    nhtml = _normalize(page_html)
                    if _dbg_dump:
                        try:
                            from pathlib import Path
                            Path('logs').mkdir(parents=True, exist_ok=True)
                            safe = p.replace('/', '_').replace('?', '_').replace('=', '_')
                            (Path('logs')/f'report_list{safe}.html').write_text(page_html, encoding='utf-8')
                        except Exception:
                            pass
                    if xs and ys:
                        coord_re = re.compile(r"\(\s*" + re.escape(xs) + r"\s*\|\s*" + re.escape(ys) + r"\s*\)")
                        pos = [m.start() for m in coord_re.finditer(nhtml)]
                        links = list(re.finditer(r'href=\"(/report\?id=[^\"]*)\"', nhtml))
                        if _dbg_log:
                            print(f"[Reports] Scanning {p}: {len(links)} report link(s), coord matches: {len(pos)}")
                        if pos and links:
                            for pc in pos:
                                best = min(links, key=lambda lm: abs(lm.start() - pc))
                                href = best.group(1)
                                if _dbg_log:
                                    # show a small context around the link
                                    bstart = max(0, best.start()-80)
                                    bend = min(len(nhtml), best.end()+80)
                                    snippet = nhtml[bstart:bend]
                                    print(f"[Reports] Candidate link near coord: {href} … {snippet[:120].replace('\n',' ')} …")
                                rep = self.session.get(f"{self.server_url}{href}")
                                if getattr(rep, "ok", False):
                                    return {"html": rep.text}
                    else:
                        m = re.search(r'href=\"(/report\?id=[^\"]*)\"', nhtml)
                        if m:
                            if _dbg_log:
                                print(f"[Reports] No coords: opening first report link {m.group(1)} from {p}")
                            rep = self.session.get(f"{self.server_url}{m.group(1)}")
                            if getattr(rep, "ok", False):
                                return {"html": rep.text}
            except Exception:
                pass
            if x is not None and y is not None:
                import re
                # Vind alle detail-links in genormaliseerde HTML
                link_iter = list(re.finditer(r'href="(/berichte\\.php\\?id=\\d+[^"]*)"', norm_html))
                # Coordvorm met pijpje en optionele spaties, met ASCII '-' na normalisatie
                xs, ys = str(int(x)), str(int(y))
                coord_re = re.compile(r"\(\s*" + re.escape(xs) + r"\s*\|\s*" + re.escape(ys) + r"\s*\)")
                pos_coord = [m.start() for m in coord_re.finditer(norm_html)]
                candidate = None
                if pos_coord and link_iter:
                    # Koppel dichtsbijzijnde link aan coord-positie
                    for pc in pos_coord:
                        best = min(link_iter, key=lambda lm: abs(lm.start() - pc))
                        if best is not None:
                            candidate = best.group(1)
                            break
                # fallback: pak eerste link als niets met coords
                if not candidate and link_iter:
                    candidate = link_iter[0].group(1)
                if not candidate:
                    return None
                # 2) detailpagina
                rep = self.session.get(f"{self.server_url}{candidate}")
                rep.raise_for_status()
                return {"html": rep.text}
            else:
                # Geen coords → neem laatste eerste link
                import re
                hrefs = re.findall(r'href="(/berichte\\.php\\?id=\\d+[^"]*)"', html)
                if not hrefs:
                    return None
                rep = self.session.get(f"{self.server_url}{hrefs[0]}")
                rep.raise_for_status()
                return {"html": rep.text}
        except Exception:
            return None

    def log_cookies(self):
        """Log all cookies for debugging."""
        print("\n" + "="*40)
        print("🍪 Current Session Cookies")
        print("="*40)
        for cookie in self.session.cookies:
            print(f"   🔑 {cookie.name}: {cookie.value}")
            if cookie.name == "JWT":
                try:
                    import jwt
                    decoded = jwt.decode(cookie.value, options={"verify_signature": False})
                    print("\n🔐 Decoded JWT Token:")
                    print("="*30)
                    print(f"   🏰 Village ID: {decoded.get('properties', {}).get('did')}")
                    print(f"   ⏰ Expires: {decoded.get('exp', 'Unknown')}")
                    print("="*30)
                except Exception as e:
                    print(f"❌ Failed to decode JWT: {e}")
        print("="*40)

    def get_troops_in_village(self):
        """Fetch troop counts in the current village (no printing)."""
        import re

        url = f"{self.server_url}/dorf1.php"
        response = self.session.get(url)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        troops_table = soup.find("table", {"id": "troops"})
        if not troops_table:
            return {}

        troops: dict[str, int] = {}
        for row in troops_table.find_all("tr"):
            img = row.find("img")
            num = row.find("td", class_="num")
            if not (img and num):
                continue
            unit_classes = img.get("class", [])
            for c in unit_classes:
                if c in ("unit", "uhero"):
                    continue
                m = re.fullmatch(r"u(\d{1,2})", c)
                if not m:
                    continue
                try:
                    code_num = int(m.group(1))
                    count = int(num.text.strip())
                    troops[f"u{code_num}"] = count
                except Exception:
                    continue
        return troops

    def _make_graphql_request(self, query: str, variables: dict = None) -> dict:
        """Make a GraphQL request to the server."""
        payload = {
            "query": query,
            "variables": variables or {}
        }
        response = self.session.post(
            f"{self.server_url}/api/v1/graphql",
            json=payload,
            headers=self._headers_json_api("/build.php?gid=16&tt=99"),
        )
        response.raise_for_status()
        return response.json()

    # === Reports (list + detail) ===
    def list_report_links(self, tab: int | None = None, max_items: int = 100) -> list[str]:
        """Return relative hrefs like /report?id=6401237|abcd&s=1 from the report overview.

        - tab can be provided (e.g., 1 for 'All'); if None, default server tab is used.
        - max_items limits how many links to return to avoid heavy scans.
        """
        url = f"{self.server_url}/report"
        try:
            params = {"s": int(tab)} if tab is not None else None
        except Exception:
            params = None
        try:
            res = self.session.get(url, params=params)
            res.raise_for_status()
            html = getattr(res, "text", "") or ""
            # Optional debug dump disabled by default
            # Extract links: support both absolute and relative hrefs used by overview
            import re
            hrefs = []
            patterns = [
                r'href=\"(/report\\?id=[^\"\s>]+)\"',  # absolute
                r'href=\"(/report/overview\\?id=[^\"\s>]+)\"',  # overview absolute
                r'href=\"(\?id=[^\"\s>]+)\"',  # relative
            ]
            for pat in patterns:
                for m in re.finditer(pat, html):
                    href = m.group(1)
                    # Normalize relative '?id=...' to '/report?id=...'
                    if href.startswith('?'):
                        href = '/report' + href
                    hrefs.append(href)
                    if len(hrefs) >= max_items:
                        break
                if len(hrefs) >= max_items:
                    break
            # Deduplicate preserving order
            seen = set()
            out = []
            for h in hrefs:
                if h not in seen:
                    out.append(h)
                    seen.add(h)
            return out
        except Exception as e:
            logging.warning(f"[Reports] Could not list report links: {e}")
            return []

    def fetch_report_detail(self, href_or_id: str) -> str | None:
        """Fetch a report detail HTML by href '/report?id=...&s=1' or id '123|hash'."""
        try:
            # Normalize input to a full '/report?...' path if it's a relative or id
            href = href_or_id
            if href.startswith('/report'):
                url = f"{self.server_url}{href}"
            elif href.startswith('?'):
                url = f"{self.server_url}/report{href}"
            elif 'id=' in href and ('?' in href or '&' in href):
                # looks like a query fragment; ensure '/report' prefix
                if not href.startswith('/'): href = '/' + href
                if not href.startswith('/report'):
                    href = '/report' + href
                url = f"{self.server_url}{href}"
            elif "|" in href:
                # plain id in form 'num|hash'
                url = f"{self.server_url}/report?id={href}"
            else:
                # fallback: treat as numeric id
                url = f"{self.server_url}/report?id={href}&s=1"
            res = self.session.get(url)
            res.raise_for_status()
            html = getattr(res, "text", "") or ""
            # Optional debug dump disabled by default
            return html
        except Exception as e:
            logging.warning(f"[Reports] Could not fetch detail for {href_or_id}: {e}")
            return None

    def get_unread_report_count(self) -> int:
        """Read the navbar reports indicator to estimate unread report count.

        Looks for: <a class="reports" ...><div class="indicator">N</div></a>
        on a common page like /dorf1.php. Returns 0 if not found.
        """
        try:
            res = self.session.get(f"{self.server_url}/dorf1.php")
            res.raise_for_status()
            html = getattr(res, "text", "") or ""
            try:
                # Fast regex to avoid parser dependency for this small task
                import re as _re
                m = _re.search(r"<a[^>]*class=\"[^\"]*reports[^\"]*\"[^>]*>.*?<div[^>]*class=\"[^\"]*indicator[^\"]*\"[^>]*>([^<]+)</div>", html, _re.IGNORECASE | _re.DOTALL)
                if not m:
                    return 0
                txt = (m.group(1) or "").strip()
                # strip bidi marks and plus sign
                txt = txt.replace("\u202d", "").replace("\u202c", "").replace("+", "").strip()
                # keep only digits
                digits = "".join(ch for ch in txt if ch.isdigit())
                return int(digits) if digits else 0
            except Exception:
                return 0
        except Exception:
            return 0

    def list_unread_reports(self, tab: int | None = None, max_items: int = 100) -> list[dict]:
        """Parse the report overview and return unread entries with ids and coords.

        Output items: { 'href': str, 'id': str|None, 'coords': (int,int)|None, 'time': str|None }
        """
        url = f"{self.server_url}/report"
        params = {"s": int(tab)} if tab is not None else None
        try:
            res = self.session.get(url, params=params)
            res.raise_for_status()
            html = getattr(res, "text", "") or ""
        except Exception:
            return []

        import re as _re
        out: list[dict] = []
        # Split by table rows in overview; look for 'newMessage' class indicating unread
        for m in _re.finditer(r"<tr[\s\S]*?</tr>", html, _re.IGNORECASE):
            row = m.group(0)
            if 'newMessage' not in row:
                continue
            # Extract link href
            href = None
            mh = _re.search(r'href=\"([^\"]*\?id=[^\"]+)\"', row)
            if mh:
                href = mh.group(1)
                if href.startswith('?'):
                    href = '/report' + href
            # Extract id param
            rid = None
            if href:
                try:
                    import urllib.parse as _up
                    qs = _up.parse_qs(_up.urlsplit(href).query)
                    rid = (qs.get('id') or [None])[0]
                except Exception:
                    rid = None
            # Extract coordinates inside row
            coords = None
            mc = _re.search(r"\(([-]?\d{1,3})\|([-]?\d{1,3})\)", row)
            if mc:
                try:
                    coords = (int(mc.group(1)), int(mc.group(2)))
                except Exception:
                    coords = None
            # Extract time text (optional)
            mt = _re.search(r"<td class=\"dat\">\s*([^<]+)<", row)
            time_txt = mt.group(1).strip() if mt else None
            out.append({"href": href, "id": rid, "coords": coords, "time": time_txt})
            if len(out) >= max_items:
                break
        return out

    def has_task_reward_indicator(self) -> bool:
        """Detects the top-bar 'new quest' speech bubble that indicates claimable tasks.

        Looks specifically for the top-bar speech bubble element on dorf1:
        e.g. <div class="bigSpeechBubble newQuestSpeechBubble"> ...
        Do not rely on static elements like 'progressiveTasksTitle' which is always present.
        """
        try:
            res = self.session.get(f"{self.server_url}/dorf1.php")
            res.raise_for_status()
            html = getattr(res, "text", "") or ""
            # Strict: only treat the explicit speech-bubble as an indicator
            return ("newQuestSpeechBubble" in html) or ("bigSpeechBubble newQuestSpeechBubble" in html)
        except Exception:
            return False

    @staticmethod
    def _extract_coords_from_report_html(html: str) -> tuple[int, int] | None:
        """Best-effort coordinates extraction from report detail HTML."""
        try:
            import re
            # Common form is (x|y) in defender target link/title
            for m in re.finditer(r"\(([-]?\d{1,3})\|([-]?\d{1,3})\)", html):
                x = int(m.group(1)); y = int(m.group(2))
                return x, y
        except Exception:
            pass
        return None

    def find_latest_oasis_report(self, x: int | None, y: int | None, tab: int | None = 1, scan_limit: int = 30) -> dict | None:
        """Scan recent reports, returning the first whose coordinates match (x|y).

        Returns dict with keys: id (if derivable), html, href.
        """
        if x is None or y is None:
            return None
        hrefs = self.list_report_links(tab=tab, max_items=scan_limit)
        for href in hrefs:
            html = self.fetch_report_detail(href)
            if not html:
                continue
            coords = self._extract_coords_from_report_html(html)
            if coords and coords == (x, y):
                # Try to pull id from href
                rid = None
                try:
                    import urllib.parse as _up
                    qs = _up.parse_qs(_up.urlsplit(href).query)
                    rid = (qs.get("id") or [None])[0]
                except Exception:
                    rid = None
                return {"id": rid, "href": href, "html": html}
        return None

    # Backwards-compatible generic alias (works for oasis or villages)
    def find_latest_report_by_coords(self, x: int | None, y: int | None, tab: int | None = 1, scan_limit: int = 30) -> dict | None:
        return self.find_latest_oasis_report(x, y, tab=tab, scan_limit=scan_limit)

    def _parse_hero_attack_from_html(self, html: str) -> int | None:
        """Best-effort parse of the hero's fighting strength (attack) from hero pages.
        Looks for common localized labels like Fighting strength / Vechtkracht / Kampfkraft, etc.
        Returns an integer or None if not found.
        """
        try:
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(" ", strip=True)
            patterns = [
                r"Fighting\s+strength\D+(\d+)",
                r"Vechtkracht\D+(\d+)",
                r"Kampfkraft\D+(\d+)",
                r"Сила\s+героя\D+(\d+)",
                r"Força\s+de\s+combate\D+(\d+)",
                r"Forza\s+d'attacco\D+(\d+)",
            ]
            for pat in patterns:
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    return int(m.group(1))
            for lbl in ("Fighting", "Vecht", "Kampf", "Attacco", "Combate"):
                el = soup.find(string=re.compile(lbl, re.IGNORECASE))
                if el and el.parent:
                    m2 = re.search(r"(\d+)", el.parent.get_text(" ", strip=True))
                    if m2:
                        return int(m2.group(1))
        except Exception:
            pass
        return None

    def get_hero_attack_estimate(self) -> int | None:
        """Fetch the hero's current fighting strength (attack) via HUD JSON or hero pages.
        Returns an integer if found, else None.
        """
        # 1) Try HUD JSON first (may not include attack on all servers)
        try:
            res = self.session.get(
                f"{self.server_url}/api/v1/hero/dataForHUD",
                headers=self._headers_ajax("/hero"),
            )
            res.raise_for_status()
            data = res.json() or {}
            for k in ("fightingStrength", "fightingstrength", "attack", "heroPower", "power"):
                if k in data and isinstance(data[k], (int, float)):
                    return int(data[k])
            stats = data.get("stats") or data.get("attributes") or {}
            for k in ("fightingStrength", "attack", "power"):
                v = stats.get(k)
                if isinstance(v, (int, float)):
                    return int(v)
        except Exception:
            pass

        # 2) Try common hero pages and parse HTML
        for path in ("/hero/attributes", "/hero.php", "/hero", "/hero/inventory"):
            try:
                page = self.session.get(f"{self.server_url}{path}")
                if getattr(page, "ok", False):
                    atk = self._parse_hero_attack_from_html(page.text)
                    if isinstance(atk, int):
                        return atk
            except Exception:
                continue
        return None

    def _extract_page_errors(self, html: str) -> list:
        """Best-effort extraction of validation/feedback errors from a Travian form page.

        Returns a list of short error messages (strings) if any likely errors are detected.
        This scans common Travian layouts across languages.
        """
        try:
            soup = BeautifulSoup(html, "html.parser")
            candidates = []

            # Common containers that hold errors or validation messages
            selectors = [
                ".error", ".errors", ".alert", ".messageLine.error", ".dialogMessage.error",
                "#error", "#errors", ".warning", ".validationError", ".textWithError"
            ]
            for sel in selectors:
                for el in soup.select(sel):
                    txt = el.get_text(" ", strip=True)
                    if txt:
                        candidates.append(txt)

            # Also inspect generic form notices/hints often placed near the troop table
            for el in soup.select(".notice, .hint, .tooltip, .errorMsg, .formError"):
                txt = el.get_text(" ", strip=True)
                if txt:
                    candidates.append(txt)

            # As a last resort, pick visible list items that often carry errors
            for el in soup.select("ul li"):
                classes = " ".join(el.get("class", []))
                if any(k in classes for k in ("error", "warning", "invalid")):
                    txt = el.get_text(" ", strip=True)
                    if txt:
                        candidates.append(txt)

            # De-duplicate while preserving order and keep it short
            seen = set()
            result = []
            for msg in candidates:
                if msg not in seen:
                    seen.add(msg)
                    result.append(msg)
                if len(result) >= 5:
                    break
            return result
        except Exception:
            return []

    def launch_farm_list(self, farm_list_id: int) -> bool:
        """Launch a farm list by its ID."""
        payload = {
            "query": """
                mutation($listId: Int!) {
                    startFarmListRaid(listId: $listId) {
                        success
                    }
                }
            """,
            "variables": {
                "listId": farm_list_id
            }
        }
        response = self.session.post(f"{self.server_url}/api/v1/graphql", json=payload)
        response.raise_for_status()
        data = response.json()
        print(f"\n📡 Server response for farm list {farm_list_id}:")
        print(f"   {'✨ Success' if data.get('data', {}).get('startFarmListRaid', {}).get('success', False) else '💥 Failed'}")
        return data.get("data", {}).get("startFarmListRaid", {}).get("success", False)

    def send_farm_list(self, list_id: int) -> bool:
        """Send a farm list by its ID."""
        payload = {
            "action": "farmList",
            "lists": [{"id": list_id}]
        }
        response = self.session.post(
            f"{self.server_url}/api/v1/farm-list/send",
            json=payload,
            headers=self._headers_json_api("/build.php?gid=16&tt=99"),
        )
        return response.status_code == 200

    def debug_tile_details(self, x: int, y: int):
        """Debug method to print all information from tile details API call."""
        url = f"{self.server_url}/api/v1/map/tile-details"
        payload = {"x": x, "y": y}
        response = self.session.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        html = data.get("html")
        if not html:
            print("No HTML response")
            return

        soup = BeautifulSoup(html, "html.parser")
        
        # Print all h1 tags (titles)
        print("\nTitles:")
        for h1 in soup.find_all("h1"):
            print(f"- {h1.text.strip()}")
        
        # Print all tables and their IDs
        print("\nTables:")
        for table in soup.find_all("table"):
            print(f"- Table ID: {table.get('id', 'No ID')}")
            print(f"  Content: {table.text.strip()[:100]}...")
        
        # Print all divs with class
        print("\nDivs with classes:")
        for div in soup.find_all("div", class_=True):
            print(f"- Div class: {div.get('class')}")
            print(f"  Content: {div.text.strip()[:100]}...")

    def get_hero_page(self) -> Optional[str]:
        """Get the hero status page HTML."""
        try:
            response = self.session.get(f"{self.server_url}/hero")
            response.raise_for_status()
            return response.text
        except Exception as e:
            logging.error(f"Failed to get hero page: {str(e)}")
            return None

    def get_hero_attributes(self):
        """Get hero attributes from the GraphQL API."""
        payload = {
            "query": """
                query {
                    ownPlayer {
                        hero {
                            id
                            health
                            isPresent
                            isOnMission
                            missionReturnTime
                            missionTarget {
                                x
                                y
                            }
                            currentVillage {
                                id
                                name
                            }
                        }
                    }
                }
            """
        }
        try:
            response = self.session.post(f"{self.server_url}/api/v1/graphql", json=payload)
            response.raise_for_status()
            data = response.json()
            if "data" in data and "ownPlayer" in data["data"] and "hero" in data["data"]["ownPlayer"]:
                return data["data"]["ownPlayer"]["hero"]
            logging.error(f"Unexpected API response format: {data}")
            return None
        except Exception as e:
            logging.error(f"Failed to get hero attributes: {str(e)}")
            return None
