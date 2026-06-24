#!/usr/bin/env python3
"""Shared constants + helpers for autorss.py and torrent-manager.py.

Single source of truth so the two scripts can't drift -- in particular the queue
gate and the metrics must agree on what an "active download" is.
"""
import fcntl
import json
import logging
import os
import subprocess
import sys

# --- paths / config ---------------------------------------------------------
BASE_DIR = "/home/fbazsa/ncore-autorss"
STATE_FILE = os.path.join(BASE_DIR, "state.json")
STATE_LOCK_FILE = os.path.join(BASE_DIR, ".state.lock")  # shared across both scripts; serialises state.json writers
PROJECT_ENV = os.path.join(BASE_DIR, ".env")          # NCORE_RSS_KEY, GRAFANA_*
TRADING_ENV = "/home/fbazsa/trading_agent/.env"        # DEEPSEEK key
TEXTFILE_DIR = "/home/fbazsa/monitoring/textfile_collector"

DOCKER = "/usr/bin/docker"
QB_CONTAINER = "qbittorrent"
QB_API = "http://localhost:8080"
QB_CATEGORY = "auto-film"
# Score note (nCore ERTEK): time/seedsize/skala_torrszam all reward KEEPING torrents.
# Deleting a seeder destroys accrued seed time and can drop a count tier, so deletion
# is a last resort. We bound the auto-film CATEGORY's own on-disk size (not host free
# space): above CATEGORY_HARD_GB the manager trims 0-peer torrents back toward it;
# above CATEGORY_CAP_GB autorss holds the queue (won't add) -- the fallback for when
# 0-peer deletion alone can't keep the category under the hard limit.
CATEGORY_HARD_GB = 480.0  # hard: above this, delete 0-peer torrents down toward this size
CATEGORY_CAP_GB = 500.0   # soft cap: above this, hold the queue (don't download)
MAX_ACTIVE_DOWNLOADS = 3  # concurrent auto-film downloads (was effectively 1) -> faster count growth + catches upload peaks
MAX_ADD_ATTEMPTS = 5      # give up a pending film after this many unconfirmed adds (avoid retrying a dead URL forever)

DL_STOPPED = {"stoppedDL", "pausedDL", "error", "missingFiles"}
UP_STOPPED = {"stoppedUP", "pausedUP", "error", "missingFiles"}
STALL_TIMEOUT_S = 6 * 3600  # a peerless download stalled this long no longer counts as active


# --- generic helpers --------------------------------------------------------
def setup_logging(name, log_file):
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in (logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        log.addHandler(h)
    return log


def flock_or_exit(lock_file, log):
    """Acquire an exclusive non-blocking lock; exit(0) if another run already holds it.

    IMPORTANT: the returned file object OWNS the lock -- the advisory lock is released
    the instant that fd is closed. The CALLER MUST keep the returned object referenced
    for the whole run (e.g. `lock = C.flock_or_exit(...)` in main()). Discarding the
    return value lets CPython garbage-collect the fd immediately, silently dropping the
    lock and defeating the single-instance guarantee."""
    f = open(lock_file, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another run holds the lock; exiting")
        sys.exit(0)
    return f


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def mutate_state(fn):
    """Locked read-modify-write of STATE_FILE: acquire the shared state lock, load the
    current state from disk, apply fn(state) in place, save atomically, release, and
    return the saved state.

    This serialises every state.json writer (autorss and torrent-manager, plus any
    overlapping runs) so a write merges into the LATEST on-disk state instead of
    overwriting a stale in-memory copy -- the only safe way to mutate it cross-process.
    Readers may use load_json without the lock: save_json's os.replace is atomic, so a
    reader always sees a complete prior-or-new file, never a torn one."""
    lf = open(STATE_LOCK_FILE, "w")
    fcntl.flock(lf, fcntl.LOCK_EX)
    try:
        state = load_json(STATE_FILE, {"films": {}, "_meta": {}})
        state.setdefault("films", {})
        state.setdefault("_meta", {})
        fn(state)
        save_json(STATE_FILE, state)
        return state
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


def env_value(env_file, name_contains):
    """Value for `name_contains` from env_file. Prefers an EXACT (case-insensitive) var
    name match; otherwise falls back to the first variable whose name CONTAINS it -- so
    a superset-named var (e.g. DEEPSEEK_BACKUP) can't shadow the one you asked for."""
    want = name_contains.upper()
    fallback = None
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if not v:
                    continue
                if k.upper() == want:
                    return v
                if fallback is None and want in k.upper():
                    fallback = v
    except FileNotFoundError:
        return None
    return fallback


# --- qBittorrent (via docker exec; localhost-auth-bypass works inside container) ---
def docker_curl(args, timeout=40):
    cmd = [DOCKER, "exec", QB_CONTAINER, "curl", "-s", "-m", "25"] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return -1, "", str(e)


def qb_torrents(category=QB_CATEGORY):
    """(list, '') on success, (None, err) on failure -- torrents in a category."""
    rc, out, err = docker_curl([QB_API + "/api/v2/torrents/info?category=" + category])
    if rc != 0:
        return None, (err or "docker exec failed").strip()[:120]
    try:
        return json.loads(out or "[]"), ""
    except json.JSONDecodeError as e:
        return None, "parse error: %s" % e


def peers(t):
    """Connected peers other than us: seeds + leechers we are connected to."""
    return (t.get("num_seeds") or 0) + (t.get("num_leechs") or 0)


def torrent_size(t):
    """Full committed on-disk size of the torrent (its eventual footprint). Uses `size`
    (not partial `completed` bytes) so the category cap reserves in-flight downloads at
    their FINAL size -- otherwise three barely-started downloads slip under the cap and
    overshoot it once they finish."""
    return t.get("size") or t.get("completed") or 0


def category_used_bytes(torrents):
    """Total committed on-disk size of the auto-film category (sum of full torrent sizes)."""
    return sum(torrent_size(t) for t in torrents or [])


def is_active_download(t):
    """True if an auto-film torrent genuinely occupies the single download slot:
    incomplete, not stopped/paused/errored, and not a dead peerless stall.

    SINGLE definition used by both the autorss queue gate and the dashboard metric."""
    if (t.get("progress") or 0) >= 1.0:
        return False  # complete -> seeding, not downloading
    if t.get("state") in DL_STOPPED:
        return False  # stopped / paused / errored / missing files
    if peers(t) == 0 and (t.get("time_active") or 0) > STALL_TIMEOUT_S:
        return False  # long-stalled with no peers -> dead; must not freeze the queue
    return True
