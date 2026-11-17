# Architecture & Infrastructure

The *what* backing the compact root `CLAUDE.md`. The *why* behind non-obvious
choices lives in `docs/decisions.md`; open limitations in `docs/KNOWN_ISSUES.md`.

## Data flow

```
nCore RSS  ──fetch──▶ autorss.py ──[Film] filter──▶ remux/720p pre-filter
                          │                                  │
                          │                          Deepseek verdict (cached per clean title)
                          │                                  │
                          ▼                                  ▼
                   docker exec curl ───────────▶ qBittorrent (category "auto-film")
                          ▲                                  │
torrent-manager.py monitor (cron 2h + autorss finally) ─────┘
   peers, reconcile, metrics (.prom), Grafana panel, up/down counters
```

Everything runs on the host as plain Python 3 (stdlib only); the only external
binary is `/usr/bin/docker` (to reach qB) plus `curl` *inside* the container.

## Scripts & roles

- **`autorss.py`** (`_run`): read keys → write `last_run_ts` → fetch RSS
  (`fetch_films`, keeps `[Film` items) → for each new torrent id: `clean_title`
  dedup, remux/720p pre-filter, Deepseek (`ask_deepseek`, cached) → record verdict →
  spawn `ensure-space` (unconditionally, so the hard cap is enforced on quiet runs too).
  Download phase: compute free slots vs `MAX_ACTIVE_DOWNLOADS`, sort pending
  newest-first, hold if category over `CATEGORY_CAP_GB`, else
  `qb_add_confirmed` each. `main()` holds the `.lock` flock for the whole run and
  always refreshes metrics in `finally` (`torrent-manager.py monitor`).
- **`torrent-manager.py`**
  - `monitor` (default): `fetch_auto_torrents` → `update_zero_timers` →
    `reconcile_state` (downloaded→removed when gone from qB) → `compute_metrics` →
    `write_metrics` → `update_films_panel`. Never deletes.
  - `ensure-space [CAP_GB]`: delete completed 0-peer seeders — the smallest single one
    covering the overage, else smallest-first — until the category's committed size is
    back under CAP_GB.
  - Accumulators: `accumulate_uploaded` / `accumulate_downloaded` (via `_accumulate`)
    maintain monotonic, deletion-safe byte counters in `manager-state.json`.
- **`autorss_common.py`**: `BASE_DIR`/paths, qB constants, `docker_curl`,
  `qb_torrents`, `peers`, `torrent_size`/`category_used_bytes` (committed size),
  `dead_swarm` (no complete copy reachable), `is_active_download` (the single
  queue-gate ⇆ metric predicate), `flock_or_exit`,
  `mutate_state` (locked state RMW), `env_value`.

## Film state machine (`state.json` → `films[<torrent_id>]`)

`pending` (Deepseek yes, not yet downloaded) → `downloaded` → `removed` (gone from
qB; re-acquirable). Terminal/other: `rejected` (Deepseek no), `skipped` (remux or
quality pre-filter — a different-quality variant may still qualify), `add_failed`
(unconfirmed after `MAX_ADD_ATTEMPTS`).

- `SEEN_STATUSES = {pending, downloaded, rejected}` block reprocessing other releases
  of the same film. `skipped`/`removed` deliberately do **not** block.
- `DECIDED_STATUSES` (adds `removed`, `add_failed`) = verdicts reusable across
  quality variants; `skipped` excluded (it never asked Deepseek).

## State files & locking

- `state.json` — films + `_meta` (run/feed timestamps). Written by BOTH scripts.
- `manager-state.json` — `zero_since` timers, `deleted_total`/`deleted_log`,
  `up_seen`/`uploaded_total`, `down_seen`/`downloaded_total`, `films_panel_hash`.
- Lock files (gitignored, empty): `.lock` (autorss run), `.manager.lock` (manager
  run), `.state.lock` (shared — serialises every `state.json` read-modify-write).
- All `state.json` mutations go through `C.mutate_state(fn)` under `.state.lock`, so
  the two cron jobs can't clobber each other. `flock_or_exit`'s returned fd MUST be
  kept referenced for the whole run (see `docs/decisions.md`).

## Disk / category policy

