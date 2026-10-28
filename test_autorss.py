#!/usr/bin/env python3
"""Unit tests for the ncore-autorss scripts  (stdlib unittest, no dependencies).

Run:  python3 test_autorss.py

Covers:
  - clean_title() year parsing, quality/separator-variant dedup, future-year guard,
    and the empty-key fallback.
  - the shared active-download predicate and that the gate and the metric use the
    SAME function.
  - seen_clean / decided status semantics: a quality 'skipped' or space-'removed'
    film no longer blocks a legitimate variant (findings #2/#10/#11).
  - flock_or_exit actually HOLDS the lock while the fd is referenced (finding #1).
  - mutate_state locked read-modify-write -> no lost updates (finding #4).
  - ensure_space deletes only completed 0-peer seeders, never an in-progress
    download or a torrent with peers (finding #3), and picks the SMALLEST SUFFICIENT
    victim rather than the one dead longest (replaying the three real 2026-08-02
    deletions, where the old rule freed up to 19x the actual overage).
  - torrent_size / category_used_bytes use committed size, not partial bytes (#5).
  - accumulate_uploaded monotonic + deletion-safe upload counter (dashboard panels).
  - env_value() exact-match preference, qb_add no-auto-stop flags, add-attempt cap.
  - films_markdown() rendering, seeding-stat columns, and pipe escaping.
  - dedup_key() case-insensitivity: an all-caps and a title-case release of the same
    film share one seen/decided entry instead of costing a redundant Deepseek call.
  - SYS_PROMPT policy: streaming-exclusive titles are not penalised vs theatrical,
    sports/non-film content stays excluded, and a title with no release year is
    judged from the model's own knowledge instead of defaulting to "old"; approval
    requires RECOGNISING the title (and naming what by), never inferring popularity
    from a recent release year.
  - ask_deepseek() surfaces the HTTP error body so a recurring failure is diagnosable.
  - decide-attempt cap: a title whose Deepseek call keeps failing is retried a bounded
    number of times then marked decide_failed, never looped on forever.
"""
import fcntl
import importlib.util
import inspect
import io
import json
import logging
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, "/home/fbazsa/ncore-autorss")
import autorss
import autorss_common as C

LOG = logging.getLogger("test_autorss")
LOG.addHandler(logging.NullHandler())
# keep app loggers quiet during tests (a NullHandler suppresses the last-resort stderr)
for _n in ("test_autorss", "autorss", "torrent-manager"):
    logging.getLogger(_n).addHandler(logging.NullHandler())


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


tm = _load("torrent_manager", "/home/fbazsa/ncore-autorss/torrent-manager.py")


class TestCleanTitle(unittest.TestCase):
    CASES = {
        "Michael.2026.720p.MA.WEBRip.DDP5.1.Atmos.x264.HUN": "Michael (2026)",
        "Se7en.1995.1080p.BluRay.x264 [Film (ENG HD)]": "Se7en (1995)",
        "Dune.Part.Two.2024.2160p.HDR-GROUP [Film (ENG HD)]": "Dune Part Two (2024)",
        "The.Web.2023.1080p.WEB-DL.DD5.1.H264 [Film (ENG HD)]": "The Web (2023)",
        "The_Matrix_1999_1080p_BluRay_x264 [Film (ENG HD)]": "The Matrix (1999)",
        "Joe.Strummer.A.jovo.nincs.megirva.2007.HUN.1080p.BluRay.x264-PLAN9 [Film (HUN HD)]":
            "Joe Strummer A jovo nincs megirva (2007)",
        "A.lako.1976.Carlotta.HUN.1080p.UHD.BluRay.DV.HDR.HEVC-PLAN9 [Film (HUN HD)]": "A lako (1976)",
        # regression: titles that contain / start with a year-like number
        "2012.2009.1080p.BluRay.x264-AMIABLE [Film (ENG HD)]": "2012 (2009)",
        "1917.2019.1080p.BluRay.x264-SPARKS [Film (ENG HD)]": "1917 (2019)",
        "2001.A.Space.Odyssey.1968.REMASTERED.1080p.BluRay [Film (ENG HD)]": "2001 A Space Odyssey (1968)",
        "Blade.Runner.2049.2017.2160p.UHD.BluRay.x265-GROUP [Film (ENG HD)]": "Blade Runner 2049 (2017)",
        "300.2006.1080p.BluRay.x264 [Film (ENG HD)]": "300 (2006)",
        "2046.2004.1080p.x264-GROUP [Film (ENG HD)]": "2046 (2004)",
        # human-readable, no release year
        "Billie Eilish - Kicsit homályos a világ [Film (ENG SD)]": "Billie Eilish - Kicsit homályos a világ",
        "1984 [Film (ENG SD)]": "1984",
    }

    def test_clean_title(self):
        for raw, expected in self.CASES.items():
            self.assertEqual(autorss.clean_title(raw), expected, msg=raw)


class TestCleanTitleVariants(unittest.TestCase):
    """The dedup key must collapse quality/separator variants of ONE film, bound the
    release year to plausible values, and never return an empty key."""

    def test_quality_variants_collapse_to_one_key(self):
        a = autorss.clean_title("The.Batman.2022.720p.BluRay.x264-GRP [Film (ENG HD)]")
        b = autorss.clean_title("The.Batman.2022.1080p.WEB-DL.x265-OTH [Film (ENG HD)]")
        self.assertEqual(a, b)
        self.assertEqual(a, "The Batman (2022)")

    def test_mixed_dot_space_separators_collapse(self):
        a = autorss.clean_title("The.Matrix Reloaded.2003.720p [Film (ENG HD)]")
        b = autorss.clean_title("The Matrix Reloaded 2003 720p [Film (ENG HD)]")
        self.assertEqual(a, b)
        self.assertEqual(a, "The Matrix Reloaded (2003)")

    def test_future_number_not_treated_as_release_year(self):
        # no real release year present -> 2049 is part of the title, not the year
        self.assertEqual(autorss.clean_title("Blade.Runner.2049.720p.BluRay [Film (ENG HD)]"),
                         "Blade Runner 2049")

    def test_empty_key_falls_back_to_raw(self):
        # title made entirely of quality tokens would clean to '' -> fall back, never empty
        out = autorss.clean_title("Web.720p.x264 [Film (ENG HD)]")
        self.assertTrue(out)
        self.assertEqual(out, "Web.720p.x264")


