"""Offline tests for derive.py and fetch_odds.py (no network, no API key):  python3 -m unittest -v"""
import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timezone

import derive
import fetch_odds

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime(2026, 11, 19, 5, 30, tzinfo=timezone.utc)


def sample_events():
    with open(os.path.join(HERE, "sample_odds_api.json")) as fh:
        return json.load(fh)


BOOT = {"teams": [{"id": 1, "name": "Arsenal", "short_name": "ARS"}, {"id": 5, "name": "Brighton", "short_name": "BHA"},
                  {"id": 6, "name": "Chelsea", "short_name": "CHE"}, {"id": 11, "name": "Hull City", "short_name": "HUL"}]}
FIXTURES = [
    {"id": 101, "event": 12, "team_h": 1, "team_a": 6, "kickoff_time": "2026-11-21T15:00:00Z", "finished": False},
    {"id": 102, "event": 12, "team_h": 5, "team_a": 11, "kickoff_time": "2026-11-22T14:00:00Z", "finished": False},
    {"id": 140, "event": 15, "team_h": 6, "team_a": 1, "kickoff_time": "2026-12-12T15:00:00Z", "finished": False},
]


class FakeAPI:
    def __init__(self, remaining="420", fixtures=FIXTURES, odds_status=200, cost="2"):
        self.calls = []
        self.remaining, self.fixtures, self.odds_status, self.cost = remaining, fixtures, odds_status, cost

    def __call__(self, url, params=None):
        self.calls.append(url)
        if url.endswith("/fixtures/"):
            return 200, {}, self.fixtures
        if url.endswith("/bootstrap-static/"):
            return 200, {}, BOOT
        if url.endswith("/v4/sports/"):
            return 200, ({"x-requests-remaining": self.remaining} if self.remaining is not None else {}), []
        if "/odds/" in url:
            assert params["regions"] == "uk" and params["markets"] == "h2h,totals" and params["oddsFormat"] == "decimal"
            body = sample_events() if self.odds_status == 200 else None
            return self.odds_status, {"x-requests-last": self.cost, "x-requests-remaining": "418"}, body
        raise AssertionError(url)

    @property
    def paid_calls(self):
        return [c for c in self.calls if "/odds/" in c]


