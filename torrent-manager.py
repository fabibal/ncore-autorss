#!/usr/bin/env python3
"""auto-film torrent manager: monitor peers, free disk space on demand, emit metrics.

Modes:
  monitor                 (default; cron every 2h, and called by autorss after each run)
                          Sample peer counts, update 0-peer timers, write the Prometheus
                          textfile metrics, refresh the Grafana "recent films" text panel.
                          Deletes only completed 0-peer torrents whose tracker has said
                          "unregistered torrent" for UNREGISTERED_GRACE_S (24h).
  ensure-space [CAP_GB]   Called by autorss before adding a torrent. If the auto-film
                          CATEGORY's committed size exceeds CAP_GB, delete the SMALLEST
                          SUFFICIENT completed 0-peer seeders (smallest single candidate
                          that covers the overage; otherwise the fewest torrents that do)
                          to get back under CAP_GB. Never deletes a torrent with
                          connected peers or one still downloading.

Shared infra (qB via docker exec, the active-download rule, json/log/lock helpers,
constants) lives in autorss_common. Stdlib only.
"""
import base64
import hashlib
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime

import autorss_common as C
from autorss_common import docker_curl, qb_torrents, peers, is_active_download

# --- config -----------------------------------------------------------------
MGR_STATE = os.path.join(C.BASE_DIR, "manager-state.json")
LOG_FILE = os.path.join(C.BASE_DIR, "torrent-manager.log")
LOCK_FILE = os.path.join(C.BASE_DIR, ".manager.lock")
METRICS_FILE = os.path.join(C.TEXTFILE_DIR, "autorss.prom")

DASH_UID = "00f835a2-f5ff-4efc-8a88-ddff8d738a3a"
FILMS_PANEL_MARK = "autorss-films-text"  # description marker on the recent-films text panel

log = logging.getLogger("torrent-manager")


# --- helpers ----------------------------------------------------------------
def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def disk_bytes(t):
    return t.get("completed") or t.get("size") or 0


def fetch_auto_torrents(retries=2):
    """Live auto-film torrents, or None if the qB query genuinely failed.
    Retries a couple times so a single flaky docker-exec/curl timeout does not
    look like an empty category (which would zero the live gauges)."""
    for attempt in range(retries + 1):
        arr, err = qb_torrents()
        if arr is not None:
            return arr
        if attempt < retries:
            time.sleep(2)
    log.error("qB query failed after %d attempts: %s", retries + 1, err)
    return None


def update_zero_timers(torrents, st):
    """Track when each torrent first reached 0 peers; clear when peers reappear."""
    zs = st.setdefault("zero_since", {})
    present = set()
    for t in torrents:
        h = t.get("hash")
        present.add(h)
        if peers(t) == 0:
            zs.setdefault(h, time.time())
        else:
            zs.pop(h, None)
    for h in list(zs):
        if h not in present:
            zs.pop(h, None)


def tracker_unregistered(h):
    """True if any tracker of torrent `h` answers "unregistered torrent" right now, False if
    none does, None if qB could not be asked (callers must treat that as "no information")."""
    rc, out, _ = docker_curl([C.QB_API + "/api/v2/torrents/trackers?hash=" + h])
    if rc != 0:
        return None
    try:
        trackers = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None
    return any("unregistered" in (tr.get("msg") or "").lower() for tr in trackers)


def update_unregistered_timers(torrents, st, check=tracker_unregistered):
    """Track when each completed torrent was first seen "unregistered" on its tracker;
    clear the timer as soon as an announce stops saying so, so a transient tracker outage
    never accumulates toward a removal. A failed lookup leaves the timer as it was."""
    us = st.setdefault("unregistered_since", {})
    present = set()
    for t in torrents:
        h = t.get("hash")
        present.add(h)
        if (t.get("progress") or 0) < 1.0:
            us.pop(h, None)
            continue
        verdict = check(h)
        if verdict is None:
            continue
        if verdict:
            us.setdefault(h, time.time())
        else:
            us.pop(h, None)
    for h in list(us):
        if h not in present:
            us.pop(h, None)


