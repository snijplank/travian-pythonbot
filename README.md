# Travian Legends Bot

Automated farming, oasis raiding, and hero operations for Travian Legends with a configurable learning loop and per‑cycle reporting.

## Features

- **Farm List Automation**
  - Automated sending of farm lists with configurable delays
  - Random timing variations to avoid detection
  - Auto-recovery from session timeouts
  - Configurable stop time (default: 9 AM)
  - Multi-village support
  - Farm list configuration per village

- **Map Analysis & Scanning**
  - Full map scanning around any village
  - Configurable scan radius
  - Multiple analysis modules:
    - Unoccupied oasis detection
    - Oasis animal power calculation
    - Distance-based sorting
  - Extensible analysis system for future features
  - Scan data persistence for offline analysis
  - Multi-village scanning support

- **Oasis Raiding**
  - Smart oasis selection based on distance
  - Animal detection to avoid losses
  - Configurable maximum raid distance
  - Support for multiple unit types
  - Hero raiding capability
  - Automatic troop availability checking
  - Multi-village support
  - Continuous raiding with 50-minute intervals

- **Progressive Tasks (Rewards) — NEW**
  - Detect and collect progressive task rewards per village
  - Uses site headers (`X-Version`, `X-Requested-With`) and `/api/v1/progressive-tasks/collectReward`
  - Optional HUD refresh after each collect
  - Toggle: `progressive_tasks.PROGRESSIVE_TASKS_ENABLE`, `progressive_tasks.PROGRESSIVE_TASKS_REFRESH_HUD`

- **Rally Tracker Learning (Toggleable) — UPDATED**
  - Queues each raid launch and parses `build.php?gid=16&tt=1` to read actual returns (bounty + surviving escorts)
  - Multipliers are nudged immediately when a return matches (or when a raid times out without returning)
  - Cycle status prints succinct rally-tracker status (`processed`, `no pendings`, ...)
  - Global toggle to disable learning entirely: `learning.LEARNING_ENABLE`
  - When disabled: no pendings written and multipliers remain fixed at 1.0

- **Hero Adventures — UPDATED**
  - Background thread auto-starts adventures when available (configurable)
  - Cycle header displays count of available adventures

## Quick Start

1) Create venv and install deps
- `cd API_based_automations/travian_bot`
- `python -m venv venv`
- `source venv/bin/activate`
- `pip install -r requirements.txt`

2) Configure
- Edit `API_based_automations/travian_bot/config.yaml`, starting with the `core_profile` block:
  - `TRAVIAN_EMAIL`, `TRAVIAN_PASSWORD`
  - `SERVER_SELECTION` (world index as shown in lobby)
  - `TRIBE_HINT` / `ESCORT_UNIT_PRIORITY` to match your tribe
  - Optional cadence/learning parameters (see below)

3) Run
- `bash run.sh` (recommended) or `python launcher.py`
- Preflight shows masked email and server index; login is non‑interactive via YAML.

## CLI

Run headless utilities without the interactive launcher.

- List villages available for scanning:
  - `cd API_based_automations/travian_bot && . venv/bin/activate`
  - `python cli.py scan`

- Start a fast map scan (humanizer disabled) for village index 0 with radius 25:
  - `python cli.py scan --village 0 --radius 25 --fast`

- Fast scan and immediately extract unoccupied oases to `database/unoccupied_oases/(x_y)/...`:
  - `python cli.py scan --village 0 --radius 25 --fast --extract`

The scan writes to `database/full_map_scans/(x_y)/full_map_scan_*.json` and shows a progress bar.

## Usage

The bot provides several operation modes through an interactive launcher:

1. **One-time Farm + Raid** - Executes one round of farming and raiding
2. **Infinite Safe Loop** - Continuously farms and raids with safety checks
3. **Reset Raid Plan** - Clears the saved raid configuration
4. **Setup New Raid Plan** - Interactive setup for a new raiding strategy
5. **Update Identity** - Updates village and player information
6. **Hero Operations** - Manages hero-related activities
7. **Map Scanning** - Analyze the map around your villages
8. **Run Multi-village Raid Planner** - Full automation for all villages
9. **Run Farm List Raider** - Run farm lists from saved configuration
10. **Configure Farm Lists** - Setup farm list automation per village

