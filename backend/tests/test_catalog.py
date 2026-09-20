"""Name normalisation and value helpers (synthetic names only)."""

import pytest

from app.catalog import BY_CODE, RULES, flag_for, map_code, norm_unit, norm_value, num, skel


@pytest.mark.parametrize(
    "name, value, unit, code",
    [
        ("Χοληστερόλη", "180", "mg/dl", "CHOL"),
        ("HDL Χοληστερόλη", "55", "mg/dl", "HDL"),
        ("LDL Χοληστερόλη", "100", "mg/dl", "LDL"),
        ("Αθηρωματικός δείκτης CHOL/HDL", "3.3", "", "ATH"),
        ("CHOL/HDL", "3.3", "", "ATH"),
        ("MCHC", "33", "g/dl", "MCHC"),
        ("MCH", "29", "pg", "MCH"),
        ("Σάκχαρο", "92", "mg/dl", "GLU"),
        ("Σάκχαρο", "Αρνητικό", "", "U_GLU"),
        ("Αιμοσφαιρίνη", "Ίχνη", "", "U_BLOOD"),
        ("Ερυθρά αιμοσφαίρια", "0 - 1", "κ.ο.π.", "U_RBC"),
        ("Ερυθρά Αιμοσφαίρια", "5.1", "10^6/μl", "RBC"),
        ("Λεμφοκύτταρα", "30", "%", "LYMPH"),
        ("Λεμφοκύτταρα", "2.1", "10^3/μl", "LYMPH_ABS"),
        ("Αλκαλική Φωσφατάση", "70", "U/L", "ALP"),
        ("Anti-TG", "10", "IU/ml", "ATG"),
        ("Anti-TPO", "5", "IU/ml", "ATPO"),
        ("Lp(a)", "12", "mg/dl", "LPA"),
        ("Νιτρώδη", "Αρνητικά", "", "U_NIT"),
        ("Οξόνη", "Αρνητική", "", "U_KET"),
        ("Αντίδραση (pH)", "6", "", "U_PH"),
        ("Ελεύθερη Θυροξίνη (FT4)", "1.2", "ng/dl", "FT4"),
        ("Τριιωδοθυρονίνη (T3)", "1.1", "ng/ml", "T3"),
        ("Κάτι άγνωστο", "1", "", None),
    ],
)
def test_map_code(name, value, unit, code):
    assert map_code(name, value, unit) == code


def test_every_rule_code_is_in_the_catalogue():
    for code, _ in RULES:
        assert code in BY_CODE, code
    for code in ("U_GLU", "U_BLOOD", "U_RBC", "NEUT_ABS", "BASO_ABS"):
        assert code in BY_CODE


def test_skeleton_folds_greek_latin_and_cyrillic_lookalikes():
    assert skel("Χοληστερόλη") == skel("ΧΟΛΗΣΤΕΡΟΛΗ")
    # Cyrillic 'о' and 'к' in an OCR'd word
    assert map_code("Кάλιο", "4.2", "mmol/l") == "K"
    assert map_code("Chol/HDL", "3", "") == "ATH"


def test_numbers_and_values():
    assert num("5,5") == 5.5
    assert num("< 0.5") == 0.5
    assert num("12 mg/dl") == 12.0
    assert num("Όχι") is None
    assert norm_value("5,50") == norm_value("5.5")
    assert norm_value("Αρνητικό") == norm_value("αρνητικο")
    # Latin look-alikes and a trailing κ.ο.π are the same text value
    assert norm_value("Σπάνια κ.ο.π.") == norm_value("Σπάνια")
    assert norm_value("oχι") == norm_value("Όχι")


def test_units():
    assert norm_unit("x10^3/ul") == norm_unit("10³/μL")
    assert norm_unit("gr/dl") == "g/dl"
    assert norm_unit("uIU/ml") == "μiu/ml"


@pytest.mark.parametrize(
    "value, rng, flag",
    [
        ("5", "4 - 10", ""),
        ("3,9", "4 - 10", "L"),
        ("11", "4-10", "H"),
        ("210", "< 200", "H"),
        ("35", "> 40", "L"),
        ("Αρνητικό", "Αρνητικό", ""),
        ("5", "", ""),
    ],
)
def test_flag(value, rng, flag):
    assert flag_for(value, rng) == flag
