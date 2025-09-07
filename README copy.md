# Travian Automation Bot

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Een geautomatiseerde **Travian Legends bot** met ondersteuning voor:
- 🌾 **Farm lists** (burst / config / full-auto loop)
- 🏰 **Oasis raiding** (interactief plan, saved config)
- 🦸 **Hero operations** (status, raiding thread)
- 🗺️ **Map scanning** (oases zoeken & weergeven)
- 👑 **Full Auto Mode** — multi-village loop met jitter & re-login

---

## 🚀 Quickstart

```bash
# clone repo
git clone https://github.com/yourname/travian-bot.git
cd travian-bot

# create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# install dependencies
pip install -r requirements.txt

# run launcher
python API_based_automations/travian_bot/launcher.py
```

---

## 📖 Usage

Start het launcher-menu:

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

🗺️ MAP SCANNING:
8) Scan & View Oases

👤 ACCOUNT:
9) Hero Operations
10) Identity & Villages
11) Test Hero Raiding Thread
```

- Identity & villages configureren → menu **10**
- **Full Auto Mode (7)** combineert farm lists, raids & hero thread
- Alle details: zie [RUN_GUIDE.md](./RUN_GUIDE.md)

---

## ⚙️ Configuratie

Aanpasbaar in [`launcher.py`](API_based_automations/travian_bot/launcher.py):

```python
WAIT_BETWEEN_CYCLES_MINUTES = 10
JITTER_MINUTES = 10
SERVER_SELECTION = 0
```

> Houd jitter aan om voorspelbare patronen te vermijden.

---

## 📂 Belangrijkste data

- `database/identity.json` — account, server & dorpen
- `database/saved_raid_plan.json` — opgeslagen raidplan
- `database/map_scans/` — oase scanresultaten

---

## 🛠️ Contributie

PR’s & issues welkom!  
Zie [TODO’s in code](./QUICK_RECON.md) of vraag features via Issues.

---

## 📜 License

MIT License — zie [LICENSE](./LICENSE)