def remove_unregistered(torrents, st, now=None):
    """Delete completed 0-peer torrents the tracker has called "unregistered" for at least
    UNREGISTERED_GRACE_S: nCore books no upload for them, so they only hold disk. Never
    touches a torrent with peers or one still downloading. Returns the number deleted."""
    us = st.get("unregistered_since", {})
    now = time.time() if now is None else now
    removed = 0
    for t in torrents:
        since = us.get(t.get("hash"))
        if since is None or now - since < C.UNREGISTERED_GRACE_S:
            continue
        if peers(t) != 0 or (t.get("progress") or 0) < 1.0:
            continue
        log.info("UNREGISTERED | %r unregistered since %s", (t.get("name") or "")[:70],
                 datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M"))
        if delete_torrent(t, st):
            us.pop(t.get("hash"), None)
            removed += 1
    return removed


def delete_torrent(t, st):
    h = t.get("hash")
    rc, out, err = docker_curl(
        ["-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
         C.QB_API + "/api/v2/torrents/delete",
         "--data-urlencode", "hashes=" + h, "--data-urlencode", "deleteFiles=true"]
    )
    code = (out or "").strip()
    if rc == 0 and code == "200":
        st["deleted_total"] = st.get("deleted_total", 0) + 1
        st.setdefault("deleted_log", []).append(
            {"ts": now_iso(), "name": (t.get("name") or "")[:90], "hash": h,
             "size_gb": round(disk_bytes(t) / 1e9, 2)}
        )
        st["deleted_log"] = st["deleted_log"][-50:]
        st.get("zero_since", {}).pop(h, None)
        log.info("DELETED | %r (%.2f GB, 0 peers)", (t.get("name") or "")[:70], disk_bytes(t) / 1e9)
        return True
    log.error("DELETE FAILED | %r http=%s err=%s", (t.get("name") or "")[:70], code, err.strip()[:80])
    return False


def select_victims(torrents, over, zs, now):
    """Ordered deletion plan for an `over`-byte overage. Candidates are completed 0-peer
    seeders ONLY whose nCore seed obligation is met (a just-downloaded episode at ratio < 1
    must not be deleted before its hit-and-run time is served). Idle ones (no peer activity
    for TRIM_IDLE_S) are chosen from first, since they earn nothing: when they alone cover
    `over`, the plan draws from them, otherwise from every candidate. Within the pool they
    are sorted smallest-first (ties broken by longest at 0 peers) and the smallest single
    one that alone covers `over` is promoted to the front; the other candidates follow so a
    failed delete falls through. Returns (victims, single) where `single` is None when no
    one candidate covers the overage and the plan combines the fewest torrents that do.

    Pure and side-effect-free purely so `dryrun-ensure-space.py` can print the exact plan
    a real run would follow. A dry-run that reimplemented this selection would silently
    drift from it the next time the sort key changes -- and a dry-run that lies is worse
    than none, since its whole job is to be trusted before an irreversible delete."""
    cands = [t for t in torrents if peers(t) == 0 and (t.get("progress") or 0) >= 1.0
             and C.seed_obligation_met(t)]
    idle = [t for t in cands if C.is_idle(t, now)]
    pool = idle if sum(disk_bytes(t) for t in idle) >= over else cands
    victims, single = _smallest_sufficient(pool, over, zs, now)
    if pool is not cands:
        in_pool = {id(t) for t in pool}
        victims += _smallest_sufficient([t for t in cands if id(t) not in in_pool], over, zs, now)[0]
    return victims, single


def _smallest_sufficient(pool, over, zs, now):
    """Deletion plan from `pool` for an `over`-byte overage: (ordered, single).

    Each step takes the SMALLEST torrent that alone covers what is still needed; when none
    does, it takes the LARGEST one and repeats on the remainder. A single sufficient torrent
    is the whole plan (single is that torrent). Otherwise the plan is the fewest torrents
    that cover the overage with a small overshoot (single is None) -- combining small ones
    smallest-first would delete many torrents, and the torrent count is a score tier
    multiplier. The unused candidates follow, smallest-first (ties: longest at 0 peers), so
    a FAILED delete still falls through to the next option instead of giving up."""
    rest = sorted(pool, key=lambda t: (disk_bytes(t), zs.get(t.get("hash"), now)))
    plan, need = [], over
    while need > 0 and rest:
        pick = next((t for t in rest if disk_bytes(t) >= need), rest[-1])
        plan.append(pick)
        need -= disk_bytes(pick)
        rest = [t for t in rest if t is not pick]
    single = plan[0] if plan and disk_bytes(plan[0]) >= over else None
    return plan + rest, single


def ensure_space(cap_gb, torrents, st):
    """Keep the auto-film CATEGORY at or below cap_gb by deleting the SMALLEST SUFFICIENT
    set of COMPLETED 0-peer seeders. Never deletes a torrent with peers, nor one still
    downloading -- candidates are disjoint from the queue gate's domain (incomplete
    downloads), so the two never disagree about the same torrent. Deletion is a last
    resort -- the score rewards seeders -- so this only acts when the category's
    committed size exceeds the cap.

    Selection (changed 2026-08-04): pick the SMALLEST single candidate that alone covers
    the overage; only if no single one covers it, take the fewest torrents that do (largest
    first, finishing with the smallest sufficient one).
    Previously this deleted the candidate that had been at 0 peers LONGEST, which is
    uncorrelated with the overage and routinely destroyed far more than needed -- live,
    a 0.2 GB overage cost a 3.82 GB seeder (19x), and the next victim in line was the
    category's oldest and second-largest contributor (46 d seeding, 121 GB uploaded,
    ratio 4.85) for a ~3 GB overage. Every seeder deleted destroys accrued seed time and
    can drop an ERTEK count tier, so freeing the least is freeing the best.

    Time-at-0-peers survives as the SECONDARY key: among candidates of equal size, the
    one dead longest goes first. That replaces the old `-disk_bytes` tie-break, which was
    unreachable dead code -- `zero_since` values are per-torrent `time.time()` floats
    (microseconds apart even within one sampling run), so the primary key never tied.
    Byte sizes DO tie exactly, so this tie-break actually fires."""
    used = C.category_used_bytes(torrents)  # committed size, matching the autorss cap gate
    over = used - int(cap_gb * 1e9)
    if over <= 0:
        log.info("ensure-space | category %.1f GB <= cap %.1f GB; no deletion", used / 1e9, cap_gb)
        return
    zs = st.setdefault("zero_since", {})
    now = time.time()  # sampled ONCE: a time.time() call inside the sort key would give
                       # an untracked candidate a different value on each comparison.
    victims, single = select_victims(torrents, over, zs, now)
    log.info("ensure-space | category %.1f GB > cap %.1f GB (over %.1f GB); %d 0-peer candidate(s); "
             "plan=%s", used / 1e9, cap_gb, over / 1e9, len(victims),
             "smallest sufficient (%.2f GB)" % (disk_bytes(single) / 1e9) if single is not None
             else "no single candidate covers it; combining the fewest that do")
    reclaim = 0
    for t in victims:
        if reclaim >= over:
            break
        if delete_torrent(t, st):
            reclaim += disk_bytes(t)
    if reclaim < over:
        log.warning("ensure-space | freed %.1f GB of %.1f GB over cap; remaining have peers or none left",
                    reclaim / 1e9, over / 1e9)
    else:
        log.info("ensure-space | freed %.1f GB", reclaim / 1e9)


def reconcile_state(live_hashes):
    """Mark 'downloaded' films whose torrent is gone from qB as 'removed', so the
    autorss state stays truthful (a torrent deleted outside the manager otherwise
    sticks at status=downloaded forever). Only call with a SUCCESSFUL qB read --
    a transient query failure must not flag everything as removed.

    Goes through the shared, locked read-modify-write so it can't clobber a concurrent
    autorss writer (e.g. the autorss run that spawned this ensure-space subprocess)."""
    changed = []
    def _mut(state):
        for fid, f in state.get("films", {}).items():
            if f.get("status") == "downloaded" and f.get("hash") and f["hash"] not in live_hashes:
                f["status"] = "removed"
                f["removed_ts"] = now_iso()
                changed.append(fid)
                log.info("RECONCILE | id=%s %r gone from qB -> status=removed",
                         fid, f.get("clean") or f.get("title"))
    C.mutate_state(_mut)
    return len(changed)


def _accumulate(torrents, st, field, seen_key, total_key):
    """Monotonic cumulative `field` bytes for the auto-film category, immune to torrent
    deletion and per-torrent counter resets. qB's per-torrent counters disappear when a
    torrent is removed, so summing them live is non-monotonic and would break Prometheus
    increase() (a drop reads as a counter reset). Instead we bank each torrent's growth
    into a running total and prune departed hashes -- their contribution stays banked, so
    the exported counter only ever rises."""
    seen = st.setdefault(seen_key, {})
    total = st.get(total_key, 0)
    present = set()
    for t in torrents:
        h = t.get("hash")
        if not h:
            continue
        present.add(h)
        cur = t.get(field) or 0
        prev = seen.get(h, 0)
        total += (cur - prev) if cur >= prev else cur  # cur < prev => per-torrent reset
        seen[h] = cur
    for h in list(seen):
        if h not in present:
            seen.pop(h, None)  # departed torrent: growth already banked, stop tracking
    st[total_key] = total
    return total


def accumulate_uploaded(torrents, st):
    """Monotonic bytes UPLOADED by the category. Powers the daily uploaded-GB stat and
    the up/down rate graph."""
    return _accumulate(torrents, st, "uploaded", "up_seen", "uploaded_total")


def accumulate_downloaded(torrents, st):
    """Monotonic bytes DOWNLOADED by the category. Powers the daily downloaded-GB stat
    and the up/down rate graph."""
    return _accumulate(torrents, st, "downloaded", "down_seen", "downloaded_total")


KINDS = ("film", "series")


def approval_source(f):
    """Who approved an approved film record: the series pilot, TMDB's word alone, or Deepseek."""
    if f.get("kind") == "series":
        return "series"
    return "tmdb" if str(f.get("reason") or "").startswith("TMDB") else "deepseek"


def split_by_kind(torrents, films):
    """{'film': [...], 'series': [...]}: a torrent is a series episode iff its hash belongs
    to a state record with kind=series; everything else counts as a film."""
    series = {(f.get("hash") or "").lower() for f in films.values()
              if f.get("kind") == "series" and f.get("hash")}
    out = {k: [] for k in KINDS}
    for t in torrents:
        out["series" if (t.get("hash") or "").lower() in series else "film"].append(t)
    return out


def compute_metrics(torrents, st):
    films = C.load_json(C.STATE_FILE, {"films": {}}).get("films", {})
    by_kind = split_by_kind(torrents, films)
    return {
        "approvals": {src: sum(1 for f in films.values()
                               if f.get("decision") is True and approval_source(f) == src)
                      for src in ("deepseek", "tmdb", "series")},
        "uploaded_by_kind": {k: _accumulate(by_kind[k], st, "uploaded", "up_seen_" + k, "uploaded_total_" + k)
                             for k in KINDS},
        "disk_by_kind": {k: sum(disk_bytes(t) for t in by_kind[k]) / 1e9 for k in KINDS},
        "seeding_by_kind": {k: sum(1 for t in by_kind[k]
                                   if (t.get("progress") or 0) >= 1.0 and t.get("state") not in C.UP_STOPPED)
                            for k in KINDS},
        "approved": sum(1 for f in films.values()
                        if f.get("decision") is True and f.get("kind") != "series"),
        "rejected": sum(1 for f in films.values() if f.get("decision") is False),
        "active": sum(1 for t in torrents if is_active_download(t)),
        # Incomplete, not deliberately stopped, and no longer counted as active -- i.e.
        # exactly what is_active_download has written off as dead. Sharing that predicate
        # instead of re-testing `peers(t) == 0` is what makes the gauge agree with the
        # queue gate: a 13-day dead-swarm stall that was really holding a slot still
        # reported stalled=0 under the old peers-only test, because it had 2 connected
        # (equally stuck) leechers.
        "stalled": sum(1 for t in torrents
                       if (t.get("progress") or 0) < 1.0
                       and t.get("state") not in C.DL_STOPPED
                       and not is_active_download(t)),
        "seeding": sum(1 for t in torrents
                       if (t.get("progress") or 0) >= 1.0 and t.get("state") not in C.UP_STOPPED),
        "disk_gb": sum(disk_bytes(t) for t in torrents) / 1e9,
        "deleted": st.get("deleted_total", 0),
        "peers_zero": sum(1 for t in torrents if peers(t) == 0),
        "uploaded_total": accumulate_uploaded(torrents, st),
        "downloaded_total": accumulate_downloaded(torrents, st),
    }


def write_metrics(m):
    lines = [
        "# HELP autorss_films_total Cumulative Deepseek decisions by outcome",
        "# TYPE autorss_films_total counter",
        'autorss_films_total{status="approved"} %d' % m["approved"],
        'autorss_films_total{status="rejected"} %d' % m["rejected"],
        "# HELP autorss_active_downloads Current auto-film torrents downloading",
        "# TYPE autorss_active_downloads gauge",
        "autorss_active_downloads %d" % m["active"],
        "# HELP autorss_seeding_total Current auto-film torrents seeding",
        "# TYPE autorss_seeding_total gauge",
        "autorss_seeding_total %d" % m["seeding"],
        "# HELP autorss_disk_used_gb Disk used by the auto-film category in GB",
        "# TYPE autorss_disk_used_gb gauge",
        "autorss_disk_used_gb %.3f" % m["disk_gb"],
        "# HELP autorss_uploaded_bytes_total Cumulative bytes uploaded by the auto-film category (monotonic)",
        "# TYPE autorss_uploaded_bytes_total counter",
        "autorss_uploaded_bytes_total %d" % m["uploaded_total"],
        "# HELP autorss_downloaded_bytes_total Cumulative bytes downloaded by the auto-film category (monotonic)",
        "# TYPE autorss_downloaded_bytes_total counter",
        "autorss_downloaded_bytes_total %d" % m["downloaded_total"],
        "# HELP autorss_deleted_total Cumulative torrents deleted by the manager",
        "# TYPE autorss_deleted_total counter",
        "autorss_deleted_total %d" % m["deleted"],
        "# HELP autorss_approvals_total Cumulative approvals by source (deepseek, tmdb = TMDB's word alone, series = pilot)",
        "# TYPE autorss_approvals_total counter",
        *['autorss_approvals_total{source="%s"} %d' % (s, n) for s, n in m["approvals"].items()],
        "# HELP autorss_uploaded_bytes_by_kind_total Cumulative bytes uploaded, split film / series (monotonic)",
        "# TYPE autorss_uploaded_bytes_by_kind_total counter",
        *['autorss_uploaded_bytes_by_kind_total{kind="%s"} %d' % (k, n) for k, n in m["uploaded_by_kind"].items()],
        "# HELP autorss_disk_used_gb_by_kind Disk used by the auto-film category in GB, split film / series",
        "# TYPE autorss_disk_used_gb_by_kind gauge",
        *['autorss_disk_used_gb_by_kind{kind="%s"} %.3f' % (k, n) for k, n in m["disk_by_kind"].items()],
        "# HELP autorss_seeding_by_kind Current seeding torrents, split film / series",
        "# TYPE autorss_seeding_by_kind gauge",
        *['autorss_seeding_by_kind{kind="%s"} %d' % (k, n) for k, n in m["seeding_by_kind"].items()],
        "# HELP autorss_peers_zero_total Current auto-film torrents with 0 connected peers",
        "# TYPE autorss_peers_zero_total gauge",
        "autorss_peers_zero_total %d" % m["peers_zero"],
        "# HELP autorss_stalled_downloads Incomplete auto-film torrents written off as dead (no peers, or no complete copy in the swarm)",
        "# TYPE autorss_stalled_downloads gauge",
        "autorss_stalled_downloads %d" % m["stalled"],
        "",
    ]
    os.makedirs(C.TEXTFILE_DIR, exist_ok=True)
    tmp = METRICS_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines))
    os.replace(tmp, METRICS_FILE)


