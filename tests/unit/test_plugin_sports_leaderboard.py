# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,disable=missing-class-docstring,invalid-name

import datetime
import json
from unittest.mock import patch, Mock

from httpx import HTTPError

import searx.plugins
import searx.preferences

from searx.extended_types import sxng_request
from searx.result_types.answer import SportsLeaderboard
from searx.plugins.sports_leaderboard import (
    _current_season,
    _football_season,
    _nba_season,
    _calendar_season,
    _is_standings_table,
    fetch_standings,
    _LEAGUES,
)

from tests import SearxTestCase
from .test_plugins import do_post_search

# ---------------------------------------------------------------------------
# Minimal HTML that mimics a Wikipedia standings table
# ---------------------------------------------------------------------------

_STANDINGS_HTML = """
<table class="wikitable sortable">
  <tbody>
    <tr>
      <th>#</th><th>Club</th><th>Pld</th><th>W</th><th>D</th><th>L</th>
      <th>GF</th><th>GA</th><th>GD</th><th>Pts</th>
    </tr>
    <tr>
      <td>1</td><td>Arsenal</td><td>20</td><td>15</td><td>3</td><td>2</td>
      <td>45</td><td>18</td><td>+27</td><td>48</td>
    </tr>
    <tr>
      <td>2</td><td>Liverpool</td><td>20</td><td>14</td><td>4</td><td>2</td>
      <td>40</td><td>16</td><td>+24</td><td>46</td>
    </tr>
    <tr>
      <td>3</td><td>Chelsea</td><td>20</td><td>12</td><td>5</td><td>3</td>
      <td>38</td><td>20</td><td>+18</td><td>41</td>
    </tr>
  </tbody>
</table>
"""

# Wrap the table in a fake Wikipedia API JSON response
_WIKI_API_RESPONSE = json.dumps({"parse": {"text": {"*": _STANDINGS_HTML}}})

# A response that contains *no* standings table
_WIKI_NO_TABLE_RESPONSE = json.dumps({"parse": {"text": {"*": "<p>No table here.</p>"}}})


def _make_mock_response(text: str) -> Mock:
    resp = Mock()
    resp.json.return_value = json.loads(text)
    return resp


class TestSeasonHelpers(SearxTestCase):
    """Unit tests for the season-string helper functions."""

    def test_football_season_aug_dec(self):
        # August → new season starts
        d = datetime.date(2024, 8, 1)
        self.assertEqual(_football_season(d), "2024-25")
        d = datetime.date(2024, 12, 31)
        self.assertEqual(_football_season(d), "2024-25")

    def test_football_season_jan_jul(self):
        # January → still in previous season
        d = datetime.date(2025, 1, 1)
        self.assertEqual(_football_season(d), "2024-25")
        d = datetime.date(2025, 7, 31)
        self.assertEqual(_football_season(d), "2024-25")

    def test_nba_season_oct_dec(self):
        d = datetime.date(2024, 10, 1)
        self.assertEqual(_nba_season(d), "2024-25")

    def test_nba_season_jan_sep(self):
        d = datetime.date(2025, 3, 1)
        self.assertEqual(_nba_season(d), "2024-25")

    def test_calendar_season(self):
        d = datetime.date(2024, 6, 1)
        self.assertEqual(_calendar_season(d), "2024")

    def test_current_season_football(self):
        d = datetime.date(2024, 9, 1)
        self.assertEqual(_current_season("football", now=d), "2024-25")

    def test_current_season_nba(self):
        d = datetime.date(2024, 11, 1)
        self.assertEqual(_current_season("nba", now=d), "2024-25")

    def test_current_season_year(self):
        d = datetime.date(2024, 6, 1)
        self.assertEqual(_current_season("year", now=d), "2024")


class TestIsStandingsTable(SearxTestCase):
    """Unit tests for _is_standings_table."""

    def test_football_headers(self):
        self.assertTrue(_is_standings_table(["#", "Club", "Pld", "W", "D", "L", "GF", "GA", "GD", "Pts"]))

    def test_basketball_headers(self):
        self.assertTrue(_is_standings_table(["Team", "W", "L", "PCT", "GB"]))

    def test_non_standings_headers(self):
        self.assertFalse(_is_standings_table(["Name", "Country", "Population"]))

    def test_empty_headers(self):
        self.assertFalse(_is_standings_table([]))


