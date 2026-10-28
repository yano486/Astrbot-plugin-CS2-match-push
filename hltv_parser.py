"""Offline HLTV selectors and PandaScore-shaped records (Python 3.12+).

Selector provenance and MIT notice: THIRD_PARTY_NOTICES.md. No optional parser
dependency is required; malformed/changed layouts fail rather than look empty.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

BASE = "https://www.hltv.org"
PAGE_HOSTS = frozenset({"www.hltv.org", "hltv.org"})
ASSET_HOSTS = PAGE_HOSTS | {"img-cdn.hltv.org"}


class ParseError(ValueError):
    """The response is not a recognizable, successful HLTV document."""


def allowed_url(url: str, *, asset: bool = False) -> bool:
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname in (ASSET_HOSTS if asset else PAGE_HOSTS)
                and parts.port in (None, 443) and parts.username is None and parts.password is None
                and not any(ord(c) <= 32 or c == "\\" for c in url))
    except (TypeError, ValueError):
        return False


def is_challenge(html: str) -> bool:
    sample = html[:100_000].lower()
    return any(marker in sample for marker in (
        "just a moment", "attention required", "sorry, you have been blocked",
        "/cdn-cgi/challenge-platform/", "cf-chl-", "challenge-form",
        "verify you are human", "verifying you are human",
    )) or ("cloudflare ray id" in sample and "access denied" in sample)


@dataclass
class _Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: _Node | None = field(default=None, repr=False)

    def has(self, cls):
        return cls in self.attrs.get("class", "").split()

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()

    def text(self):
        if self.tag in {"script", "style"}:
            return ""
        return re.sub(r"\s+", " ", " ".join(
            child.text() if isinstance(child, _Node) else child for child in self.children
        )).strip()


class _Document(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: v or "" for k, v in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self._VOID:
            if len(self.stack) > 200:
                raise ParseError("HTML nesting exceeds limit")
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _first(node, *classes):
    return next((n for n in node.walk() if any(n.has(c) for c in classes)), None) if node else None


def _all(node, *classes):
    return [n for n in node.walk() if any(n.has(c) for c in classes)] if node else []


def _txt(node):
    return node.text() if node else ""


def _ancestors(node):
    while node is not None:
        yield node
        node = node.parent


def _tree(html):
    if not isinstance(html, str) or not html.strip() or is_challenge(html):
        raise ParseError("HLTV verification or empty response")
    root = _Document(html).root
    title = " ".join(n.text() for n in root.walk() if n.tag == "title").lower()
    if any(term in title for term in ("not found", "access denied", "server error", "service unavailable", "error 50")):
        raise ParseError("HLTV error document")
    headings = " ".join(n.text() for n in root.walk() if n.tag in {"h1", "h2"}).lower()
    if any(term in headings for term in ("an error occurred", "page not found", "access denied", "service unavailable")):
        raise ParseError("HLTV error document")
    return root


def _id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value or "")):
        return None
    number = int(value)
    return number if number > 0 else None


def _path_id(url, kind):
    if not allowed_url(urljoin(BASE, url or "")):
        return None
    match = re.match(rf"/{kind}/([0-9]+)(?:/|$)", urlsplit(urljoin(BASE, url or "")).path)
    return _id(match.group(1)) if match else None


def _link(node, kind):
    return next((n for n in node.walk() if n.tag == "a" and _path_id(n.attrs.get("href"), kind)), None) if node else None


def _iso(value):
    number = _id(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _source(url):
    return {"_source": "hltv", "_source_url": url, "_stars": None}


def _image(node, name=None):
    candidates = []
    for img in node.walk() if node else []:
        if img.tag != "img":
            continue
        if name and (img.attrs.get("title") or img.attrs.get("alt") or "").casefold() != name.casefold():
            continue
        url = urljoin(BASE, img.attrs.get("data-cookieblock-src") or img.attrs.get("src") or "")
        if not allowed_url(url, asset=True) or url == BASE:
            continue
        rank = 0 if img.has("day-only") else 2 if img.has("night-only") else 1
        candidates.append((rank, url))
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def _team(team_id, name, image=None, url=None):
    return {"id": team_id, "name": name or "TBD", "image_url": image,
            **_source(url or (f"{BASE}/team/{team_id}/-" if team_id else None))}


def _event(event_id, name, url=None):
    return {"id": event_id, "name": name or None, "tier": None,
            **_source(url or (f"{BASE}/events/{event_id}/-" if event_id else None))}


def _stars(node):
    attr = next((n.attrs["data-stars"] for n in node.walk() if "data-stars" in n.attrs), None)
    if attr is not None and re.fullmatch(r"[0-5]", attr):
        return int(attr)
    rating = _first(node, "match-rating", "matchRating")
    if rating:
        return min(5, sum(n.has("fa-star") and not n.has("faded") for n in rating.walk()))
    return None


def _bo(text):
    match = re.search(r"\b(?:bo\s*|best of\s+)([1-9][0-9]?)\b", text, re.I)
    return int(match.group(1)) if match else None


def parse_upcoming_matches(html: str) -> list[dict]:
    root = _tree(html)
    now = datetime.now(timezone.utc)
    cards = []
    for node in _all(root, "match", "upcomingMatch"):
        parent = node.parent
        while parent and not (parent.has("match") or parent.has("upcomingMatch")):
            parent = parent.parent
        if parent is None:
            cards.append(node)
    if not cards:
        if _first(root, "upcomingMatchesWrapper"):
            return []
        raise ParseError("Missing HLTV upcoming match list")
    by_id = {}
    for card in cards:
        link = _link(card, "matches")
        if link is None:
            raise ParseError("Unrecognized HLTV match card")
        url = urljoin(BASE, link.attrs["href"])
        mid = _path_id(url, "matches")
        time_node = _first(card, "match-time", "matchTime")
        scheduled = _iso(time_node.attrs.get("data-unix")) if time_node else None
        upcoming_container = any(n.has("upcomingMatchesWrapper") or n.has("upcomingMatch") for n in _ancestors(card))
        # On the modern /matches page, live scoreboards replace .match-time;
        # .matchLive is a star widget, NOT a live marker (reference selectors).
        live = (card.has("liveMatch") or card.has("live-match")
                or (scheduled is not None and datetime.fromisoformat(scheduled) <= now)
                or (time_node is None and not upcoming_container))
        status = "running" if live else "not_started" if scheduled else "unknown"
        event_link = _link(card, "events")
        event_node = next((n for n in card.walk() if "data-event-id" in n.attrs), None)
        eid = _id(event_node.attrs["data-event-id"]) if event_node else None
        eid = eid or (_path_id(event_link.attrs["href"], "events") if event_link else None)
        headline = next((n.attrs["data-event-headline"] for n in card.walk() if n.attrs.get("data-event-headline")), "")
        event_name = headline or _txt(event_link) or _txt(_first(card, "matchEventName")) or _txt(event_node)
        names = [_txt(n) for n in _all(card, "match-teamname", "matchTeamName")][:2]
        links = [n for n in card.walk() if n.tag == "a" and _path_id(n.attrs.get("href"), "team")]
        teams = []
        for i in range(2):
            tid = _id(card.attrs.get(f"data-team{i + 1}") or card.attrs.get(f"data-team{i + 1}-id"))
            if tid is None and len(links) > i:
                tid = _path_id(links[i].attrs["href"], "team")
            name = names[i] if len(names) > i else "TBD"
            teams.append({"opponent": _team(tid, name, _image(card, name)), "type": "Team"})
        event = _event(eid, event_name, urljoin(BASE, event_link.attrs["href"]) if event_link else None)
        row = {"id": mid, "name": _txt(_first(card, "match-stage")) or f"{teams[0]['opponent']['name']} vs {teams[1]['opponent']['name']}",
               "scheduled_at": scheduled, "begin_at": None, "status": status,
               "number_of_games": _bo(_txt(_first(card, "match-meta", "matchMeta"))),
               "opponents": teams, "tournament": event, "league": dict(event),
               "results": [], "games": [], "winner": None, "streams_list": [],
               **_source(url), "_stars": _stars(card)}
        previous = by_id.get(mid)
        if previous is None or (previous["tournament"]["id"] is None and eid is not None):
            by_id[mid] = row
    return sorted(by_id.values(), key=lambda row: (row["status"] != "running", row["scheduled_at"] is None, row["scheduled_at"] or "", row["id"]))


def parse_events(html: str) -> list[dict]:
    root = _tree(html)
    cards = [n for n in _all(root, "big-event", "small-event") if n.tag == "a"]
    known_nodes = {id(n) for n in cards}
    for section in (n for n in root.walk() if n.attrs.get("id") == "FEATURED"):
        for card in section.walk():
            if card.tag == "a" and _path_id(card.attrs.get("href"), "events") and id(card) not in known_nodes:
                cards.append(card)
                known_nodes.add(id(card))
    if not cards:
        if _first(root, "events-month", "events-months"):
            return []
        raise ParseError("Missing HLTV event list")
    rows = {}
    for card in cards:
        url = urljoin(BASE, card.attrs.get("href", ""))
        eid = _path_id(url, "events")
        featured = card.has("big-event") or any(n.attrs.get("id") == "FEATURED" for n in _ancestors(card))
        name = _txt(_first(card, "big-event-name", "event-name", "eventname"))
        if not name and card.has("small-event"):
            name = _txt(_first(card, "text-ellipsis"))
        if not name and featured:
            name = next((n.attrs["title"].strip() for n in card.walk() if n.tag == "img" and n.attrs.get("title")), "")
        if not eid or not name:
            raise ParseError("Unrecognized HLTV event card")
        dates = [n.attrs["data-unix"] for n in card.walk() if "data-unix" in n.attrs]
        values = _all(_first(card, "additional-info"), "col-value")
        previous = rows.get(eid, {})
        row = {**_event(eid, name, url), "league": _event(eid, name, url), "_featured": bool(featured or previous.get("_featured")),
                     "begin_at": _iso(dates[0]) if dates else None,
                     "end_at": _iso(dates[-1]) if len(dates) > 1 else None,
                     "location": _txt(_first(card, "big-event-location", "smallCountry")) or None,
                     "prize_pool": _txt(values[1]) if len(values) > 1 else None,
                     "teams": []}
        rows[eid] = {key: value if value is not None else previous.get(key) for key, value in row.items()}
    return sorted(rows.values(), key=lambda row: (row["begin_at"] is None, row["begin_at"] or "", row["id"]))


def parse_team(html: str, team_id: int) -> dict:
    root = _tree(html)
    name = _txt(_first(root, "profile-team-name"))
    if not name or _id(team_id) is None:
        raise ParseError("Missing HLTV team profile")
    canonical = next((n.attrs.get("href") for n in root.walk() if n.tag == "link" and n.attrs.get("rel") == "canonical"), None)
    if canonical and _path_id(canonical, "team") != team_id:
        raise ParseError("Wrong HLTV team profile")
    return {**_team(team_id, name, _image(_first(root, "profile-team-logo-container")), canonical),
            "location": _txt(_first(root, "team-country")) or None}


def parse_search(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ParseError("Invalid HLTV typeahead response") from exc
    roots = data if isinstance(data, list) else [data]
    root = next((r for r in roots if isinstance(r, dict) and "teams" in r), None)
    if root is None or not isinstance(root["teams"], list):
        raise ParseError("Missing HLTV typeahead teams")
    rows = {}
    for item in root["teams"]:
        if not isinstance(item, dict) or not _id(item.get("id")) or not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ParseError("Unrecognized HLTV typeahead team")
        tid = _id(item["id"])
        logo = item.get("teamLogoDay") or item.get("teamLogoNight")
        rows[tid] = _team(tid, item["name"].strip(), logo if logo and allowed_url(logo, asset=True) else None)
    return list(rows.values())


def _score(node):
    text = _txt(node)
    return int(text) if re.fullmatch(r"[0-9]+", text) else None


def parse_match(html: str, url: str) -> dict:
    root = _tree(html)
    mid = _path_id(url, "matches")
    canonical = next((n.attrs.get("href") for n in root.walk() if n.tag == "link" and n.attrs.get("rel") == "canonical"), None)
    if canonical and _path_id(canonical, "matches") != mid:
        raise ParseError("Wrong HLTV match detail")
    boxes = [_first(root, "team1-gradient"), _first(root, "team2-gradient")]
    timing = _first(root, "timeAndEvent")
    if not mid or timing is None or any(box is None for box in boxes):
        raise ParseError("Missing HLTV match detail")
    teams, scores = [], []
    for box in boxes:
        link = _link(box, "team")
        tid = _path_id(link.attrs["href"], "team") if link else None
        name = _txt(_first(box, "teamName")) or "TBD"
        teams.append(_team(tid, name, _image(box, name)))
        scores.append(_score(_first(box, "won", "lost", "draw")))
    countdown = _txt(_first(timing, "countdown")).lower()
    if "cancel" in countdown or "deleted" in countdown:
        status = "canceled"
    elif "postpon" in countdown:
        status = "postponed"
    elif "match over" in countdown or "finished" in countdown:
        status = "finished"
    elif "live" in countdown:
        status = "running"
    else:
        status = "not_started" if _first(timing, "time", "date") else "unknown"
    veto = _txt(_first(_first(root, "veto-box"), "preformatted-text"))
    event_link = _link(_first(timing, "event"), "events")
    eid = _path_id(event_link.attrs["href"], "events") if event_link else None
    event = _event(eid, _txt(event_link), urljoin(BASE, event_link.attrs["href"]) if event_link else None)
    stamp = next((n.attrs["data-unix"] for n in timing.walk() if "data-unix" in n.attrs), None)
    games = []
    for position, holder in enumerate(_all(root, "mapholder"), 1):
        name = _txt(_first(holder, "mapname"))
        left, right = _first(holder, "results-left"), _first(holder, "results-right")
        ls, rs = _score(_first(left, "results-team-score")), _score(_first(right, "results-team-score"))
        stats = next((n for n in holder.walk() if n.tag == "a" and re.search(r"/mapstatsid/[0-9]+/", n.attrs.get("href", ""))), None)
        result = _first(holder, "results")
        # Live scores and even won/lost classes are not final without STATS.
        finished = bool(stats and result and result.has("played") and ls is not None and rs is not None
                        and ((left and left.has("won")) or (right and right.has("won"))))
        idx = 0 if left and left.has("won") else 1
        gw = {"id": teams[idx]["id"]} if finished and teams[idx]["id"] else None
        game_status = "finished" if finished else "not_played" if name.upper() in {"TBA", "DEFAULT", "", "-"} or ls is None else "running"
        games.append({"position": position, "status": game_status, "winner": gw, "name": name or None,
                      "forfeit": False, "results": [{"team_id": t["id"], "score": score} for t, score in zip(teams, (ls, rs)) if t["id"] and score is not None]})
    best_of = _bo(veto)
    # HLTV's BO1 headline is a round score; PandaScore's match results are
    # map wins. Keep the original round scores on the individual game only.
    if best_of == 1 and all(s is not None for s in scores) and max(scores) > 1:
        scores = [int(scores[0] > scores[1]), int(scores[1] > scores[0])] if status == "finished" else [0, 0]
    winner = None
    if status == "finished" and all(s is not None for s in scores) and scores[0] != scores[1]:
        team = teams[0 if scores[0] > scores[1] else 1]
        if team["id"]:
            winner = {"id": team["id"], "name": team["name"]}
    return {"id": mid, "name": veto.split("*", 1)[1].strip() if "*" in veto else "",
            "status": status, "scheduled_at": _iso(stamp), "begin_at": None,
            "number_of_games": best_of, "opponents": [{"opponent": t, "type": "Team"} for t in teams],
            "league": dict(event), "tournament": event, "winner": winner, "games": games,
            "results": [{"team_id": t["id"], "score": score} for t, score in zip(teams, scores) if t["id"] and score is not None],
            "draw": status == "finished" and scores[0] is not None and scores[0] == scores[1],
            "forfeit": "forfeit" in countdown or "walkover" in countdown,
            "streams_list": [], **_source(url), "_stars": _stars(root)}
