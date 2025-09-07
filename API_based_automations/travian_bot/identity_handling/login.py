import requests
import hashlib
import base64
import secrets
import os
from pathlib import Path
try:
    from config.config import settings
except Exception:
    class _SettingsFallback:
        TRAVIAN_EMAIL = ""
        TRAVIAN_PASSWORD = ""
    settings = _SettingsFallback()

# === Credential handling (YAML-only) ===
def _get_credentials(interactive: bool = True) -> tuple[str, str]:
    # Read from YAML config and normalize whitespace
    email = (getattr(settings, "TRAVIAN_EMAIL", "") or "").strip()
    password = (getattr(settings, "TRAVIAN_PASSWORD", "") or "").strip()
    if (not email or not password) and interactive:
        print("No credentials in config.yaml. Please enter them now (not saved to disk).")
        email = input("Enter your Travian email: ").strip()
        password = input("Enter your Travian password: ").strip()
    if not email or not password:
        raise RuntimeError("Missing credentials. Set TRAVIAN_EMAIL and TRAVIAN_PASSWORD in config.yaml or run interactively.")
    return email, password

def generate_code_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b'=').decode()
    return verifier, challenge

def login_to_lobby(email, password):
    code_verifier, code_challenge = generate_code_pair()
    session = requests.Session()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.travian.com",
        "Referer": "https://www.travian.com/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Connection": "keep-alive",
    }

    login_url = "https://identity.service.legends.travian.info/provider/login?client_id=HIaSfC2LNQ1yXOMuY7Pc2uIH3EqkAi26"
    login_payload = {
        "login": email,
        "password": password,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256"
    }
    login_resp = session.post(login_url, json=login_payload, headers=headers)
    try:
        login_resp.raise_for_status()
    except requests.HTTPError as e:
        # Surface server error text to help diagnose credential/header issues
        detail = ''
        try:
            detail = f" — {login_resp.text[:200]}" if login_resp.text else ''
        except Exception:
            pass
        raise requests.HTTPError(f"{e}{detail}")
    code = login_resp.json().get("code")

    auth_url = "https://lobby.legends.travian.com/api/auth/code"
    auth_payload = {
        "locale": "en-EN",
        "code": code,
        "code_verifier": code_verifier
    }
    session.post(auth_url, json=auth_payload, headers=headers).raise_for_status()

    return session

def get_avatars(session):
    graphql_url = "https://lobby.legends.travian.com/api/graphql"
    graphql_payload = {
        "variables": {},
        "query": """
        {
          avatars {
            uuid
            name
            gameworld {
              metadata {
                url
                name
              }
            }
          }
        }
        """
    }
    headers = {"Content-Type": "application/json"}
    response = session.post(graphql_url, json=graphql_payload, headers=headers)
    response.raise_for_status()
    data = response.json()["data"]["avatars"]

    avatars = []
    for a in data:
        avatars.append({
            "uuid": a["uuid"],
            "name": a["name"],
            "world_name": a["gameworld"]["metadata"]["name"],
            "world_url": a["gameworld"]["metadata"]["url"]
        })
    return avatars

def login_to_server(session, avatars, selection=None, interactive=True):
    if selection is None:
        if not interactive:
            raise RuntimeError("No server selection provided and interactive mode is disabled.")
        print("Your servers:")
        for i, a in enumerate(avatars):
            print(f"[{i}] {a['world_name']} — {a['world_url']}")
        selection = int(input("Which one would you like to log into? "))

    selected = avatars[selection]

    play_url = f"https://lobby.legends.travian.com/api/avatar/play/{selected['uuid']}"
    play_resp = session.post(play_url)
    play_resp.raise_for_status()
    redirect_info = play_resp.json()
    code = redirect_info["code"]
    server_auth_url = f"{selected['world_url'].rstrip('/')}/api/v1/auth?code={code}&response_type=redirect"

    server_session = requests.Session()
    server_session.cookies.update(session.cookies.get_dict())
    auth_resp = server_session.get(server_auth_url, allow_redirects=True)
    auth_resp.raise_for_status()

    print(f"[+] Successfully logged into {selected['world_name']} at {selected['world_url']}")
    return server_session, selected['world_url']

# === MAIN LOGIN WRAPPER ===

def login(email=None, password=None, server_selection=None, interactive=True):
    if email is None or password is None:
        email, password = _get_credentials(interactive=interactive)

    session = login_to_lobby(email, password)
    avatars = get_avatars(session)
    server_session, server_url = login_to_server(session, avatars, selection=server_selection, interactive=interactive)
    return server_session, server_url

# === TEST ===

def main():
    email, password = _get_credentials(interactive=True)
    session = login_to_lobby(email, password)
    avatars = get_avatars(session)
    server_session, server_url = login_to_server(session, avatars)

    res = server_session.get(server_url)
    print("[+] Game server main page loaded:", res.status_code)

if __name__ == "__main__":
    main()
