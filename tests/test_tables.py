"""Table parsing on the exact strings found in the PDF's text layer."""

from src.extract_tables import parse_flattened_rows, parse_key_value_row, parse_value, table_to_markdown

P14_ROWS = (
    "Off 0.27W 0.27W 0.29W Sleep 0.56W 0.57W 0.59W Idle—Display On 20.5W 20.6W 20.9W Power adapter, "
    "no load 0.10W 0.10W 0.12W Power adapter efficiency 91.1% 91.5% 91.9%"
)


def test_flattened_rows_split_into_labelled_rows():
    rows, leftover = parse_flattened_rows(P14_ROWS, 3)
    assert leftover == ""
    assert [r["label"] for r in rows] == ["Off", "Sleep", "Idle—Display On", "Power adapter, no load", "Power adapter efficiency"]
    assert [c["raw"] for c in rows[3]["cells"]] == ["0.10W", "0.10W", "0.12W"]


def test_unparsable_text_is_reported_not_dropped():
    rows, leftover = parse_flattened_rows("Off 0.27W 0.27W 0.29W stray words", 3)
    assert len(rows) == 1 and leftover == "stray words"


def test_values_keep_units_qualifiers_and_direction():
    assert parse_value("346 kg CO2e") == {"raw": "346 kg CO2e", "value": 346.0, "unit": "kg CO2e"}
    assert parse_value("<1%")["qualifier"] == "<"
    assert parse_value("↓>20%") == {"raw": "↓>20%", "value": 20.0, "unit": "%", "qualifier": ">", "direction": "decrease"}
    assert parse_value("–99 kg")["value"] == -99
    assert "value" not in parse_value("n/a")


def test_key_value_rows():
    row = parse_key_value_row("• Production 50%")
    assert row == {"label": "Production", "cells": [{"raw": "50%", "value": 50.0, "unit": "%"}], "indent": 1}
    assert parse_key_value_row("Apple emissions from utility-purchased electricity (scope 2) 0 kg CO2e")["cells"][0]["value"] == 0
    assert parse_key_value_row("We model different configurations of our products") is None


def test_markdown_keeps_header_with_rows():
    rows, _ = parse_flattened_rows(P14_ROWS, 3)
    md = table_to_markdown({"title": "Power consumption for iMac", "columns": ["Mode", "100V", "115V", "230V"], "rows": rows, "notes": []})
    assert md.splitlines()[1] == "| Mode | 100V | 115V | 230V |"
    assert "| Sleep | 0.56W | 0.57W | 0.59W |" in md
