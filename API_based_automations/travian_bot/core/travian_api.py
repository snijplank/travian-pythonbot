import requests
import re
from bs4 import BeautifulSoup
from analysis.animal_to_power_mapping import get_animal_power
from typing import Optional
import logging


class TravianAPI:
    def __init__(self, session: requests.Session, server_url: str):
        self.session = session
        self.server_url = server_url

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
        response = self.session.post(f"{self.server_url}/api/v1/graphql", json=payload)
        response.raise_for_status()
        return response.json()["data"]["ownPlayer"]

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
        response = self.session.post(f"{self.server_url}/api/v1/graphql", json=payload)
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
        response = self.session.post(f"{self.server_url}/api/v1/graphql", json=payload)
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
        response = self.session.post(url, json=payload)
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

    def get_oasis_animal_count(self, x: int, y: int) -> int:
        """Get total count of animals in an oasis."""
        url = f"{self.server_url}/api/v1/map/tile-details"
        payload = {"x": x, "y": y}
        response = self.session.post(url, json=payload)
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
                    if 1 <= n <= 10:
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
        action_input = soup.select_one('input[name="action"]')
        if not action_input:
            raise Exception("[-] No action input found during preparation.")

        action = action_input["value"]

        button = soup.find("button", id="confirmSendTroops")
        if not button:
            raise Exception("[-] Confirm button not found during preparation.")
        onclick = button.get("onclick")
        checksum_match = re.search(r"value\s*=\s*'([a-f0-9]+)'", onclick)
        if not checksum_match:
            raise Exception("[-] Checksum not found in onclick during preparation.")
        checksum = checksum_match.group(1)

        return {
            "action": action,
            "checksum": checksum,
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
                    if 1 <= n <= 10:
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

        final_payload = {
            "action": attack_info["action"],
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

        res = self.session.post(f"{self.server_url}/build.php?gid=16&tt=2", data=final_payload, allow_redirects=False)
        res.raise_for_status()

        return res.status_code == 302 and res.headers.get("Location") == "/build.php?gid=16&tt=1"

    def get_tile_html(self, x, y):
        url = f"{self.server_url}/api/v1/map/tile-details"
        res = self.session.post(url, json={"x": x, "y": y})
        res.raise_for_status()
        return res.json()["html"]

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
        """Fetch troop counts in the current village."""
        import re

        url = f"{self.server_url}/dorf1.php"
        response = self.session.get(url)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        troops_table = soup.find("table", {"id": "troops"})
        if not troops_table:
            print("⚠️ Troops table not found.")
            return {}

        troops = {}
        print("\n⚔️ Current troops in village:")
        print("="*30)
        for row in troops_table.find_all("tr"):
            img = row.find("img")
            num = row.find("td", class_="num")
            if img and num:
                unit_classes = img.get("class", [])
                for c in unit_classes:
                    if c in ("unit", "uhero"):
                        # Ignore generic class and hero; hero is not sent via normal troop slots
                        continue
                    m = re.fullmatch(r"u(\d{1,2})", c)
                    if m:
                        code_num = int(m.group(1))
                        try:
                            count = int(num.text.strip())
                            troops[f"u{code_num}"] = count
                            print(f"   🛡️ u{code_num}: {count:,} units")
                        except ValueError:
                            continue
                    else:
                        print(f"🔍 Unrecognized unit class: {c}")
        print("="*30)
        return troops

    def _make_graphql_request(self, query: str, variables: dict = None) -> dict:
        """Make a GraphQL request to the server."""
        payload = {
            "query": query,
            "variables": variables or {}
        }
        response = self.session.post(f"{self.server_url}/api/v1/graphql", json=payload)
        response.raise_for_status()
        return response.json()

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
        response = self.session.post(f"{self.server_url}/api/v1/farm-list/send", json=payload)
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