def _t(progress, state, seeds=0, leechs=0, time_active=60,
       num_complete=None, availability=None):
    t = {"progress": progress, "state": state, "num_seeds": seeds,
         "num_leechs": leechs, "time_active": time_active}
    # Set ONLY when explicitly given: an absent num_complete/availability is qB's
    # "unknown", which dead_swarm must read as alive. Defaulting them to 0 here would
    # silently mark every existing fixture as a dead swarm.
    if num_complete is not None:
        t["num_complete"] = num_complete
    if availability is not None:
        t["availability"] = availability
    return t


class TestActiveDownload(unittest.TestCase):
    def test_predicate(self):
        A = C.is_active_download
        self.assertFalse(A(_t(1.0, "uploading")), "complete = seeding, not active")
        self.assertTrue(A(_t(0.3, "downloading", seeds=2)), "downloading with peers = active")
        self.assertFalse(A(_t(0.3, "stoppedDL")), "stopped = not active")
        self.assertFalse(A(_t(0.3, "pausedDL")), "paused = not active")
        self.assertFalse(A(_t(0.3, "error")), "errored = not active")
        self.assertTrue(A(_t(0.0, "stalledDL", time_active=60)), "recently stalled still counts")
        self.assertFalse(A(_t(0.0, "stalledDL", time_active=7 * 3600)),
                         "peerless stall past timeout = dead, must not block the queue")
        self.assertTrue(A(_t(0.5, "stalledDL", seeds=1, time_active=99 * 3600)),
                        "has a seed -> still active even if old")

    def test_gate_and_metric_share_one_function(self):
        # the queue gate (autorss) and the dashboard metric (torrent-manager) are
        # literally the same shared function -- they cannot disagree.
        self.assertIs(autorss.is_active_download, tm.is_active_download)
        self.assertIs(autorss.is_active_download, C.is_active_download)


class TestDeadSwarm(unittest.TestCase):
    """A download whose swarm holds no complete copy can never finish, so past the stall
    timeout it must stop occupying a slot -- even though it has connected peers.

    Live regression: `Storm.on.Sesame.Street.2026` sat at progress=0 for 13 DAYS with
    num_leechs=2 (so peers() == 2 and the peerless rule never fired), num_complete=0 and
    availability=0. It held 1 of MAX_ACTIVE_DOWNLOADS the entire time and the `stalled`
    gauge read 0, so nothing surfaced it."""

    def test_unknown_swarm_is_never_dead(self):
        # qB reports -1 (or omits the field) before the tracker answers. Unknown must
        # read as alive -- guessing dead would abandon healthy just-added downloads.
        self.assertFalse(C.dead_swarm(_t(0.0, "downloading")))
        self.assertFalse(C.dead_swarm(_t(0.0, "downloading", num_complete=-1)))
        self.assertFalse(C.dead_swarm(_t(0.0, "downloading", num_complete=-1, availability=0)))

    def test_seeded_swarm_is_not_dead(self):
        self.assertFalse(C.dead_swarm(_t(0.5, "downloading", num_complete=3, availability=0.4)))

    def test_seederless_but_complete_between_peers_is_not_dead(self):
        # No single seeder, but the connected peers' pieces cover the whole file, so it
        # can still finish. Only the sub-1.0 case is hopeless.
        self.assertFalse(C.dead_swarm(_t(0.5, "downloading", num_complete=0, availability=1.0)))
        self.assertFalse(C.dead_swarm(_t(0.5, "downloading", num_complete=0, availability=2.7)))

    def test_no_complete_copy_anywhere_is_dead(self):
        self.assertTrue(C.dead_swarm(_t(0.0, "stalledDL", num_complete=0, availability=0)))
        self.assertTrue(C.dead_swarm(_t(0.3, "downloading", num_complete=0, availability=0.9)))

    def test_dead_swarm_frees_the_slot_only_after_the_timeout(self):
        A = C.is_active_download
        live = dict(progress=0.0, state="stalledDL", leechs=2, seeds=0,
                    num_complete=0, availability=0)
        self.assertTrue(A(_t(time_active=60, **live)),
                        "a fresh dead-swarm download is just a slow start, not death")
        self.assertTrue(A(_t(time_active=C.STALL_TIMEOUT_S, **live)),
                        "boundary is strictly greater-than, matching the peerless rule")
        self.assertFalse(A(_t(time_active=13 * 86400, **live)),
                         "the live 13-day case: connected leechers, but nothing to get")

    def test_peers_alone_no_longer_prove_liveness(self):
        # The exact gap the old predicate had: peers() == 2 short-circuited the only
        # stall rule, so this returned True forever.
        old_rule_would_pass = C.peers(_t(0.0, "stalledDL", leechs=2)) != 0
        self.assertTrue(old_rule_would_pass)
        self.assertFalse(C.is_active_download(
            _t(0.0, "stalledDL", leechs=2, time_active=13 * 86400,
               num_complete=0, availability=0)))

    def test_healthy_download_with_peers_stays_active_indefinitely(self):
        self.assertTrue(C.is_active_download(
            _t(0.5, "downloading", seeds=1, leechs=4, time_active=99 * 3600,
               num_complete=2, availability=1.8)))


class TestStalledMetricCountsDeadSwarm(unittest.TestCase):
    """The gauge must agree with the queue gate. Under the old peers-only test a
    dead-swarm stall that really was holding a slot still reported stalled=0."""

    def setUp(self):
        # compute_metrics reads state.json for the approved/rejected counts; keep the
        # test off the live file.
        self.real_load = C.load_json
        C.load_json = lambda path, default=None: {"films": {}}

    def tearDown(self):
        C.load_json = self.real_load

    def _metrics(self, torrents):
        return tm.compute_metrics(torrents, {})

    def test_dead_swarm_stall_is_counted(self):
        dead = _t(0.0, "stalledDL", leechs=2, time_active=13 * 86400,
                  num_complete=0, availability=0)
        self.assertEqual(self._metrics([dead])["stalled"], 1)

    def test_active_and_stalled_are_disjoint(self):
        dead = _t(0.0, "stalledDL", leechs=2, time_active=13 * 86400,
                  num_complete=0, availability=0)
        healthy = _t(0.4, "downloading", seeds=2, num_complete=5, availability=2.0)
        seeding = _t(1.0, "stalledUP", seeds=1)
        m = self._metrics([dead, healthy, seeding])
        self.assertEqual(m["active"], 1, "only the healthy download occupies a slot")
        self.assertEqual(m["stalled"], 1, "only the dead-swarm one is written off")
        self.assertEqual(m["seeding"], 1)

    def test_stopped_download_is_not_stalled(self):
        # Deliberately stopped is not the same as dead; it was never in the running.
        stopped = _t(0.2, "pausedDL", time_active=99 * 3600,
                     num_complete=0, availability=0)
        self.assertEqual(self._metrics([stopped])["stalled"], 0)