class DeriveTests(unittest.TestCase):
    def test_even_match(self):
        d = derive.derive([(2.9, 3.3, 2.9), (2.8, 3.4, 2.8)], [(2.5, 1.95, 1.95)])
        self.assertAlmostEqual(d["p_home"], d["p_away"], places=9)
        self.assertAlmostEqual(d["lambda_home"], d["lambda_away"], places=6)
        # P(Poisson(T) <= 2) = 0.5  ->  T = 2.674…
        self.assertAlmostEqual(d["exp_goals"], 2.674, places=2)
        self.assertAlmostEqual(d["cs_home"], math.exp(-d["lambda_away"]), places=12)
        self.assertEqual(d["books"], 2)

    def test_favourite(self):
        d = derive.derive([(1.5, 4.5, 6.5)], [(2.5, 1.8, 2.0), (3.5, 3.2, 1.33)])
        self.assertGreater(d["p_home"], 0.6)
        self.assertAlmostEqual(d["p_home"] + d["p_draw"] + d["p_away"], 1.0, places=12)
        self.assertGreater(d["lambda_home"], d["lambda_away"])
        w, _, l = derive.match_probs(d["lambda_home"], d["lambda_away"])
        self.assertAlmostEqual(w - l, d["p_home"] - d["p_away"], places=6)
        self.assertGreater(d["cs_home"], d["cs_away"])

    def test_without_totals_and_invalid_prices(self):
        d = derive.derive([(2.1, 3.4, 3.6), (0, 3.4, 3.6), ("x", 1, 2)], [(2.25, 1.9, 1.9)])
        self.assertEqual(d["books"], 1)                  # invalid triples dropped, quarter line ignored
        self.assertTrue(1.5 < d["exp_goals"] < 4.0)      # total from the draw probability
        self.assertIsNone(derive.derive([], []))

    def test_football_data_row(self):
        # football-data.co.uk 2016-17, Burnley v Swansea (B365/BW/IW/LB/PS/WH/VC, Bb averages over/under 2.5)
        h2h = [(2.4, 3.3, 3.25), (2.45, 3.1, 2.95), (2.5, 3.3, 2.65), (2.45, 3.25, 3.1), (2.47, 3.32, 3.19),
               (2.5, 3.2, 2.9), (2.5, 3.2, 3.25)]
        d = derive.derive(h2h, [(2.5, 2.3, 1.61)])
        self.assertAlmostEqual(d["p_home"], 0.3928, places=3)
        self.assertAlmostEqual(d["exp_goals"], 2.329, places=2)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fetch_odds.STATE_PATH = os.path.join(self.tmp.name, "state.json")
        fetch_odds.OUT_PATH = os.path.join(self.tmp.name, "odds.json")
        self.env = {"ODDS_API_KEY": "test-key"}

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        with open(fetch_odds.STATE_PATH) as fh:
            return json.load(fh)

    def test_requires_key(self):
        api = FakeAPI()
        self.assertTrue(fetch_odds.run(NOW, api, {}).startswith("error"))
        self.assertEqual(api.calls, [])

    def test_skips_without_fixture_in_8_days(self):
        api = FakeAPI(fixtures=[FIXTURES[2]])
        self.assertIn("no Premier League fixture", fetch_odds.run(NOW, api, self.env))
        self.assertEqual(api.paid_calls, [])
        self.assertFalse(any("the-odds-api" in c for c in api.calls))

    def test_skips_when_monthly_budget_used(self):
        fetch_odds.write_json(fetch_odds.STATE_PATH, {"month": "2026-11", "credits_used": 199})
        api = FakeAPI()
        self.assertIn("monthly budget", fetch_odds.run(NOW, api, self.env))
        self.assertFalse(any("the-odds-api" in c for c in api.calls))

    def test_new_month_resets_ledger(self):
        fetch_odds.write_json(fetch_odds.STATE_PATH, {"month": "2026-10", "credits_used": 200})
        api = FakeAPI()
        self.assertTrue(fetch_odds.run(NOW, api, self.env).startswith("ok"))
        self.assertEqual(self.state()["month"], "2026-11")
        self.assertEqual(self.state()["credits_used"], 2)

    def test_skips_when_account_quota_low_or_unknown(self):
        for remaining in ("149", None, "abc"):
            api = FakeAPI(remaining=remaining)
            self.assertTrue(fetch_odds.run(NOW, api, self.env).startswith("skip"), remaining)
            self.assertEqual(api.paid_calls, [])
        self.assertFalse(os.path.exists(fetch_odds.STATE_PATH))

    def test_full_run_writes_derived_numbers_only(self):
        api = FakeAPI()
        self.assertTrue(fetch_odds.run(NOW, api, self.env).startswith("ok"))
        self.assertEqual(len(api.paid_calls), 1)
        self.assertEqual(self.state()["credits_used"], 2)
        with open(fetch_odds.OUT_PATH) as fh:
            out = json.load(fh)
        self.assertEqual(sorted(out["fixtures"]), ["101", "102"])        # "Real Madrid v Getafe" is not mapped
        allowed = {"p_home", "p_draw", "p_away", "exp_goals", "lambda_home", "lambda_away", "cs_home", "cs_away",
                   "books", "home", "away", "gw", "kickoff", "updated"}
        for e in out["fixtures"].values():
            self.assertEqual(set(e), allowed)
            self.assertAlmostEqual(e["p_home"] + e["p_draw"] + e["p_away"], 1.0, places=3)
        ars = out["fixtures"]["101"]
        self.assertEqual((ars["home"], ars["away"], ars["books"]), ("ARS", "CHE", 3))
        self.assertGreater(ars["p_home"], ars["p_away"])
        self.assertEqual(out["fixtures"]["102"]["books"], 2)               # "Brighton and Hove Albion" -> BHA
        self.assertNotIn("price", json.dumps(out))

    def test_failed_paid_request_is_booked_and_not_retried(self):
        api = FakeAPI(odds_status=500, cost="2")
        self.assertTrue(fetch_odds.run(NOW, api, self.env).startswith("error"))
        self.assertEqual(len(api.paid_calls), 1)
        self.assertEqual(self.state()["credits_used"], 2)
        self.assertFalse(os.path.exists(fetch_odds.OUT_PATH))

    def test_previous_entries_kept_until_kickoff(self):
        fetch_odds.write_json(fetch_odds.OUT_PATH, {"format": 1, "fixtures": {
            "140": {"kickoff": "2026-12-12T15:00:00Z", "p_home": 0.5},
            "90": {"kickoff": "2026-11-10T15:00:00Z", "p_home": 0.5}}})
        self.assertTrue(fetch_odds.run(NOW, FakeAPI(), self.env).startswith("ok"))
        with open(fetch_odds.OUT_PATH) as fh:
            self.assertEqual(sorted(json.load(fh)["fixtures"]), ["101", "102", "140"])


if __name__ == "__main__":
    unittest.main()
