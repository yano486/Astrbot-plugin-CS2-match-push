"""Editable push-card layouts. Missing coordinates keep the current auto flow."""

from __future__ import annotations

from copy import deepcopy

from .config import ConfigError, integer, string

CARD_WIDTH = 640
COORD_MIN = -80
COORD_MAX_X = 720
COORD_MAX_Y = 4000
NOTE_MAX = 80

_ELEMENT = (
    ("kicker", "左上角 CS2 标签", "页眉"),
    ("title", "标题", "页眉"),
    ("context", "页眉右侧赛事信息", "页眉"),
    ("timestamp", "播报时间", "页眉"),
    ("left_logo", "左侧队标", "对阵"),
    ("right_logo", "右侧队标", "对阵"),
    ("left_name", "左侧队名", "对阵"),
    ("right_name", "右侧队名", "对阵"),
    ("center", "中间 VS / 比分", "对阵"),
    ("start_time", "开赛时间", "对阵"),
    ("timezone", "北京时间标注", "对阵"),
    ("round_marks", "各局胜负标记", "对阵"),
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

CARD_TYPES = {
    "daily": {
        "label": "赛程日报",
        "elements": (
            "kicker", "title", "timestamp", "event_name", "day_group",
            "match_time", "match_logos", "match_names", "match_vs", "match_bo",
            "extra_note", "footer_brand", "footer_tag",
        ),
    },
    "reminder": {
        "label": "赛前提醒",
        "elements": (
            "kicker", "title", "context", "left_logo", "right_logo",
            "left_name", "right_name", "center", "start_time", "timezone",
            "extra_note", "footer_brand", "footer_tag",
        ),
    },
    "result": {
        "label": "赛事结果",
        "elements": (
            "kicker", "title", "context", "left_logo", "right_logo",
            "left_name", "right_name", "center", "round_marks",
            "extra_note", "footer_brand", "footer_tag",
        ),
    },
    "reschedule": {
        "label": "赛程变更",
        "elements": (
            "kicker", "title", "context", "left_logo", "right_logo",
            "left_name", "right_name", "center", "start_time", "timezone",
            "extra_note", "footer_brand", "footer_tag",
        ),
    },
    "tournament": {
        "label": "赛事开幕",
        "elements": (
            "kicker", "title", "body", "team_gallery",
            "extra_note", "footer_brand", "footer_tag",
        ),
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
    return cards


def default_card(kind: str) -> dict:
    spec = CARD_TYPES[kind]
    return {
        "note": "",
        "elements": {
            name: {"visible": True, "x": None, "y": None}
            for name in spec["elements"]
        },
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
        "x": _coord(value.get("x"), f"{name}.x", COORD_MAX_X),
        "y": _coord(value.get("y"), f"{name}.y", COORD_MAX_Y),
    }


def validate_card_layout(value) -> dict:
    if value is None:
        return default_layout()
    if not isinstance(value, dict):
        raise ConfigError("card_layout must be an object")
    result = default_layout()
    for kind, incoming in value.items():
        if kind not in CARD_TYPES:
            raise ConfigError(f"unknown card type: {kind}")
        if not isinstance(incoming, dict):
            raise ConfigError(f"card_layout.{kind} must be an object")
        card = result[kind]
        if "note" in incoming:
            card["note"] = string(incoming.get("note") or "", f"card_layout.{kind}.note")
            if len(card["note"]) > NOTE_MAX:
                raise ConfigError(f"card_layout.{kind}.note is too long")
        elements = incoming.get("elements")
        if elements is None:
            elements = {key: value for key, value in incoming.items() if key != "note"}
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