class TestRemuxFilter(unittest.TestCase):
    def test_skips_remux_and_full_disc(self):
        R = autorss.REMUX_RE
        for raw in [
            "Dune.Part.Two.2024.REMUX.1080p [Film (ENG HD)]",
            "Some.Movie.2025.BD25 [Film (ENG HD)]",
            "Some.Movie.2025.BD50 [Film (ENG HD)]",
            "Some.Movie.2025.BDMV [Film (ENG HD)]",
            "Some.Movie.2025.Disc.1 [Film (ENG HD)]",
            "Some.Movie.2025.FullBD [Film (ENG HD)]",
            "Some.Movie.2025.Complete.Bluray [Film (ENG HD)]",
        ]:
            self.assertTrue(R.search(raw), msg=raw)

    def test_keeps_normal_releases(self):
        R = autorss.REMUX_RE
        for raw in [
            "Some.Movie.2025.1080p.WEB-DL.x264 [Film (ENG HD)]",
            "Some.Movie.2025.2160p.BluRay.x265 [Film (ENG HD)]",  # BluRay alone is fine
            "Disclaimer.2024.1080p [Film (ENG HD)]",              # 'disc' not a whole word
        ]:
            self.assertFalse(R.search(raw), msg=raw)


class TestWantedQuality(unittest.TestCase):
    def test_720p_always_wanted(self):
        self.assertTrue(autorss.wanted_quality("A.Film.2026.720p.WEB-DL [Film (ENG HD)]", set()))

    def test_1080p_wanted_only_without_720p_sibling(self):
        t = "A.Film.2026.1080p.WEB-DL.x264-GRP [Film (ENG HD)]"
        key = autorss.dedup_key(autorss.clean_title(t))
        self.assertTrue(autorss.wanted_quality(t, set()))
        self.assertFalse(autorss.wanted_quality(t, {key}))

    def test_2160p_and_sd_never_wanted(self):
        for t in ["A.Film.2026.2160p.WEB-DL [Film (ENG HD)]",
                  "A.Film.2026.WEB-DL.H.264 [Film (HUN SD)]"]:
            self.assertFalse(autorss.wanted_quality(t, set()), msg=t)


class TestSysPromptPolicy(unittest.TestCase):
    """The approval prompt must not require a theatrical release -- a streaming-
    exclusive title (e.g. a Netflix/Amazon original) can be just as mainstream and
    high-demand -- but sports broadcasts and other non-film content must stay
    excluded, and a title with no release year must be judged from the model's own
    knowledge rather than defaulting to "old" (root cause of the Night Patrol
    rejected-then-approved inconsistency).

    The popularity half is RECOGNITION-gated: dropping the theatrical requirement left
    the model inferring "highly anticipated" from the release year alone, which approved
    a literal placeholder ("Untitled Home Invasion Romance (2025)") and an obscure
    import ("Wu ming zhi bei Pi ji tai lai (2025)") and drove approvals to 87% of
    recent-year titles."""

    def test_theatrical_release_not_required(self):
        p = autorss.SYS_PROMPT.lower()
        self.assertNotIn("had a theatrical cinema release", p)
        self.assertNotIn("direct-to-streaming", p)
        self.assertIn("distribution channel does not", p)
        self.assertIn("streaming-exclusive", p)

    def test_sports_and_non_film_content_still_excluded(self):
        p = autorss.SYS_PROMPT.lower()
        for term in ("sports recordings", "match broadcasts", "documentaries",
                     "stand-up specials", "made-for-tv movies", "awards shows"):
            self.assertIn(term, p, msg=term)

    def test_missing_year_guidance_present(self):
        p = autorss.SYS_PROMPT.lower()
        self.assertIn("no release year", p)
        self.assertIn("never assume it is", p)

    def test_json_response_format_instruction_intact(self):
        # the {{"download": true, ...}} example must survive the .format() call unescaped
        self.assertIn('"download": true', autorss.SYS_PROMPT)

    def test_popularity_requires_recognition_not_inference(self):
        p = autorss.SYS_PROMPT.lower()
        self.assertIn("recognition, never inference", p)
        self.assertIn("says nothing about popularity", p)
        self.assertIn("inferring from the title", p)

    def test_approval_reason_must_name_the_recognised_fact(self):
        """Self-enforcing: an approval has to cite cast/director/studio/franchise, so a
        content-free "recognised mainstream film with buzz" verdict is a rejection."""
        p = autorss.SYS_PROMPT.lower()
        self.assertIn("the reason must name what you recognise the film by", p)
        self.assertIn("never answer true with a reason that hedges", p)

    def test_placeholder_titles_are_never_anticipated(self):
        self.assertIn("'Untitled ...' are placeholders", autorss.SYS_PROMPT)

    def test_recency_and_streaming_inclusiveness_survive_the_tightening(self):
        # the tightening is on the popularity half ONLY -- recency, channel-neutrality
        # and eligibility of titles postdating the model's knowledge must stay intact
        p = autorss.SYS_PROMPT.lower()
        self.assertIn("within the last 12 months", p)
        self.assertIn("streaming-exclusive", p)
        self.assertIn("announced or upcoming project", p)


class TestAskDeepseekErrorDiagnosable(unittest.TestCase):
    """A DeepSeek HTTP error must surface the response BODY (the API's actual error
    detail), not just the bare status line -- otherwise a recurring failure is
    undiagnosable from the log (all that was ever recorded was
    "HTTP Error 400: Bad Request", with no indication of why)."""

    def test_http_error_body_included_in_message(self):
        err_body = b'{"error": {"message": "something specific broke"}}'

        def fake_urlopen(req, timeout=60):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(err_body))

        real = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(RuntimeError) as ctx:
                autorss.ask_deepseek("Some Title (2026)", "fake-key")
            self.assertIn("something specific broke", str(ctx.exception))
            self.assertIn("400", str(ctx.exception))
        finally:
            urllib.request.urlopen = real