### Getting Started with Full Automation

To set up full automation (Option 8), follow these steps in order:

1. **Update Identity** (Option 5)
   - Saves your villages and current faction label (e.g. `Huns`)
   - Required for both farm lists and oasis raids

2. **Configure Farm Lists** (Option 10)
   - Set up which farm lists to run for each village
   - Enable/disable automation for each list

3. **Map Analysis** (Option 7)
   - Scan the map around each village
   - Analyze for unoccupied oases and other targets
   - Save data for raid planning

4. **Setup Raid Plans** (Option 4)
   - Configure raid settings for each village
   - Set maximum distances and unit combinations
   - Save the configuration

5. **Run Multi-village Raid Planner** (Option 8)
   - The bot will:
     - Run all enabled farm lists for each village
     - Execute oasis raids based on saved plans
     - Repeat every 50 minutes
     - Handle errors and session timeouts

## Configuration (YAML)

All runtime configuration is read from `API_based_automations/travian_bot/config.yaml` (single source of truth). `.env` is not used.

- **Core profile**
  - `SERVER_SELECTION`: lobby index of your world (0‑based)
  - `TRIBE_HINT`: documentation helper; keep in sync with your Travian tribe
  - `ESCORT_UNIT_PRIORITY`: default escort order; adjust when you change tribe

- Core cadence
  - `WAIT_BETWEEN_CYCLES_MINUTES`: minutes between cycles (default 10)
  - `JITTER_MINUTES`: random jitter (default 10)
  - `SERVER_SELECTION`: index of your world (0‑based)
- Daily limiter
  - `ENABLE_CYCLE_LIMITER`: `true/false`
  - `DAILY_MAX_RUNTIME_MINUTES`, `DAILY_BLOCKS`
- Logging
  - `LOG_LEVEL`: `INFO`|`DEBUG`|...
  - `LOG_DIR`: directory for logs
- Raid setup
  - `SKIP_FARM_LISTS_FIRST_RUN`: `true/false`
  - `ESCORT_UNIT_PRIORITY`: preferred `tX` order
- Learning loop (escort adjustments)
  - `LEARNING_ENABLE`: `true|false` (global on/off)
  - `LEARNING_MIN_MUL`, `LEARNING_MAX_MUL`
  - `LEARNING_LOSS_THRESHOLD_LOW`, `LEARNING_LOSS_THRESHOLD_HIGH`
  - `LEARNING_STEP_UP_ON_LOST`
  - `LEARNING_STEP_UP_ON_HIGH_LOSS`
  - `LEARNING_STEP_UP_ON_FULL_LOOT`
  - `LEARNING_PAUSE_ON_LOSS_SEC`
  - `LEARNING_PRIORITY_RETRY_SEC`
- Credentials
  - `TRAVIAN_EMAIL`, `TRAVIAN_PASSWORD`

- Progressive tasks
  - `PROGRESSIVE_TASKS_ENABLE`: `true|false`
  - `PROGRESSIVE_TASKS_REFRESH_HUD`: `true|false`

## Identity & Tribe Detection

- Identity lives in `API_based_automations/travian_bot/database/identity.json`.
- The bot now derives the numeric `tribe_id` automatically from the `faction` text using `core/unit_catalog.py::FACTION_TO_TRIBE`.
- Keep the faction string accurate (e.g. `Huns`, `Romans`); the YAML `TRIBE_HINT` is informational only.
- If Travian introduces a new tribe, update the mapping table and rerun `setup_identity.py`.

- Reports
  - `PROCESS_RALLY_RETURNS`: toggle the rally tracker pass each cycle

## Learning Loop (Rally Overview)

- After each raid the bot queues a pending entry in `database/learning/pending_rally.json` (when `LEARNING_ENABLE: true`).
- The rally tracker polls `build.php?gid=16&tt=1` per village, matches returning raids by target + arrival time, and adjusts the multiplier using the actual bounty and surviving escorts.
- Expected return timestamps are derived from the launch timestamp + travel duration (captured during the send step). If a raid never returns before `RALLY_RETURN_TIMEOUT_SEC`, it is treated as a full loss. Partial losses are inferred from the difference between sent vs. returning troops.
- When `LEARNING_ENABLE: false`, no pendings are queued and multipliers remain fixed at 1.0.

