#!/usr/bin/env python3
"""Show what `torrent-manager.py ensure-space CAP` WOULD delete, without deleting it.

Read-only: queries live qBittorrent and reads manager-state.json, but never writes state
and never calls the qB delete endpoint. Deleting a seeder is irreversible and destroys
accrued ERTEK `time`/`seedsize`, so this exists to make a trim inspectable BEFORE it runs.

The plan comes from `torrent_manager.select_victims` -- the same function the real
ensure_space calls -- so what you see here is what a real run does, not a lookalike.

Usage:
    python3 dryrun-ensure-space.py                # plan at the real CATEGORY_HARD_GB cap
    python3 dryrun-ensure-space.py 479 476        # plan at one or more hypothetical caps

A cap at or above current usage prints "no-op", which is the normal healthy answer.
"""
import importlib.util
import sys
import time

sys.path.insert(0, "/home/fbazsa/ncore-autorss")
import autorss_common as C


def _load(name, path):
    """torrent-manager.py has a hyphen, so it can't be imported by name."""
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


tm = _load("torrent_manager", "/home/fbazsa/ncore-autorss/torrent-manager.py")


def describe(t, zs, now):
    dead_d = (now - zs.get(t.get("hash"), now)) / 86400
    last = t.get("last_activity") or 0
    idle_d = (now - last) / 86400 if last > 0 else float("nan")
    return ("   %6.2f GB | dead %5.1f d | idle %5.1f d | ratio %6.2f | up %7.1f GB | %s"
            % (tm.disk_bytes(t) / 1e9, dead_d, idle_d, t.get("ratio") or 0,
               (t.get("uploaded") or 0) / 1e9, (t.get("name") or "")[:55]))


def main():
    caps = [float(a) for a in sys.argv[1:]] or [float(C.CATEGORY_HARD_GB)]

    torrents = tm.fetch_auto_torrents()
    if torrents is None:
        sys.exit("qB query failed -- cannot plan (the real run refuses to delete blind too)")
    st = C.load_json(tm.MGR_STATE, {})
    zs = st.get("zero_since", {})
    now = time.time()

    used = C.category_used_bytes(torrents)
    print("category: %.2f GB committed across %d torrent(s)" % (used / 1e9, len(torrents)))

    for cap in caps:
        over = used - int(cap * 1e9)
        if over <= 0:
            print("\ncap %.1f GB | %.2f GB under -- no-op, nothing would be deleted"
                  % (cap, -over / 1e9))
            continue
        victims, single = tm.select_victims(torrents, over, zs, now)
        # Replay ensure_space's own stop condition: it deletes until the overage is
        # covered, so the plan is a prefix of the victim list, not the whole list.
        plan, freed = [], 0
        for t in victims:
            if freed >= over:
                break
            plan.append(t)
            freed += tm.disk_bytes(t)

        print("\ncap %.1f GB | over %.2f GB | %d eligible candidate(s) | plan: %s"
              % (cap, over / 1e9, len(victims),
                 ("idle-first, " if plan and C.is_idle(plan[0], now) else "")
                 + ("smallest sufficient" if single is not None
                    else "no single candidate covers it; combining the fewest that do")))
        for t in plan:
            print(describe(t, zs, now))
        if freed < over:
            print("   !! only %.2f GB of %.2f GB coverable -- the rest have peers or are"
                  " still downloading" % (freed / 1e9, over / 1e9))
        else:
            print("   => frees %.2f GB for a %.2f GB overage (%.1fx), %d torrent(s)"
                  % (freed / 1e9, over / 1e9, freed / over, len(plan)))


if __name__ == "__main__":
    main()