class TestFetchStandings(SearxTestCase):
    """Tests for the Wikipedia scraping helper."""

    @patch("searx.plugins.sports_leaderboard.get")
    def test_fetch_returns_headers_and_rows(self, mock_get):
        mock_get.return_value = _make_mock_response(_WIKI_API_RESPONSE)
        result = fetch_standings("2024-25 Premier League")
        self.assertIsNotNone(result)
        headers, rows = result  # type: ignore
        self.assertIn("Pts", headers)
        self.assertEqual(len(rows), 3)
        # First row should be Arsenal
        self.assertEqual(rows[0][1], "Arsenal")

    @patch("searx.plugins.sports_leaderboard.get")
    def test_fetch_no_table_returns_none(self, mock_get):
        mock_get.return_value = _make_mock_response(_WIKI_NO_TABLE_RESPONSE)
        result = fetch_standings("2024-25 FakeLeague")
        self.assertIsNone(result)

    @patch("searx.plugins.sports_leaderboard.get")
    def test_fetch_http_error_returns_none(self, mock_get):
        mock_get.side_effect = HTTPError("network error")
        result = fetch_standings("2024-25 Premier League")
        self.assertIsNone(result)


class TestSportsLeaderboardPlugin(SearxTestCase):
    """Integration tests for the SXNGPlugin."""

    def setUp(self):
        super().setUp()
        engines = {}
        self.storage = searx.plugins.PluginStorage()
        self.storage.load_settings({"searx.plugins.sports_leaderboard.SXNGPlugin": {"active": True}})
        self.storage.init(self.app)
        self.pref = searx.preferences.Preferences(["simple"], ["general"], engines, self.storage)
        self.pref.parse_dict({"locale": "en"})

    def test_plugin_store_init(self):
        self.assertEqual(1, len(self.storage))

    @patch("searx.plugins.sports_leaderboard.get")
    def test_serie_a_query_returns_leaderboard(self, mock_get):
        mock_get.return_value = _make_mock_response(_WIKI_API_RESPONSE)
        with self.app.test_request_context():
            sxng_request.preferences = self.pref
            search = do_post_search("serie a", self.storage)
        answers = list(search.result_container.answers)
        self.assertEqual(len(answers), 1)
        answer = answers[0]
        self.assertIsInstance(answer, SportsLeaderboard)
        self.assertEqual(answer.league, "Serie A")
        self.assertIn("Pts", answer.headers)
        self.assertTrue(len(answer.rows) > 0)

    @patch("searx.plugins.sports_leaderboard.get")
    def test_premier_league_query(self, mock_get):
        mock_get.return_value = _make_mock_response(_WIKI_API_RESPONSE)
        with self.app.test_request_context():
            sxng_request.preferences = self.pref
            search = do_post_search("premier league", self.storage)
        answers = list(search.result_container.answers)
        self.assertEqual(len(answers), 1)
        self.assertIsInstance(answers[0], SportsLeaderboard)
        self.assertEqual(answers[0].league, "Premier League")

    @patch("searx.plugins.sports_leaderboard.get")
    def test_nba_query(self, mock_get):
        mock_get.return_value = _make_mock_response(_WIKI_API_RESPONSE)
        with self.app.test_request_context():
            sxng_request.preferences = self.pref
            search = do_post_search("nba", self.storage)
        answers = list(search.result_container.answers)
        self.assertEqual(len(answers), 1)
        self.assertIsInstance(answers[0], SportsLeaderboard)
        self.assertEqual(answers[0].league, "NBA")

    @patch("searx.plugins.sports_leaderboard.get")
    def test_unknown_query_returns_no_answer(self, mock_get):
        mock_get.return_value = _make_mock_response(_WIKI_API_RESPONSE)
        with self.app.test_request_context():
            sxng_request.preferences = self.pref
            search = do_post_search("lorem ipsum", self.storage)
        answers = list(search.result_container.answers)
        self.assertEqual(len(answers), 0)

    @patch("searx.plugins.sports_leaderboard.get")
    def test_pageno_2_returns_no_answer(self, mock_get):
        mock_get.return_value = _make_mock_response(_WIKI_API_RESPONSE)
        with self.app.test_request_context():
            sxng_request.preferences = self.pref
            search = do_post_search("serie a", self.storage, pageno=2)
        answers = list(search.result_container.answers)
        self.assertEqual(len(answers), 0)

    @patch("searx.plugins.sports_leaderboard.get")
    def test_wikipedia_error_returns_no_answer(self, mock_get):
        mock_get.side_effect = HTTPError("network error")
        with self.app.test_request_context():
            sxng_request.preferences = self.pref
            search = do_post_search("serie a", self.storage)
        answers = list(search.result_container.answers)
        self.assertEqual(len(answers), 0)

    def test_all_leagues_have_required_keys(self):
        for key, meta in _LEAGUES.items():
            self.assertIn("name", meta, f"League '{key}' missing 'name'")
            self.assertIn("wiki", meta, f"League '{key}' missing 'wiki'")
            self.assertIn("season_type", meta, f"League '{key}' missing 'season_type'")
            self.assertIn("{season}", meta["wiki"], f"League '{key}' wiki template missing {{season}}")
