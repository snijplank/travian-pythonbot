# 📌 Roadmap – Travian Bot (actueel)

Deze roadmap reflecteert wat af is, wat in gang is gezet en wat logisch is als volgende stap. De focus blijft op stabiliteit, stealth en uitbreidbaarheid.

## 1️⃣ Stabiliteit & Basis
**Doel: voorspelbaar, veilig en makkelijk te bedienen.**
- [x] Config-centralisatie (YAML-only) met validatie in loader
  - `config.yaml` als single source of truth; geen `.env`
- [x] Logging & monitoring
  - Rotating file handler (`logs/bot.log`)
  - Cycle status + per-cycle rapport (raids sent/skipped, redenen, hero status, learning changes)
- [x] Error handling verbeteringen
  - Non-interactive login; betere foutmeldingen bij 4xx
- [x] Cycle-limiter (daglimiet + blokken)

## 2️⃣ Stealth & Humanizer
**Doel: natuurlijk gedrag, variatie en lage detecteerbaarheid.**
- [x] Per-request think-time + af en toe langere pauzes (configurable)
- [x] Operatie-jitter vóór grote stappen (planner, farm lists)
- [x] Dorpsvolgorde shufflen per cycle
- [x] Random subset van farm lists per dorp (configurable)
- [x] Random skip van farm lists per dorp per cycle (kans)
- [x] Incidentele ‘map view’ (dorf1/dorf2) vóór cruciale acties
- [x] “Coffee break” tussen cycles (kans + extra minuten)

## 3️⃣ Raid-intelligentie & Learning
**Doel: verliezen minimaliseren, escort slim aanpassen.**
- [x] Learning loop: pending → report parse → multiplier nudge (configurable thresholds/steps)
- [x] Per-oase multiplier toegepast op escort aantallen
- [x] Centrale unit-catalogus (u↔t, namen per stam)
- [x] Global toggle: `LEARNING_ENABLE` (zet learning/pendings/ReportChecker uit)
- [x] Cycle statusregel met report‑samenvatting (processed/no pendings/no unread)
- [ ] Escort planner 2.0 (multi-unit fallback + safety factors per dier-type)
- [ ] Cooldown per oase op recente nederlaag (tijdelijk skippen)

## 4️⃣ Farm & Oasis UX
**Doel: beheerbaarheid en inzicht.**
- [ ] TTY/CLI editor: farm lists aan/uit per dorp, live toggles
- [ ] Auto-refresh farm lists uit GraphQL → lokale config sync + waarschuwing
- [ ] Statsoverzicht per raid-plan (succestratio, verliezen, haul)

## 4.1 🏗️ Dorpbeheer (nieuw)
**Doel: basisproductie en militaire opbouw automatiseren, per dorp.**
- [ ] Resourcevelden upgraden (per dorp, per profiel)
  - Profielen: Balanced | Crop‑focus | Wood/Clay early game
  - Respecteer bouwwachttijden; vermijd wachtrijen botsachtig snel te vullen
  - Budgetbewust: minimum resourcebuffer + limiet per cycle
- [ ] Gebouwen upgraden (fundamentals)
  - Warehouse/Granary bij resource overflow‑dreiging
  - Main Building voor snellere bouw
- [ ] Troepen trainen per dorp
  - Per dorp een trainingsprofiel (inf/cav mix) + caps per dag/uur
  - Respecteer resource buffers en held‑status (bij lage health minder training)
  - Queue awareness: detecteer vol/looptijd; niet spammen

### 4.1.1 Nieuw‑dorp preset (gerealiseerd)
- [x] Standaard opbouwpreset (resources → infra → CP → militair → settlers → resources 5)
- [x] Alleen draaien bij detectie van nieuw dorp (GraphQL villages vs state JSON)
- [x] Best‑effort HTML‑parser voor bouw/upgrade knoppen (universeel over varianten)
- [ ] Queue/resource awareness (niet spammen, wachten bij tekorten)
- [ ] Server/taal‑fine‑tuning van building‑detectie

## 5️⃣ Integraties & Observability
**Doel: meldingen en zichtbaarheid.**
- [ ] Discord/Telegram: notificaties (cycle done, errors, hero dood, raids klaar)
- [ ] Optionele JSON logging naast human logs (grep/analyses)
- [ ] Dashboard (Matplotlib of simpele web UI) voor trends (raids/dag, hero XP)

## 5.1 🦸 Held‑automatisering (nieuw)
**Doel: held efficiënt inzetten zonder risico.**
- [x] Hero status/raiding thread (aanwezig)
 - [x] Held naar avonturen sturen (adventures)
  - Detectie via React viewData (GraphQL) op /hero/adventures (mapId, duration, difficulty)
  - Achtergrondthread start automatisch kortste geschikte adventure (config: health/min/max duur/gevaar)
  - Fallback via HTML‑form/URL submit; debug dumps bij fouten
  - Volgende stap: resultaten loggen (loot/XP) voor rapportage

