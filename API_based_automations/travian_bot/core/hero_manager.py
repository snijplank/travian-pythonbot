import re
from bs4 import BeautifulSoup
import os
from .hero_runner import try_send_hero_to_oasis
import logging
import json
import html
from dataclasses import dataclass
from typing import Optional, Tuple
from bs4 import BeautifulSoup
from pathlib import Path
from requests import HTTPError

@dataclass
class HeroStatus:
    is_present: bool
    health: Optional[float]
    is_on_mission: bool
    mission_return_time: Optional[str]
    mission_target: Optional[Tuple[int, int]]
    current_village_id: Optional[str]
    current_village_name: Optional[str]
    is_in_known_village: bool
    level: Optional[int]
    experience: Optional[int]
    experience_percent: Optional[float]

class HeroManager:
    def __init__(self, api):
        self.api = api

    def _refresh_session(self) -> bool:
        """Best-effort ping to refresh cookies/session when HUD auth fails."""
        try:
            self.api.session.get(f"{self.api.server_url}/dorf1.php", timeout=10)
            return True
        except Exception as exc:
            logging.debug(f"[Hero] Session refresh failed: {exc}")
            return False

    def _is_known_village(self, village_id: str) -> bool:
        """Check if village_id exists in identity.json."""
        try:
            current_dir = os.path.dirname(__file__)
            database_dir = os.path.join(current_dir, '..', 'database')
            identity_path = os.path.join(database_dir, 'identity.json')
            identity_path = os.path.abspath(identity_path)

            with open(identity_path, "r", encoding="utf-8") as f:
                identity = json.load(f)
            for server in identity.get("travian_identity", {}).get("servers", []):
                for village in server.get("villages", []):
                    if str(village.get("village_id")) == str(village_id):
                        return True
            return False
        except Exception as e:
            logging.error(f"Failed to check village in identity: {e}")
            return False

    def _extract_village_info(self, status_title: str) -> Tuple[Optional[str], Optional[str]]:
        """Extract village ID and name from hero status title."""
        try:
            # Look for village ID in URL
            did_match = re.search(r'newdid=(\d+)', status_title)
            village_id = did_match.group(1) if did_match else None
            
            # Look for village name in HTML
            name_match = re.search(r'>([^<]+)</a>', status_title)
            village_name = name_match.group(1) if name_match else None
            
            return village_id, village_name
        except Exception as e:
            logging.error(f"Failed to extract village info: {e}")
            return None, None

    def fetch_hero_status(self) -> Optional[HeroStatus]:
        """Fetch hero status from the HUD API endpoint."""
        try:
            url = f"{self.api.server_url}/api/v1/hero/dataForHUD"
            response = self.api.session.get(
                url,
                headers=self.api._headers_ajax("/hero"),
                timeout=15,
            )
            if response.status_code == 401:
                logging.warning("[Hero] HUD request returned 401; attempting to refresh Travian headers.")
                refreshed = False
                current_x = getattr(self.api, "_x_version", None)
                refresh_fn = getattr(self.api, "refresh_x_version", None)
                if callable(refresh_fn):
                    try:
                        new_version = refresh_fn()
                        if new_version:
                            if current_x != new_version:
                                logging.info(f"[Hero] X-Version header updated to {new_version}. Retrying HUD request.")
                            else:
                                logging.debug("[Hero] X-Version header unchanged after refresh attempt.")
                        else:
                            logging.debug("[Hero] X-Version refresh returned no value.")
                    except Exception as exc:
                        logging.debug(f"[Hero] X-Version refresh failed: {exc}")
                    else:
                        response = self.api.session.get(
                            url,
                            headers=self.api._headers_ajax("/hero"),
                            timeout=15,
                        )
                        refreshed = True

                if response.status_code == 401:
                    if refreshed:
                        logging.warning("[Hero] HUD request still unauthorized after X-Version refresh; retrying with session ping.")
                    else:
                        logging.warning("[Hero] HUD request still unauthorized; refreshing session and retrying once.")
                    if self._refresh_session():
                        response = self.api.session.get(
                            url,
                            headers=self.api._headers_ajax("/hero"),
                            timeout=15,
                        )
                if response.status_code == 401:
                    logging.warning("[Hero] HUD request still unauthorized after refresh. Skipping hero update this cycle.")
                    return None

            response.raise_for_status()
            data = response.json()
            
            # Debug: Print raw response
            logging.debug("Raw hero HUD response:")
            logging.debug(json.dumps(data, indent=2))
            
            # Extract village_id from 'url' field
            url = data.get("url", "")
            village_id = None
            url_match = re.search(r"newdid=(\d+)", url)
            if url_match:
                village_id = url_match.group(1)
            
            # Extract village_name from heroStatusTitle <a> tag (unescape first)
            village_name = None
            status_title = data.get("heroStatusTitle", "")
            status_title_unescaped = html.unescape(status_title)
            name_match = re.search(r">([^<]+)</a>", status_title_unescaped)
            if name_match:
                village_name = name_match.group(1)
            
            is_in_known_village = self._is_known_village(village_id) if village_id else False
            
            # Determine if hero is on mission via inline icon; heroHome icon means at home
            is_on_mission = "heroHome" not in (data.get("statusInlineIcon", "") or "")
            
            return HeroStatus(
                # Present means at home (not on a mission). 'alive' only means hero is not dead.
                is_present=(not is_on_mission),
                health=data.get("health"),
                is_on_mission=is_on_mission,
                mission_return_time=None,  # TODO: Extract from mission info if available
                mission_target=None,  # TODO: Extract from mission info if available
                current_village_id=village_id,
                current_village_name=village_name,
                is_in_known_village=is_in_known_village,
                level=data.get("level"),
                experience=data.get("experience"),
                experience_percent=data.get("experiencePercent")
            )
            
        except HTTPError as e:
            status = getattr(e.response, "status_code", "?")
            logging.warning(f"Failed to fetch hero status (HTTP {status}): {e}")
            return None
        except Exception as e:
            logging.error(f"Failed to fetch hero status: {e}")
            return None

    def send_hero_with_escort(self, village, oasis):
        """Send hero to attack an oasis with robust preflight and debug dumps.
        Ensures we're in the correct village context and that the rally point send form is reachable.
        """
        # 0) Quick availability check: if there are zero troops available, fail fast with a clear message
        try:
            available = self.api.get_troops_in_village()  # returns dict like {"u61": 5, ...}
            total_non_hero = sum(v for v in available.values() if isinstance(v, int))
            if total_non_hero <= 0:
                msg = (
                    "Geen escort mogelijk: er zijn momenteel geen troepen in het dorp. "
                    "Tip: zet escort-minimum in je config lager of wacht tot troepen terug zijn."
                )
                logging.error(f"[HeroRaider] {msg}")
                raise ValueError(msg)
        except Exception:
            # If the availability probe fails, continue; deeper send will still try and dump HTML if needed
            pass

        try:
            # 1) Ensure village context (important for correct troop slots and rally point state)
            vid = str(village.get("village_id") if isinstance(village, dict) else getattr(village, "village_id", ""))
            if vid:
                self.api.session.get(f"{self.api.server_url}/dorf1.php?newdid={vid}")

            # 2) Prefetch rally point send tab (tt=2) and dump HTML for diagnostics
            rp_res = self.api.session.get(f"{self.api.server_url}/build.php?gid=16&tt=2")
            try:
                Path("logs").mkdir(parents=True, exist_ok=True)
                (Path("logs")/"rallypoint_tt2_prefetch.html").write_text(rp_res.text, encoding="utf-8")
            except Exception:
                pass
        except Exception as e:
            logging.error(f"[HeroRaider] Preflight error before sending hero: {e}")

        # 3) Delegate to the actual sender
        try:
            return try_send_hero_to_oasis(self.api, village, oasis)
        except ValueError as e:
            # Known user-facing validation (e.g., no troops selected)
            logging.error(f"[HeroRaider] {e}")
            raise
        except Exception as e:
            # On failure, try to capture the current rally point page for debugging
            try:
                rp_err = self.api.session.get(f"{self.api.server_url}/build.php?gid=16&tt=2")
                Path("logs").mkdir(parents=True, exist_ok=True)
                (Path("logs")/"rallypoint_tt2_error_dump.html").write_text(rp_err.text, encoding="utf-8")
            except Exception:
                pass
            logging.error(f"[HeroRaider] ❌ Hero raid skipped or failed during send: {e}")
            raise
