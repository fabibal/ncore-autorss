#!/usr/bin/env python3
"""TMDB lookups for the film filter (stdlib only).

Deepseek decides from memory, which fails on recent releases it has never seen. TMDB
gives objective signals (release dates, popularity, votes) for a title. Nothing here
decides anything: `lookup` returns the facts, the caller applies the policy.

The credential is TMDB's v4 "API Read Access Token" (TMDB_READ_TOKEN in .env), sent as
a Bearer header so it never lands in a URL or an exception message. Without it every
function is inert (`token()` is empty and `lookup` is never called).
"""
import json
import re
import unicodedata
import urllib.parse
import urllib.request
from datetime import date, timedelta

import autorss_common as C

TMDB_URL = "https://api.themoviedb.org/3"
# Digital and physical release types in /movie/{id}/release_dates. These (not the
# primary date, which is often a festival premiere or the theatrical run) mark when a
# WEB-DL / BluRay can exist for nCore.
DIGITAL, PHYSICAL = 4, 5


def token():
    """TMDB read-access token from .env, or '' when not configured."""
    return C.env_value(C.PROJECT_ENV, "TMDB_READ_TOKEN") or ""


def norm(s):
    """Accent-, case- and punctuation-insensitive form, so an ASCII-folded nCore title
    ("Parhuzamos tortenetek") equals TMDB's "Párhuzamos történetek"."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", s.casefold())


def split_clean(clean):
    """('Name', 2026) from a clean_title "Name (2026)"; ('Name', None) when it has no year."""
    m = re.match(r"^(.*) \(((?:19|20)\d{2})\)$", clean)
    return (m.group(1), int(m.group(2))) if m else (clean, None)


def _year(release_date):
    try:
        return int((release_date or "")[:4])
    except ValueError:
        return None


def pick_match(results, name, year, today=None):
    """Best TMDB search result for a release, or None when nothing matches confidently.

    A result must carry the same normalised title (or original title). With a release
    year the TMDB year must be within 1 of it. Without one, a same-named film is only
    accepted if it is recent (this or last year, or announced), because a bare common
    title ("Momo") would otherwise match an old, popular namesake. Highest popularity
    wins among the survivors."""
    today = today or date.today()
    want = norm(name)
    if not want:
        return None
    best = None
    for r in results or []:
        if want not in (norm(r.get("title")), norm(r.get("original_title"))):
            continue
        y = _year(r.get("release_date"))
        if y is None:
            continue
        if year is not None:
            if abs(y - year) > 1:
                continue
        elif y < today.year - 1:
            continue
        if best is None or (r.get("popularity") or 0) > (best.get("popularity") or 0):
            best = r
    return best


def parse_available_date(release_dates):
    """Earliest digital/physical release date ('YYYY-MM-DD') of any country from a
    /movie/{id}/release_dates body, or None if it lists none."""
    dates = []
    for country in (release_dates or {}).get("results", []):
        for rel in country.get("release_dates", []):
            if rel.get("type") in (DIGITAL, PHYSICAL) and rel.get("release_date"):
                dates.append(rel["release_date"][:10])
    return min(dates) if dates else None


def qualifies(facts, today=None):
    """Why TMDB alone says this film is worth seeding, or None. A film qualifies when it
    became available (digital/physical, else its primary date) within the last 12 months,
    is not a documentary or TV movie, and is popular enough (TMDB_MIN_POPULARITY) with a
    minimum of votes so a one-vote popularity spike does not count.

    Calibrated on 35 days of verdicts: pop >= 30 adds ~6 films/week on top of Deepseek's
    ~2, near the July-August rate that filled the category. Lower admits too many."""
    if not facts:
        return None
    today = today or date.today()
    cutoff = (today - timedelta(days=365)).isoformat()
    when = facts.get("available_date") or facts.get("release_date") or ""
    if when < cutoff and (facts.get("release_date") or "") < cutoff:
        return None
    if {99, 10770} & set(facts.get("genre_ids") or []):
        return None
    if facts.get("popularity", 0) < C.TMDB_MIN_POPULARITY or facts.get("vote_count", 0) < C.TMDB_MIN_VOTES:
        return None
    return "TMDB: popularity %.0f, %d votes, available %s" % (
        facts["popularity"], facts["vote_count"], facts.get("available_date") or facts.get("release_date"))


def approval_reason(clean, tok, today=None):
    """lookup + qualifies in one call; raises like lookup on a TMDB failure."""
    return qualifies(lookup(clean, tok, today), today)


def _get(path, params, tok):
    url = TMDB_URL + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def lookup(clean, tok, today=None):
    """Facts about the film behind a clean_title, or None when TMDB has no confident
    match. Raises on a network / HTTP / parse failure -- the caller must treat that as
    "no information", never as "not found".

    Searches the English index first, then Hungarian (nCore often carries only the
    Hungarian title); the second pass is skipped when the first already matched."""
    name, year = split_clean(clean)
    match = None
    for lang in ("en-US", "hu-HU"):
        results = _get("/search/movie", {"query": name, "language": lang,
                                         "include_adult": "false"}, tok).get("results", [])
        match = pick_match(results, name, year, today)
        if match:
            break
    if not match:
        return None
    avail = parse_available_date(_get("/movie/%d/release_dates" % match["id"], {}, tok))
    return {
        "id": match["id"],
        "title": match.get("title"),
        "release_date": match.get("release_date"),
        "available_date": avail,
        "popularity": match.get("popularity") or 0.0,
        "vote_count": match.get("vote_count") or 0,
        "vote_average": match.get("vote_average") or 0.0,
        "language": match.get("original_language"),
        "genre_ids": match.get("genre_ids") or [],
    }
