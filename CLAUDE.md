# ncore-autorss — Project Guide

nCore RSS → Deepseek mainstream-film filter → qBittorrent auto-seeder, tuned to
maximise the nCore "ERTEK" seeding score. Three stdlib-only Python scripts running
on the server via cron (no Docker compose of its own — it drives the existing
`qbittorrent` container). Prometheus + Grafana monitoring.

## Repo map

- `autorss.py` — main pipeline (cron */5 min). RSS fetch → remux/720p pre-filter →
  Deepseek verdict → queue downloads (≤`MAX_ACTIVE_DOWNLOADS`, newest-approved first).
  Series pilot: a few TMDB-popular 720p episodes/day into the same category (`kind=series`).
- `torrent-manager.py` — `monitor` (cron 2h + called by autorss in `finally`): peer
  sampling, metrics, state reconcile, Grafana panel, up/down accumulators.
  `ensure-space [CAP_GB]`: trim completed 0-peer seeders when the category is over cap.
  **Never deletes a torrent with peers or one still downloading.**
- `watchlist.json` — hand-edited shows to follow (`plan_watch`): every new single episode, any resolution, no TMDB gate, outside the pilot quota; dropped after `WATCH_IDLE_S` (28d) without a new episode.
- `tmdb.py` — TMDB lookup (needs `TMDB_READ_TOKEN` in `.env`; inert without it). Adds an
  approval when Deepseek rejects a popular, recent film it never heard of.
  `tmdb-replay.py` — read-only: caches TMDB facts for recent verdicts to calibrate it.
- `autorss_common.py` — shared spine: constants, qB API via `docker exec`, flock,
  locked state I/O (`mutate_state`), `env_value`, the `is_active_download` predicate.
- `dryrun-ensure-space.py` — read-only: prints the trim plan `ensure-space` would follow
  (shares `select_victims`, so it can't drift from the real run). Deletes nothing.
- `test_autorss.py` — stdlib unittest, no deps (`python3 test_autorss.py`).
- `docs/` — `architecture.md` (what), `decisions.md` (why), `KNOWN_ISSUES.md` (open).

## qBittorrent access (the core gotcha)

qB runs in the `qbittorrent` Docker container with localhost-auth-bypass. Host
requests get **403 through Docker NAT**, so every qB API call goes via
`docker exec qbittorrent curl ...` (`autorss_common.docker_curl`) — never host curl.

## Key tunables (`autorss_common.py`)

| Const | Value | Meaning |
|---|---|---|
| `CATEGORY_HARD_GB` | 600 | trim completed 0-peer seeders above this category size |
| `CATEGORY_CAP_GB` | 620 | hold the queue (don't add) above this |
| `MAX_ACTIVE_DOWNLOADS` | 3 | concurrent auto-film downloads |
| `MAX_ADD_ATTEMPTS` | 15 | unconfirmed adds before `status=add_failed` (per-run count: 15 x 5 min = 75 min) |
| `STALL_TIMEOUT_S` | 6h | peerless download past this = dead, frees a slot |
| `TMDB_MIN_POPULARITY` / `TMDB_MIN_VOTES` | 30 / 20 | TMDB popularity+votes that approve a film Deepseek rejected |
| `SERIES_DAILY_MAX` / `SERIES_MAX_BYTES` | 3 / 1.5 GB | series pilot: episodes queued per rolling 24h / max size each |
| `TRIM_IDLE_S` | 14d | ensure-space deletes seeders idle this long first (smallest sufficient among them) |
| `HNR_MARGIN_S` | 2h | slack on nCore's hit-and-run seed time before ensure-space may delete a seeder |
| `UNREGISTERED_GRACE_S` | 24h | tracker says "unregistered torrent" this long -> `monitor` removes it |

Disk policy bounds the **category's committed size**, not host free space; deletion
is a last resort. Filter regexes, `MAX_YEAR`, and the status sets live in `autorss.py`.

## Key commands

```bash
python3 test_autorss.py                       # tests (stdlib, no deps)
python3 torrent-manager.py monitor            # refresh metrics + Grafana panel; removes 24h-unregistered torrents (0-peer seeders, or any incomplete one)
python3 dryrun-ensure-space.py [CAP_GB ...]   # what a trim WOULD delete (read-only)
python3 torrent-manager.py ensure-space 600   # manual trim to a cap (GB)
python3 autorss.py                            # one pipeline run
docker exec qbittorrent curl -s "http://localhost:8080/api/v2/torrents/info?category=auto-film"
```

## Context — read when relevant

| Task | Read first |
|---|---|
| Architecture / cron / metrics / Grafana / GitHub | `docs/architecture.md` |
| Why a non-obvious choice was made | `docs/decisions.md` |
| The seeding score being optimised (ERTEK) | `docs/decisions.md` |
| Open problems / accepted limitations | `docs/KNOWN_ISSUES.md` |

## CLAUDE.md maintenance rules

- Keep concise (~60 lines). Overflow goes to `docs/`, not here.
- No detail inferable from the code directly; the *why* lives in `docs/decisions.md`.
- Secrets (`.env` passkey, Grafana creds, Deepseek key) are gitignored — never
  document their values.
