# ncore-autorss

Automatic seeder for the Hungarian private tracker [nCore](https://ncore.pro): it reads the
RSS feed, picks the new releases that are likely to get a lot of leechers, adds them to
qBittorrent, and keeps the whole thing inside a disk budget - without ever deleting a
torrent that someone is still downloading from you.

Three small Python 3 scripts, **standard library only** (no `pip install`), run from cron,
with Prometheus + Grafana monitoring.

```
 nCore RSS --> autorss.py --> film filter --> Deepseek verdict --+
                  |          (remux / 720p)        ^             |
                  |                            TMDB rescue       v
                  |                                         qBittorrent
                  +--> series pilot + watchlist ----------> (category "auto-film")
                                                                 |
 torrent-manager.py  <-- peers, state, up/down counters ---------+
        |
        +--> Prometheus textfile metrics --> Grafana
        +--> ensure-space: trim completed, peerless seeders when over the size cap
```

## Why this exists

nCore ranks members with a seeding score (the "ERTEK" formula). It rewards many torrents,
a large total seeded size, long uninterrupted seed time and fresh, in-demand uploads, and it
punishes deleting. So the design is:

- **Seed new, popular titles** - they get the most leechers, so they give the most upload.
- **Keep what you seed.** The disk limit applies to the category's size, and deletion is a
  last resort, never touching a torrent that has peers or is still downloading.
- **Stay hands-off.** Everything is cron-driven and idempotent; state is kept locally.

The full reasoning, including the score formula and the measurements behind each choice, is
in [`docs/decisions.md`](docs/decisions.md).

## How it works

**1. Film pipeline** (`autorss.py`, every 5 minutes)
1. Fetch the RSS feed, keep `[Film` items, skip anything already seen.
2. Pre-filter: remux / full-disc, 2160p and SD releases are dropped, and a 1080p release is
   dropped when a 720p variant of the same film is in the feed. Quality variants of one film
   share a cleaned-title key, so each film is judged once.
3. Ask [Deepseek](https://www.deepseek.com) whether the film is mainstream and released in
   the last 12 months. The model must name the cast, director, studio or franchise it
   recognises the film by, otherwise the answer is "no".
4. **TMDB rescue** (optional): if Deepseek rejects a recent film it simply never heard of,
   [TMDB](https://www.themoviedb.org) popularity and vote counts can still approve it.
5. Queue approved films into qBittorrent, newest first, up to 3 downloads at a time, and
   hold the queue when the category is over its size cap.

**2. Series** (same script)
- *Pilot*: a few single 720p episodes per day of TMDB-popular shows, into the same category.
- *Watchlist* (`watchlist.json`): shows you pick by hand. Every new single episode is queued,
  any resolution, no popularity check. A show drops off after 28 days with no new episode.

**3. Manager** (`torrent-manager.py`, every 2 hours and after each autorss run)
- `monitor`: samples peers, reconciles state with qBittorrent, writes Prometheus metrics,
  updates a Grafana panel, removes torrents the tracker has called "unregistered" for 24 h.
- `ensure-space [CAP_GB]`: if the category is over its limit, deletes completed seeders with
  zero peers - idle ones first, the smallest sufficient set, and only after the tracker's
  hit-and-run seed time has been served.

**Safety rules**: never delete a torrent with peers or one that is still downloading; a
stalled download (no peers for 6 h) stops occupying a slot; qBittorrent adds are confirmed
before a film is marked downloaded and retried a bounded number of times.

## Requirements

- Linux host with Python 3 (developed on 3.12) and cron
- Docker, with qBittorrent running in a container named `qbittorrent`
  (see "The qBittorrent gotcha" below)
- An nCore account and its RSS key
- A [Deepseek](https://platform.deepseek.com) API key
- Optional: a TMDB read-access token (enables the TMDB rescue and the series pilot)
- Optional: Prometheus + node_exporter (textfile collector) and Grafana

## Setup

```bash
git clone https://github.com/<you>/ncore-autorss.git
cd ncore-autorss
cp .env.example .env      # fill in your values; .env is gitignored
python3 test_autorss.py   # 155 tests, no dependencies
```

Then point the paths at the top of `autorss_common.py` (`BASE_DIR`, `TRADING_ENV`,
`TEXTFILE_DIR`, `GRAFANA_TOKEN_FILE`), and the two `sys.path` lines in
`dryrun-ensure-space.py` and `test_autorss.py`, at your own locations. They are plain
constants, not environment-driven.

Configuration lives in `.env` (see `.env.example`):

| Variable | Needed for |
|---|---|
| `NCORE_RSS_KEY` | the `key=` value from your nCore RSS URL |
| `TMDB_READ_TOKEN` | optional: TMDB rescue and the series pilot (inert without it) |
| `GRAFANA_URL`, `GRAFANA_USER`, `GRAFANA_PASS` | optional: the "recent films" panel |

The Deepseek key is read from the variable `DEEPSEEK` in the file `TRADING_ENV` points to,
so it is not duplicated in this project. Change that constant if you would rather keep it in
`.env`. Nothing secret is ever committed: `.env`, runtime state and logs are in `.gitignore`.

### Cron

```cron
*/5 * * * * /usr/bin/python3 /path/to/ncore-autorss/autorss.py              >> /path/to/ncore-autorss/cron.log 2>&1
0 */2 * * * /usr/bin/python3 /path/to/ncore-autorss/torrent-manager.py monitor >> /path/to/ncore-autorss/torrent-manager-cron.log 2>&1
```

`logrotate.conf` is a user-level logrotate config (no sudo needed) for the four log files.

## The qBittorrent gotcha

qBittorrent runs in Docker with localhost authentication bypass. Requests from the host reach
it through Docker NAT, so qBittorrent sees them as non-local and answers 403. Every API call
therefore runs *inside* the container: `docker exec qbittorrent curl ...`
(`autorss_common.docker_curl`). Do not "simplify" this to a host-side `curl`.

## Tuning

All tunables are constants in `autorss_common.py`:

| Constant | Default | Meaning |
|---|---|---|
| `CATEGORY_HARD_GB` | 600 | above this, trim completed 0-peer seeders |
| `CATEGORY_CAP_GB` | 620 | above this, hold the queue (add nothing) |
| `MAX_ACTIVE_DOWNLOADS` | 3 | concurrent auto downloads |
| `STALL_TIMEOUT_S` | 6 h | a peerless download this old counts as dead |
| `TMDB_MIN_POPULARITY` / `TMDB_MIN_VOTES` | 30 / 20 | TMDB bar for approving a film Deepseek rejected |
| `SERIES_DAILY_MAX` / `SERIES_MAX_BYTES` | 5 / 1.5 GB | series pilot: episodes per 24 h, max size each |
| `WATCH_IDLE_S` | 28 d | watchlist show dropped after this long without a new episode |
| `TRIM_IDLE_S` | 14 d | seeders idle this long are trimmed first |
| `HNR_MARGIN_S` | 2 h | slack on hit-and-run seed time before a seeder may be deleted |
| `UNREGISTERED_GRACE_S` | 24 h | tracker says "unregistered" this long, then remove |

### watchlist.json

```json
{"shows": [{"name": "Some Show", "since": "2026-11-12", "have": ["S01E01"]}]}
```

`since` is the date you added the show (the 28-day idle timer counts from it or from the last queued episode); `have` lists episodes you already downloaded by hand, which are skipped.

## Commands

```bash
python3 autorss.py                          # one full pipeline run
python3 torrent-manager.py monitor          # refresh metrics and the Grafana panel
python3 torrent-manager.py ensure-space 600 # trim the category to 600 GB now
python3 dryrun-ensure-space.py [CAP_GB ...] # show what a trim WOULD delete (deletes nothing)
python3 tmdb-replay.py                      # read-only: cache TMDB facts for recent verdicts
python3 test_autorss.py                     # test suite
```

## Monitoring

`torrent-manager.py monitor` writes Prometheus textfile metrics for node_exporter's textfile
collector, for example `autorss_seeding_total`, `autorss_disk_used_gb`,
`autorss_active_downloads`, `autorss_films_total{status}`, `autorss_peers_zero_total`, and the
monotonic, deletion-safe counters `autorss_uploaded_bytes_total` and
`autorss_downloaded_bytes_total`. `autorss.py` adds liveness metrics
(`autorss_last_run_timestamp_seconds`, `autorss_last_success_timestamp_seconds`). A Grafana
"Auto-Film" row charts daily and hourly upload/download and lists the last 50 approved films
and episodes. The full metric list and dashboard notes are in
[`docs/architecture.md`](docs/architecture.md).

## Repository map

| Path | Role |
|---|---|
| `autorss.py` | main pipeline: RSS, filter, Deepseek, TMDB, series, queueing |
| `torrent-manager.py` | `monitor` and `ensure-space` |
| `tmdb.py` | TMDB lookups (inert without a token) |
| `autorss_common.py` | constants, qBittorrent access, locking, state I/O |
| `dryrun-ensure-space.py`, `tmdb-replay.py` | read-only inspection helpers |
| `test_autorss.py` | stdlib `unittest` suite |
| `watchlist.json` | your hand-edited list of shows to follow |
| `logrotate.conf` | user-level log rotation |
| `docs/architecture.md` | what: data flow, state, locking, metrics |
| `docs/decisions.md` | why: the reasoning behind non-obvious choices |
| `docs/KNOWN_ISSUES.md` | open problems and accepted limitations |

## Disclaimer

This is a personal tool built around one private tracker's rules. Read nCore's rules before
automating anything against it, and respect the tracker's hit-and-run and seeding
requirements. Use at your own risk. Film filtering relies on an LLM and can be wrong; see
[`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md).
