#!/usr/bin/env python3
"""auto-film torrent manager: monitor peers, free disk space on demand, emit metrics.

Modes:
  monitor                 (default; cron every 2h, and called by autorss after each run)
                          Sample peer counts, update 0-peer timers, write the Prometheus
                          textfile metrics, refresh the Grafana "recent films" text panel.
                          Never deletes.
  ensure-space [CAP_GB]   Called by autorss before adding a torrent. If the auto-film
                          CATEGORY's committed size exceeds CAP_GB, delete the worst
                          completed 0-peer seeders (longest at 0 peers, largest on disk
                          to break ties) until the category is back under CAP_GB. Never
                          deletes a torrent with connected peers or one still downloading.

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


def ensure_space(cap_gb, torrents, st):
    """Keep the auto-film CATEGORY at or below cap_gb by deleting COMPLETED 0-peer
    seeders (longest at 0 peers first, largest on disk to break ties). Never deletes a
    torrent with peers, nor one still downloading -- candidates are disjoint from the
    queue gate's domain (incomplete downloads), so the two never disagree about the same
    torrent. Deletion is a last resort -- the score rewards seeders -- so this only acts
    when the category's committed size exceeds the cap."""
    used = C.category_used_bytes(torrents)  # committed size, matching the autorss cap gate
    over = used - int(cap_gb * 1e9)
    if over <= 0:
        log.info("ensure-space | category %.1f GB <= cap %.1f GB; no deletion", used / 1e9, cap_gb)
        return
    zs = st.setdefault("zero_since", {})
    # worst candidate first: longest at 0 peers (oldest timestamp), then largest on disk.
    # Only completed seeders are eligible -- an in-progress download is never a victim.
    cands = sorted(
        (t for t in torrents if peers(t) == 0 and (t.get("progress") or 0) >= 1.0),
        key=lambda t: (zs.get(t.get("hash"), time.time()), -disk_bytes(t)),
    )
    log.info("ensure-space | category %.1f GB > cap %.1f GB (over %.1f GB); %d 0-peer candidate(s)",
             used / 1e9, cap_gb, over / 1e9, len(cands))
    reclaim = 0
    for t in cands:
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


def accumulate_uploaded(torrents, st):
    """Monotonic cumulative bytes uploaded by the auto-film category, immune to torrent
    deletion and per-torrent counter resets. qB's per-torrent `uploaded` disappears when
    a torrent is removed, so summing it live is non-monotonic and would break Prometheus
    increase() (a drop reads as a counter reset). Instead we bank each torrent's growth
    into a running total and prune departed hashes -- their contribution stays banked, so
    the exported counter only ever rises. Powers the daily/weekly uploaded-GB panels."""
    seen = st.setdefault("up_seen", {})
    total = st.get("uploaded_total", 0)
    present = set()
    for t in torrents:
        h = t.get("hash")
        if not h:
            continue
        present.add(h)
        cur = t.get("uploaded") or 0
        prev = seen.get(h, 0)
        total += (cur - prev) if cur >= prev else cur  # cur < prev => per-torrent reset
        seen[h] = cur
    for h in list(seen):
        if h not in present:
            seen.pop(h, None)  # departed torrent: growth already banked, stop tracking
    st["uploaded_total"] = total
    return total


