#!/usr/bin/env python3
"""nCore RSS -> Deepseek filter -> qBittorrent auto-seeder.

Fetches the nCore RSS feed, keeps only "[Film" items, asks Deepseek whether each
new film is a well-known mainstream title worth seeding, and (if yes) adds it to
qBittorrent under category "auto-film" -- at most one active auto download at a
time. State is kept locally so nothing is re-processed.

qBittorrent runs in a Docker container with localhost-auth-bypass; host requests
get 403 through Docker NAT, so API calls are issued via `docker exec` from inside
the container (see autorss_common). The nCore passkey is read from .env.
"""
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import autorss_common as C
from autorss_common import is_active_download

# --- config -----------------------------------------------------------------
LOG_FILE = os.path.join(C.BASE_DIR, "autorss.log")
LOCK_FILE = os.path.join(C.BASE_DIR, ".lock")
RUN_METRICS_FILE = os.path.join(C.TEXTFILE_DIR, "autorss_run.prom")

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
MANAGER = os.path.join(C.BASE_DIR, "torrent-manager.py")

SYS_PROMPT = (
    "You decide whether a film is worth seeding on a private tracker. "
    "The goal is to seed new, high-demand releases, not old uploads of "
    "classic films. "
    "Today is {today}. "
    "Answer true ONLY for a mainstream narrative feature film that "
    "(a) you actually RECOGNISE by title and know to be highly anticipated "
    "or widely popular with the general public, "
    "and (b) is newly out or was released within the last 12 months "
    "(on or after {cutoff}), whether that release was theatrical, "
    "streaming-exclusive, or both. The distribution channel does not "
    "matter -- judge only recency and mainstream popularity. "
    "Criterion (a) is RECOGNITION, never inference: a recent release year "
    "is evidence of recency only and says nothing about popularity. When "
    "you answer true, the reason MUST name what you recognise the film by "
    "-- its cast, its director, its studio, or the franchise it belongs "
    "to. If you can name none of those, you are inferring from the title "
    "alone: answer false. Titles like 'Untitled ...' are placeholders and "
    "are always false. "
    "Recognition does include a film you know only as an announced or "
    "upcoming project; the release itself is already published on the "
    "tracker, so never answer false merely because it postdates your "
    "knowledge or you cannot confirm it shipped. "
    "Your reason must agree with your answer: never answer true with a "
    "reason that hedges, admits doubt, or concedes that the cast, "
    "director, studio or franchise is unknown to you. "
    "Answer false for anything older, even famous classics or acclaimed "
    "blockbusters. "
    "Also answer false for anything that is not a mainstream narrative "
    "feature film, including: sports recordings and match broadcasts, "
    "TV series and made-for-TV movies, low-budget or obscure direct-to-video "
    "titles, documentaries, concert and performance films, stand-up "
    "specials, awards shows, and similar non-film content. "
    "If the title has no release year, judge its recency and legitimacy "
    "from your own knowledge of that specific title -- never assume it is "
    "old or ineligible just because no year was given; but if you do not "
    "recognise the title at all, it fails (a) and the answer is false. "
    "Respond with strict minified JSON only, no markdown and no extra text: "
    '{{"download": true, "reason": "..."}}  with reason under 15 words.'
).format(
    today=datetime.now().strftime("%Y-%m-%d"),
    cutoff=(datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"),
)

# BluRay remux / full-disc releases are huge and low-demand for auto-seeding -> skip.
REMUX_RE = re.compile(
    r"\b(remux|bd25|bd50|bdmv|disc|fullbd)\b|complete\.bluray", re.I
)

# Auto-seed 720p releases; a 1080p release is accepted only when no 720p release of the
# same film is in the current feed. Anything else (2160p, SD, ...) is skipped.
RES_720P_RE = re.compile(r"\b720p\b", re.I)
RES_1080P_RE = re.compile(r"\b1080p\b", re.I)

# Release/quality tokens stripped from titles that carry no usable year.
QUALITY_RE = re.compile(
    r"\b(2160p|1080p|720p|480p|uhd|bluray|blu-ray|bdrip|brrip|web-?dl|web-?rip|web|"
    r"hdtv|dvdrip|dvd|remux|hdr10|hdr|dv|sdr|x264|x265|h\.?264|h\.?265|hevc|avc|xvid|"
    r"divx|aac|ac3|dts(-hd)?|ddp?5\.?1|ddp|truehd|atmos|flac|10bit|imax|proper|repack|"
    r"extended|unrated|remastered|read|nfo|hun|eng|multi|dual|amzn|nf|dsnp|hmax|atvp)\b",
    re.I,
)

log = logging.getLogger("autorss")

# A release year is a 19xx/20xx token no later than next year; a larger number (e.g.
# "Blade Runner 2049") is part of the title, not a release year.
MAX_YEAR = datetime.now().year + 1

# Statuses with a real Deepseek verdict (download true/false) whose decision can be
# reused across quality variants of the same film. Excludes 'skipped' (a per-release
# quality/format pre-filter, never a film-level verdict) and 'decide_pending'/
# 'decide_failed' (no verdict was ever obtained -- nothing to reuse).
DECIDED_STATUSES = {"pending", "downloaded", "rejected", "removed", "add_failed"}
# Statuses that should block reprocessing OTHER releases of the same film. Excludes
# 'skipped' (a different-quality variant may still qualify), 'removed' (freed for
# space -- allow re-acquisition when it trends again), and 'decide_pending'/
# 'decide_failed' (never evaluated -- must not block a real verdict on another release).
SEEN_STATUSES = {"pending", "downloaded", "rejected"}


# --- helpers ----------------------------------------------------------------
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_title(raw):
    """Strip the [Film ...] tag and quality/encoding noise; keep name (+year) as a
    stable dedup key shared by every quality variant of one film.

    The release year is the LAST plausible 19xx/20xx token (<= next year) before the
    quality block; a future-looking number in the title ("Blade Runner 2049") is kept
    as part of the name. Separators are always normalised (dots/underscores -> spaces)
    so dotted scene names and spaced names collapse to the SAME key. Falls back to the
    tag-stripped raw title if cleaning would otherwise leave an empty key.
    """
    raw_stripped = re.sub(r"\s*\[[^\]]*\]\s*$", "", raw.strip()).strip()
    t = re.sub(r"\s*\[[^\]]*\]\s*$", "", raw_stripped).strip()
    years = [m for m in re.finditer(r"(?<![0-9])(19|20)\d{2}(?![0-9])", t)
             if int(m.group(0)) <= MAX_YEAR]
    if years:
        ym = years[-1]
        head = t[: ym.start()].replace(".", " ").replace("_", " ")
        name = re.sub(r"\s+", " ", head).strip(" .-_")
        if name:
            return f"{name} ({ym.group(0)})"
        # the year sits at the very start -> the title itself is a number; clean as no-year
    t = t.replace(".", " ").replace("_", " ")
    t = QUALITY_RE.sub(" ", t)
    t = re.sub(r"[._]+", " ", t)
    cleaned = re.sub(r"\s+", " ", t).strip(" .-_")
    return cleaned or raw_stripped or raw.strip()


def dedup_key(clean):
    """Case-insensitive form of a clean title for dedup/cache lookups, so an all-caps
    scene release ("POWER BALLAD (2026)") and a title-case one of the SAME film
    ("Power Ballad (2026)") share one cache entry instead of costing two Deepseek
    calls (or worse, two independent -- and possibly different -- verdicts)."""
    return clean.casefold()


def seen_clean_titles(films):
    """Dedup keys of clean titles with a final disposition (pending/downloaded/rejected)
    that should block reprocessing other releases of the same film. Excludes 'skipped'
    and 'removed' so a different-quality variant or a re-trending freed film can still
    be picked up."""
    return {dedup_key(v["clean"]) for v in films.values()
            if v.get("clean") and v.get("status") in SEEN_STATUSES}


def wanted_quality(title, feed_720p_keys):
    """720p always; 1080p only if the film has no 720p release in the current feed."""
    if RES_720P_RE.search(title):
        return True
    return bool(RES_1080P_RE.search(title)) and dedup_key(clean_title(title)) not in feed_720p_keys


def fetch_films(rss_key):
    """(items_total, [{id, title, link}]) for RSS items whose title contains '[Film'.

    items_total counts every <item> so an empty/HTML feed (passkey expired) is
    visible as 0 even when no films match.
    """
    url = "https://ncore.pro/rss.php?key=" + rss_key
    req = urllib.request.Request(url, headers={"User-Agent": "ncore-autorss/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    root = ET.fromstring(data)  # raises on HTML/garbage -> caught as a fetch failure
    items = root.findall(".//item")
    films = []
    for it in items:
        title = (it.findtext("title") or "").strip()
        if "[Film" not in title:
            continue
        link = (it.findtext("link") or "").strip()
        m = re.search(r"[?&]id=(\d+)", link)
        if not m:
            continue
        films.append({"id": m.group(1), "title": title, "link": link})
    return len(items), films


def ask_deepseek(clean, key):
    """Return (download: bool, reason: str). Raises on API/parse failure.

    On an HTTP error, reads the response BODY (DeepSeek's actual error detail, e.g. a
    content-policy or request-format complaint) into the exception message instead of
    just the bare status line -- otherwise a recurring failure is undiagnosable from
    the log alone (all we'd ever see is "HTTP Error 400: Bad Request")."""
    body = json.dumps(
        {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": SYS_PROMPT},
                {"role": "user", "content": clean},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        DEEPSEEK_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {detail}") from None
    content = resp["choices"][0]["message"]["content"].strip()
    content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
    obj = json.loads(content)
    return bool(obj.get("download", False)), str(obj.get("reason", ""))[:200]


def qb_create_category():
    C.docker_curl(["-X", "POST", C.QB_API + "/api/v2/torrents/createCategory",
                   "--data-urlencode", "category=" + C.QB_CATEGORY])  # 409 if exists -- harmless


def qb_add(link):
    """POST the URL to qBittorrent. (accepted, detail). Accepted != actually fetched."""
    rc, out, err = C.docker_curl(
        ["-w", "\n%{http_code}", "-X", "POST", C.QB_API + "/api/v2/torrents/add",
         "--data-urlencode", "urls=" + link,
         "--data-urlencode", "category=" + C.QB_CATEGORY,
         # Never auto-stop seeding: -1 = no limit (overrides any global ratio/time cap).
         # A stopped/paused torrent stops counting toward ERTEK time/seedsize/up(interval).
         "--data-urlencode", "ratioLimit=-1",
         "--data-urlencode", "seedingTimeLimit=-1"]
    )
    lines = out.splitlines()
    code = lines[-1].strip() if lines else ""
    body = "\n".join(lines[:-1]).strip()
    # Newer qB answers 202 (async add, body has pending_count) instead of 200.
    accepted = rc == 0 and code in ("200", "202") and body.lower() != "fails."
    return accepted, f"http={code} body={body!r} err={err.strip()[:80]}"


def qb_add_confirmed(link, timeout_s=20):
    """Add the URL and verify a new torrent actually appears (qB fetches the .torrent
    asynchronously, so HTTP 200 only means 'accepted'). (ok, detail, hash_or_None)."""
    before, err = C.qb_torrents()
    if before is None:
        return False, "pre-add snapshot failed: " + err, None
    before_hashes = {t.get("hash") for t in before}
    accepted, detail = qb_add(link)
    if not accepted:
        return False, "add not accepted: " + detail, None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(2)
        cur, _ = C.qb_torrents()
        if cur is None:
            continue
        new = {t.get("hash") for t in cur} - before_hashes
        if new:
            return True, "confirmed", next(iter(new))
    return False, f"accepted but no new torrent within {timeout_s}s (fetch failed?)", None


def qb_active_auto():
    """(active_count, info). count<0 means the query failed."""
    arr, err = C.qb_torrents()
    if arr is None:
        return -1, err
    active = [t for t in arr if is_active_download(t)]
    return len(active), f"{len(arr)} in category, {len(active)} active"


def write_run_metrics(meta):
    """Liveness textfile so a silently-stopped pipeline is detectable in Prometheus."""
    lines = [
        "# HELP autorss_last_run_timestamp_seconds Unix time autorss last ran",
        "# TYPE autorss_last_run_timestamp_seconds gauge",
        "autorss_last_run_timestamp_seconds %d" % meta.get("last_run_ts", 0),
        "# HELP autorss_last_success_timestamp_seconds Unix time autorss last fetched a non-empty feed",
        "# TYPE autorss_last_success_timestamp_seconds gauge",
        "autorss_last_success_timestamp_seconds %d" % meta.get("last_success_ts", 0),
        "# HELP autorss_feed_items Items in the last RSS fetch (0 = empty/error feed)",
        "# TYPE autorss_feed_items gauge",
        "autorss_feed_items %d" % meta.get("feed_items", 0),
        "# HELP autorss_feed_films Film items in the last RSS fetch",
        "# TYPE autorss_feed_films gauge",
        "autorss_feed_films %d" % meta.get("feed_films", 0),
        "",
    ]
    os.makedirs(C.TEXTFILE_DIR, exist_ok=True)
    tmp = RUN_METRICS_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines))
    os.replace(tmp, RUN_METRICS_FILE)


# --- main -------------------------------------------------------------------
def _refresh_metrics():
    """Regenerate Prometheus metrics via the manager (also samples 0-peer timers)."""
    try:
        subprocess.run([sys.executable, MANAGER, "monitor"], timeout=120)
    except Exception as e:
        log.error("metrics refresh failed: %s", e)


def main():
    C.setup_logging("autorss", LOG_FILE)
    lock_fd = C.flock_or_exit(LOCK_FILE, log)  # keep referenced for the whole run; closing it drops the lock
    try:
        _run()
    finally:
        _refresh_metrics()
        lock_fd.close()


def _run():
    deepseek_key = C.env_value(C.TRADING_ENV, "DEEPSEEK")
    if not deepseek_key:
        raise SystemExit("no DEEPSEEK key in " + C.TRADING_ENV)
    rss_key = C.env_value(C.PROJECT_ENV, "NCORE")
    if not rss_key:
        raise SystemExit("no NCORE_RSS_KEY in " + C.PROJECT_ENV)

    ts = int(time.time())
    state = C.mutate_state(lambda s: s["_meta"].update({"last_run_ts": ts}))

    try:
        n_items, films = fetch_films(rss_key)
    except Exception as e:
        log.error("RSS fetch failed: %s", e)
        state = C.mutate_state(lambda s: s["_meta"].update({"feed_items": 0, "feed_films": 0}))
        write_run_metrics(state["_meta"])
        return

    def _set_feed(s):
        s["_meta"].update({"feed_items": n_items, "feed_films": len(films)})
        if n_items > 0:
            s["_meta"]["last_success_ts"] = int(time.time())
    state = C.mutate_state(_set_feed)
    if n_items == 0:
        log.warning("RSS returned 0 items -- passkey expired or feed down?")
    write_run_metrics(state["_meta"])

    # "new" also includes a previously-failed Deepseek call still under its retry cap
    # (status=decide_pending) -- it never got a real verdict, so it must be retried,
    # not treated as already-seen.
    new = [f for f in films if f["id"] not in state["films"]
           or state["films"][f["id"]].get("status") == "decide_pending"]
    log.info("run start: %d items, %d film(s), %d new", n_items, len(films), len(new))

    # Reuse a prior Deepseek verdict across quality variants of one film. Only real
    # verdicts -- NOT quality/format skips -- so a 720p variant of a remux-skipped film
    # still gets evaluated on its own merits. Keyed by dedup_key so an all-caps and a
    # title-case release of the same film share one cached verdict.
    decided = {
        dedup_key(v["clean"]): (v["decision"], v.get("reason", ""))
        for v in state["films"].values()
        if v.get("clean") and "decision" in v and v.get("status") in DECIDED_STATUSES
    }
    # Films with a final disposition -> don't reprocess another release of the same film.
    seen_clean = seen_clean_titles(state["films"])
    feed_720p_keys = {dedup_key(clean_title(f["title"])) for f in films
                      if RES_720P_RE.search(f["title"]) and not REMUX_RE.search(f["title"])}
    for f in new:
        clean = clean_title(f["title"])
        key = dedup_key(clean)
        if key in seen_clean:
            log.info("already seen: %s", f["title"])
            continue
        if REMUX_RE.search(f["title"]):
            rec = {"title": f["title"], "clean": clean, "decision": False,
                   "reason": "remux/full disc filter", "status": "skipped",
                   "link": f["link"], "asked_ts": now()}
            state = C.mutate_state(lambda s, fid=f["id"], r=rec: s["films"].__setitem__(fid, r))
            log.info("SKIP | %r | reason=remux/full disc filter", f["title"])
            continue
        if not wanted_quality(f["title"], feed_720p_keys):
            rec = {"title": f["title"], "clean": clean, "decision": False,
                   "reason": "quality filter (720p, or 1080p without a 720p variant)", "status": "skipped",
                   "link": f["link"], "asked_ts": now()}
            state = C.mutate_state(lambda s, fid=f["id"], r=rec: s["films"].__setitem__(fid, r))
            log.info("SKIP | %r | reason=quality filter (720p, or 1080p without a 720p variant)", f["title"])
            continue
        cached = key in decided
        if cached:
            decision, reason = decided[key]
        else:
            try:
                decision, reason = ask_deepseek(clean, deepseek_key)
            except Exception as e:
                state = C.mutate_state(
                    lambda s, fid=f["id"], t=f["title"], c=clean, l=f["link"], err=str(e):
                        _mark_decide_attempt(s, fid, t, c, l, err))
                attempt_rec = state["films"][f["id"]]
                if attempt_rec["status"] == "decide_failed":
                    log.error("DECIDE FAILED | %r -- giving up after %d attempts: %s",
                              clean, C.MAX_DECIDE_ATTEMPTS, e)
                else:
                    log.error("deepseek error for %r (attempt %d/%d, will retry): %s",
                              clean, attempt_rec["decide_attempts"], C.MAX_DECIDE_ATTEMPTS, e)
                continue
            decided[key] = (decision, reason)
        rec = {"title": f["title"], "clean": clean, "decision": decision, "reason": reason,
               "status": "pending" if decision else "rejected",
               "link": f["link"], "asked_ts": now()}
        state = C.mutate_state(lambda s, fid=f["id"], r=rec: s["films"].__setitem__(fid, r))
        seen_clean.add(key)
        log.info("DECISION%s | %r -> download=%s | reason=%r",
                 " (cached)" if cached else "", clean, decision, reason)

    # Trim the auto-film category back under its hard cap. This deletes only completed
    # 0-peer torrents and no-ops while the category is under CATEGORY_HARD_GB, so in
    # normal operation it removes nothing.
    #
    # Runs BEFORE the pending/slots gates below (moved 2026-08-16), not after them.
    # It used to sit just above the add loop, which made the hard cap enforceable ONLY
    # on a run that actually queued a download: every quiet run returned at "no pending
    # downloads" and never reached the trim. Live, that left the category parked at
    # 483.3 GB against the 480 GB cap for 26 h -- a completed download had pushed it over
    # and approvals then went quiet, so nothing came back to collect. Nothing between
    # here and the add loop grows the category, so this is still "trim before adding".
    try:
        subprocess.run([sys.executable, MANAGER, "ensure-space", str(C.CATEGORY_HARD_GB)], timeout=300)
    except Exception as e:
        log.error("manager ensure-space failed: %s", e)

    # Download phase: keep up to MAX_ACTIVE_DOWNLOADS active auto-film downloads.
    pending = [fid for fid, v in state["films"].items() if v.get("status") == "pending"]
    if not pending:
        log.info("no pending downloads")
        log.info("run done")
        return

    active, info = qb_active_auto()
    if active < 0:
        log.error("qBittorrent query failed: %s", info)
        log.info("run done")
        return
    slots = C.MAX_ACTIVE_DOWNLOADS - active
    if slots <= 0:
        log.info("HOLD | %d active >= max %d (%s); %d pending", active, C.MAX_ACTIVE_DOWNLOADS, info, len(pending))
        log.info("run done")
        return

    # Newest-approved first: hot releases peak in leechers in their first hours, so
    # grabbing them early maximises up(interval) and keeps avg(torrdate) fresh.
    pending.sort(key=lambda fid: state["films"][fid].get("asked_ts", ""), reverse=True)

    # Soft cap: if the category is still over CATEGORY_CAP_GB (0-peer deletion couldn't
    # bring it down -- everything has peers), HOLD rather than add more. Existing seeders
    # are never sacrificed just to fit a new download.
    arr, qerr = C.qb_torrents()
    if arr is None:
        log.error("qBittorrent query failed: %s", qerr)
        log.info("run done")
        return
    used_gb = C.category_used_bytes(arr) / 1e9
    if used_gb > C.CATEGORY_CAP_GB:
        log.info("HOLD | category %.1f GB > cap %.1f GB; not adding (seeders protected); %d pending",
                 used_gb, C.CATEGORY_CAP_GB, len(pending))
        log.info("run done")
        return

    qb_create_category()
    added = 0
    for fid in pending[:slots]:
        v = state["films"].get(fid)
        if v is None:
            continue
        clean = v.get("clean")
        ok, detail, thash = qb_add_confirmed(v["link"])
        if ok:
            ats = now()
            state = C.mutate_state(lambda s, fid=fid, h=thash, a=ats: _mark_downloaded(s, fid, h, a))
            added += 1
            log.info("ADDED | id=%s %r -> category=%s hash=%s", fid, clean, C.QB_CATEGORY, thash)
        else:
            state = C.mutate_state(lambda s, fid=fid: _mark_add_attempt(s, fid))
            f = state["films"].get(fid, {})
            if f.get("status") == "add_failed":
                log.error("ADD FAILED | id=%s %r (%s) -- gave up after %d attempts",
                          fid, clean, detail, C.MAX_ADD_ATTEMPTS)
            else:
                log.error("ADD UNCONFIRMED | id=%s %r (%s) -- left pending, will retry", fid, clean, detail)

    log.info("run done (added %d of %d slot(s), %d pending)", added, slots, len(pending))


def _mark_downloaded(s, fid, thash, added_ts):
    f = s["films"].get(fid)
    if f is not None:
        f.update({"status": "downloaded", "added_ts": added_ts, "hash": thash})


def _mark_decide_attempt(s, fid, title, clean, link, detail):
    """Record a failed Deepseek call and count it; after MAX_DECIDE_ATTEMPTS give up
    (status=decide_failed) so a title that fails every time isn't retried forever. The
    `detail` (from ask_deepseek, includes the HTTP body when available) is kept as the
    reason so a stuck title is diagnosable from state.json, not just a log line that
    ages out. decision stays None throughout -- this is not a real verdict, so it must
    never be counted as approved/rejected (compute_metrics uses `is True`/`is False`)
    nor reused via DECIDED_STATUSES/SEEN_STATUSES."""
    n = s["films"].get(fid, {}).get("decide_attempts", 0) + 1
    rec = {"title": title, "clean": clean, "link": link, "asked_ts": now(),
           "decision": None, "decide_attempts": n}
    if n >= C.MAX_DECIDE_ATTEMPTS:
        rec["status"] = "decide_failed"
        rec["reason"] = f"deepseek call failed {n}x, giving up: {detail}"[:300]
    else:
        rec["status"] = "decide_pending"
        rec["reason"] = f"deepseek call failed (attempt {n}/{C.MAX_DECIDE_ATTEMPTS}): {detail}"[:300]
    s["films"][fid] = rec


def _mark_add_attempt(s, fid):
    """Count an unconfirmed add; after MAX_ADD_ATTEMPTS give up so a dead/duplicate URL
    isn't retried forever (qb_add_confirmed never sees a NEW hash for an already-present
    infohash, which would otherwise pin the film at 'pending')."""
    f = s["films"].get(fid)
    if f is None:
        return
    n = f.get("add_attempts", 0) + 1
    f["add_attempts"] = n
    if n >= C.MAX_ADD_ATTEMPTS:
        f["status"] = "add_failed"
        f["reason"] = "add unconfirmed after %d attempts" % n


if __name__ == "__main__":
    main()