class TestDecideAttemptCap(unittest.TestCase):
    """A title whose Deepseek call keeps failing is retried a bounded number of times
    (status=decide_pending, with the failure detail kept as `reason` so it's
    diagnosable) then given up on (status=decide_failed) -- never looped on forever."""

    def test_gives_up_after_max_attempts_and_stays_diagnosable(self):
        s = {"films": {}}
        for i in range(1, C.MAX_DECIDE_ATTEMPTS):
            autorss._mark_decide_attempt(s, "x", "Some.Title.2026 [Film (ENG HD)]",
                                          "Some Title (2026)", "http://link", "HTTP 400: boom")
            self.assertEqual(s["films"]["x"]["decide_attempts"], i)
            self.assertEqual(s["films"]["x"]["status"], "decide_pending")
            self.assertIn("boom", s["films"]["x"]["reason"])
            self.assertIsNone(s["films"]["x"]["decision"])
        autorss._mark_decide_attempt(s, "x", "Some.Title.2026 [Film (ENG HD)]",
                                      "Some Title (2026)", "http://link", "HTTP 400: boom")
        self.assertEqual(s["films"]["x"]["status"], "decide_failed")
        self.assertIsNone(s["films"]["x"]["decision"])

    def test_decide_pending_and_failed_excluded_from_seen_and_decided(self):
        # neither status is a real verdict -> must not block other releases of the
        # same film (SEEN_STATUSES) nor be reused as a cached decision (DECIDED_STATUSES)
        films = {
            "a": {"clean": "Some Title (2026)", "status": "decide_pending", "decision": None},
            "b": {"clean": "Other Title (2026)", "status": "decide_failed", "decision": None},
        }
        self.assertEqual(autorss.seen_clean_titles(films), set())
        decided = {
            autorss.dedup_key(v["clean"]): v["decision"]
            for v in films.values()
            if v.get("clean") and "decision" in v and v.get("status") in autorss.DECIDED_STATUSES
        }
        self.assertEqual(decided, {})


class TestSeenAndDecidedStatuses(unittest.TestCase):
    """Findings #2/#10/#11: a per-release quality 'skipped' or a space-freed 'removed'
    film must NOT block a legitimate variant; only a real Deepseek verdict
    (pending/downloaded/rejected) does. Decisions are reused for everything except
    quality skips (which never asked Deepseek)."""

    FILMS = {
        "111": {"clean": "Frozen (2013)", "status": "removed", "decision": True, "reason": "hit"},
        "222": {"clean": "Dune Part Two (2024)", "status": "rejected", "decision": False, "reason": "old"},
        "333": {"clean": "Michael (2026)", "status": "downloaded", "decision": True, "reason": "hit"},
        "444": {"clean": "Wicked (2024)", "status": "skipped", "decision": False, "reason": "720p only"},
        "555": {"clean": "Heretic (2024)", "status": "pending", "decision": True, "reason": "hit"},
        "666": {"status": "pending"},  # no clean -> must not add None / crash
    }

    def test_seen_excludes_skipped_and_removed(self):
        seen = autorss.seen_clean_titles(self.FILMS)
        self.assertEqual(seen, {autorss.dedup_key("Dune Part Two (2024)"),
                                 autorss.dedup_key("Michael (2026)"),
                                 autorss.dedup_key("Heretic (2024)")})
        # a freed ('removed') film's new release is NOT blocked -> can be re-acquired
        self.assertNotIn(autorss.dedup_key(autorss.clean_title(
            "Frozen.2013.720p.WEB-DL.x264-OTHER [Film (ENG HD)]")), seen)
        # a quality-'skipped' film's 720p variant is NOT blocked -> gets reconsidered
        self.assertNotIn(autorss.dedup_key(autorss.clean_title(
            "Wicked.2024.720p.BluRay-XX [Film (ENG HD)]")), seen)
        # a rejected/downloaded film's other release IS blocked
        self.assertIn(autorss.dedup_key(autorss.clean_title(
            "Dune.Part.Two.2024.720p.BluRay-XX [Film (ENG HD)]")), seen)
        # an ALL-CAPS scene release of an already-blocked film is ALSO blocked
        self.assertIn(autorss.dedup_key(autorss.clean_title(
            "DUNE.PART.TWO.2024.720P.BLURAY-XX [Film (ENG HD)]")), seen)

    def test_decided_cache_excludes_skipped_only(self):
        decided = {
            autorss.dedup_key(v["clean"]): (v["decision"], v.get("reason", ""))
            for v in self.FILMS.values()
            if v.get("clean") and "decision" in v and v.get("status") in autorss.DECIDED_STATUSES
        }
        self.assertIn(autorss.dedup_key("Dune Part Two (2024)"), decided)   # rejected verdict reusable
        self.assertIn(autorss.dedup_key("Frozen (2013)"), decided)         # removed -> reuse True on re-acquire
        self.assertNotIn(autorss.dedup_key("Wicked (2024)"), decided)      # skipped never asked Deepseek
        self.assertEqual(decided[autorss.dedup_key("Dune Part Two (2024)")], (False, "old"))
        # cross-case lookup hits the same cached verdict (no redundant Deepseek call)
        self.assertEqual(decided[autorss.dedup_key("DUNE PART TWO (2024)")], (False, "old"))


class TestDedupKeyCaseInsensitive(unittest.TestCase):
    """An all-caps scene release and a title-case release of the SAME film must share
    one dedup/cache entry. Before this fix, 'POWER.BALLAD.2026...' (all-caps) and
    'Power.Ballad.2026...' (title-case) produced different clean_title() strings and
    so were treated as two unrelated films -- a redundant Deepseek call at best, two
    independently-diverging verdicts at worst."""

    def test_case_insensitive_match(self):
        a = autorss.clean_title("Power.Ballad.2026.1080p.WEB.h264-ETHEL [Film (ENG HD)]")
        b = autorss.clean_title("POWER.BALLAD.2026.720p.AMZN.WEB-DL.DDP5.1.Atmos.H.264-BYNDR [Film (ENG HD)]")
        self.assertNotEqual(a, b)                          # display casing legitimately differs
        self.assertEqual(autorss.dedup_key(a), autorss.dedup_key(b))  # dedup key matches

    def test_accented_titles_casefold(self):
        # Hungarian accented characters must fold consistently too
        self.assertEqual(autorss.dedup_key("Féltékenység"), autorss.dedup_key("FÉLTÉKENYSÉG"))


