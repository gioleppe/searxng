# SPDX-License-Identifier: AGPL-3.0-or-later
"""A plugin to display sports league standings in an answer box.

When a user searches for a major sports league (e.g. ``serie a``,
``premier league``, ``nba``), the plugin fetches the current standings table
from Wikipedia and renders it as an *answer* at the top of the search results.

Supported leagues
-----------------

*European Football*

- Premier League (England)
- La Liga (Spain)
- Serie A (Italy)
- Bundesliga (Germany)
- Ligue 1 (France)
- Eredivisie (Netherlands)
- Primeira Liga (Portugal)
- EFL Championship (England)

*American sports*

- NBA (basketball)
- NFL (American football)
- MLB (baseball)
- NHL (ice hockey)

*Rugby*

- Six Nations Championship
"""

import typing
import datetime
import re

from flask_babel import gettext
from httpx import HTTPError
import lxml.html

from searx.network import get
from searx.plugins import Plugin, PluginInfo
from searx.result_types import EngineResults
from searx.result_types.answer import SportsLeaderboard

if typing.TYPE_CHECKING:
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.plugins import PluginCfg

# ---------------------------------------------------------------------------
# League metadata
# ---------------------------------------------------------------------------

# Wikipedia base URL for article fetching
_WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"

# Each entry maps a *normalised* search term to a dict with:
#   name         – human-readable league name
#   wiki         – Wikipedia article title template; {season} is replaced at
#                  runtime with the current season string
#   season_type  – controls how the season string is built:
#                  "football"  →  "YYYY-YY"  (season starts Aug, ends May)
#                  "nba"       →  "YYYY-YY"  (season starts Oct, ends Jun)
#                  "year"      →  "YYYY"     (calendar year)

_LEAGUES: dict[str, dict[str, str]] = {
    # --- European Football ---
    "premier league": {
        "name": "Premier League",
        "wiki": "{season} Premier League",
        "season_type": "football",
    },
    "la liga": {
        "name": "La Liga",
        "wiki": "{season} La Liga",
        "season_type": "football",
    },
    "serie a": {
        "name": "Serie A",
        "wiki": "{season} Serie A",
        "season_type": "football",
    },
    "bundesliga": {
        "name": "Bundesliga",
        "wiki": "{season} Fußball-Bundesliga",
        "season_type": "football",
    },
    "ligue 1": {
        "name": "Ligue 1",
        "wiki": "{season} Ligue 1",
        "season_type": "football",
    },
    "eredivisie": {
        "name": "Eredivisie",
        "wiki": "{season} Eredivisie",
        "season_type": "football",
    },
    "primeira liga": {
        "name": "Primeira Liga",
        "wiki": "{season} Primeira Liga",
        "season_type": "football",
    },
    "efl championship": {
        "name": "EFL Championship",
        "wiki": "{season} EFL Championship",
        "season_type": "football",
    },
    # --- American Sports ---
    "nba": {
        "name": "NBA",
        "wiki": "{season} NBA season",
        "season_type": "nba",
    },
    "nfl": {
        "name": "NFL",
        "wiki": "{season} NFL season",
        "season_type": "year",
    },
    "mlb": {
        "name": "MLB",
        "wiki": "{season} MLB season",
        "season_type": "year",
    },
    "nhl": {
        "name": "NHL",
        "wiki": "{season} NHL season",
        "season_type": "nba",
    },
    # --- Rugby ---
    "six nations": {
        "name": "Six Nations Championship",
        "wiki": "{season} Six Nations Championship",
        "season_type": "year",
    },
}

# Maximum number of standings rows to display in the answer box
_MAX_ROWS = 20

# Minimum number of columns a table must have to be considered a standings table
_MIN_COLS = 3

