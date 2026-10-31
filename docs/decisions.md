# Decisions

The *why* behind non-obvious choices. The *what* is in `docs/architecture.md`.

## The optimisation target: nCore "ERTEK" seeding score

Everything about the disk/seeding policy exists to maximise the nCore score:

```
ERTEK = up(interval) · avg(torrdate) · time · seedsize · skala_torrszam
        · regdate · upload · owntorr
```

- `up(interval)` — bytes uploaded in the last 7 days / uninterrupted seed run.
- `avg(torrdate)` — average age of running torrents. **Direction unverified:** nCore's text
  says "átlag életkora" (average *age*), and in a product a larger age raises the score,
  i.e. older would be better; this file used to say "newer = better" with no source. The
  "fresh" choices below stand on `up(interval)` (young torrents upload ~14x more per day),
  not on this term.
- `time` — total seed time across all running torrents.
- `seedsize` — total bytes seeding.
- `skala_torrszam` — torrent-count tier multiplier (step function: 0–10=1×,
  11–25=1.5×, 26–75=2×, 76–150=2.5×, 151–250=3×, 251+=3.5×).
- `regdate`, `upload` (lifetime), `owntorr` (own uploads) — account-level, **not
  controllable by this bot**.
- The first five are measured over the last 7 days *or the uninterrupted seeding period*
  ("a seed-elés megszakítása nélkül"), so downtime (qB restart, modem outage) may shrink
  the window. Exact semantics are not confirmed; keep seeding continuous.

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

## The trim runs every cycle, not only when a download is queued

`autorss.py` spawns `ensure-space` right after the decision loop, **above** the
`if not pending: return` / free-slot gates. It used to sit just above the add loop,
which read naturally ("trim before adding") but made `CATEGORY_HARD_GB` enforceable
only as a side effect of adding something.

That is the wrong coupling, because the category grows on *completion*, not on add:
a download that finishes pushes the category over the cap on a run that queues
nothing. Live on 2026-08-16 the category sat at 483.3 GB against the 480 GB cap for
26 h — a completed download had pushed it over, approvals then went quiet, and every
run returned at "no pending downloads" without ever reaching the trim. The 2-hour
`monitor` cron never deletes by design, so nothing else was watching. Hoisting the
call restores the invariant the docs already claimed (the cap is a cap) and still
trims before adding — nothing between the new position and the add loop grows the
category. Cost is one extra no-op manager subprocess per quiet 15-minute run.

Keeping `monitor` non-deleting was deliberate: it is the command a human runs by hand
and the one autorss calls from a `finally`, so it must stay safe to invoke anywhere.
`ensure-space` remains the single command that can delete.

## Never auto-stop seeding (`ratioLimit=-1`, `seedingTimeLimit=-1`)

Set per-torrent on add (and back-filled onto existing torrents). A paused/stopped
torrent stops counting toward `time`/`seedsize`/`up(interval)`, so any global qB
ratio/seed-time cap would silently bleed score. `-1` = no limit, overriding globals.

## Concurrency + newest-first

`MAX_ACTIVE_DOWNLOADS=3` (was effectively 1) grows the torrent count faster (crosses
`skala_torrszam` tiers sooner). Pending downloads are taken newest-approved first:
hot releases peak in leechers in their first hours, maximising `up(interval)`.

## A download slot is freed by futility, not by loneliness

`is_active_download` writes a download off as dead after `STALL_TIMEOUT_S` if it has
no connected peers **or** its swarm holds no complete copy (`dead_swarm`:
`num_complete == 0` and `availability < 1.0`).

The peerless half alone was not enough. Live, `Storm.on.Sesame.Street.2026` held a
slot for **13 days** at `progress=0` with `num_leechs=2` — so `peers()` was 2, the
only stall rule never fired, and `num_complete=0`/`availability=0` meant not one byte
was ever obtainable. Connected peers who are themselves stuck are not evidence of
progress; the swarm simply had no seeder and never would. With `MAX_ACTIVE_DOWNLOADS=3`
that is a third of the download capacity lost permanently, and it also charged the
torrent's full `size` against the category cap while occupying no disk.

Both halves require the timeout first, so a slow start is never mistaken for death.
`num_complete` is only trusted when it is explicitly `0` — qB reports `-1` (or omits
it) before the tracker answers, and treating "unknown" as "dead" would abandon healthy
just-added downloads. `availability >= 1.0` also rescues the seederless-but-complete
case, where connected peers collectively hold every piece and the torrent really can
finish.

