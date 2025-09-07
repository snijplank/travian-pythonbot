# 📌 Roadmap – Travian Bot

## 1️⃣ Stabiliteit & Basis
**Doel: zorgen dat de bot voorspelbaar en veilig draait.**
- [ ] **Cycle-limiter (max 10u/dag)**  
  - Config-optie (in `.env` of `config.json`) om totale draaitijd per dag te beperken.  
  - Splitsen over meerdere blokken (bijv. 3× ~3u).  
- [ ] **Logging & monitoring**  
  - Schrijf logs ook naar bestand (`logs/bot.log`) met rotating handler.  
  - Statusmeldingen per cycle (start, raids done, hero summary).  
- [ ] **Error recovery**  
  - Verbeter re-login fallback (bijv. max retries, cooldown).  
  - Automatisch skippen van farm/oasis run bij herhaald falen.  
- [ ] **Config-centralisatie**  
  - Eén plek voor `WAIT_BETWEEN_CYCLES`, `JITTER`, `SERVER_SELECTION`, daily limits.  
  - `.env` of `config.yaml` → makkelijker voor eindgebruikers.  

---

## 2️⃣ Uitbreidingen & Features
**Doel: meer functionaliteit en flexibiliteit toevoegen.**
- [ ] **Advanced scheduling**  
  - Dag/nachtprofiel (bv. ’s nachts alleen hero-raids).  
  - Random breaks om bot-gedrag menselijker te maken.  
- [ ] **Farm/oasis manager**  
  - UI/CLI om farm lists live te togglen.  
  - Statistieken per raid-plan (success rate, losses).  
- [ ] **Hero AI uitbreiden**  
  - Slimme detectie van gevechtsverlies → auto-pauze.  
  - Hero-items beheren (zalf, cage, scrolls).  
- [ ] **Map intelligence**  
  - Oases combineren met spelerinfo (farmable dorpen herkennen).  
  - Export naar CSV/JSON voor offline analyse.  

---

## 3️⃣ Optimalisatie & Integraties
**Doel: duurzaamheid, community en tooling.**
- [ ] **Discord / Telegram integratie**  
  - Notificaties bij cycle, fouten, hero dood, raids klaar.  
  - Commands om bot op afstand te pauzeren/starten.  
- [ ] **Stats dashboard**  
  - Grafieken van raids per dag, hero XP, resource haul.  
  - Kan eenvoudig via `matplotlib` of web-dashboard (Flask/FastAPI).  
- [ ] **Plugin-architectuur**  
  - Makkelijk nieuwe “features” inschalen (bv. wonder support, alliance pushes).  
- [ ] **Testing & CI**  
  - Unit tests voor API-calls & parsers.  
  - GitHub Actions om install/run smoke tests te doen.  

---

⚖️ **Prioriteit-volgorde (kort):**  
1. Cycle limiter + logging (essentieel voor veilige runs)  
2. Config centraliseren  
3. Hero & raid managers uitbreiden  
4. Externe integraties (Discord/Telegram)  




Roadmap — vervolg

1) Robuustheid & configuratie (Short-term)
	•	Centraliseer unit-namen en stats
Eén bron (bv. core/unit_catalog.py) met:
	•	uXX ↔ tY mapping
	•	naam per stam
	•	attack/defense/speed/cargo per unit (we hebben al combat_stats – samenvoegen).
	•	Config hardening
	•	.env + config.yaml validatie (required keys, types) met duidelijke foutmeldingen.
	•	Consistente keys: ESCORT_UNIT_PRIORITY, ESCORT_SAFETY_FACTOR, WAIT_BETWEEN_CYCLES_MINUTES, JITTER_MINUTES, DAILY_RAID_LIMIT.
	•	Held-ATK caching
Cache hero attack (5 min) met fallback naar laatste bekende waarde → minder requests, stabieler.
	•	Herkenbare skip vs fail overal
Alle “Failed…” messages harmoniseren (we begonnen hiermee). Maak een Result-object (sent | skipped | failed + reason).

2) Anti-frictie & observability
	•	CLI flags voor launcher
--full-auto, --server=…, --skip-first-farm-lists, --headless (slaat menu over; handig voor cron/systemd).
	•	Metrics & counters
In-memory en logfile:
	•	raids_sent / raids_skipped / hero_sends / hero_skips
	•	redenen (geen escort, te weinig troepen, token-fail, confirm-missing)
	•	gemiddelde cycle-duur, requests per cycle.
	•	Structured logging
Log naar JSON (optioneel) naast human logs → makkelijke grep/analyses.

3) Raid-intelligentie
	•	Escort planner 2.0
	•	Multi-unit fallback (t5 → t3 → t1 met mix als enkelvoud niet genoeg is).
	•	Veiligheidsfactor per dier-type (tigers/beren hoger).
	•	Oasis-target selectie
	•	Prioriteer oases op power/afstand/expected loot.
	•	Optie: “skip oases met recente nederlaag” (cooldown per target).
	•	Learning loop (eenvoudig)
	•	Sla uitkomst per target op (success/fail/returns).
	•	Pas aanbeveling aan (minder/méér escorts) op basis van resultaat.

4) Farm lists UX & beheersbaarheid
	•	Farm list editor (TTY menu): create/enable/disable per dorp.
	•	Auto-refresh farm lists
Sync uit GraphQL → configfile → run. Waarschuwing bij desync.
	•	Rate-limit & backoff
Eenvormige retry/backoff wrapper (429/5xx), jitter per call.

5) Scheduler & multi-account
	•	Per-dorp schema
	•	Eigen cycle-timers per dorp (niet alles tegelijk).
	•	Multi-account (optioneel)
Structuur klaarzetten voor meerdere .env profielen met sequentiële runs.

6) Veiligheid & stealth
	•	Realistisch gedrag
	•	Extra random delays/jitter per actie (al deels aanwezig, verfijnen per endpoint).
	•	Variëren van user-agent / request order (binnen verantwoord kader).
	•	Request-budget per uur/dag
	•	Hard caps met duidelijke logs: voorkomt lockouts.
	•	Sanity guards
	•	Niet raiden bij hero health < X%,
	•	Geen escort sturen als dorp onder minimum defensievoorraad zakt (configurable).

7) Testing & CI
	•	Unit tests voor:
	•	uXX → tY mapping, naamresolver, combat berekeningen, token/confirm parsers.
	•	Golden file tests voor HTML parsers (mini fixtures).
	•	Pre-commit: black, ruff/flake8, mypy (light).
	•	GitHub Actions: lint + tests op push/PR.

8) Data & opslag
	•	Kleine state DB (SQLite/JSON)
	•	per-dorp counters, laatst-geraide oases, hero_atk cache, cooldowns.
	•	Export rapport
	•	Dagrapport in logs/reports/YYYYMMDD.md: aantal raids, skips, top redenen, held status.

⸻

Kleine, concrete issues die we zo kunnen oppakken
	1.	unit_catalog: samenvoegen naam+stats+mappings; alle modules laten importeren (verwijder duplicatie).
	2.	hero_atk_cache: cache met TTL en fallback.
	3.	Result-type (sent/skipped/failed) + consistent logging.
	4.	CLI: --full-auto, --server INDEX, --headless.
	5.	JSON logging toggle: LOG_JSON=true.
	6.	Multi-unit escortmix (t5+t1) met clamp en skip bij shortage.
	7.	Oasis selection scoring (distance, power, cooldown).
	8.	Retry/backoff wrapper + central HTTP client.
	9.	Tests: mapping/parsers; fixtures voor send_hero_form_dump.html.
	10.	Daily report writer.