def _epoch(s):
    try:
        return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp())
    except (ValueError, TypeError):
        return 0


def recent_approved_films(limit=50):
    """Last `limit` approved films and series episodes (status downloaded or pending), newest first."""
    films = C.load_json(C.STATE_FILE, {"films": {}}).get("films", {})
    rows = []
    for fid, f in films.items():
        if f.get("decision") is True and f.get("status") != "removed":
            rows.append({
                "title": f.get("clean") or f.get("title") or fid,
                "status": "downloaded" if f.get("status") == "downloaded" else "approved",
                "reason": f.get("reason", ""),
                "source": approval_source(f),
                "epoch": _epoch(f.get("added_ts") or f.get("asked_ts") or ""),
                "hash": (f.get("hash") or "").lower(),
            })
    rows.sort(key=lambda r: r["epoch"], reverse=True)
    return rows[:limit]


def torrent_stats(torrents):
    """Map hash -> stats (size, seeding days, uploaded GB, GB/day, ratio) for live auto-film
    torrents. GB/day is blank (None) until a torrent has seeded half a day: a few hours of
    upload divided by a tiny denominator would top the table for the wrong reason."""
    stats = {}
    for t in torrents or []:
        h = (t.get("hash") or "").lower()
        if not h:
            continue
        seed_days = (t.get("seeding_time") or 0) / 86400.0
        up_gb = (t.get("uploaded") or 0) / 1e9
        stats[h] = {
            "size_gb": disk_bytes(t) / 1e9,
            "seed_days": seed_days,
            "up_gb": up_gb,
            "per_day": up_gb / seed_days if seed_days >= 0.5 else None,
            "ratio": t.get("ratio") or 0.0,
        }
    return stats


