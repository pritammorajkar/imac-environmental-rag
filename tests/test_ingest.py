"""Extraction checks against the real report. Expected values were read from the PDF."""

import re


def _one(elements, **match):
    found = [e for e in elements if all(e.get(k) == v for k, v in match.items())]
    assert len(found) == 1, f"expected one element matching {match}, got {len(found)}"
    return found[0]


def _cell(table, row_label, column):
    row = next(r for r in table["rows"] if r["label"] == row_label)
    return row["cells"][table["columns"].index(column) - 1]


def test_report_basics(built):
    report = built["report"]
    assert report["page_count"] == 15
    assert report["failures"] == []
    assert report["counts"]["tables"] == 3
    assert report["counts"]["endnotes"] == 19
    assert report["counts"]["figures"] == {"chart": 1, "infographic": 1, "missing-graphic": 1, "image": 7}


def test_endnotes_parsed_in_sequence(elements):
    notes = sorted((e for e in elements if e["type"] == "footnote"), key=lambda e: e["number"])
    assert [n["number"] for n in notes] == list(range(1, 20))
    assert notes[13]["text"] == "Recycled copper in the thermal module applies to iMac (four ports) only."
    assert notes[10]["text"] == "Renewable energy emissions are too small to be visible on the chart."
    assert notes[0]["text"].endswith("not including packaging or in-box accessories.")
    assert "1) No use of clean electricity" in notes[6]["text"]  # numbered list inside a note is not a new note


def test_every_endnote_is_linked_to_where_it_is_cited(elements, by_id):
    for note in (e for e in elements if e["type"] == "footnote"):
        assert note["referenced_by"], f"endnote {note['number']} has no back-reference"
    fn14 = by_id["fn-14"]["referenced_by"]
    assert [r["page"] for r in fn14] == [5]
    assert "thermal module components" in fn14[0]["context"]
    assert {r["element_id"] for r in by_id["fn-11"]["referenced_by"]} == {"figure-1"}
    assert by_id["fn-6"]["referenced_by"][0]["element_id"] is None  # orphan marker on page 2


def test_power_consumption_table(elements, by_id):
    table = _one(elements, type="table", title="Power consumption for iMac")
    assert table["pages"] == [14]
    assert table["columns"] == ["Mode", "100V", "115V", "230V"]
    assert [r["label"] for r in table["rows"]] == [
        "Off", "Sleep", "Idle—Display On", "Power adapter, no load", "Power adapter efficiency",
    ]
    assert _cell(table, "Sleep", "115V") == {"raw": "0.57W", "value": 0.57, "unit": "W"}
    assert _cell(table, "Power adapter efficiency", "230V")["value"] == 91.9
    assert table["related_footnotes"] == [18]
    assert table["id"] in by_id["fn-18"]["related_elements"]


def test_greenhouse_gas_table(elements):
    table = _one(elements, type="table", title="Greenhouse gas emissions — iMac (two ports) 256GB")
    rows = {r["label"]: r for r in table["rows"]}
    assert rows["Total product footprint"]["cells"][0]["value"] == 346
    assert rows["Total product footprint"]["cells"][0]["unit"] == "kg CO2e"
    assert rows["Product use"]["parent"] == "Life cycle product emissions (scope 3)"
    assert rows["End-of-life processing"]["cells"][0]["qualifier"] == "<"
    reduction = rows["GHG reductions achieved[^7]"]["cells"][0]
    assert (reduction["direction"], reduction["qualifier"], reduction["value"]) == ("decrease", ">", 20)
    assert table["notes"] == ["Note: Percentages may not total 100 due to rounding."]


def test_configuration_table(elements):
    table = _one(elements, type="table", title="Configuration — iMac (four ports) 512GB")
    assert table["rows"][0]["cells"][0]["raw"] == "391 kg CO2e"
    assert "following configuration" in table["context"]


def test_carbon_chart_spans_pages_and_keeps_only_printed_values(by_id):
    chart = by_id["figure-1"]
    assert chart["kind"] == "chart" and chart["pages"] == [2, 3]
    assert [(v["value"], v["unit"]) for v in chart["values"]] == [(445, "kg"), (-99, "kg"), (346, "kg")]
    assert [v["caption"] for v in chart["values"]] == ["Baseline emissions[^7]", "Emissions reductions", "iMac emissions[^10]"]
    assert all(v["source"] == "text_layer" for v in chart["values"])
    assert chart["graphics_present"] is False
    assert "Non-air shipping" in chart["labels"] and "Renewable energy emissions[^11]" in chart["labels"]
    assert chart["checks"] == ["445 kg + (–99 kg) = 346 kg (consistent)"]
    assert chart["footnote_refs"] == [7, 8, 9, 10, 11]


def test_packaging_infographic(by_id):
    info = by_id["figure-2"]
    assert info["kind"] == "infographic" and info["pages"] == [8]
    assert [v["raw"] for v in info["values"]] == ["100%", "76%", "100%"]
    assert info["values"][1]["caption"] == "recycled content in fiber packaging"


def test_missing_energy_chart_is_reported_not_invented(by_id, built):
    placeholder = by_id["figure-3"]
    assert placeholder["kind"] == "missing-graphic" and placeholder["pages"] == [9]
    assert placeholder["title"] == "Energy consumption of ENERGY STAR–rated products"
    assert placeholder["values"] == []
    assert any("figure-3" in w for w in built["report"]["warnings"])


def test_images_catalogued_but_not_indexed_without_vision(elements):
    images = [e for e in elements if e["type"] == "figure" and e["kind"] == "image"]
    assert len(images) == 7
    assert {e["pages"][0] for e in images} == {1, 5}
    assert not any(e["indexable"] for e in images)


def test_no_content_indexed_twice(elements):
    text = " ".join(e["text"] for e in elements if e["type"] == "text")
    for claimed in ["0.57W", "391 kg", "Baseline emissions", "Power consumption for iMac", "Non-air shipping",
                    "Renewable energy emissions are too small", "Product Environmental Report 2"]:
        assert claimed not in text, f"{claimed!r} leaked into narrative text"


def test_footnote_markers_rendered_and_co2_subscript_kept(elements):
    text = " ".join(e["text"] for e in elements)
    assert "We also use 100 percent recycled copper in multiple thermal module components.[^14]" in text
    assert "346 kg CO2e" in text
    assert not re.search(r"CO\[\^2\]", text)


def test_sections_follow_headings(elements):
    sections = {e["section"] for e in elements if e["type"] == "text"}
    assert {"Design and Source", "Make", "Package and Ship", "Use", "Recover", "Carbon Footprint",
            "Taking responsibility for our products at every stage"} <= sections


def test_warnings_flag_known_pdf_defects(built):
    warnings = " ".join(built["report"]["warnings"])
    assert "White (invisible) text lines" in warnings
    assert "footnote marker 6 is not attached" in warnings
    assert "no bars/segments" in warnings