Per‑cycle report (printed by the launcher):
- Raids sent/skipped + skip reasons summary
- Hero status (present/health/level)
- Recent learning multiplier changes
Raw metrics are stored in `database/metrics.json` and counters reset each cycle.

Cycle header status (human‑readable)
- Unread reports count, task rewards available, adventure count
- Example: `[Main] 📬 Unread reports: 3 | 🎁 Task rewards: 2 | 🗺️ Adventures: 1`

Rally processing status (per cycle)
- Example: `[Main] 📨 Rally tracker: processed 1` or `no pendings`

## Attack Detector (OCR → Discord)

Optional screen monitor that detects “incoming attack” text and posts a Discord message (with screenshot when possible).

- Enable via launcher Tools menu (option 12):
  - Toggle Attack Detector ON/OFF
  - Set Discord Webhook URL
  - Send test notification (with screenshot when GUI available)

- Configure in `config.yaml`:
  - `ATTACK_DETECTOR_ENABLE: true|false`
  - `ATTACK_DETECTOR_DISCORD_WEBHOOK: "https://discord.com/api/webhooks/..."`
  - `ATTACK_DETECTOR_INTERVAL_BASE`, `ATTACK_DETECTOR_INTERVAL_JITTER`
  - `ATTACK_DETECTOR_COOLDOWN_SEC` (min. gap between notifications)
  - `ATTACK_DETECTOR_OCR_LANGS: ["en", ...]`, `ATTACK_DETECTOR_GPU: false`
  - `ATTACK_DETECTOR_SEND_SCREENSHOT: true|false`
  - `ATTACK_DETECTOR_USE_HOTKEYS: false` and `ATTACK_DETECTOR_REFRESH_COMBO: "f5"|"ctrl+r"|"command+r"`

Notes:
- Screenshots require a desktop/GUI. In headless environments, the detector sends text-only messages.
- Dependencies are in `requirements.txt` (pyautogui, Pillow, easyocr). Install inside the venv when you plan to use the detector.

## Data Paths

- Farm lists: `database/farm_lists/`
- Raid plans: `database/raid_plans/`
- Identity: `database/identity.json`
- Map scans: `database/full_map_scans/`
- Unoccupied oases: `database/unoccupied_oases/`
- Learning store: `database/learning/oasis_stats.json`
- Learning pendings: `database/learning/pending_rally.json`
- Metrics: `database/metrics.json`

## Crontab Examples

Run the bot or CLI periodically via cron (adjust absolute paths and indices as needed).

- Full‑auto loop every hour starting at minute 2:
  - `2 * * * * cd /absolute/path/to/API_based_automations/travian_bot && /bin/bash -lc '. venv/bin/activate && python launcher.py' >> logs/cron.log 2>&1`

- Fast map scan of village index 0 every 6 hours:
  - `5 */6 * * * cd /absolute/path/to/API_based_automations/travian_bot && /bin/bash -lc '. venv/bin/activate && python cli.py scan --village 0 --radius 25 --fast' >> logs/scan_cron.log 2>&1`

Tip: use different schedules for heavy scans vs. regular farming to spread load and remain stealthy.

## Troubleshooting

- Missing `bs4` → activate venv: `source venv/bin/activate`; `pip install -r requirements.txt`.
- “Missing credentials” → set `TRAVIAN_EMAIL` and `TRAVIAN_PASSWORD` in YAML.
- 4xx on login → recheck email/password and `SERVER_SELECTION`; review additional error text.
- No troops table → UI/language/layout can vary; retry.
- No learning changes yet → wait ≥5 minutes after raids; checker runs periodically.

## Contributing

Feel free to submit issues and pull requests.

## Disclaimer

This is an unofficial bot for Travian Legends. Use at your own risk. The authors are not responsible for any consequences of using this bot. This bot exists for an educational purpose (my own) to prove I understand Travian API and structures. That's all. 

## License

MIT License
