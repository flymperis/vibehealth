"""The verification rules (strict text check), on synthetic rows and lines."""

from app.verify import NEEDS_REVIEW, VERIFIED, ReaderRow, combine, first_occurrences, text_confirms


def one(cands, code):
    [c] = [c for c in cands if c.test_code == code]
    return c


def test_both_readers_agree():
    a = [ReaderRow("Ουρία", "30", "mg/dl", "10 - 50", page=1)]
    b = [ReaderRow("Ουρία", "30.0", "", "", page=1, code="UREA")]
    c = one(combine(a, b, []), "UREA")
    assert c.status == VERIFIED
    assert c.unit == "mg/dl" and c.ref_range == "10 - 50"
    assert c.reader_a == "30" and c.reader_b == "30.0"


def test_readers_differ_without_text():
    a = [ReaderRow("Ουρία", "30", page=1)]
    b = [ReaderRow("Ουρία", "36", page=1, code="UREA")]
    c = one(combine(a, b, []), "UREA")
    assert c.status == NEEDS_REVIEW
    assert "differ" in c.reason


def test_text_decides_between_readers():
    a = [ReaderRow("Ουρία", "30", page=1)]
    b = [ReaderRow("Ουρία", "36", page=1, code="UREA")]
    c = one(combine(a, b, ["Ουρία 36 mg/dl 10-50"]), "UREA")
    assert c.status == VERIFIED
    assert c.value == "36"
    assert "reader B" in c.reason


def test_single_reader_confirmed_by_text():
    a = [ReaderRow("Κάλιο", "4,5", "mmol/l", page=1)]
    c = one(combine(a, [], ["Κάλιο (K) 4.5 mmol/l 3.5 - 5.1"]), "K")
    assert c.status == VERIFIED


def test_single_reader_not_in_text():
    a = [ReaderRow("Κάλιο", "4.5", page=1)]
    c = one(combine(a, [], ["Νάτριο 4.5"]), "K")
    assert c.status == NEEDS_REVIEW
    assert "only reader A" in c.reason


def test_hdl_is_not_confirmed_by_the_chol_hdl_line():
    # The value 3.9 appears only on the ratio line: that line maps to ATH, not HDL.
    lines = ["HDL Χοληστερόλη 55 mg/dl > 40", "Λόγος CHOL/HDL 3.9 < 5"]
    assert not text_confirms("HDL", "3.9", lines)
    assert text_confirms("ATH", "3.9", lines)
    c = one(combine([ReaderRow("HDL", "3.9", page=1)], [], lines), "HDL")
    assert c.status == NEEDS_REVIEW


def test_text_value_needs_a_line_without_other_digits():
    # "Όξινη" is printed on the pH line next to a number: not a confirmation.
    assert not text_confirms("U_PH", "Όξινη", ["Αντίδραση (pH) Όξινη 5.5"])
    assert text_confirms("U_PH", "Όξινη", ["Αντίδραση (pH) Όξινη"])
    a = [ReaderRow("Αντίδραση (pH)", "Οξινη", page=2)]
    assert one(combine(a, [], ["Αντίδραση (pH) Όξινη 5.5"]), "U_PH").status == NEEDS_REVIEW


def test_number_must_match_whole():
    assert not text_confirms("UREA", "3", ["Ουρία 30 mg/dl"])
    assert not text_confirms("UREA", "30", ["Ουρία 130 mg/dl"])
    # the decimal separator may differ between reader and text
    assert text_confirms("UREA", "0,9", ["Ουρία 0.9"])
    assert text_confirms("UREA", "0.9", ["Ουρία 0,9"])


def test_urine_twin_confirmed_by_its_line():
    a = [ReaderRow("Σάκχαρο", "Αρνητικό", page=2)]
    c = one(combine(a, [], ["Σάκχαρο Αρνητικό"]), "U_GLU")
    assert c.status == VERIFIED


def test_first_occurrence_wins():
    rows = [
        ReaderRow("Ουρία", "41", page=3),  # history table on a later page
        ReaderRow("Ουρία", "30", page=1),
        ReaderRow("Ουρία", "", page=1),
        ReaderRow("Κρεατινίνη", "-", page=1),
    ]
    first = first_occurrences(rows)
    assert first["UREA"].value == "30"
    assert "CREA" not in first
    c = one(combine(rows, [ReaderRow("Ουρία", "30", page=1, code="UREA")], []), "UREA")
    assert c.status == VERIFIED and c.page == 1


def test_unknown_names_go_to_review_once():
    a = [ReaderRow("Κάτι άγνωστο", "5", page=1), ReaderRow("Κάτι άγνωστο", "6", page=2)]
    cands = combine(a, [], [])
    assert len(cands) == 1
    assert cands[0].test_code is None
    assert cands[0].status == NEEDS_REVIEW


def test_flag_is_computed():
    a = [ReaderRow("Σάκχαρο", "130", "mg/dl", "70 - 110", page=1)]
    b = [ReaderRow("Σάκχαρο", "130", page=1, code="GLU")]
    assert one(combine(a, b, []), "GLU").flag == "H"
