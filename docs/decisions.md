# Decisions

The *why* behind non-obvious choices. The *what* is in `docs/architecture.md`.

## The optimisation target: nCore "ERTEK" seeding score

Everything about the disk/seeding policy exists to maximise the nCore score:

```
ERTEK = up(interval) · avg(torrdate) · time · seedsize · skala_torrszam
        · regdate · upload · owntorr
```

- `up(interval)` — bytes uploaded in the last 7 days / uninterrupted seed run.
- `avg(torrdate)` — average age of running torrents (newer = better).
- `time` — total seed time across all running torrents.
- `seedsize` — total bytes seeding.
- `skala_torrszam` — torrent-count tier multiplier (step function: 0–10=1×,
  11–25=1.5×, 26–75=2×, 76–150=2.5×, 151–250=3×, 251+=3.5×).
- `regdate`, `upload` (lifetime), `owntorr` (own uploads) — account-level, **not
  controllable by this bot**.

Because the score is a **product**, every factor matters and a 0-peer torrent still
contributes full weight to `time`, `seedsize`, and `skala_torrszam` (only `up` is
zero). That single fact drives the policies below.

## Disk policy bounds the category, not host free space

A flat host-free-space floor would force deleting seeders to keep headroom, which
craters `time`/`seedsize`/`skala_torrszam`. Instead we bound the auto-film
category's own committed size (`CATEGORY_HARD_GB`/`CATEGORY_CAP_GB`) and **prefer
holding the queue over deleting**. Deletion is a genuine last resort.

## Deletion: only completed 0-peer seeders, largest-first

`ensure_space` candidates are `peers==0 AND progress>=1.0` only — never an
in-progress download (would discard partial data and waste the slot) and never a
torrent with peers. Largest-first within the 0-peer set frees the deficit while
removing the **fewest** torrents, best preserving the `skala_torrszam` count tier.
Its domain (completed seeders) is disjoint from the queue gate's (incomplete
downloads), so the two predicates can't disagree about the same torrent.

## Never auto-stop seeding (`ratioLimit=-1`, `seedingTimeLimit=-1`)

Set per-torrent on add (and back-filled onto existing torrents). A paused/stopped
torrent stops counting toward `time`/`seedsize`/`up(interval)`, so any global qB
ratio/seed-time cap would silently bleed score. `-1` = no limit, overriding globals.

## Concurrency + newest-first

`MAX_ACTIVE_DOWNLOADS=3` (was effectively 1) grows the torrent count faster (crosses
`skala_torrszam` tiers sooner). Pending downloads are taken newest-approved first:
hot releases peak in leechers in their first hours, maximising `up(interval)` and
keeping `avg(torrdate)` fresh.

## seen_clean / decided exclude skipped & removed

A remux or non-720p variant indexed first must NOT block the legitimate 720p variant
of the same film. So the quality pre-filter `skipped` status does not enter
`seen_clean` (dedup) or `decided` (verdict reuse), and `removed` (freed for space)
does not block re-acquisition. Only real Deepseek verdicts
(`pending`/`downloaded`/`rejected`) block other releases.

## Monotonic, deletion-safe upload/download counters

qB's per-torrent `uploaded`/`downloaded` vanish when a torrent is removed, so
summing them live is non-monotonic — a drop reads as a counter reset and breaks
Prometheus `increase()`. `_accumulate` banks each torrent's growth into a running
total in `manager-state.json` and prunes departed hashes (their contribution stays
banked), so the exported counters only ever rise. This is what makes the daily/7-day
up/down Grafana panels correct.

## Locked state I/O + the flock-lifetime rule

All `state.json` mutations go through `mutate_state(fn)` (load → mutate → save under
`.state.lock`), so a writer merges into the latest on-disk state instead of clobbering
a stale copy — necessary because autorss and the manager (cron + autorss-spawned)
both write it. `flock_or_exit` returns the lock file object: the advisory lock is
released the instant that fd is closed, so callers MUST keep it referenced for the
whole run (`lock = C.flock_or_exit(...)`). Discarding it lets the GC drop the lock
immediately — the original single-instance guarantee was silently non-functional
because both callers discarded the return value.

## qB via `docker exec`, not host curl

qB has localhost-auth-bypass enabled; host requests arrive through Docker NAT with a
non-localhost source and get **403**. Issuing `curl` from inside the container
(`docker exec qbittorrent curl ...`) presents as localhost and bypasses auth.

## Deepseek filter scope

Approve only theatrically-released, mainstream films from the last 12 months
(rolling cutoff in `SYS_PROMPT`); reject classics, TV movies, direct-to-streaming,
docs, sports, concerts. This serves `up(interval)` (high demand) and `avg(torrdate)`
(fresh). Verdicts are cached per clean title so quality variants reuse one call.

## 720p-only filter (open tradeoff)

`RES_720P_RE` keeps only 720p releases. This maximises torrent count per GB (favours
`skala_torrszam`) at the cost of per-torrent `seedsize`/`up`. Whether to allow 1080p
depends on whether the count tier or seedsize is the binding lever at the current
torrent count + free disk — deliberately left as a tunable decision, not yet changed.