## 9️⃣ Progressive Tasks (nieuw)
**Doel: automatisch claimen van openstaande beloningen.**
- [x] Parse `/tasks` (inline JSON en data-attributes)
- [x] POST `/api/v1/progressive-tasks/collectReward` met `X-Version` en `X-Requested-With`
- [x] Optionele HUD refresh na elke claim
- [x] Toggles: `PROGRESSIVE_TASKS_ENABLE`, `PROGRESSIVE_TASKS_REFRESH_HUD`

## 6️⃣ Scheduler & Multi-account
**Doel: flexibel draaien met meerdere profielen.**
- [ ] Per-dorp schema + eigen timers
- [ ] Multi-account structuur (sequentieel, aparte profielen/credentials)

## 7️⃣ Testing & CI
**Doel: regressies voorkomen.**
- [ ] Unit tests: u↔t mapping, naamresolver, token/confirm parsers
- [ ] Golden-file tests voor HTML parsers
- [ ] GitHub Actions: lint + tests op push/PR

## 8️⃣ Data & Opslag
**Doel: state en rapportage.**
- [x] Metrics snapshot per cycle (`database/metrics.json`)
- [ ] Kleine state-DB (SQLite/JSON): per-dorp counters, laatst geraid, cooldowns, hero_atk cache
- [ ] Dagrapport `logs/reports/YYYYMMDD.md` met kerncijfers

---

🎯 Kortlopende prioriteiten
1. Escort planner 2.0 (multi-unit mix + safety per dier-type)
2. Cooldown per oase + target scoring (afstand, power, recent verlies)
3. CLI flags (–full-auto, –server, –headless) en farm/oasis TTY editor
4. Discord/Telegram notificaties (minimaal: cycle done + errors)
5. JSON logging toggle + basis unit tests (mapping/parsers)

➡️ Opvolgende prioriteiten (dorp & held)
6. Resourceveld‑upgrades per dorp (profielen + budgetten)
7. Troepentraining per dorp (profielen + caps)
8. Held‑adventures integreren met health/loot‑guards

🧩 Kleine, concrete issues
1) Result-type (sent/skipped/failed + reason) als standaard object in logs/metrics
2) Retry/backoff wrapper (429/5xx) rondom HTTP-calls
3) Hero ATK caching (TTL, fallback)
4) Dagrapport writer (samenvatting + top redenen)
5) Unit stats uitbreiden (speed/cargo) in unit_catalog en gebruiken bij planner

---

## ✅ Acceptance Criteria — Next Deliverables

1) Escort Planner 2.0
- Mix: Planner kiest automatisch multi-unit samenstelling (t‑slots) bij tekort aan voorkeursunit.
- Safety: Dier‑type weging (bv. tijger > beer > wolf > zwijn > rat/spin) beïnvloedt benodigde escort.
- Config: Safety‑factoren en fallback‑order instelbaar in `config.yaml`.
- Correctheid: Geen negatieve aantallen; clamp op beschikbaarheid; skip met duidelijke reden bij structureel tekort.
- Logs: Toon gekozen mix, basis vs adjusted aantallen, reden van keuzes (fallback/safety).
- Tests: Unit test met 3 scenario’s (genoeg voorkeursunit, gemengd tekort, zware dieren) slaagt.

2) Cooldown per Oase + Target Scoring
- Cooldown: Na “lost” of >X% verlies zet systeem een per‑oase cooldown (persist in state) en skipt binnen window.
- Scoring: Score combineert afstand, power, recent resultaat, laatste raid‑tijd; gewichten in `config.yaml`.
- Selectie: Planner sorteert oases op score en logt top 5 met componenten.
- Persist: Cooldowns en laatste score/win/loss worden opgeslagen (JSON/SQLite).
- Tests: Unit test voor cooldown toepassen + scoring volgorde met vaste inputs.

3) CLI & TTY UX
- CLI: `python launcher.py --full-auto --server N --headless` start zonder menu; flags overschrijven YAML waar logisch.
- TTY: Simpele editor voor farm lists/raid plan toggles (enable/disable), met veilige persist.
- Docs: README sectie “CLI & TTY” met voorbeelden.
- Smoke: `--full-auto` import/run smoke test werkt in CI (headless).

4) Notificaties (Telegram/Discord)
- Basis: Meldingen voor cycle done (samenvatting), errors (trace), hero dood/missie.
- Config: In/uit via `config.yaml` + token/webhook per kanaal.
- Throttle: Minimaal interval tussen meldingen om spam te voorkomen.
- Tests: Droge run stuurt mock call; geen secrets in logs.

5) JSON Logging + Unit Tests
- JSON: Toggle `LOG_JSON` levert gestructureerde events met `ts,type,reason,result,latency`.
- Schema: Documenteer minimaal event‑types (raid_sent, raid_skip, hero_status, error, http_call).
- CI: GitHub Actions draait lint + unit tests (mapping/parsers/token‑parser) bij push.
- Fixtures: Kleine HTML fixtures voor parser‑tests (token/confirm, troops table).
