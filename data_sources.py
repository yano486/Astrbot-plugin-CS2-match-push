"""Source selection and normalization shared by commands, push jobs and WebUI."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from .client import PandaScoreClient
from .config import SOURCE_DEFAULTS, enabled_sources
from .source_identity import SourceIdentity, normalized_name, qualified_id


def normalize_team(team, source):
    row = deepcopy(team)
    if row.get("id") is not None:
        row["_remote_id"] = row["id"]
        row["id"] = qualified_id(source, row["id"])
    row["_source"] = source
    return row


def normalize_record(record, source, kind="match"):
    if not isinstance(record, dict) or record.get("id") is None:
        raise ValueError("provider returned a record without an id")
    row = normalize_team(record, source)
    row["_fetched_at"] = row.get("_fetched_at") or datetime.now(timezone.utc).isoformat()
    if kind == "match":
        opponents = row.get("opponents") or []
        if not isinstance(opponents, list):
            raise ValueError("provider returned invalid opponents")
        normalized_opponents = []
        for item in opponents:
            if not isinstance(item, dict):
                raise ValueError("provider returned an invalid opponent")
            normalized_opponents.append(dict(item, opponent=normalize_team(item.get("opponent") or {}, source)))
        row["opponents"] = normalized_opponents
        if (row.get("tournament") or {}).get("id") is not None:
            row["tournament"] = normalize_team(row["tournament"], source)
        if (row.get("winner") or {}).get("id") is not None:
            row["winner"] = normalize_team(row["winner"], source)
        row["results"] = [dict(item, team_id=qualified_id(source, item["team_id"]))
                          if item.get("team_id") is not None else item for item in row.get("results") or []]
        for game in row.get("games") or []:
            if (game.get("winner") or {}).get("id") is not None:
                game["winner"] = normalize_team(game["winner"], source)
    else:
        teams = row.get("teams") or []
        if not isinstance(teams, list) or any(not isinstance(team, dict) for team in teams):
            raise ValueError("provider returned invalid tournament teams")
        row["teams"] = [normalize_team(team, source) for team in teams]
    return row


class SourceRouter:
    def __init__(self, config, store, *, clients=None):
        self.config = {**SOURCE_DEFAULTS, **config}
        self.store = store
        self.enabled = enabled_sources(self.config)
        self.primary = self.config["primary_source"]
        self.identity = SourceIdentity(store)
        self._clients = {}
        self._provided = clients or {}
        self._closed = False
        self._health = {name: {"enabled": name in self.enabled, "state": "idle" if name in self.enabled else "disabled",
                               "message": "尚未检查" if name in self.enabled else "未启用", "last_success": None}
                        for name in ("pandascore", "hltv")}
        self.team_search = SimpleNamespace(popular=self.popular_teams)
        self._teams = {}

    def _client(self, source):
        if source not in self._clients:
            if source in self._provided:
                self._clients[source] = self._provided[source]
            elif source == "pandascore":
                self._clients[source] = PandaScoreClient(str(self.config.get("pandascore_token") or ""))
            else:
                from .hltv_client import HltvClient
                self._clients[source] = HltvClient(self.config)
        return self._clients[source]

    def status(self):
        return deepcopy(self._health)

    async def _call(self, source, method, *args, **kwargs):
        if self._closed:
            return None
        if source == "pandascore" and not str(self.config.get("pandascore_token") or "").strip():
            self._health[source].update(state="not_configured", message="请填写 PandaScore Token")
            return None
        try:
            client = self._client(source)
            timeout = 75 if source == "pandascore" else 65 + min(int(self.config.get("hltv_request_min_gap", 90)), 120)
            if method == "manual_verify":
                timeout = 300
                self._health[source].update(state="verifying", message="请在浏览器中完成人工验证")
            result = await asyncio.wait_for(getattr(client, method)(*args, **kwargs), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._health[source].update(state="error", message=f"{source} 请求失败（{type(exc).__name__}）")
            return None
        details = client.status() if hasattr(client, "status") else {}
        provider_state = details.get("state")
        degraded = {"blocked", "challenge", "error", "unavailable", "backoff", "verifying"}
        if result is None or (method == "manual_verify" and isinstance(result, dict) and not result.get("ok")):
            state = details.get("state")
            self._health[source].update(state=state if state and state not in {"ready", "ok"} else "error",
                                        message=(details.get("message") or "数据请求未成功，保留旧数据") if state not in {"ready", "ok"} else "数据请求未成功，保留旧数据")
        elif provider_state in degraded:
            self._health[source].update(state=provider_state,
                                        message=details.get("message") or "来源当前受阻，正在使用已验证缓存")
        else:
            self._health[source].update(state="ready", message="连接正常",
                                       last_success=datetime.now(timezone.utc).isoformat())
        if self._closed:
            return None
        return result

    async def _list(self, method, *args, kind="match", **kwargs):
        results = await asyncio.gather(*(self._call(source, method, *args, **kwargs) for source in self.enabled))
        if self._closed:
            return None
        rows = []
        snapshots = deepcopy(self.store.get(f"source_{kind}_snapshots", {}))
        updates = {}
        successful_sources = set()
        for source, items in zip(self.enabled, results):
            normalized = None
            if items is not None:
                try:
                    if not isinstance(items, (list, tuple)):
                        raise ValueError("provider returned an invalid collection")
                    normalized = [normalize_record(item, source, kind) for item in items]
                except Exception as exc:
                    self._health[source].update(state="error", message=f"{source} 响应格式错误（{type(exc).__name__}），保留最近成功数据")
            if normalized is None:
                # Only replay the last successful response, never the long-lived identity catalog.
                for item in snapshots.get(source, []):
                    stale = deepcopy(item)
                    stale["_stale"] = True
                    rows.append(stale)
                continue

            snapshots[source] = deepcopy(normalized)
            successful_sources.add(source)
            updates[f"source_{kind}_snapshots"] = snapshots
            rows.extend(normalized)
            for item in normalized:
                for op in item.get("opponents") or []:
                    team = op.get("opponent") or {}
                    if team.get("id") is not None:
                        self._teams[team["id"]] = team
        if not successful_sources and not rows:
            return None
        return self.identity.merge(rows, self.primary, kind, extra_updates=updates)

    async def get_upcoming_matches(self, per_page=50, *, until=None):
        return await self._list("get_upcoming_matches", per_page=per_page, until=until)

    async def get_upcoming_tournaments(self, per_page=50):
        return await self._list("get_upcoming_tournaments", per_page=per_page, kind="tournament")

    async def get_match_result(self, match_id):
        canonical, remotes = self.identity.remote_ids(match_id)
        order = sorted(self.enabled, key=lambda source: source != self.primary)
        for source in order:
            if source not in remotes:
                continue
            result = await self._call(source, "get_match_result", remotes[source])
            if result is not None:
                row = normalize_record(result, source)
                row.update(id=canonical, _sources=[source], _source_ids=remotes)
                return row
        return None

    async def get_match_player_stats(self, match_id):
        _canonical, remotes = self.identity.remote_ids(match_id)
        pandascore_id = remotes.get("pandascore")
        if pandascore_id is None or "pandascore" not in self.enabled:
            return None
        return await self._call("pandascore", "get_match_player_stats", pandascore_id)

    async def get_match_games(self, match_id):
        _canonical, remotes = self.identity.remote_ids(match_id)
        pandascore_id = remotes.get("pandascore")
        if pandascore_id is None or "pandascore" not in self.enabled:
            return None
        return await self._call("pandascore", "get_match_games", pandascore_id)

    async def search_teams(self, query, per_page=20):
        results = await asyncio.gather(*(self._call(source, "search_teams", query, per_page=per_page) for source in self.enabled))
        teams = []
        for source, items in zip(self.enabled, results):
            for item in items or []:
                if not isinstance(item, dict) or item.get("id") is None:
                    continue
                team = normalize_team(item, source)
                teams.append(team)
                self._teams[team["id"]] = team
        return teams[:per_page]

    async def get_team(self, team_id):
        source = "hltv" if str(team_id).startswith("hltv:") else "pandascore"
        if source not in self.enabled:
            return self._teams.get(team_id)
        raw_id = int(str(team_id).split(":")[-1])
        team = await self._call(source, "get_team", raw_id)
        return normalize_team(team, source) if team else None

    async def fetch_bytes(self, url, max_bytes=2 * 1024 * 1024):
        source = "hltv" if "hltv.org" in str(url) and "hltv" in self.enabled else self.enabled[0]
        return await self._call(source, "fetch_bytes", url, max_bytes=max_bytes)

    def popular_teams(self):
        if "pandascore" in self.enabled:
            client = self._client("pandascore")
            if hasattr(client, "team_search"):
                result = client.team_search.popular()
                if isinstance(result, dict) and isinstance(result.get("teams"), list):
                    result = deepcopy(result)
                    result["teams"] = [normalize_team(team, "pandascore")
                                       for team in result["teams"]
                                       if isinstance(team, dict) and team.get("id") is not None]
                    for team in result["teams"]:
                        self._teams[team["id"]] = team
                return result
        return {"teams": list(self._teams.values()), "source": "hltv"}

    def is_followed(self, team):
        followed = self.store.get_followed_teams()
        name = normalized_name(team.get("name"))
        team_id = team.get("id")
        if team_id is not None:
            identity = self.identity.team_identity(team_id, team.get("_source"))
            for item in followed:
                if item.get("id") is None:
                    if name and normalized_name(item.get("name")) == name:
                        return True
                    continue
                followed_identity = self.identity.team_identity(item["id"])
                if identity == followed_identity:
                    return True
            return False
        return bool(name and any(item.get("id") is None and normalized_name(item.get("name")) == name
                                 for item in followed))

    def followed_teams_for_panel(self):
        output = []
        for team in self.store.get_followed_teams():
            item = deepcopy(team)
            origin = "hltv" if str(team.get("id", "")).startswith("hltv:") else "pandascore"
            active = [origin] if origin in self.enabled else []
            identity = self.identity.team_identity(team.get("id")) if team.get("id") is not None else None
            for source in self.enabled:
                linked = any(key.startswith(source + ":") and value == identity
                             for key, value in self.store.get("source_team_aliases", {}).items())
                if linked and source not in active:
                    active.append(source)
            item["_active_sources"] = active
            item["_source_link_pending"] = any(source not in active for source in self.enabled)
            output.append(item)
        return output

    async def check_sources(self):
        await asyncio.gather(*(self._call(source, "get_upcoming_matches", per_page=1) for source in self.enabled))
        return self.status()

    async def manual_verify(self):
        if "hltv" not in self.enabled:
            return {"ok": False, "msg": "请先启用 HLTV 数据源"}
        result = await self._call("hltv", "manual_verify")
        return result if isinstance(result, dict) else {"ok": False, "msg": "HLTV 人工验证未完成，请查看数据源状态"}

    async def close(self):
        self._closed = True
        await asyncio.gather(*(client.close() for client in self._clients.values()))
