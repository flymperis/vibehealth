"""The glm-ocr text parser, on made-up report text."""

from app.glm_parser import parse, truncated


def by_code(rows):
    return {r["code"]: r for r in rows}


def test_dotted_leader_lines():
    text = "\n".join([
        "ΑΙΜΑΤΟΛΟΓΙΚΕΣ ΕΞΕΤΑΣΕΙΣ",
        "Αιμοσφαιρίνη ........ 14,2 g/dl 13.5 - 17.5",
        "Αιματοκρίτης ....... 42.0 % 40 - 52",
        "Χοληστερόλη ....... 185 mg/dl < 200",
        "Ημερομηνία 01/02/2020 ....... 5",
    ])
    rows = by_code(parse(text))
    assert set(rows) == {"HGB", "HCT", "CHOL"}
    assert rows["HGB"]["value"] == "14,2"
    assert rows["HGB"]["unit"] == "g/dl"
    assert rows["HGB"]["reference_range"] == "13.5 - 17.5"
    assert rows["CHOL"]["reference_range"] == "< 200"


def test_markdown_table():
    text = "| Εξέταση | Αποτέλεσμα | Μονάδες | Τιμές |\n|---|---|---|---|\n| Κρεατινίνη | 0.9 | mg/dl | 0.7 - 1.3 |"
    rows = parse(text)
    assert rows == [dict(name="Κρεατινίνη", value="0.9", unit="mg/dl", reference_range="0.7 - 1.3", code="CREA")]


def test_html_table_with_absolute_count():
    text = (
        "<table><tr><td>Λεμφοκύτταρα</td><td>30.0</td><td>%</td>"
        "<td>20 - 45 2.10 10^3/μl 1.0 - 4.0</td></tr></table>"
    )
    rows = by_code(parse(text))
    assert rows["LYMPH"]["value"] == "30.0"
    assert rows["LYMPH"]["reference_range"] == "20 - 45"
    assert rows["LYMPH_ABS"]["value"] == "2.10"
    assert rows["LYMPH_ABS"]["unit"] == "10^3/μl"
    assert rows["LYMPH_ABS"]["reference_range"] == "1.0 - 4.0"


def test_urine_layout():
    text = "\n".join([
        "Χροιά ..... : Κίτρινη",
        "Σάκχαρο ..... : Αρνητικό",
        "Πυοσφαίρια ..... : 0 - 2 κ.ο.π.",
        "Ερυθρά ..... : 1 - 3 κ.ο.π.",
        "Επιθήλια ..... : Σπάνια 0 - 3 κ.ο.π.",
        'Νιτρώδη ..... Αρνητικά "',
        "Ουροχολινογόνο 0.2 0.2 - 1 E.U./dl",
    ])
    rows = by_code(parse(text))
    assert rows["U_COLOR"]["value"] == "Κίτρινη"
    assert rows["U_GLU"]["value"] == "Αρνητικό"
    assert rows["U_WBC"]["value"] == "0 - 2"
    assert rows["U_RBC"]["value"] == "1 - 3"  # bare "Ερυθρά": fuzzy name + urine twin rule
    assert rows["U_EPI"]["value"].startswith("Σπάνια")
    assert rows["U_NIT"]["value"] == "Αρνητικά"
    assert rows["U_NIT"]["reference_range"] == ""
    assert rows["U_URO"]["unit"] == "E.U./dl"
    assert rows["U_URO"]["reference_range"] == "0.2 - 1"


def test_empty_grid_is_not_a_value():
    rows = parse("Μονοπύρηνα ..... Λεμφοκύτταρα ..... Ηωσινόφιλα .....")
    assert rows == []


def test_latex_bits_are_cleaned():
    rows = by_code(parse("Λευκά αιμοσφαίρια ...... 6.5 $10^{3}/\\mu l$ 4 - 10"))
    assert rows["WBC"]["value"] == "6.5"
    assert rows["WBC"]["unit"] == "10^3/μl"


def test_fuzzy_fallback_for_garbled_names():
    rows = by_code(parse("Τριγλυκεριδα ....... 120 mg/dl < 150"))
    assert "TRIG" in rows
    assert parse("Χωρίς σχέση λέξη ....... 120 mg/dl") == []


def test_truncation_detection():
    assert truncated("Ουρία ..... 30 mg/dl\nΚρεατινίνη . . . . . . .")
    assert truncated("Ουρία ..........")
    assert not truncated("Ουρία ..... 30 mg/dl 10 - 50")
