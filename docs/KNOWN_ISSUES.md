# Known Issues

Standing limitations only. Resolved bugs live in git history and, where they changed
behaviour, in `docs/decisions.md`.

## Open / accepted

- **Numeric-title year ambiguity in `clean_title`.** A film whose title *is* a
  plausible year with no separate release year (`1917`, `2012`, `300`) is inherently
  ambiguous — the embedded number can't be distinguished from a release year. Only the
  clear future-year case (`Blade Runner 2049` with no release year) is fixed via the
  `MAX_YEAR` bound. Worst case: two variants of such a film get different dedup keys
  (a redundant Deepseek call), not data loss.

- **A no-year title can still be misjudged if Deepseek doesn't recognize it.** When a
  release carries no year at all (a real, common scene-naming gap, not just the
  numeric-title case above), `ask_deepseek` has no date to check and SYS_PROMPT tells
  the model to judge recency from its own knowledge instead of defaulting to "old".
  Verified live: this fixes the case where the model already recognizes the specific
  title (e.g. a well-known franchise sequel is correctly approved even with no year).
  It does NOT fix the case where the model has no confident knowledge of a real but
  less-famous title — a small theatrical/streaming release it can't place from the
  title alone still gets rejected as "old", same as before. This is a genuine
  model-knowledge gap, not a promptable bias — a more permissive "assume new unless
  proven old" variant was tested live and did not fix the failing case either, though
  it also didn't regress genuinely-old titles. Closing it for real would mean fetching
  actual metadata per no-year title (e.g. the nCore torrent detail page linked in the
  RSS item, or an external film database) — deliberately out of scope for now;
  accepted as a residual limitation.

- **`ensure-space` can no-op under lock contention (low impact).** If the 2-hour cron
  `monitor` holds `.manager.lock` exactly when an autorss run spawns `ensure-space`,
  the trim is skipped (the manager exits on the held lock). Harmless: autorss's
  `CATEGORY_CAP_GB` hold-gate still prevents overshoot, and skipping a 0-peer trim
  actually favours the seeding score (keeps seeders). The next run trims.

- **Single-host concurrency model.** `mutate_state` + `.state.lock` make `state.json`
  writes safe across the two cron jobs on **one host**. There is no multi-host
  coordination (not needed — it runs on one server). `reconcile_state` rewrites
  `state.json` every monitor run even when nothing changed (cheap, atomic; minor mtime
  churn).

- **Dashboard up/down panels need history to populate.** The monotonic counters were
  (re)baselined when first deployed, so `increase(...[24h])` and the 7-day rate graph
  show partial/extrapolated values until 24 h / 7 days of scrapes have accrued. Not a
  bug — just a warm-up window.

- **`qb_add_confirmed` can't disambiguate simultaneous new torrents.** It detects "our"
  torrent by diffing hash sets before/after the add. If another new torrent appears in
  the category during its poll window it could attribute the wrong hash. In practice
  the single-writer lock (autorss never overlaps itself) and the fact that deletions
  can't create false positives make this safe. A re-added *duplicate infohash* never
  produces a new hash → bounded by `MAX_ADD_ATTEMPTS` → `status=add_failed` rather than
  retried forever.

- **`peers()` counts connected peers, not swarm size.** `num_seeds + num_leechs` is
  connections we currently hold; a torrent with a large swarm but no live connections
  (just added, firewalled moment) reads as 0-peer, which can briefly affect its
  `zero_since` timer and deletion candidacy. Self-corrects on the next sample.

- **External-dependency risk.** Approvals depend on the Deepseek API being reachable
  and the model's recency knowledge; misclassifications and transient API failures are
  possible (the latter just defer the film to the next run). An expired nCore passkey
  surfaces as `feed_items=0` in metrics, not an error.

## Benign / cosmetic

- A torrent deleted *between* monitor runs loses the upload/download delta accrued
  since the previous sample (it's pruned before that final delta is banked).
  Negligible vs the totals.

- The Grafana films-panel matcher requires either the `autorss-films-text` description
  marker or a title-substring panel that is **already** `type:text`. A freshly recreated
  dashboard whose films panel is a non-text panel without the marker won't be
  auto-converted — a deliberate safety choice (it refuses to destroy a data panel).
  Set the marker manually if needed.

- The two Grafana panel titles are English (`Daily Uploaded GB`, etc.) while the rest
  of the Auto-Film row is Hungarian — cosmetic inconsistency.
