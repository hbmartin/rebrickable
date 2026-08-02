from __future__ import annotations

import os
from pathlib import Path

import pytest

from rebrickable import Bom, ColorRef, Config, PartRef
from rebrickable.bom import BomItem
from rebrickable.errors import ConfigLoadError
from rebrickable.exports import python_lookup_snippet
from rebrickable.urls import minifig_url, part_url, set_url, theme_url


def test_config_defaults_missing_and_secret_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yml"
    monkeypatch.setenv("REBRICKABLE_API_KEY", "environment-secret")
    config = Config.load(path)
    assert config.api_key == "environment-secret"
    assert "environment-secret" not in repr(config)
    config.write(path)
    assert "api_key" not in path.read_text()
    config.write(path, include_secrets=True)
    assert "environment-secret" in path.read_text()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "payload", ["[]", "unknown: true", "base_url: http://unsafe.test"]
)
def test_invalid_config_raises(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "config.yml"
    path.write_text(payload)
    with pytest.raises(ConfigLoadError):
        Config.load(path)


def test_url_builders_quote_identifiers() -> None:
    assert part_url("a/b c").endswith("/a%2Fb%20c/")
    assert set_url("100-1").endswith("/100-1/")
    assert minifig_url("fig-1").endswith("/fig-1/")
    assert theme_url(1).endswith("/1/")


def test_bom_csv_normalization_diff_and_export() -> None:
    bom = Bom.from_csv("Part,Color,Quantity\n3001,4,2\n3001,4,3\n")
    assert bom.total_quantity == 5
    assert bom.unique_count == 1
    assert bom.duplicate_rows[0].occurrences == 2
    other = Bom.normalize([BomItem(PartRef("ldraw", "3001"), ColorRef("ldraw", 4), 7)])
    assert bom.diff(other).rows[0].delta == 2

    assert PartRef("ldraw", " 3001B.DAT ") == PartRef("ldraw", "3001b")
    assert PartRef("rebrickable", "3001B").value == "3001B"
    assert ColorRef("ldraw", "4") == ColorRef("ldraw", 4)
    with pytest.raises(ValueError, match="integer"):
        ColorRef("ldraw", "x")
    merged = Bom.from_csv(
        "Part,Color,Quantity\n3001B.dat,4,2\n3001b,4,3\n 3001B ,4,1\n"
    )
    assert merged.unique_count == 1
    assert merged.total_quantity == 6
    assert bom.diff(Bom.from_csv("Part,Color,Quantity\n3001.DAT,4,5\n")).rows == ()
    rebrickable_bom = Bom.normalize(
        [BomItem(PartRef("rebrickable", "3001"), ColorRef("rebrickable", 4), 1)],
    )
    assert (
        rebrickable_bom.to_rebrickable_csv()
        == "part_num,color_id,quantity\r\n3001,4,1\r\n"
    )


def test_bom_xml_and_invalid_rows() -> None:
    bom = Bom.from_rebrickable_xml(
        "<INVENTORY><ITEM><ITEMID>3001</ITEMID><COLOR>4</COLOR><MINQTY>2</MINQTY></ITEM></INVENTORY>"
    )
    assert bom.items[0].quantity == 2
    with pytest.raises(ValueError):
        Bom.from_csv("bad,header\n1,2")
    with pytest.raises(ValueError):
        Bom.from_csv("Part,Color,Quantity\n3001,4,0\n")
    with pytest.raises(ValueError, match="no header"):
        Bom.from_csv("")
    with pytest.raises(ValueError, match="lacks"):
        Bom.from_rebrickable_xml(
            "<INVENTORY><ITEM><ITEMID>3001</ITEMID></ITEM></INVENTORY>"
        )


def test_rebrickable_csv_parser_and_aliases() -> None:
    inventory = Bom.from_rebrickable_csv(
        "part_num,color_id,quantity,is_spare\n3001,4,2,False\n"
    )
    assert inventory.items == (
        BomItem(
            PartRef("rebrickable", "3001"),
            ColorRef("rebrickable", 4),
            2,
        ),
    )
    aliased = Bom.from_rebrickable_csv("Part,Color,Qty\n3002,1,3\n")
    assert aliased.total_quantity == 3
    with pytest.raises(ValueError, match="requires"):
        Bom.from_rebrickable_csv("part_num,quantity\n3001,2\n")
    with pytest.raises(ValueError, match="row 2"):
        Bom.from_rebrickable_csv("part_num,color_id,quantity\n3001,4,nope\n")
    with pytest.raises(ValueError, match="no header"):
        Bom.from_rebrickable_csv("")


def test_bom_missing_file_and_inline_content_detection(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        Bom.from_csv("missing_bom.csv")
    with pytest.raises(FileNotFoundError, match="not found"):
        Bom.from_csv(str(tmp_path / "nope.csv"))
    with pytest.raises(FileNotFoundError, match="not found"):
        Bom.from_rebrickable_xml("missing_inventory.xml")
    header_only = Bom.from_csv("Part,Color,Quantity")
    assert header_only.items == ()


def test_bom_file_sources_namespace_guard_and_python_snippet(tmp_path: Path) -> None:
    csv_path = tmp_path / "bom.csv"
    csv_path.write_text("Part,Color,Quantity\n3001,4,2\n")
    assert Bom.from_csv(csv_path).total_quantity == 2
    xml_path = tmp_path / "bom.xml"
    xml_path.write_text(
        "<INVENTORY><ITEM><PART>3001</PART><COLOR>4</COLOR><QTY>2</QTY></ITEM></INVENTORY>"
    )
    assert Bom.from_rebrickable_xml(xml_path).total_quantity == 2
    with pytest.raises(ValueError, match="Rebrickable identifiers"):
        Bom.from_csv(csv_path).to_rebrickable_csv()
    snippet = python_lookup_snippet("part", "3001")
    compile(snippet, "<snippet>", "exec")
    with pytest.raises(ValueError, match="kind"):
        python_lookup_snippet("color", "4")
