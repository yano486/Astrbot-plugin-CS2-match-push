"""Conservative source associations and durable notification identities."""

from copy import deepcopy
from datetime import datetime, timezone
import unicodedata


def normalized_name(value):
    return "".join(c for c in unicodedata.normalize("NFKC", str(value or "")).casefold() if c.isalnum())


def remote_key(source, value):
    return f"{source}:{value}"


def qualified_id(source, value):
    return int(value) if source == "pandascore" else f"hltv:{int(value)}"


def timestamp(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def event_name(row):
    values = [str((row.get(field) or {}).get("name") or "") for field in ("league", "serie", "tournament")]
    unique = []
    for value in values:
        name = normalized_name(value)
        if name and not any(name in item for item in unique):
            unique = [item for item in unique if item not in name]
            unique.append(name)
    return "".join(unique)


def team_names(row):
    return tuple(sorted(normalized_name((op.get("opponent") or {}).get("name"))
                        for op in (row.get("opponents") or [])[:2]))


def same_match(left, right):
    names = team_names(left)
    if len(names) != 2 or any(not n or n in {"tbd", "tba", "unknown"} for n in names):
        return False
    if names != team_names(right) or not event_name(left) or event_name(left) != event_name(right):
        return False
    a, b = timestamp(left.get("scheduled_at") or left.get("begin_at")), timestamp(right.get("scheduled_at") or right.get("begin_at"))
    if a is None or b is None or abs(a - b) > 15 * 60:
        return False
    bo1, bo2 = left.get("number_of_games"), right.get("number_of_games")
    return not (bo1 and bo2 and bo1 != bo2)


def same_tournament(left, right):
    a, b = timestamp(left.get("begin_at")), timestamp(right.get("begin_at"))
    return (bool(normalized_name(left.get("name")))
            and normalized_name(left.get("name")) == normalized_name(right.get("name"))
            and a is not None and b is not None and abs(a - b) <= 24 * 3600)


class SourceIdentity:
    def __init__(self, store):
        self.store = store

    def merge(self, current, primary="pandascore", kind="match", *, extra_updates=None):
        aliases_key = f"source_{kind}_aliases"
        catalog_key = f"source_{kind}_catalog"
        aliases = deepcopy(self.store.get(aliases_key, {}))
        preexisting = list(aliases)
        catalog = deepcopy(self.store.get(catalog_key, {}))
        for row in current:
            key = remote_key(row["_source"], row["_remote_id"])
            catalog[key] = deepcopy(row)
            aliases.setdefault(key, row["id"])
        same = same_match if kind == "match" else same_tournament
        current_keys = {remote_key(row["_source"], row["_remote_id"]) for row in current}
        snapshot_rows = self.store.get(f"source_{kind}_snapshots", {})
        comparison_keys = set(current_keys)
        for rows in snapshot_rows.values():
            comparison_keys.update(remote_key(row["_source"], row["_remote_id"]) for row in rows)

        # Repair legacy/corrupt many-to-one aliases before considering new links.
        occupied = {}
        for key, canonical in list(aliases.items()):
            source = key.split(":", 1)[0]
            marker = (canonical, source)
            if marker in occupied and occupied[marker] != key and key in catalog:
                aliases[key] = catalog[key]["id"]
            else:
                occupied[marker] = key

        ambiguous = set()
        pairs = []
        for key in current_keys:
            row = catalog[key]
            canonical = aliases[key]
            candidates = [other for other, item in catalog.items()
                          if item["_source"] != row["_source"]
                          and (other in comparison_keys or aliases.get(other) == canonical)
                          and same(row, item)]
            if len(candidates) == 1:
                other = candidates[0]
                reverse = [name for name, item in catalog.items()
                           if item["_source"] != catalog[other]["_source"]
                           and (name in comparison_keys or aliases.get(name) == aliases.get(other))
                           and same(catalog[other], item)]
                if len(reverse) == 1:
                    pair = tuple(sorted((key, other)))
                    if pair not in pairs:
                        pairs.append(pair)
                    continue
            if candidates:
                ambiguous.update([key, *candidates])
        coalesces = []
        confirmed_pairs = []
        for left, right in pairs:
            left_id, right_id = aliases[left], aliases[right]
            former = {left_id, right_id}
            affected = [key for key, value in aliases.items() if value in former]
            by_source = {}
            for key in affected:
                by_source.setdefault(key.split(":", 1)[0], set()).add(key)
            if any(len(keys) > 1 for keys in by_source.values()):
                ambiguous.update((left, right))
                continue

            old_keys = [key for key in preexisting if aliases.get(key) in former]
            if old_keys:
                canonical = aliases[old_keys[0]]
            else:
                preferred = next((key for key in (left, right) if key.startswith("pandascore:")), left)
                canonical = aliases[preferred]
            for key, value in list(aliases.items()):
                if value in former:
                    aliases[key] = canonical
            coalesces.append((canonical, former, kind))
            confirmed_pairs.append((left, right))

        team_aliases = deepcopy(self.store.get("source_team_aliases", {}))
        old_team_keys = list(team_aliases)
        for left, right in confirmed_pairs:
            left_row, right_row = catalog[left], catalog[right]
            if kind != "match":
                continue
            def teams_by_name(match):
                output = {}
                for opponent in match.get("opponents") or []:
                    team = (opponent.get("opponent") or {}) if isinstance(opponent, dict) else {}
                    name = normalized_name(team.get("name"))
                    if name and team.get("id") is not None and team.get("_remote_id") is not None:
                        output[name] = team
                return output

            left_teams, right_teams = teams_by_name(left_row), teams_by_name(right_row)
            for name in left_teams.keys() & right_teams.keys():
                left_team, right_team = left_teams[name], right_teams[name]
                left_key = remote_key(left_row["_source"], left_team["_remote_id"])
                right_key = remote_key(right_row["_source"], right_team["_remote_id"])
                if left_key == right_key:
                    continue
                left_id = team_aliases.setdefault(left_key, left_team["id"])
                right_id = team_aliases.setdefault(right_key, right_team["id"])
                former = {left_id, right_id}
                members = [key for key, value in team_aliases.items() if value in former]
                by_source = {}
                for key in members:
                    by_source.setdefault(key.split(":", 1)[0], set()).add(key)
                if any(len(keys) > 1 for keys in by_source.values()):
                    team_aliases[left_key] = left_team["id"]
                    team_aliases[right_key] = right_team["id"]
                    continue
                old_keys = [key for key in old_team_keys if team_aliases.get(key) in former]
                if old_keys:
                    canonical_team = team_aliases[old_keys[0]]
                else:
                    canonical_team = left_team["id"] if left_key.startswith("pandascore:") else right_team["id"]
                for key, value in list(team_aliases.items()):
                    if value in former:
                        team_aliases[key] = canonical_team
        # A bounded recent catalog supports a source switch without unbounded state growth.
        if len(catalog) > 2000:
            active = {remote_key(row["_source"], row["_remote_id"]) for row in current}
            keep = [key for key in catalog if key not in active][-max(0, 2000 - len(active)):]
            catalog = {key: catalog[key] for key in (*keep, *active)}
        updates = {aliases_key: aliases, catalog_key: catalog, **(extra_updates or {})}
        if kind == "match":
            updates["source_team_aliases"] = team_aliases
        self.store.save_source_state(updates, coalesces=coalesces)
        grouped = {}
        for row in current:
            key = remote_key(row["_source"], row["_remote_id"])
            grouped.setdefault(aliases[key], []).append(row)
        result = []
        for canonical, rows in grouped.items():
            chosen = next((row for row in rows if row["_source"] == primary), rows[0])
            item = deepcopy(chosen)
            item["id"] = canonical
            present = {row["_source"] for row in rows}
            item["_sources"] = [source for source in ("pandascore", "hltv") if source in present]
            item["_source_ids"] = {row["_source"]: row["_remote_id"] for row in rows}
            item["_source_conflicts"] = [field for field in ("scheduled_at", "begin_at", "status", "winner")
                                         if len({str(row.get(field)) for row in rows if row.get(field) is not None}) > 1]
            item["_possible_duplicate"] = any(remote_key(row["_source"], row["_remote_id"]) in ambiguous for row in rows)
            ps_row = next((row for row in rows if row["_source"] == "pandascore"), None)
            if ps_row:
                item["_pandascore_tier"] = (ps_row.get("tournament") or {}).get("tier")
            hl_row = next((row for row in rows if row["_source"] == "hltv"), None)
            if hl_row:
                item["_hltv_stars"] = hl_row.get("_stars")
                if kind == "tournament":
                    item["_hltv_featured"] = bool(hl_row.get("_featured"))
            for key, value in aliases.items():
                if value == canonical and key in catalog:
                    item["_source_ids"].setdefault(catalog[key]["_source"], catalog[key]["_remote_id"])
            result.append(item)
        return result

    def remote_ids(self, value, kind="match"):
        aliases = self.store.get(f"source_{kind}_aliases", {})
        if isinstance(value, str) and value.startswith("hltv:"):
            source_key = f"hltv:{int(value.split(':', 1)[1])}"
        else:
            source_key = f"pandascore:{int(value)}"
        canonical = aliases.get(source_key, aliases.get(str(value), value))
        result = {}
        for key, target in aliases.items():
            if target == canonical:
                source, remote = key.split(":", 1)
                result[source] = int(remote)
        if not result:
            if isinstance(value, str) and value.startswith("hltv:"):
                result["hltv"] = int(value.split(":", 1)[1])
            else:
                result["pandascore"] = int(value)
        return canonical, result

    def team_identity(self, value, source=None):
        aliases = self.store.get("source_team_aliases", {})
        if isinstance(value, str) and value.startswith("hltv:"):
            key = f"hltv:{int(value.split(':', 1)[1])}"
        elif source in {"pandascore", "hltv"}:
            raw = str(value).split(":", 1)[-1]
            key = remote_key(source, raw)
        else:
            key = f"pandascore:{int(value)}"
        return aliases.get(key, value)