# Header keywords that indicate a standings table (case-insensitive)
_STANDINGS_KEYWORDS = {"pts", "points", "pct", "gp", "gw", "pld", "mp", "w", "l", "t"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _football_season(now: datetime.date) -> str:
    """Return a football-style season string, e.g. ``"2024-25"``."""
    year = now.year
    if now.month >= 8:
        return f"{year}-{str(year + 1)[2:]}"
    return f"{year - 1}-{str(year)[2:]}"


def _nba_season(now: datetime.date) -> str:
    """Return an NBA/NHL-style season string, e.g. ``"2024-25"``."""
    year = now.year
    if now.month >= 10:
        return f"{year}-{str(year + 1)[2:]}"
    return f"{year - 1}-{str(year)[2:]}"


def _calendar_season(now: datetime.date) -> str:
    """Return a calendar-year season string, e.g. ``"2024"``."""
    return str(now.year)


def _current_season(season_type: str, now: datetime.date | None = None) -> str:
    """Return the current season string for *season_type*."""
    if now is None:
        now = datetime.date.today()
    if season_type == "football":
        return _football_season(now)
    if season_type == "nba":
        return _nba_season(now)
    return _calendar_season(now)


def _clean_text(element: lxml.html.HtmlElement) -> str:
    """Return clean text from an lxml element (strips footnote superscripts)."""
    for sup in element.xpath(".//sup"):
        parent = sup.getparent()
        if parent is not None:
            parent.remove(sup)
    return re.sub(r"\s+", " ", element.text_content()).strip()


def _parse_wikitable(
    table: lxml.html.HtmlElement,
) -> tuple[list[str], list[list[str]]]:
    """Extract headers and data rows from a Wikipedia *wikitable* element.

    Returns ``(headers, rows)``; both may be empty if the table cannot be
    parsed.
    """
    headers: list[str] = []
    rows: list[list[str]] = []

    for tr in table.xpath(".//tr"):
        ths = tr.xpath(".//th")
        tds = tr.xpath(".//td")

        if ths and not tds:
            # Pure header row – capture the first one as column names
            if not headers:
                headers = [_clean_text(th) for th in ths]
        elif tds:
            # Data row – align with headers
            cells = tr.xpath(".//th | .//td")
            row = [_clean_text(c) for c in cells]
            if row:
                rows.append(row)

    return headers, rows


def _is_standings_table(headers: list[str]) -> bool:
    """Return *True* if *headers* look like a sports standings table."""
    normalised = {h.lower().strip("#").strip() for h in headers}
    return bool(normalised & _STANDINGS_KEYWORDS)


def fetch_standings(wiki_title: str) -> tuple[list[str], list[list[str]]] | None:
    """Fetch the Wikipedia article *wiki_title* and extract the first
    standings table found.

    Returns ``(headers, rows)`` or ``None`` if no standings table is found or
    the request fails.
    """
    params = {
        "action": "parse",
        "page": wiki_title,
        "prop": "text",
        "format": "json",
        "redirects": "true",
        "section": "0",
    }

    try:
        resp = get(_WIKIPEDIA_API, params=params, timeout=3.0)
        data = resp.json()
    except (HTTPError, Exception):  # pylint: disable=broad-except
        return None

    html_text = data.get("parse", {}).get("text", {}).get("*", "")
    if not html_text:
        # section 0 has no table; try the full article
        params.pop("section")
        try:
            resp = get(_WIKIPEDIA_API, params=params, timeout=3.0)
            data = resp.json()
        except (HTTPError, Exception):  # pylint: disable=broad-except
            return None
        html_text = data.get("parse", {}).get("text", {}).get("*", "")

    if not html_text:
        return None

    try:
        tree = lxml.html.fromstring(html_text)
    except Exception:  # pylint: disable=broad-except
        return None

    tables = tree.xpath('//table[contains(@class, "wikitable")]')
    for table in tables:
        headers, rows = _parse_wikitable(table)
        if not headers or len(headers) < _MIN_COLS:
            continue
        if not _is_standings_table(headers):
            continue
        # Trim rows to a reasonable display size
        rows = [r for r in rows if any(c for c in r)]
        return headers, rows[:_MAX_ROWS]

    return None


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class SXNGPlugin(Plugin):
    """Show sports league standings as an answer box.

    The plugin detects league-name keywords in the search query, then
    fetches the current standings table from Wikipedia and renders it inline.
    """

    id = "sports_leaderboard"

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)

        self.info = PluginInfo(
            id=self.id,
            name=gettext("Sports leaderboard"),
            description=gettext(
                "Display current standings for major sports leagues (Premier League, Serie A, NBA, …)."
            ),
            examples=["serie a", "premier league", "nba"],
            preference_section="query",
        )

    def post_search(self, request: "SXNG_Request", search: "SearchWithPlugins") -> EngineResults:
        results = EngineResults()

        # Only show on the first page
        if search.search_query.pageno > 1:
            return results

        query = search.search_query.query.lower().strip()

        league_meta = _LEAGUES.get(query)
        if league_meta is None:
            return results

        season = _current_season(league_meta["season_type"])
        wiki_title = league_meta["wiki"].format(season=season)

        standing = fetch_standings(wiki_title)
        if standing is None:
            return results

        headers, rows = standing
        if not rows:
            return results

        wiki_url = "https://en.wikipedia.org/wiki/" + wiki_title.replace(" ", "_")
        results.add(
            SportsLeaderboard(
                league=league_meta["name"],
                season=season,
                headers=headers,
                rows=rows,
                url=wiki_url,
            )
        )
        return results