class TestFlockHeld(unittest.TestCase):
    """Finding #1: the lock must actually be held while the returned fd is referenced
    (the bug was callers discarding it, letting the GC drop the lock immediately)."""

    def test_lock_is_held_until_closed(self):
        path = tempfile.mktemp(suffix=".lock")
        try:
            held = C.flock_or_exit(path, LOG)  # keep the reference -> lock held
            other = open(path, "w")
            with self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.close()  # release
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # now succeeds
            other.close()
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestMutateState(unittest.TestCase):
    """Finding #4: locked read-modify-write merges into the latest on-disk state, so a
    later writer can't clobber an earlier one's field."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._sf, self._lf = C.STATE_FILE, C.STATE_LOCK_FILE
        C.STATE_FILE = os.path.join(self.dir, "state.json")
        C.STATE_LOCK_FILE = os.path.join(self.dir, ".state.lock")

    def tearDown(self):
        C.STATE_FILE, C.STATE_LOCK_FILE = self._sf, self._lf

    def test_no_lost_update(self):
        st = C.mutate_state(lambda s: s["films"].update({"A": {"status": "downloaded", "hash": "h"}}))
        self.assertIn("films", st)
        self.assertIn("_meta", st)
        # a separate writer flips A -> removed (e.g. the manager's reconcile)
        C.mutate_state(lambda s: s["films"]["A"].update({"status": "removed"}))
        # then autorss adds B; the removed flag must survive (no clobber)
        st = C.mutate_state(lambda s: s["films"].update({"B": {"status": "pending"}}))
        self.assertEqual(st["films"]["A"]["status"], "removed")
        self.assertIn("B", st["films"])
        # and it is what's actually on disk
        disk = C.load_json(C.STATE_FILE, {})
        self.assertEqual(disk["films"]["A"]["status"], "removed")
        self.assertIn("B", disk["films"])


def _seed(h, size, seeds=0, progress=1.0, uploaded=0):
    return {"hash": h, "name": h, "size": size,
            "completed": size if progress >= 1.0 else int(size * progress),
            "progress": progress, "num_seeds": seeds, "num_leechs": 0, "uploaded": uploaded}


class TestEnsureSpaceSafety(unittest.TestCase):
    """Finding #3: ensure_space deletes ONLY completed 0-peer seeders -- never an
    in-progress download, never a torrent with peers."""

    def setUp(self):
        self.real_delete = tm.delete_torrent
        self.deleted = []
        tm.delete_torrent = lambda t, st: (self.deleted.append(t["hash"]) or True)

    def tearDown(self):
        tm.delete_torrent = self.real_delete

    def test_only_completed_zero_peer_seeders_deleted(self):
        gb = 10 ** 9
        torrents = [
            _seed("dead", 100 * gb),                 # completed, 0 peers -> deletable
            _seed("live", 100 * gb, seeds=5),        # has peers -> protected
            _seed("dl", 100 * gb, progress=0.4),     # in-progress -> protected
        ]
        st = {"zero_since": {}}
        tm.ensure_space(150.0, torrents, st)   # used=300GB committed, over=150GB
        self.assertEqual(self.deleted, ["dead"])

    def test_under_cap_deletes_nothing(self):
        st = {"zero_since": {}}
        tm.ensure_space(500.0, [_seed("dead", 100 * 10 ** 9)], st)
        self.assertEqual(self.deleted, [])


class TestEnsureSpaceReachableOnQuietRuns(unittest.TestCase):
    """The hard-cap trim has to be reachable on EVERY cycle, not only on one that queues
    a download.

    It used to sit below the `if not pending: return` early exit, which made
    CATEGORY_HARD_GB enforceable only as a side effect of adding. Live, that parked the
    category at 483.3 GB against the 480 GB cap for 26 h: a completed download pushed it
    over, approvals then went quiet, and every subsequent run returned at "no pending
    downloads" without ever reaching the trim.

    Ordering is the whole fix, so it is pinned structurally -- exercising it behaviourally
    would mean standing up fakes for RSS, Deepseek, qB and subprocess just to observe the
    order of two statements."""

    def _src(self):
        return inspect.getsource(autorss._run)

    def test_trim_precedes_the_quiet_run_early_return(self):
        src = self._src()
        self.assertLess(
            src.index('"ensure-space"'), src.index('"no pending downloads"'),
            "ensure-space slipped back below the quiet-run early return; the hard cap "
            "is only enforced on runs that queue a download again")

    def test_trim_still_precedes_the_add_path(self):
        # The other half of the invariant: space must be freed BEFORE anything is queued,
        # so the trim also has to stay above the soft-cap gate and the add loop.
        src = self._src()
        self.assertLess(src.index('"ensure-space"'), src.index("CATEGORY_CAP_GB"))

    def test_trim_targets_the_hard_cap(self):
        src = self._src()
        trim = src.index('"ensure-space"')
        self.assertIn("CATEGORY_HARD_GB", src[trim:trim + 200],
                      "the trim must be invoked with the hard cap, not the soft one")


# Candidate sizes (GB) sampled from the live auto-film category on 2026-08-04.
POOL_GB = [1.07, 1.50, 1.61, 1.80, 3.00, 3.32, 3.82, 6.89, 7.51, 8.45, 24.95]
# The three real deletions of 2026-08-02: (name, overage GB, GB the OLD rule actually freed).
HISTORICAL = [
    ("Billy.Idol.Should.Be.Dead", 2.9, 6.89),
    ("Night.Patrol", 1.5, 7.51),
    ("Silent.Rebellion", 0.2, 3.82),
]


class TestEnsureSpaceSmallestSufficient(unittest.TestCase):
    """Victim selection is SMALLEST SUFFICIENT, not longest-at-0-peers.

    The old rule deleted whichever dead seeder had been at 0 peers longest -- a quantity
    uncorrelated with the overage -- so it routinely freed far more than needed (live, a
    0.2 GB overage cost a 3.82 GB seeder). Now the smallest single candidate that covers
    the overage is taken; only when no single one covers it are candidates combined
    smallest-first."""

    def setUp(self):
        self.real_delete = tm.delete_torrent
        self.deleted = []
        self.fail_hashes = set()

        def fake(t, st):
            if t["hash"] in self.fail_hashes:
                return False
            self.deleted.append(t["hash"])
            return True

        tm.delete_torrent = fake

    def tearDown(self):
        tm.delete_torrent = self.real_delete

    @staticmethod
    def _pool(sizes_gb):
        return [_seed("t%02d" % i, int(gb * 10 ** 9)) for i, gb in enumerate(sizes_gb)]

    @staticmethod
    def _cap_for(torrents, over_gb):
        """Cap that puts the category exactly `over_gb` over. Returns (cap, real over),
        where the real over is recomputed the way ensure_space does so the expectations
        stay exact despite the GB<->byte float roundtrip."""
        used = C.category_used_bytes(torrents)
        cap_gb = (used - over_gb * 1e9) / 1e9
        return cap_gb, used - int(cap_gb * 1e9)

    def _freed(self, torrents):
        by_hash = {t["hash"]: t for t in torrents}
        return sum(tm.disk_bytes(by_hash[h]) for h in self.deleted)

    def test_picks_smallest_single_candidate_that_covers_the_overage(self):
        torrents = self._pool(POOL_GB)
        cap, over = self._cap_for(torrents, 2.9)
        tm.ensure_space(cap, torrents, {"zero_since": {}})
        expected = min((t for t in torrents if tm.disk_bytes(t) >= over), key=tm.disk_bytes)
        self.assertEqual(self.deleted, [expected["hash"]])
        self.assertEqual(tm.disk_bytes(expected), 3 * 10 ** 9)  # 3.00 GB, not the 24.95 GB one

    def test_historical_deletions_would_have_been_proportionate(self):
        """Replay of the three 2026-08-02 events: one torrent each, still covering the
        overage, and strictly cheaper than what the old rule actually deleted."""
        for name, over_gb, old_freed_gb in HISTORICAL:
            with self.subTest(name):
                self.deleted = []
                torrents = self._pool(POOL_GB)
                cap, over = self._cap_for(torrents, over_gb)
                tm.ensure_space(cap, torrents, {"zero_since": {}})
                freed = self._freed(torrents)
                self.assertEqual(len(self.deleted), 1, "one victim, not a cascade")
                self.assertGreaterEqual(freed, over, "still gets back under the cap")
                self.assertLess(freed, old_freed_gb * 1e9, "cheaper than the old rule")

    def test_combines_smallest_first_when_no_single_candidate_covers(self):
        torrents = self._pool([1.0, 2.0, 3.0])
        cap, over = self._cap_for(torrents, 4.5)  # bigger than any one candidate
        tm.ensure_space(cap, torrents, {"zero_since": {}})
        self.assertEqual(self.deleted, ["t00", "t01", "t02"])
        self.assertGreaterEqual(self._freed(torrents), over)

    def test_equal_sizes_tie_broken_by_longest_at_zero_peers(self):
        """The replaced `-disk_bytes` tie-break was unreachable (zero_since floats never
        tie). Byte sizes DO tie exactly, so time-at-0-peers now works as the tie-break."""
        torrents = self._pool([5.0, 5.0, 5.0])
        cap, _ = self._cap_for(torrents, 1.0)
        st = {"zero_since": {"t00": 3000.0, "t01": 1000.0, "t02": 2000.0}}
        tm.ensure_space(cap, torrents, st)
        self.assertEqual(self.deleted, ["t01"])  # dead longest among the equal-size three

    def test_failed_delete_falls_through_to_the_next_candidate(self):
        torrents = self._pool(POOL_GB)
        cap, over = self._cap_for(torrents, 2.9)
        self.fail_hashes = {"t04"}  # the 3.00 GB smallest-sufficient pick fails
        tm.ensure_space(cap, torrents, {"zero_since": {}})
        self.assertTrue(self.deleted, "a failed delete must not abort the trim")
        self.assertGreaterEqual(self._freed(torrents), over)

    def test_never_selects_a_torrent_with_peers_or_an_incomplete_download(self):
        torrents = self._pool([9.0]) + [
            _seed("tiny_live", 1 * 10 ** 9, seeds=3),        # smallest, but has peers
            _seed("tiny_dl", 1 * 10 ** 9, progress=0.5),     # smallest, but downloading
        ]
        cap, _ = self._cap_for(torrents, 0.5)
        tm.ensure_space(cap, torrents, {"zero_since": {}})
        self.assertEqual(self.deleted, ["t00"])


class TestSelectVictimsMatchesRealRun(unittest.TestCase):
    """`dryrun-ensure-space.py` promises to show what a real trim WOULD delete, and it is
    trusted before an irreversible delete -- so the plan it prints must not be able to
    drift from what ensure_space actually does. Both call select_victims; these tests pin
    that they agree, which is what makes the standalone script safe to keep."""

    def setUp(self):
        self.real_delete = tm.delete_torrent
        self.deleted = []
        tm.delete_torrent = lambda t, st: (self.deleted.append(t["hash"]), True)[1]

    def tearDown(self):
        tm.delete_torrent = self.real_delete

    @staticmethod
    def _plan(torrents, over, zs):
        """The dry-run's own replay of ensure_space's stop condition: delete down the
        victim list only until the overage is covered."""
        victims, _ = tm.select_victims(torrents, over, zs, 0.0)
        plan, freed = [], 0
        for t in victims:
            if freed >= over:
                break
            plan.append(t["hash"])
            freed += tm.disk_bytes(t)
        return plan

    def _assert_agrees(self, torrents, over_gb, zs=None):
        zs = zs or {}
        used = C.category_used_bytes(torrents)
        cap_gb = (used - over_gb * 1e9) / 1e9
        over = used - int(cap_gb * 1e9)
        predicted = self._plan(torrents, over, zs)
        tm.ensure_space(cap_gb, torrents, {"zero_since": dict(zs)})
        self.assertEqual(predicted, self.deleted)

    def test_agrees_when_one_candidate_covers_the_overage(self):
        self._assert_agrees([_seed("t%d" % i, int(gb * 10 ** 9))
                             for i, gb in enumerate(POOL_GB)], 2.9)

    def test_agrees_when_candidates_must_be_combined(self):
        self._assert_agrees([_seed("t%d" % i, int(gb * 10 ** 9))
                             for i, gb in enumerate([1.0, 2.0, 3.0])], 4.5)

    def test_agrees_on_the_equal_size_tie_break(self):
        self._assert_agrees([_seed("t%d" % i, 5 * 10 ** 9) for i in range(3)], 1.0,
                            zs={"t0": 3000.0, "t1": 1000.0, "t2": 2000.0})

    def test_plan_excludes_peered_and_incomplete_torrents(self):
        torrents = [_seed("dead", 9 * 10 ** 9),
                    _seed("live", 1 * 10 ** 9, seeds=3),
                    _seed("dl", 1 * 10 ** 9, progress=0.5)]
        victims, _ = tm.select_victims(torrents, 1, {}, 0.0)
        self.assertEqual([t["hash"] for t in victims], ["dead"])

    def test_single_is_none_only_when_no_candidate_covers_alone(self):
        torrents = [_seed("t%d" % i, int(gb * 10 ** 9)) for i, gb in enumerate([1.0, 2.0])]
        self.assertIsNone(tm.select_victims(torrents, 5 * 10 ** 9, {}, 0.0)[1])
        self.assertIsNotNone(tm.select_victims(torrents, 2 * 10 ** 9, {}, 0.0)[1])


class TestTorrentSize(unittest.TestCase):
    """Finding #5: the category cap measures committed (full) size, not partial bytes,
    so in-flight downloads are reserved at their final footprint."""

    def test_uses_full_size(self):
        self.assertEqual(C.torrent_size({"size": 10, "completed": 3}), 10)
        self.assertEqual(C.torrent_size({"completed": 7}), 7)        # no size -> completed
        self.assertEqual(C.category_used_bytes([{"size": 10, "completed": 3},
                                                {"size": 5, "completed": 5}]), 15)
        self.assertEqual(C.category_used_bytes([]), 0)
        self.assertEqual(C.category_used_bytes(None), 0)


class TestAccumulateUploaded(unittest.TestCase):
    """The exported upload counter must be monotonic: immune to torrent deletion and
    per-torrent counter resets, so Prometheus increase() (the daily/weekly panels) is
    correct."""

    def test_monotonic_through_growth_deletion_and_reset(self):
        st = {}
        # initial: bank both torrents' current uploaded
        self.assertEqual(tm.accumulate_uploaded(
            [{"hash": "a", "uploaded": 100}, {"hash": "b", "uploaded": 50}], st), 150)
        # 'a' grows by 30, 'b' unchanged
        self.assertEqual(tm.accumulate_uploaded(
            [{"hash": "a", "uploaded": 130}, {"hash": "b", "uploaded": 50}], st), 180)
        # 'b' deleted: total must NOT drop (its growth stays banked); 'b' is pruned
        self.assertEqual(tm.accumulate_uploaded([{"hash": "a", "uploaded": 130}], st), 180)
        self.assertNotIn("b", st["up_seen"])
        # 'a' counter resets (cur < prev) -> count the new accumulation only
        self.assertEqual(tm.accumulate_uploaded([{"hash": "a", "uploaded": 20}], st), 200)

    def test_uploaded_and_downloaded_track_independently(self):
        st = {}
        torrents = [{"hash": "a", "uploaded": 100, "downloaded": 40}]
        self.assertEqual(tm.accumulate_uploaded(torrents, st), 100)
        self.assertEqual(tm.accumulate_downloaded(torrents, st), 40)
        self.assertEqual(st["uploaded_total"], 100)
        self.assertEqual(st["downloaded_total"], 40)
        self.assertIn("up_seen", st)
        self.assertIn("down_seen", st)


class TestEnvValue(unittest.TestCase):
    def test_parsing(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write('# comment\nNCORE_RSS_KEY="abc123"\nGRAFANA_USER=admin\nEMPTY=\n')
            path = f.name
        try:
            self.assertEqual(C.env_value(path, "ncore"), "abc123")   # case-insensitive + dequoted
            self.assertEqual(C.env_value(path, "GRAFANA_USER"), "admin")
            self.assertIsNone(C.env_value(path, "EMPTY"))            # blank value -> None
            self.assertIsNone(C.env_value(path, "NOPE"))
            self.assertIsNone(C.env_value("/no/such/file.env", "x"))
        finally:
            os.unlink(path)

    def test_exact_match_preferred_over_substring(self):
        # finding #25: a superset-named var must not shadow the exact one
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("DEEPSEEK_BACKUP=wrongkey\nDEEPSEEK=rightkey\nNCORE_RSS_KEY=pk\n")
            path = f.name
        try:
            self.assertEqual(C.env_value(path, "DEEPSEEK"), "rightkey")  # exact wins
            self.assertEqual(C.env_value(path, "NCORE"), "pk")          # substring fallback intact
        finally:
            os.unlink(path)


class TestQbAddNoAutostop(unittest.TestCase):
    """The add request must pin ratioLimit/seedingTimeLimit to -1 so a torrent never
    auto-stops seeding (which would stop counting toward the seeding score)."""

    def test_add_sends_no_limit_flags(self):
        captured = {}
        real = C.docker_curl
        C.docker_curl = lambda args, timeout=40: (captured.__setitem__("args", args), (0, "Ok.\n200", ""))[1]
        try:
            accepted, _ = autorss.qb_add("http://example/t.torrent")
        finally:
            C.docker_curl = real
        self.assertTrue(accepted)
        self.assertIn("ratioLimit=-1", captured["args"])
        self.assertIn("seedingTimeLimit=-1", captured["args"])

    def test_202_async_add_is_accepted(self):
        """Live regression: qB answered 202 {"pending_count":1} and the torrent WAS added."""
        real = C.docker_curl
        body = '{"added_torrent_ids":[],"failure_count":0,"pending_count":1,"success_count":0}'
        C.docker_curl = lambda args, timeout=40: (0, body + "\n202", "")
        try:
            accepted, _ = autorss.qb_add("http://example/t.torrent")
        finally:
            C.docker_curl = real
        self.assertTrue(accepted)


class TestAddAttemptCap(unittest.TestCase):
    """Findings #9/#12: an unconfirmable add is retried a bounded number of times, then
    marked add_failed so a dead/duplicate URL isn't retried forever."""

    def test_gives_up_after_max_attempts(self):
        s = {"films": {"x": {"status": "pending"}}}
        for i in range(1, C.MAX_ADD_ATTEMPTS):
            autorss._mark_add_attempt(s, "x")
            self.assertEqual(s["films"]["x"]["add_attempts"], i)
            self.assertEqual(s["films"]["x"]["status"], "pending")
        autorss._mark_add_attempt(s, "x")  # the MAX_ADD_ATTEMPTS-th
        self.assertEqual(s["films"]["x"]["status"], "add_failed")
        autorss._mark_add_attempt(s, "missing")  # unknown id -> no crash

    def test_mark_downloaded(self):
        s = {"films": {"x": {"status": "pending"}}}
        autorss._mark_downloaded(s, "x", "deadbeef", "2026-06-24 12:00:00")
        self.assertEqual(s["films"]["x"]["status"], "downloaded")
        self.assertEqual(s["films"]["x"]["hash"], "deadbeef")


