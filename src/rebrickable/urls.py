"""Pure public Rebrickable page URL builders."""

from urllib.parse import quote

_WEB_BASE = "https://rebrickable.com"


def _entity_url(kind: str, identifier: str | int) -> str:
    return f"{_WEB_BASE}/{kind}/{quote(str(identifier), safe='')}/"


def part_url(part_num: str) -> str:
    return _entity_url("parts", part_num)


def set_url(set_num: str) -> str:
    return _entity_url("sets", set_num)


def minifig_url(fig_num: str) -> str:
    return _entity_url("minifigs", fig_num)


def theme_url(theme_id: int) -> str:
    return _entity_url("themes", theme_id)