def films_markdown(rows, stats=None):
    """Render the recent-films list as a Grafana-friendly markdown table.

    Replaces the old per-film Prometheus info-metric (free-text labels in Prometheus
    are an anti-pattern and churn stale series); the data is plain text here instead.

    `stats` maps torrent hash -> seeding stats; cells are blank for films with no
    live torrent (pending, or qB unreachable).
    """
    if not rows:
        return "_No approved films._"
    stats = stats or {}
    def esc(s):
        return str(s).replace("|", "\\|").replace("\n", " ")
    out = ["| Title | Source | Status | Reason | Added | Size (GB) | Seeding (days) | Uploaded (GB) | GB/day | Ratio |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        when = datetime.fromtimestamp(r["epoch"]).strftime("%Y-%m-%d %H:%M") if r["epoch"] else ""
        s = stats.get(r.get("hash"))
        size_gb = "%.1f" % s["size_gb"] if s else ""
        seed_days = "%.1f" % s["seed_days"] if s else ""
        up_gb = "%.2f" % s["up_gb"] if s else ""
        per_day = "%.2f" % s["per_day"] if s and s.get("per_day") is not None else ""
        ratio = "%.2f" % s["ratio"] if s else ""
        out.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            esc(r["title"]), esc(r.get("source", "")), esc(r["status"]), esc(r["reason"]), when,
            size_gb, seed_days, up_gb, per_day, ratio))
    return "\n".join(out)


