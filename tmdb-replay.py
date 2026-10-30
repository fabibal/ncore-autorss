#!/usr/bin/env python3
"""Read-only: fetch TMDB facts for the films the filter has seen, to calibrate a rule.

  python3 tmdb-replay.py [DAYS]     (default 35)

Looks up every distinct clean title that was judged in the last DAYS days plus every live
auto-film seeder, and stores the result in tmdb-cache.json (re-runs only fetch what is
missing). Prints a short summary; analyse the cache to pick thresholds. It changes
nothing: no qB writes, no state writes.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

import autorss_common as C
import tmdb

CACHE = os.path.join(C.BASE_DIR, "tmdb-cache.json")


def main():
    tok = tmdb.token()
    if not tok:
        raise SystemExit("no TMDB_READ_TOKEN in " + C.PROJECT_ENV)
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 35
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    films = C.load_json(C.STATE_FILE, {"films": {}}).get("films", {})
    titles = {f["clean"] for f in films.values()
              if f.get("clean") and f.get("decision") is not None
              and (f.get("asked_ts") or "") >= cutoff}
    titles |= {f["clean"] for f in films.values() if f.get("clean") and f.get("hash")}

    cache = C.load_json(CACHE, {})
    todo = sorted(t for t in titles if t not in cache)
    print("%d titles, %d cached, %d to fetch" % (len(titles), len(titles) - len(todo), len(todo)))
    failed = 0
    for i, clean in enumerate(todo, 1):
        try:
            cache[clean] = tmdb.lookup(clean, tok)
        except Exception as e:
            failed += 1
            print("lookup failed for %r: %s" % (clean, e))
            if failed >= 5:
                print("too many failures, stopping")
                break
        if i % 50 == 0:
            C.save_json(CACHE, cache)
        time.sleep(0.05)
    C.save_json(CACHE, cache)
    found = sum(1 for t in titles if cache.get(t))
    print("matched %d of %d titles (%d not found in TMDB)" % (found, len(titles), len(titles) - found))


if __name__ == "__main__":
    main()