Bounded by the auto-film **category's committed size** (`category_used_bytes`, full
`size` not partial `completed`), not host free space. `< CATEGORY_HARD_GB (600)`:
no deletion. `600–620`: trim completed 0-peer seeders toward 600. `> CATEGORY_CAP_GB
(620)` and can't trim: hold the queue, add nothing. `/data/torrents` lives on `/`
(~1.8 TB, shared with the OS).

## Cron schedule

```cron
*/5  * * * * /usr/bin/python3 <project>/autorss.py            >> .../cron.log 2>&1
0   */2 * * * /usr/bin/python3 <project>/torrent-manager.py monitor >> .../torrent-manager-cron.log 2>&1
```

`autorss.py` also invokes the manager twice per run: `ensure-space` after the decision
loop (before the pending/slot gates, so a run with nothing to add still trims), and
`monitor` in its `finally`. Manager runs self-exclude via `.manager.lock`.

Logs (`cron.log`, `autorss.log`, `torrent-manager.log`, `torrent-manager-cron.log`) are
rotated weekly, 8 kept, gzip'd, by a user-level logrotate (no sudo on the host): config
`logrotate.conf`, state `.logrotate.state` (gitignored), run daily from the user crontab
(`25 4 * * *`, errors to `logrotate.log`). No `copytruncate`: each cron run reopens the log.

## Metrics (Prometheus textfile collector)

Written to `TEXTFILE_DIR` (the node_exporter textfile-collector directory), scraped via node_exporter.

`autorss.prom` (torrent-manager `monitor`):
`autorss_films_total{status=approved|rejected}`, `autorss_active_downloads`,
`autorss_seeding_total`, `autorss_disk_used_gb`, `autorss_uploaded_bytes_total`
(counter, monotonic, deletion-safe), `autorss_downloaded_bytes_total` (same),
`autorss_deleted_total`, `autorss_peers_zero_total`, `autorss_stalled_downloads`.

`autorss_run.prom` (autorss `write_run_metrics` — liveness):
`autorss_last_run_timestamp_seconds`, `autorss_last_success_timestamp_seconds`,
`autorss_feed_items`, `autorss_feed_films`.

## Grafana dashboard

- Dashboard "Server Overview" (UID set as `DASH_UID` in `torrent-manager.py`),
  **"Auto-Film" row**, backed by a Prometheus datasource.
- Panels: 5 stats (Deepseek döntések, Aktív letöltések + seedelés, 0 peer torrentek,
  Törlések, Disk usage), a **Daily Upload / Download (GB)** bar chart
  (`increase(...[1d])/1e9`, 1d step, w=12 at row 2 x=0; green Upload + blue
  Download). It uses `timeFrom:7d` + `timeShift:-1d` (window `[now-6d, now+1d]`):
  the +1d end is REQUIRED because a 1d step aligns to UTC midnight, so without it
  the current in-progress day is never an eval point and the panel renders blank.
  An **Upload / Download rate (GB/h)** timeseries
  (`rate(...[$__rate_interval])*3600/1e9`, `timeFrom:30d`, legend
  mean/max/last, GB axis), and a markdown text panel
  **"Last 50 Approved Films & Series"** (description marker `autorss-films-text`;
  lists films and series episodes with Source, Size, GB/day).
- The text panel is auto-refreshed by `update_films_panel`: GET
  `/api/dashboards/uid/<UID>`, mutate the matched panel, POST `/api/dashboards/db`
  with `overwrite:false` + the fetched `version` (optimistic lock). It matches by
  the marker, or a title-substring panel that is already `type:text` — it will not
  convert/destroy a data panel.
- Same Basic-auth GET/POST pattern is used to add/edit panels. Hash-gated so an
  unchanged panel isn't re-pushed.

## Repository

- `.gitignore` excludes secrets and machine-local state: `.env`, `.env.*` (keeps
  `.env.example`), `state.json`, `manager-state.json`, `tmdb-cache.json`, `*.log`, the
  three lock files, `__pycache__/`.

## Environment / secrets

- `PROJECT_ENV` = `./​.env` (gitignored): `NCORE_RSS_KEY`, `GRAFANA_URL`,
  `GRAFANA_USER`, `GRAFANA_PASS`. Template: `.env.example`.
- `TRADING_ENV` (a separate `.env`, path set in `autorss_common.py`): the `DEEPSEEK` API key (read
  from there, not from this project's `.env`).
- `env_value` prefers an exact var-name match, falling back to substring.
