"""Raw HLTV client. Only the source router qualifies these positive integer IDs."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import quote_plus

from . import hltv_parser as parser
from .hltv_fetcher import HltvFetcher


class HltvClient:
    def __init__(self, config: dict, *, transport=None):
        self._fetcher = HltvFetcher(config, transport=transport)

    def status(self) -> dict:
        return self._fetcher.status()

    async def close(self):
        await self._fetcher.close()

    async def manual_verify(self):
        return await self._fetcher.manual_verify()

    async def fetch_bytes(self, url, max_bytes=2 * 1024 * 1024):
        return await self._fetcher.fetch_bytes(url, max_bytes=max_bytes)

    async def _parsed(self, path, parse, *, kind="html"):
        document = await self._fetcher.fetch(f"{parser.BASE}{path}", kind=kind, validator=parse)
        if document is None:
            return None
        rows = parse(document.text)
        def annotate(record):
            record["_fetched_at"], record["_cached"] = document.fetched_at, document.cached
            return record
        return [annotate(row) for row in rows] if isinstance(rows, list) else annotate(rows)

    @staticmethod
    def _limit(value):
        return max(1, min(int(value), 100))

    @staticmethod
    def _identifier(value):
        if isinstance(value, bool):
            return None
        match = re.fullmatch(r"([1-9][0-9]*)(?:/([A-Za-z0-9][A-Za-z0-9-]*))?", str(value))
        return (int(match.group(1)), match.group(2) or "-") if match else None

    async def get_upcoming_matches(self, per_page=50, *, until=None):
        rows = await self._parsed("/matches", parser.parse_upcoming_matches)
        if rows is None:
            return None
        now = datetime.now(timezone.utc)
        if until is not None:
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            until = until.astimezone(timezone.utc)
        filtered = []
        for row in rows:
            stamp = row.get("scheduled_at")
            if stamp:
                at = datetime.fromisoformat(stamp)
                if (at <= now and row["status"] != "running") or (until is not None and at > until):
                    continue
            filtered.append(row)
        # As in PandaScoreClient, a cutoff means return the complete available
        # window, not just a first page. HLTV exposes one page, not API paging.
        return filtered if until is not None else filtered[:self._limit(per_page)]

    async def get_upcoming_tournaments(self, per_page=50):
        rows = await self._parsed("/events", parser.parse_events)
        if rows is None:
            return None
        now = datetime.now(timezone.utc)
        return [row for row in rows if not row["begin_at"] or datetime.fromisoformat(row["begin_at"]) > now][:self._limit(per_page)]

    async def search_teams(self, query, per_page=20):
        query = str(query).strip()
        if not query:
            return []
        rows = await self._parsed(f"/search?term={quote_plus(query)}", parser.parse_search, kind="json")
        return rows[:self._limit(per_page)] if rows is not None else None

    async def get_team(self, team_id_or_slug):
        identifier = self._identifier(team_id_or_slug)
        if identifier is None:
            # A name/slug has no trustworthy numeric identity until typeahead
            # resolves it. Never turn a partial name match into a different team.
            if not isinstance(team_id_or_slug, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", team_id_or_slug) or team_id_or_slug.startswith("hltv:"):
                return None
            query = team_id_or_slug.replace("-", " ")
            rows = await self.search_teams(query)
            key = lambda value: " ".join(value.casefold().replace("-", " ").split())
            matches = [r for r in rows or [] if key(r["name"]) == key(query)]
            if len(matches) != 1:
                return None
            identifier = (matches[0]["id"], "-")
        team_id, slug = identifier
        return await self._parsed(f"/team/{team_id}/{slug}", lambda html: parser.parse_team(html, team_id))

    async def get_match_player_stats(self, match_id):
        return None

    async def get_match_games(self, match_id):
        return None

    async def get_match_result(self, match_id):
        identifier = self._identifier(match_id)
        if identifier is None:
            return None
        mid, slug = identifier
        path = f"/matches/{mid}/{slug}"
        return await self._parsed(path, lambda html: parser.parse_match(html, f"{parser.BASE}{path}"))