def grafana_auth_header():
    """`Authorization` value: the service-account token if its file is readable, else the
    .env admin login (Basic), else None."""
    try:
        with open(C.GRAFANA_TOKEN_FILE) as f:
            token = f.read().strip()
    except OSError:
        token = ""
    if token:
        return "Bearer " + token
    user = C.env_value(C.PROJECT_ENV, "GRAFANA_USER")
    pw = C.env_value(C.PROJECT_ENV, "GRAFANA_PASS")
    if user and pw:
        return "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
    return None


def update_films_panel(st, torrents=None):
    """Push the recent-films markdown into the Grafana text panel -- only when it
    changed (hash in manager-state), so there's no dashboard version spam."""
    if torrents is None:
        # qB unreachable: preserve the last good panel instead of blanking the live
        # seeding columns (which would churn the dashboard and drop data on every blip).
        return
    md = films_markdown(recent_approved_films(), torrent_stats(torrents))
    h = hashlib.sha256(md.encode()).hexdigest()
    if st.get("films_panel_hash") == h:
        return
    base = C.env_value(C.PROJECT_ENV, "GRAFANA_URL")
    auth = grafana_auth_header()
    if not (base and auth):
        log.warning("films panel: Grafana URL/creds missing (%s, %s); skipping",
                    C.PROJECT_ENV, C.GRAFANA_TOKEN_FILE)
        return
    try:
        req = urllib.request.Request(base + "/api/dashboards/uid/" + DASH_UID,
                                     headers={"Authorization": auth})
        d = json.loads(urllib.request.urlopen(req, timeout=20).read())
        dash = d["dashboard"]
        # Match the marker first; fall back to the title substring ONLY for a panel that
        # is already a text panel, so we never convert (and destroy) a data panel that
        # merely shares the word "film" in its title.
        target = None
        title_nontext = False
        for p in dash.get("panels", []):
            if p.get("description") == FILMS_PANEL_MARK:
                target = p
                break
            if "jóváhagyott film" in (p.get("title") or "").lower():
                if p.get("type") == "text":
                    target = p
                    break
                title_nontext = True
        if target is None:
            if title_nontext:
                log.warning("films panel: a panel matches by title but is not a text panel; "
                            "refusing to convert it. Set its description to %r.", FILMS_PANEL_MARK)
            else:
                log.warning("films panel: target panel not found on dashboard")
            return
        # update the markdown text panel
        target["type"] = "text"
        target["description"] = FILMS_PANEL_MARK
        target["options"] = {"mode": "markdown", "content": md}
        for k in ("targets", "transformations", "fieldConfig", "datasource"):
            target.pop(k, None)
        # overwrite=False + the fetched version = optimistic locking: a concurrent
        # dashboard edit makes Grafana reject (412) rather than be silently stomped.
        payload = json.dumps({"dashboard": dash, "overwrite": False,
                              "message": "autorss: refresh recent films"}).encode()
        preq = urllib.request.Request(base + "/api/dashboards/db", data=payload, method="POST",
            headers={"Authorization": auth, "Content-Type": "application/json"})
        urllib.request.urlopen(preq, timeout=20).read()
        st["films_panel_hash"] = h
        log.info("films panel updated (%d rows)", len(recent_approved_films()))
    except Exception as e:
        log.warning("films panel update failed: %s", e)