class TestFilmsMarkdown(unittest.TestCase):
    def test_render_and_escape(self):
        rows = [{"title": "Dune | Part Two (2024)", "status": "downloaded",
                 "reason": "Blockbuster", "epoch": 1718740800, "hash": "abc"}]
        stats = {"abc": {"seed_days": 3.5, "up_gb": 12.0, "ratio": 2.0}}
        md = tm.films_markdown(rows, stats)
        self.assertIn("| Title | Status | Reason | Added | Seeding (days) | Uploaded (GB) | Ratio |", md)
        self.assertIn("Dune \\| Part Two (2024)", md)   # pipe escaped so the table isn't broken
        self.assertIn("downloaded", md)
        self.assertIn("3.5", md)        # seeding-stat columns rendered
        self.assertIn("12.00", md)
        self.assertIn("2.00", md)
        self.assertIn("_No approved films._", tm.films_markdown([]))

    def test_blank_stats_when_no_live_torrent(self):
        rows = [{"title": "X", "status": "approved", "reason": "r", "epoch": 0, "hash": "none"}]
        md = tm.films_markdown(rows, {})   # no stats for this hash -> blank cells
        self.assertIn("| X | approved | r |", md)


class TestRemoveUnregistered(unittest.TestCase):
    """A torrent the tracker calls "unregistered" earns no ERTEK, so monitor removes it --
    but only after UNREGISTERED_GRACE_S, only when it is a completed 0-peer seeder, and a
    recovered or unreadable tracker never counts toward the grace."""

    def setUp(self):
        self.real_delete = tm.delete_torrent
        self.deleted = []
        tm.delete_torrent = lambda t, st: (self.deleted.append(t["hash"]) or True)
        self.t0 = 1_000_000.0

    def tearDown(self):
        tm.delete_torrent = self.real_delete

    def _timers(self, verdicts, torrents, st):
        tm.update_unregistered_timers(torrents, st, check=lambda h: verdicts[h])

    def test_not_deleted_before_grace(self):
        st = {"unregistered_since": {"a": self.t0}}
        n = tm.remove_unregistered([_seed("a", 10 ** 9)], st, now=self.t0 + C.UNREGISTERED_GRACE_S - 1)
        self.assertEqual((n, self.deleted), (0, []))

    def test_deleted_after_grace(self):
        st = {"unregistered_since": {"a": self.t0}}
        n = tm.remove_unregistered([_seed("a", 10 ** 9)], st, now=self.t0 + C.UNREGISTERED_GRACE_S)
        self.assertEqual((n, self.deleted), (1, ["a"]))
        self.assertNotIn("a", st["unregistered_since"])

    def test_torrent_with_peers_or_still_downloading_is_protected(self):
        st = {"unregistered_since": {"peers": self.t0, "dl": self.t0}}
        torrents = [_seed("peers", 10 ** 9, seeds=1), _seed("dl", 10 ** 9, progress=0.4)]
        n = tm.remove_unregistered(torrents, st, now=self.t0 + 10 * C.UNREGISTERED_GRACE_S)
        self.assertEqual((n, self.deleted), (0, []))

    def test_timer_starts_once_and_is_not_reset_while_still_unregistered(self):
        st = {}
        self._timers({"a": True}, [_seed("a", 10 ** 9)], st)
        first = st["unregistered_since"]["a"]
        self._timers({"a": True}, [_seed("a", 10 ** 9)], st)
        self.assertEqual(st["unregistered_since"]["a"], first)

    def test_recovered_tracker_clears_timer(self):
        st = {"unregistered_since": {"a": self.t0}}
        self._timers({"a": False}, [_seed("a", 10 ** 9)], st)
        self.assertNotIn("a", st["unregistered_since"])

    def test_failed_lookup_changes_nothing(self):
        st = {"unregistered_since": {"a": self.t0}}
        self._timers({"a": None, "b": None}, [_seed("a", 10 ** 9), _seed("b", 10 ** 9)], st)
        self.assertEqual(st["unregistered_since"], {"a": self.t0})

    def test_departed_and_incomplete_torrents_are_dropped_from_timers(self):
        st = {"unregistered_since": {"gone": self.t0, "dl": self.t0}}
        self._timers({"dl": True}, [_seed("dl", 10 ** 9, progress=0.4)], st)
        self.assertEqual(st["unregistered_since"], {})

    def test_tracker_unregistered_reads_the_message(self):
        real = tm.docker_curl
        try:
            tm.docker_curl = lambda a: (0, json.dumps(
                [{"url": "** [DHT] **", "msg": ""}, {"url": "http://t", "msg": "err: Unregistered torrent"}]), "")
            self.assertTrue(tm.tracker_unregistered("a"))
            tm.docker_curl = lambda a: (0, json.dumps([{"url": "http://t", "msg": ""}]), "")
            self.assertFalse(tm.tracker_unregistered("a"))
            tm.docker_curl = lambda a: (1, "", "boom")
            self.assertIsNone(tm.tracker_unregistered("a"))
            tm.docker_curl = lambda a: (0, "not json", "")
            self.assertIsNone(tm.tracker_unregistered("a"))
        finally:
            tm.docker_curl = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