The `autorss_stalled_downloads` gauge is now the exact complement (incomplete, not
stopped, not active) rather than a separate `peers == 0` test. Under the old test the
13-day stall reported `stalled=0` while genuinely holding a slot — the gauge disagreed
with the gate it was supposed to describe.

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
(high demand; `avg(torrdate)`'s direction is unverified, see the top). Verdicts are cached per clean title (via
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

## 720p preferred, 1080p as fallback

`wanted_quality` keeps 720p releases, and 1080p ones only when no 720p release of the
same film (by `dedup_key`) is in the current RSS feed. 720p maximises torrent count per
GB (favours `skala_torrszam`); at the disk cap a 1080p roughly halves the count and
drops the tier, so it is only taken when it is the sole variant, which widens the pool
of fresh films without costing count on films that have a 720p. 2160p stays excluded.
Only new feed items are evaluated; earlier quality-skipped 1080p items are not
revisited. A 720p variant that appears in a later run is blocked by `seen_clean` once
its 1080p sibling was approved.

## Unregistered torrents are removed after 24h

When nCore drops a torrent, the tracker answers `unregistered torrent` and books no
upload for it, so it only holds disk (14 such torrents, 81 GB, were found 2026-10-28
with 0 peers each). `torrent-manager.py monitor` now stamps each completed torrent
whose tracker says so (`unregistered_since`, manager-state.json) and deletes it with its
files once that has held for `UNREGISTERED_GRACE_S` (24h) and it has 0 peers.

The timer clears the moment an announce stops saying "unregistered", and a failed
tracker lookup leaves it untouched, so a transient nCore outage never adds up toward a
deletion; 24h outlasts a day-long outage. The same rule as the space trim applies:
never a torrent with peers or one still downloading. The old `t.ncore.pro` "Host not
found" tracker error is NOT a trigger -- only the literal "unregistered" message is.

## TMDB adds approvals on top of Deepseek

Deepseek decides from memory, so a film released after its training data is rejected as
"unrecognised" however big it is (`Verity`, `Hope`, `Buddy` were all rejected that way
in October). Approvals fell from ~10/week (Jul-Aug) to ~2/week from mid-September while
the portfolio aged: 65 of 88 seeders are over 60 days old, and the youngest earn ~14x the
upload per day of the oldest. `tmdb.py` looks the title up on TMDB and `qualifies` approves
it when it became available within 12 months, is not a documentary/TV movie, and has
popularity >= `TMDB_MIN_POPULARITY` with >= `TMDB_MIN_VOTES` votes.

- **Adds only.** TMDB runs only after a Deepseek rejection and never overturns an
  approval. Popularity alone is a middling predictor (Spearman 0.46 against upload/day;
  the 422 GB uploader `Silent Friend` has popularity 6), and Deepseek's recognised
  approvals include several low-popularity strong seeders. An OR keeps all of those.
- **Threshold 30.** Replayed on 35 days of verdicts (`tmdb-replay.py`): popularity >= 30
  adds ~6 films/week on top of Deepseek's ~2, close to the Jul-Aug rate that filled the
  category; >= 10 would add ~10/week and push it into the trim.
- **Date = digital/physical release**, not TMDB's primary date, which is often a festival
  premiere: `Frankenstein` (Venice Aug 2025) and `Sentimental Value` (Cannes May 2025)
  are top uploaders and a primary-date rule would have rejected both.
- **Failure is "no information".** A TMDB network/HTTP error leaves the Deepseek verdict
  and never touches `decide_attempts`; after the first failure TMDB is skipped for the
  rest of that run so an outage costs one timeout, not one per film.
- **Known limits.** A first-day rejection is permanent (`SEEN_STATUSES`), and a rising
  film is not re-evaluated. Hungarian-titled releases without a year often do not match
  (`Mocskos zsaruk`, `Parhuzamos tortenetek`), so they fall back to Deepseek alone.

## Series pilot (episodes into the same category)

The score multiplies by a torrent-count tier (76-150 = 2.5x, 151-250 = 3x, 251+ = 3.5x)
while `seedsize` is plain bytes, so many small torrents beat few big ones at the same
disk use. The feed carries ~230 series items/day (`Sorozat (ENG HD)`), ~1 GB per 720p
episode. `plan_series` queues single 720p episodes (<= 1.5 GB, from the RSS `size`) of
shows TMDB calls popular (popularity >= 30, >= 100 votes, not reality/news/talk/soap), at
most `SERIES_DAILY_MAX` (3) per rolling 24h, one release per SxxExx. Season packs and
dated dailies are ignored. Episodes are stored in `state["films"]` with `kind="series"`
and `size`, so the add loop, reconcile and trim work unchanged; films sort ahead of
episodes in the pending order, and the film counters exclude them (the recent-approved table lists both, `Source` = series). Per-show verdicts
are cached 2 days under `state["series"]["shows"]`; a TMDB failure records nothing.

- **It will not reach the 3x tier at the 480 GB cap.** Films (~36 GB/week via TMDB) plus 3
  episodes/day (~21 GB/week) fill the ~81 GB headroom in about 1.5 weeks; after that each
  add triggers a delete and the count flattens (~130-140, below 151). The pilot measures
  upload per GB, it is not a route to 3x unless `CATEGORY_HARD_GB` is raised (host disk
  has ~900 GB free). Quota 3, not 6, because of this.
- **Hit-and-run.** Once the cap binds, the smallest-sufficient trim would often pick an
  episode downloaded hours earlier. nCore requires, per torrent with >= 5% or >= 200 MB
  downloaded, either ratio 1.0 or `(1 - ratio) * (48 + 0.4 * downloaded_GB)` hours of
  seed time, checked at month end; a sanctionless allowance exists per rank, beyond it
  download bans of 5 / 15 / 30 days. `seed_obligation_met` (autorss_common) encodes that
  with `HNR_MARGIN_S` (2h) slack for the tracker's announce lag, and `select_victims`
  only offers torrents that meet it. A 1 GB episode at ratio 0 needs ~48.4h, a 4.5 GB film
  ~50h. A flat 48h age floor, the first version, would have been just short. The seeders
  here sit at ratio 10-40, so in practice the rule only ever shields fresh downloads.
- **No count floor in the trim.** The "never trim below 76 torrents" guard was planned but
  dropped: the count is 88 and rising, the trim only fires above 480 GB, and the tier
  floor is far below where the trim can leave it.
- **How to judge it (~2 weeks):** upload per GB on disk per day of `kind=series` records
  vs films added in the same window (young torrents upload ~14x more than old ones, so do
  not compare to the old seeders), from `state.json` (`kind`, `size`, `hash`) and qB's
  `uploaded`.