def compute_metrics(torrents, st):
    films = C.load_json(C.STATE_FILE, {"films": {}}).get("films", {})
    return {
        "approved": sum(1 for f in films.values() if f.get("decision") is True),
        "rejected": sum(1 for f in films.values() if f.get("decision") is False),
        "active": sum(1 for t in torrents if is_active_download(t)),
        "stalled": sum(1 for t in torrents
                       if (t.get("progress") or 0) < 1.0
                       and t.get("state") not in C.DL_STOPPED and peers(t) == 0),
        "seeding": sum(1 for t in torrents
                       if (t.get("progress") or 0) >= 1.0 and t.get("state") not in C.UP_STOPPED),
        "disk_gb": sum(disk_bytes(t) for t in torrents) / 1e9,
        "deleted": st.get("deleted_total", 0),
        "peers_zero": sum(1 for t in torrents if peers(t) == 0),
        "uploaded_total": accumulate_uploaded(torrents, st),
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
        "# HELP autorss_deleted_total Cumulative torrents deleted by the manager",
        "# TYPE autorss_deleted_total counter",
        "autorss_deleted_total %d" % m["deleted"],
        "# HELP autorss_peers_zero_total Current auto-film torrents with 0 connected peers",
        "# TYPE autorss_peers_zero_total gauge",
        "autorss_peers_zero_total %d" % m["peers_zero"],
        "# HELP autorss_stalled_downloads Incomplete auto-film torrents stalled with 0 peers",
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


def recent_approved_films(limit=10):
    """Last `limit` Deepseek-approved films (status downloaded or pending), newest first."""
    films = C.load_json(C.STATE_FILE, {"films": {}}).get("films", {})
    rows = []
    for fid, f in films.items():
        if f.get("decision") is True and f.get("status") != "removed":
            rows.append({
                "title": f.get("clean") or f.get("title") or fid,
                "status": "downloaded" if f.get("status") == "downloaded" else "approved",
                "reason": f.get("reason", ""),
                "epoch": _epoch(f.get("added_ts") or f.get("asked_ts") or ""),
                "hash": (f.get("hash") or "").lower(),
            })
    rows.sort(key=lambda r: r["epoch"], reverse=True)
    return rows[:limit]


def torrent_stats(torrents):
    """Map hash -> seeding stats (days, uploaded GB, ratio) for live auto-film torrents."""
    stats = {}
    for t in torrents or []:
        h = (t.get("hash") or "").lower()
        if not h:
            continue
        stats[h] = {
            "seed_days": (t.get("seeding_time") or 0) / 86400.0,
            "up_gb": (t.get("uploaded") or 0) / 1e9,
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
        return "_Nincs jóváhagyott film._"
    stats = stats or {}
    def esc(s):
        return str(s).replace("|", "\\|").replace("\n", " ")
    out = ["| Film | Státusz | Ok | Hozzáadva | Seedelés (days) | Feltöltve (GB) | Ratio |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        when = datetime.fromtimestamp(r["epoch"]).strftime("%Y-%m-%d %H:%M") if r["epoch"] else ""
        s = stats.get(r.get("hash"))
        seed_days = "%.1f" % s["seed_days"] if s else ""
        up_gb = "%.2f" % s["up_gb"] if s else ""
        ratio = "%.2f" % s["ratio"] if s else ""
        out.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            esc(r["title"]), esc(r["status"]), esc(r["reason"]), when, seed_days, up_gb, ratio))
    return "\n".join(out)


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
    user = C.env_value(C.PROJECT_ENV, "GRAFANA_USER")
    pw = C.env_value(C.PROJECT_ENV, "GRAFANA_PASS")
    if not (base and user and pw):
        log.warning("films panel: Grafana creds missing in %s; skipping", C.PROJECT_ENV)
        return
    auth = base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
    try:
        req = urllib.request.Request(base + "/api/dashboards/uid/" + DASH_UID,
                                     headers={"Authorization": "Basic " + auth})
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
            headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
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
        log.info("run done (%s) | approved=%d rejected=%d active=%d seeding=%d disk=%.1fGB up=%.1fGB deleted=%d zero_peers=%d stalled=%d",
                 cmd, m["approved"], m["rejected"], m["active"], m["seeding"], m["disk_gb"], m["uploaded_total"] / 1e9, m["deleted"], m["peers_zero"], m["stalled"])
    else:
        C.save_json(MGR_STATE, st)  # qB unreachable -> preserve metrics + last panel
        log.warning("run done (%s) | qB unreachable -> metrics preserved (not overwritten)", cmd)
    lock_fd.close()


if __name__ == "__main__":
    main()
