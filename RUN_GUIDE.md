# Run Guide — Travian Automation Launcher

This guide explains **how to install and run** the primary launcher for your Travian bot.

> Primary entrypoint: `API_based_automations/travian_bot/launcher.py`  
> Features include farm lists, oasis raids, map scanning, hero ops, and a multi‑village full‑auto loop.

---

## 1) Requirements

- **Python 3.10+** recommended (3.8+ may work, but not tested here)
- Linux (Debian/Ubuntu/Alpine) or WSL2/macOS
- `pip`, `venv`, and build tools for native wheels (e.g., `python3-dev`, `build-essential`) if needed

### Install prerequisites (Debian/Ubuntu)
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

---

## 2) Create & activate a virtual environment

From the project root (where this file lives), run:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

To deactivate later:
```bash
deactivate
```

---

## 3) Install dependencies

If your project has `requirements.txt` files, install them now (root and/or module‑level). Common pattern:
```bash
# root-level requirements
if [ -f requirements.txt ]; then pip install -r requirements.txt; fi

# module-specific requirements (adjust paths if present)
find . -maxdepth 3 -name "requirements.txt" -not -path "./.venv/*" -exec bash -c 'echo "Installing {}" && pip install -r "{}"' \;
```

If you use `pyproject.toml`, you can also:
```bash
pip install .
```

---

## 4) First run: the launcher

Run the launcher from the project root:
```bash
python API_based_automations/travian_bot/launcher.py
```

You’ll see a menu like:

```
🌾 FARM LIST:
1) Farm burst
2) Configure farm lists
3) Run farm from config

🏰 OASIS RAID:
4) Setup raid plan
5) Reset raid plan
6) Test raid (single village)

🤖 AUTOMATION:
7) 👑 FULL AUTO MODE 👑
   • Farm lists + Oasis raids
   • Multi-village loop

🗺️ MAP SCANNING:
8) Scan & View Oases

👤 ACCOUNT:
9) Hero Operations
10) Identity & Villages
11) Test Hero Raiding Thread (Standalone)
```

---

## 5) Identity & login

The launcher calls a login flow via `identity_handling/login.py`.  
Use **Menu → 10) Identity & Villages** to set up or review identity:

- **Set up new identity**: creates/updates `database/identity.json`
- **View current identity**: prints faction and villages
- **Update village coordinates**: interactively edit per village

If identity is missing or corrupted, the tool will prompt you to configure it.  
Tip: Keep a safe backup of `database/identity.json`.

---

## 6) Farm lists & raid plan

- **Menu 2 – Configure farm lists**: synchronize/update farm lists from in‑game.
- **Menu 3 – Run farm from config**: executes farm list raids for all villages from identity.
- **Menu 4 – Setup raid plan**: interactive oasis raid planning per village.
- **Menu 5 – Reset raid plan**: clears saved plan (`database/saved_raid_plan.json`).
- **Menu 6 – Test raid**: single‑village oasis raid (good for sanity checks).

Saved configs & data
- `database/saved_raid_plan.json` — raid plan definition
- `database/identity.json` — villages & server metadata
- map scan outputs — see `database/map_scans/*` (naming may vary)

---

## 7) Full Auto Mode (recommended workflow)

**Menu 7 – FULL AUTO MODE** orchestrates:
- multi‑village oasis raids (using saved plan)
- farm list raids (optional skip on the first cycle)
- background **hero raiding thread**
- periodic cycles with **wait + jitter**

On start it asks:
- **Delay before start?** — optionally wait X minutes
- **Skip farm lists on first run?** — useful after long downtime

Cycle timing (edit in `launcher.py` if needed):
```python
WAIT_BETWEEN_CYCLES_MINUTES = 10
JITTER_MINUTES = 10
SERVER_SELECTION = 0
```

> Tip: Keeping jitter avoids predictable patterns and reduces rate‑limit/anti‑bot risk.

---

## 8) Map scanning

**Menu 8 – Scan & View Oases**
- Scan for unoccupied oases near your villages
- View latest results per village (select from identity list)

Outputs are persisted and can be reused by the raid planner.

---

## 9) Hero operations

**Menu 9 – Hero Operations**
- Status checks and automated actions for your hero
- A **hero raiding thread** is also launched automatically in Full Auto Mode

**Menu 11 – Test Hero Raiding Thread**: run the hero thread stand‑alone for diagnostics.

---

## 10) Logs, data & recovery

- The launcher prints logs to **stdout**. Use `screen`, `tmux` or systemd to keep it running.
- Persistent data lives in `database/`:
  - `identity.json`, `saved_raid_plan.json`, scan outputs, caches, etc.
- If a cycle fails, the launcher attempts **re-login** and continues:
  ```
  ⚠️ Error during cycle … → 🔁 Attempting re-login → ✅ Re-login successful.
  ```

To stop gracefully: `Ctrl+C` (the hero thread is daemonized and will exit with the main process).

---

## 11) Running headless (screen/tmux)

```bash
# using screen
screen -S travian
source .venv/bin/activate
python API_based_automations/travian_bot/launcher.py

# detach:  Ctrl+A then D
# resume:  screen -r travian
```

---

## 12) Updating & keeping a baseline

- Keep a **baseline zip** of known‑good code & configs.
- When updating, apply changes in a feature branch and test:
  - `Menu 6` (Test raid) for a safe validation
  - `Menu 11` (Hero thread) to isolate hero logic
- Version your `database/*.json` files or maintain backups outside the repo.

---

## 13) Common issues

- **`ModuleNotFoundError`** → ensure virtualenv is active & requirements installed.
- **Login loops / auth errors** → re‑run **Menu 10** and verify server selection & cookies.
- **No villages found** → your `identity.json` is empty or malformed.
- **Farm list actions skipped** → you chose to skip on first run; it will resume next cycles.

---

## 14) Configuration knobs you may want to tune

In `launcher.py`:
```python
WAIT_BETWEEN_CYCLES_MINUTES = 10   # base wait between cycles
JITTER_MINUTES = 10                # random +/- jitter in minutes
SERVER_SELECTION = 0               # default server index for login
```

Feature modules:
- **Farm lists**: `features/farm_lists/*`
- **Oasis raids**: `features/raiding/*`, `oasis_raiding_from_scan_list_main.py`
- **Hero**: `features/hero/*`
- **Identity**: `identity_handling/*`
- **Map scan**: `features/map_scanning/*`

---

**That’s it!** You can now run the launcher and iterate on features confidently.
