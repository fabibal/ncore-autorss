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

## Deletion: only completed 0-peer seeders, smallest sufficient

`ensure_space` candidates are `peers==0 AND progress>=1.0` only — never an
in-progress download (would discard partial data and waste the slot) and never a
torrent with peers. Its domain (completed seeders) is disjoint from the queue gate's
(incomplete downloads), so the two predicates can't disagree about the same torrent.

Among those candidates we take the **smallest single one that covers the overage**;
only if no single candidate covers it do we combine smallest-first. Removing one
torrent preserves the `skala_torrszam` count tier exactly as well as the largest-first
rule this replaced, but destroys far less accrued `time`/`seedsize`.

Two earlier rules were wrong here. The doc used to claim largest-first while the code
actually sorted by *longest at 0 peers* — a quantity uncorrelated with the overage,
so it consistently over-freed: across the three real deletions of 2026-08-02 it took
6.89/7.51/3.82 GB to cover overages of 2.9/1.5/0.2 GB (up to **19x**), and the next
victim in line was the category's oldest and second-largest contributor (46 d seeding,
121 GB uploaded, ratio 4.85) for a ~3 GB overage. Replayed against the same candidate
pools, smallest-sufficient would have freed 3.00/1.50/1.07 GB — one torrent each.

The old `-disk_bytes` tie-break under that key was unreachable dead code: `zero_since`
holds a per-torrent `time.time()` float, so even torrents that hit 0 peers in the same
sampling run differ by microseconds (measured: 7 torrents spread over 42 µs) and the
primary key never tied. Time-at-0-peers is now the *secondary* key, where it does fire —
byte sizes tie exactly.

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

Approve mainstream, high-demand films from the last 12 months (rolling cutoff in
`SYS_PROMPT`) regardless of distribution channel -- a streaming-exclusive original
(Netflix/Amazon/etc.) is judged the same as a theatrical release, since it can
generate just as much seeding demand. Reject classics, TV series/movies, low-budget
direct-to-video, docs, sports, concerts, awards shows. This serves `up(interval)`
(high demand) and `avg(torrdate)` (fresh). Verdicts are cached per clean title (via
`dedup_key`, case-insensitive) so quality variants -- including differently-cased
scene releases of the same film -- reuse one call.

Distribution channel was dropped as a criterion because it no longer tracked
"mainstream/anticipated": plenty of streaming-exclusive originals are just as
popular as theatrical films, and the old wording caused real false positives
(a Netflix-exclusive sequel was approved on the false claim of a "theatrical
release") without protecting against anything -- the sports/TV/documentary/
low-budget exclusions do the actual filtering work.

Dropping it did, however, remove an accidental brake. With no channel test left, the
model started inferring "highly anticipated" from the **release year alone**: approvals
of recent-year titles went 66% -> 87% and weekly acquisitions 13-16 -> 27, which is
what first pushed the category into the 480 GB trim threshold. The failures were
unmistakable -- a literal placeholder, `Untitled Home Invasion Romance (2025)`, was
approved as "likely popular and recent", and an obscure import,
`Wu ming zhi bei Pi ji tai lai (2025)`, as "highly anticipated".

So criterion (a) is now **recognition-gated**: an approval must name what the model
recognises the film by (cast, director, studio, or franchise), which makes the claim
self-checking -- a content-free "recognised mainstream film with buzz" is a rejection.
Two counterweights keep this from swallowing the recency fix: a film known only as an
*announced or upcoming* project still counts as recognised, and the model is told the
release exists (it came off the tracker) so it must not reject merely because the title
postdates its knowledge. Both were needed -- without them the gate collapsed to 4-9%
approval and threw out genuinely mainstream titles (`Soulm8te`, `The Death of Robin
Hood`). Measured live over the 46 post-deploy verdicts: 59% -> 22-24% approval, with
`Avatar Aang`, `Soulm8te`, `The Death of Robin Hood` and `Storm on Sesame Street` still
approved and both named false positives rejected.

When `clean_title` finds no release year (a real, common scene-naming gap, not
just the numeric-title case below), `ask_deepseek` gets no date to check against.
SYS_PROMPT now explicitly tells the model not to default to "old" just because a
year is missing, and to judge recency from its own knowledge of that exact title
instead. Verified live: this fixes the case where the model already recognizes the
title (e.g. a well-known franchise sequel), but NOT the case where it doesn't
confidently know the specific title -- that's a model-knowledge gap no prompt
wording can close, not a fixable bias. See KNOWN_ISSUES.md.

## 720p-only filter (open tradeoff)

`RES_720P_RE` keeps only 720p releases. This maximises torrent count per GB (favours
`skala_torrszam`) at the cost of per-torrent `seedsize`/`up`. Whether to allow 1080p
depends on whether the count tier or seedsize is the binding lever at the current
torrent count + free disk — deliberately left as a tunable decision, not yet changed.
