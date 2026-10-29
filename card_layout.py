"""Editable push-card layouts. Missing coordinates keep the current auto flow."""

from __future__ import annotations

from copy import deepcopy

from .config import ConfigError, integer, string

CARD_WIDTH = 640
MIN_WIDTH = 480
MAX_WIDTH = 1600
MIN_HEIGHT = 0
MAX_HEIGHT = 4000
COORD_MIN = -80
NOTE_MAX = 80
BADGE_MAX = 24
CUSTOM_TEXT_MAX = 40
MAX_CUSTOMS = 8

_ELEMENT = (
    ("kicker", "左上角 CS2 标签", "页眉"),
    ("title", "标题", "页眉"),
    ("context", "页眉右侧赛事信息", "页眉"),
    ("timestamp", "播报时间", "页眉"),
    ("extra_badge", "自定义角标", "页眉"),
    ("left_logo", "左侧队标", "对阵"),
    ("right_logo", "右侧队标", "对阵"),
    ("left_name", "左侧队名", "对阵"),
    ("right_name", "右侧队名", "对阵"),
    ("center", "中间 VS / 比分", "对阵"),
    ("start_time", "开赛时间", "对阵"),
    ("timezone", "北京时间标注", "对阵"),
    ("round_marks", "各局胜负标记", "对阵"),
    ("player_stats", "选手数据总表", "正文"),
    ("left_players", "左侧选手 K-D / Rating", "正文"),
    ("right_players", "右侧选手 K-D / Rating", "正文"),
    ("series_score", "系列赛比分", "正文"),
    ("map_board", "地图比分", "正文"),
    ("stage_meta", "赛事阶段 / BO", "正文"),
    ("event_name", "赛事名称", "正文"),
    ("day_group", "今天/明天分组", "正文"),
    ("match_time", "日报开赛时间", "正文"),
    ("match_logos", "日报对阵队标", "正文"),
    ("match_names", "日报对阵队名", "正文"),
    ("match_vs", "日报 VS", "正文"),
    ("match_bo", "日报赛制", "正文"),
    ("body", "说明文字", "正文"),
    ("team_gallery", "参赛战队图标", "正文"),
    ("extra_note", "自定义说明", "页脚"),
    ("footer_brand", "左下角品牌", "页脚"),
    ("footer_tag", "右下角标签", "页脚"),
)

_BY_ID = {item[0]: item for item in _ELEMENT}
_SKIP_KEYS = {"note", "badge", "elements", "width", "height", "customs"}
_BASE = ("kicker", "title", "extra_badge", "extra_note", "footer_brand", "footer_tag")

CARD_TYPES = {
    "daily": {
        "label": "赛程日报",
        "elements": _BASE + (
            "timestamp", "event_name", "day_group",
            "match_time", "match_logos", "match_names", "match_vs", "match_bo",
        ),
    },
    "reminder": {
        "label": "赛前提醒",
        "elements": _BASE + (
            "context", "left_logo", "right_logo", "left_name", "right_name",
            "center", "start_time", "timezone",
        ),
    },
    "result": {
        "label": "赛事结果",
        "elements": _BASE + (
            "context", "left_logo", "right_logo", "left_name", "right_name",
            "center", "round_marks", "stage_meta", "series_score", "map_board", "player_stats", "left_players", "right_players",
        ),
    },
    "reschedule": {
        "label": "赛程变更",
        "elements": _BASE + (
            "context", "left_logo", "right_logo", "left_name", "right_name",
            "center", "start_time", "timezone",
        ),
    },
    "tournament": {
        "label": "赛事开幕",
        "elements": _BASE + ("body", "team_gallery"),
    },
}


def detect_card_type(title: str) -> str:
    title = title or ""
    if "赛程日报" in title or "每日赛程" in title:
        return "daily"
    if "赛事结果" in title:
        return "result"
    if "赛程变更" in title:
        return "reschedule"
    if "赛事开幕" in title or "参赛提醒" in title:
        return "tournament"
    return "reminder"


def catalog() -> dict:
    cards = {}
    for kind, spec in CARD_TYPES.items():
        cards[kind] = {
            "label": spec["label"],
            "elements": [
                {"id": name, "label": _BY_ID[name][1], "group": _BY_ID[name][2]}
                for name in spec["elements"]
            ],
        }
    return {
        "cards": cards,
        "canvas": {
            "min_width": MIN_WIDTH,
            "max_width": MAX_WIDTH,
            "min_height": MIN_HEIGHT,
            "max_height": MAX_HEIGHT,
            "default_width": CARD_WIDTH,
            "max_customs": MAX_CUSTOMS,
        },
    }


