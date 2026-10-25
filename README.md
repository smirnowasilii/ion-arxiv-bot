# ion-arxiv-bot
Daily arXiv digest of trapped-ion physics, delivered to Telegram.

📢 **Channel: [t.me/ion_arxiv](https://t.me/ion_arxiv)** — join to get new papers.



Watches arXiv for new papers on trapped ions and posts them to Telegram, tagged by topic:

| Tag | Covers |
|---|---|
| 💻 Quantum computing | gates, QCCD/shuttling, QEC, logical qubits, benchmarking |
| 🧲 Quantum simulation | spin models, many-body dynamics, lattice gauge, time crystals |
| ⏱️ Clocks & frequency metrology | Al⁺/Yb⁺/Ca⁺ clocks, quantum-logic spectroscopy, ²²⁹Th nuclear clock |
| 🔗 Networks & quantum memories | remote entanglement, photonic interconnects, frequency conversion, memory nodes |
| 📡 Sensing & fundamental physics | squeezing, dark matter, EDM, isotope shifts, Penning-trap precision tests |
| 🛠️ Control & hardware | cooling, integrated photonics, trap fabrication, readout |
| ⚗️ Molecular, HCI & hybrid | molecular ions, highly charged ions, ion–atom systems |

Papers whose abstract says *we demonstrate / we observe / we measure…* also get a **🧪 Experiment** tag.
Everything (categories, keywords, topics) lives in `config.yaml` — edit freely.

## 1. Create the bot

1. In Telegram, message **@BotFather** → `/newbot` → pick a name → copy the token.
2. Install: `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`
3. Preview what it would post (no Telegram needed):
   ```bash
   python bot.py dry-run --days 7
   ```

## 2. Run it

**Option A — interactive bot (needs an always-on machine: server, Raspberry Pi, VPS)**
```bash
export TELEGRAM_BOT_TOKEN=123456:ABC...
python bot.py run
```
Open your bot in Telegram, send `/subscribe`. It checks arXiv every 3 h (configurable).
For 24/7 operation use the included `ion-arxiv-bot.service` (instructions inside).

Commands:
- `/subscribe`, `/unsubscribe`
- `/latest 10` — newest relevant papers
- `/topic clocks 5` — newest in one topic (`/topics` lists keys)
- `/search "quantum memory" Ba` — search within trapped-ion papers
- `/check` — check now · `/status`

Works in groups and channels too (add the bot, send `/subscribe`; for a channel, put its id in `default_chat_ids`).
To keep the bot private, put your Telegram user id in `allowed_user_ids` (`/start` shows it).

**Option B — free, no server: GitHub Actions (push notifications only, no commands)**
1. Put this folder in a GitHub repo (private is fine).
2. Get your chat id: run `python bot.py run` once locally and send `/start` — it replies with the id.
3. Repo → Settings → Secrets → Actions: add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. The workflow in `.github/workflows/arxiv-watch.yml` runs every 4 h and commits `state.json`.

**Option C — cron on any machine:** `0 */4 * * * cd /path/to/bot && TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... .venv/bin/python bot.py once`

## How it works

1. Queries the arXiv API (newest first) for papers in the configured categories (quant-ph, physics.atom-ph, …) matching `query_terms` — a broad net.
2. Keeps only papers whose title/abstract matches `relevance.require_any` (trapped ion, Paul/Penning trap, ion crystal, species like ⁴⁰Ca⁺ / Yb⁺ …) and none of `exclude_any` (ion implantation, Li-ion batteries…). LaTeX is converted to Unicode before matching.
3. Tags topics, remembers sent arXiv ids in `state.json`, and posts only new ones.

arXiv publishes new submissions once a day (~20:00 US Eastern, Sun–Thu → about 03:00–04:00 Moscow time), so you'll mostly see one batch per morning.

## Tuning tips

- Too much noise from a term? Remove it from `require_any` (`single ion` is the broadest one).
- Missing papers? Add the phrase to both `query_terms` and `require_any`.
- Prefer one digest message per batch? Set `telegram.mode: compact`.
- Terms match at word starts, so `magnetometr` catches magnetometer/magnetometry. Use `re:` for regex.
