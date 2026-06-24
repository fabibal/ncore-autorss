#!/usr/bin/env python3
"""Unit tests for the ncore-autorss scripts  (stdlib unittest, no dependencies).

Run:  python3 test_autorss.py

Covers:
  - clean_title() year parsing, incl. the regression for year-titled films.
  - the shared active-download predicate (incl. the dead-stall case that used to
    freeze the queue) and that the gate and the metric use the SAME function.
  - env_value() parsing (.env loader for the passkey / Grafana creds).
  - films_markdown() rendering + pipe escaping (the Grafana text-panel content).
"""
import importlib.util
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "/home/fbazsa/ncore-autorss")
import autorss
import autorss_common as C


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
        # regression (bug #2): titles that contain / start with a year-like number
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


def _t(progress, state, seeds=0, leechs=0, time_active=60):
    return {"progress": progress, "state": state, "num_seeds": seeds,
            "num_leechs": leechs, "time_active": time_active}


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
        # bug #1 + #4: the queue gate (autorss) and the dashboard metric (torrent-manager)
        # are now literally the same shared function -- they cannot disagree.
        self.assertIs(autorss.is_active_download, tm.is_active_download)
        self.assertIs(autorss.is_active_download, C.is_active_download)


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


class TestAlreadySeenDedup(unittest.TestCase):
    def test_same_film_other_torrent_id_is_seen(self):
        # state already holds one film (under any status); a DIFFERENT torrent ID /
        # release of the SAME film must be recognised as already seen.
        films = {
            "111": {"clean": "Frozen (2013)", "status": "removed"},
            "222": {"clean": "Dune Part Two (2024)", "status": "rejected"},
            "333": {"clean": "Michael (2026)", "status": "downloaded"},
            "444": {"status": "pending"},  # no clean yet -> must not crash / add None
        }
        seen = autorss.seen_clean_titles(films)
        self.assertEqual(seen, {"Frozen (2013)", "Dune Part Two (2024)", "Michael (2026)"})

        # a new torrent for an already-seen film (different name + ID) is deduped
        new_release = "Frozen.2013.1080p.WEB-DL.x264-OTHER [Film (ENG HD)]"
        self.assertIn(autorss.clean_title(new_release), seen)
        # a genuinely new film is not
        self.assertNotIn(autorss.clean_title("Wicked.2024.1080p.BluRay [Film (ENG HD)]"), seen)


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


class TestFilmsMarkdown(unittest.TestCase):
    def test_render_and_escape(self):
        rows = [{"title": "Dune | Part Two (2024)", "status": "downloaded",
                 "reason": "Blockbuster", "epoch": 1718740800}]
        md = tm.films_markdown(rows)
        self.assertIn("| Film | Státusz | Ok | Mikor |", md)
        self.assertIn("Dune \\| Part Two (2024)", md)   # pipe escaped so the table isn't broken
        self.assertIn("downloaded", md)
        self.assertIn("_Nincs jóváhagyott film._", tm.films_markdown([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