# --- main -------------------------------------------------------------------
def main():
    C.setup_logging("torrent-manager", LOG_FILE)
    lock_fd = C.flock_or_exit(LOCK_FILE, log)  # keep referenced for the whole run; closing it drops the lock
    cmd = sys.argv[1] if len(sys.argv) > 1 else "monitor"
    st = C.load_json(MGR_STATE, {"zero_since": {}, "deleted_total": 0, "deleted_log": []})

    torrents = fetch_auto_torrents()
    qb_ok = torrents is not None
    if qb_ok:
        update_zero_timers(torrents, st)

    if cmd == "monitor" and qb_ok:
        update_unregistered_timers(torrents, st)
        if remove_unregistered(torrents, st):
            torrents = fetch_auto_torrents()
            qb_ok = torrents is not None
            if qb_ok:
                update_zero_timers(torrents, st)

    if cmd == "ensure-space":
        cap_gb = float(sys.argv[2]) if len(sys.argv) > 2 else C.CATEGORY_HARD_GB
        if qb_ok:
            ensure_space(cap_gb, torrents, st)
            torrents = fetch_auto_torrents()
            qb_ok = torrents is not None
            if qb_ok:
                update_zero_timers(torrents, st)
        else:
            log.error("ensure-space skipped: qB query failed (won't delete blind)")
    elif cmd != "monitor":
        log.error("unknown command %r (use: monitor | ensure-space [CAP_GB])", cmd)

    # Only touch qB-derived state on a SUCCESSFUL read. On failure, leave the last
    # metrics file and timers untouched so a transient timeout doesn't collapse the
    # live gauges (disk/seeding) to zero and flap the dashboard.
    if qb_ok:
        reconcile_state({t.get("hash") for t in torrents})
        m = compute_metrics(torrents, st)
        write_metrics(m)
        update_films_panel(st, torrents)
        C.save_json(MGR_STATE, st)
        log.info("run done (%s) | approved=%d rejected=%d active=%d seeding=%d disk=%.1fGB up=%.1fGB down=%.1fGB deleted=%d zero_peers=%d stalled=%d",
                 cmd, m["approved"], m["rejected"], m["active"], m["seeding"], m["disk_gb"], m["uploaded_total"] / 1e9, m["downloaded_total"] / 1e9, m["deleted"], m["peers_zero"], m["stalled"])
    else:
        C.save_json(MGR_STATE, st)  # qB unreachable -> preserve metrics + last panel
        log.warning("run done (%s) | qB unreachable -> metrics preserved (not overwritten)", cmd)
    lock_fd.close()


if __name__ == "__main__":
    main()