_DEFAULT_OFF = {
    "extra_badge", "player_stats", "left_players", "right_players",
    "series_score", "map_board", "stage_meta",
}

def default_card(kind: str) -> dict:
    spec = CARD_TYPES[kind]
    return {
        "note": "",
        "badge": "",
        "width": CARD_WIDTH,
        "height": 0,
        "elements": {
            name: {"visible": name not in _DEFAULT_OFF, "x": None, "y": None}
            for name in spec["elements"]
        },
        "customs": [],
    }


def default_layout() -> dict:
    return {kind: default_card(kind) for kind in CARD_TYPES}


def _coord(value, name: str, maximum: int):
    if value is None:
        return None
    return integer(value, name, COORD_MIN, maximum)


def _element_state(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object")
    visible = value.get("visible", True)
    if not isinstance(visible, bool):
        raise ConfigError(f"{name}.visible must be a boolean")
    return {
        "visible": visible,
        "x": _coord(value.get("x"), f"{name}.x", MAX_WIDTH + 80),
        "y": _coord(value.get("y"), f"{name}.y", MAX_HEIGHT),
    }


def _customs(value, name: str) -> list[dict]:
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a list")
    if len(value) > MAX_CUSTOMS:
        raise ConfigError(f"{name} supports at most {MAX_CUSTOMS} items")
    result = []
    used = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError(f"{name}[{index}] must be an object")
        ident = string(item.get("id") or f"custom_{index + 1}", f"{name}[{index}].id", nonempty=True)
        if not ident.startswith("custom_") or ident in used:
            raise ConfigError(f"{name}[{index}].id is invalid")
        used.add(ident)
        text = string(item.get("text") or "", f"{name}[{index}].text")
        if len(text) > CUSTOM_TEXT_MAX:
            raise ConfigError(f"{name}[{index}].text is too long")
        visible = item.get("visible", True)
        if not isinstance(visible, bool):
            raise ConfigError(f"{name}[{index}].visible must be a boolean")
        result.append({
            "id": ident,
            "text": text,
            "visible": visible,
            "x": _coord(item.get("x"), f"{name}[{index}].x", MAX_WIDTH + 80),
            "y": _coord(item.get("y"), f"{name}[{index}].y", MAX_HEIGHT),
        })
    return result


def validate_card_layout(value) -> dict:
    if value is None:
        return default_layout()
    if not isinstance(value, dict):
        raise ConfigError("card_layout must be an object")
    incoming_cards = value.get("cards") if isinstance(value.get("cards"), dict) else value
    result = default_layout()
    for kind, incoming in incoming_cards.items():
        if kind in {"cards", "canvas"}:
            continue
        if kind not in CARD_TYPES:
            raise ConfigError(f"unknown card type: {kind}")
        if not isinstance(incoming, dict):
            raise ConfigError(f"card_layout.{kind} must be an object")
        card = result[kind]
        if "note" in incoming:
            card["note"] = string(incoming.get("note") or "", f"card_layout.{kind}.note")
            if len(card["note"]) > NOTE_MAX:
                raise ConfigError(f"card_layout.{kind}.note is too long")
        if "badge" in incoming:
            card["badge"] = string(incoming.get("badge") or "", f"card_layout.{kind}.badge")
            if len(card["badge"]) > BADGE_MAX:
                raise ConfigError(f"card_layout.{kind}.badge is too long")
        if "width" in incoming:
            card["width"] = integer(incoming.get("width") or CARD_WIDTH, f"card_layout.{kind}.width", MIN_WIDTH, MAX_WIDTH)
        if "height" in incoming:
            card["height"] = integer(incoming.get("height") or 0, f"card_layout.{kind}.height", MIN_HEIGHT, MAX_HEIGHT)
        if "customs" in incoming:
            card["customs"] = _customs(incoming.get("customs"), f"card_layout.{kind}.customs")
        elements = incoming.get("elements")
        if elements is None:
            elements = {key: item for key, item in incoming.items() if key not in _SKIP_KEYS}
        if not isinstance(elements, dict):
            raise ConfigError(f"card_layout.{kind}.elements must be an object")
        allowed = set(CARD_TYPES[kind]["elements"])
        for name, state in elements.items():
            if name not in allowed:
                raise ConfigError(f"unknown element {name} for {kind}")
            card["elements"][name] = _element_state(state, f"card_layout.{kind}.{name}")
    return result


def resolve_card_layout(saved, title: str) -> dict:
    kind = detect_card_type(title)
    merged = validate_card_layout(saved)
    spec = deepcopy(merged[kind])
    spec["type"] = kind
    return spec


def sample_notification(kind: str) -> dict:
    if kind not in CARD_TYPES:
        raise ConfigError("unknown card type")
    teams = [
        {"name": "Vitality", "image": None},
        {"name": "MOUZ", "image": None},
        {"name": "Nemiga", "image": None},
        {"name": "FURIA", "image": None},
        {"name": "MIBR", "image": None},
        {"name": "Eternal Fire", "image": None},
    ]
    if kind == "daily":
        return {
            "text": (
                "【CS2 赛程日报】10月30日 05:10 播报\n"
                "今天（共 2 场）\n"
                "20:00 Nemiga vs Vitality BO3 · PGL Masters\n"
                "23:00 MOUZ vs FURIA BO3 · PGL Masters\n"
                "今天（共 1 场）\n"
                "21:00 MIBR vs Eternal Fire BO3 · Stake Ranked"
            ),
            "team_marks": teams,
        }
    if kind == "result":
        return {
            "text": "【CS2 赛事结果】\nPGL Masters\n胜者：Vitality",
            "team_names": ["Vitality", "MOUZ"],
            "center_text": "2 : 1",
            "team_marks": teams[:2],
            "round_winners": [0, 1, 0],
            "winner_index": 0,
            "match_detail": {
                "name": "Quarter-final",
                "number_of_games": 3,
                "tournament": {"name": "PGL Masters Bucharest 2026"},
                "results": [{"team_id": 1, "score": 2}, {"team_id": 2, "score": 1}],
                "opponents": [
                    {"opponent": {"id": 1, "name": "Vitality"}},
                    {"opponent": {"id": 2, "name": "MOUZ"}},
                ],
                "games": [
                    {"position": 1, "status": "finished", "map": {"name": "Nuke"},
                     "results": [{"team_id": 1, "score": 13}, {"team_id": 2, "score": 11}], "winner": {"id": 1}},
                    {"position": 2, "status": "finished", "map": {"name": "Dust2"},
                     "results": [{"team_id": 1, "score": 9}, {"team_id": 2, "score": 13}], "winner": {"id": 2}},
                    {"position": 3, "status": "finished", "map": {"name": "Ancient"},
                     "results": [{"team_id": 1, "score": 16}, {"team_id": 2, "score": 12}], "winner": {"id": 1}},
                ],
            },
            "player_stats": {
                "teams": [
                    {"name": "Vitality", "players": [
                        {"name": "ZywOo", "stats": {"counts": {"kills": 48, "deaths": 30}, "per_game_averages": {"adr": 92.1, "kast": 81.2, "hltv_game_rating": 1.42}}},
                        {"name": "apEX", "stats": {"counts": {"kills": 31, "deaths": 36}, "per_game_averages": {"adr": 68.4, "kast": 72.0, "hltv_game_rating": 0.98}}},
                    ]},
                    {"name": "MOUZ", "players": [
                        {"name": "xertioN", "stats": {"counts": {"kills": 40, "deaths": 34}, "per_game_averages": {"adr": 84.0, "kast": 74.5, "hltv_game_rating": 1.18}}},
                        {"name": "siuhy", "stats": {"counts": {"kills": 28, "deaths": 38}, "per_game_averages": {"adr": 61.2, "kast": 69.1, "hltv_game_rating": 0.87}}},
                    ]},
                ]
            },
        }
    if kind == "reschedule":
        return {
            "text": "【CS2 赛程变更】\nPGL Masters\n原定：20:00\n调整：21:30",
            "team_names": ["Vitality", "MOUZ"],
            "center_text": "VS",
            "start_time": "21:30",
            "team_marks": teams[:2],
        }
    if kind == "tournament":
        return {
            "text": (
                "【CS2 赛事开幕通知】\n"
                "赛事名称：PGL Masters\n"
                "参赛战队：Vitality、MOUZ、FURIA、The MongolZ"
            ),
            "team_marks": teams,
        }
    return {
        "text": "【CS2 赛前提醒】\nPGL Masters",
        "team_names": ["Vitality", "MOUZ"],
        "center_text": "VS",
        "start_time": "20:00",
        "team_marks": teams[:2],
    }
