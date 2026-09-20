"""The lab tests VibeHealth knows, and how printed names map onto them.

The mapping is the one measured in the extraction benchmark
(see docs/DESIGN.md): a printed name is folded into a
Latin "skeleton" (accents dropped, Greek and Cyrillic look-alikes turned into
Latin letters) and matched against an ordered list of patterns. Order matters:
"CHOL/HDL" has to hit the atherogenic index before it can hit HDL, "MCHC"
before "MCH", and so on.

Urine and blood share several names (Σάκχαρο, Αιμοσφαιρίνη, Ερυθρά). The
benchmark rule decides by the value: a qualitative result ("Αρνητικό") or a
per-field unit (κ.ο.π.) is the urine test.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Test:
    code: str
    name_en: str
    name_el: str
    specimen: str  # "blood" | "urine"
    category: str
    unit: str = ""  # the usual unit; values keep the unit as printed
    paired_with: str | None = None  # code of the sibling test shown combined with this one (display only)


def _t(code, en, el, category, unit="", specimen="blood", paired_with=None) -> Test:
    return Test(code, en, el, specimen, category, unit, paired_with)


TESTS: list[Test] = [
    # Haematology
    _t("RBC", "Red blood cells", "Ερυθρά αιμοσφαίρια", "hematology", "10^6/μL"),
    _t("HGB", "Haemoglobin", "Αιμοσφαιρίνη", "hematology", "g/dL"),
    _t("HCT", "Haematocrit", "Αιματοκρίτης", "hematology", "%"),
    _t("MCV", "Mean cell volume", "Μέσος όγκος ερυθρών", "hematology", "fL"),
    _t("MCH", "Mean cell haemoglobin", "Μέση περιεκτικότητα Hb", "hematology", "pg"),
    _t("MCHC", "Mean cell Hb concentration", "Μέση συγκέντρωση Hb", "hematology", "g/dL"),
    _t("RDW", "Red cell distribution width", "Εύρος κατανομής ερυθρών", "hematology", "%"),
    _t("HYPO", "Hypochromia", "Υποχρωμία", "hematology"),
    _t("WBC", "White blood cells", "Λευκά αιμοσφαίρια", "hematology", "10^3/μL"),
    _t("NEUT", "Neutrophils", "Πολυμορφοπύρηνα", "hematology", "%", paired_with="NEUT_ABS"),
    _t("LYMPH", "Lymphocytes", "Λεμφοκύτταρα", "hematology", "%", paired_with="LYMPH_ABS"),
    _t("MONO", "Monocytes", "Μονοπύρηνα", "hematology", "%", paired_with="MONO_ABS"),
    _t("EOS", "Eosinophils", "Ηωσινόφιλα", "hematology", "%", paired_with="EOS_ABS"),
    _t("BASO", "Basophils", "Βασεόφιλα", "hematology", "%", paired_with="BASO_ABS"),
    _t("NEUT_ABS", "Neutrophils (absolute)", "Πολυμορφοπύρηνα (απόλυτος)", "hematology", "10^3/μL"),
    _t("LYMPH_ABS", "Lymphocytes (absolute)", "Λεμφοκύτταρα (απόλυτος)", "hematology", "10^3/μL"),
    _t("MONO_ABS", "Monocytes (absolute)", "Μονοπύρηνα (απόλυτος)", "hematology", "10^3/μL"),
    _t("EOS_ABS", "Eosinophils (absolute)", "Ηωσινόφιλα (απόλυτος)", "hematology", "10^3/μL"),
    _t("BASO_ABS", "Basophils (absolute)", "Βασεόφιλα (απόλυτος)", "hematology", "10^3/μL"),
    _t("PLT", "Platelets", "Αιμοπετάλια", "hematology", "10^3/μL"),
    _t("PCT", "Plateletcrit", "Αιμοπεταλιοκρίτης", "hematology", "%"),
    _t("MPV", "Mean platelet volume", "Μέσος όγκος αιμοπεταλίων", "hematology", "fL"),
    _t("PDW", "Platelet distribution width", "Εύρος κατανομής αιμοπεταλίων", "hematology", "%"),
    _t("ESR", "ESR", "Ταχύτητα καθίζησης (ΤΚΕ)", "hematology", "mm"),
    _t("RETIC", "Reticulocytes", "Δικτυοερυθροκύτταρα", "hematology", "%"),
    # Diabetes
    _t("GLU", "Glucose", "Σάκχαρο", "diabetes", "mg/dL"),
    _t("HBA1C", "HbA1c", "Γλυκοζυλιωμένη αιμοσφαιρίνη", "diabetes", "%"),
    _t("INS", "Insulin", "Ινσουλίνη", "diabetes", "μIU/mL"),
    # Kidney
    _t("UREA", "Urea", "Ουρία", "kidney", "mg/dL"),
    _t("CREA", "Creatinine", "Κρεατινίνη", "kidney", "mg/dL"),
    _t("URIC", "Uric acid", "Ουρικό οξύ", "kidney", "mg/dL"),
    # Lipids
    _t("CHOL", "Total cholesterol", "Χοληστερόλη", "lipids", "mg/dL"),
    _t("HDL", "HDL cholesterol", "HDL χοληστερόλη", "lipids", "mg/dL"),
    _t("LDL", "LDL cholesterol", "LDL χοληστερόλη", "lipids", "mg/dL"),
    _t("TRIG", "Triglycerides", "Τριγλυκερίδια", "lipids", "mg/dL"),
    _t("ATH", "Cholesterol / HDL ratio", "Αθηρωματικός δείκτης", "lipids"),
    _t("LPA", "Lipoprotein (a)", "Λιποπρωτεΐνη (a)", "lipids", "mg/dL"),
    # Liver
    _t("AST", "AST (SGOT)", "Τρανσαμινάση AST (SGOT)", "liver", "U/L"),
    _t("ALT", "ALT (SGPT)", "Τρανσαμινάση ALT (SGPT)", "liver", "U/L"),
    _t("GGT", "Gamma-GT", "γ-GT", "liver", "U/L"),
    _t("ALP", "Alkaline phosphatase", "Αλκαλική φωσφατάση", "liver", "U/L"),
    _t("ALB", "Albumin", "Αλβουμίνη", "liver", "g/dL"),
    # Iron and vitamins
    _t("FERR", "Ferritin", "Φερριτίνη", "vitamins", "ng/mL"),
    _t("B12", "Vitamin B12", "Βιταμίνη B12", "vitamins", "pg/mL"),
    _t("FOL", "Folic acid", "Φυλλικό οξύ", "vitamins", "ng/mL"),
    _t("VITD", "Vitamin D (25-OH)", "Βιταμίνη D (25-OH)", "vitamins", "ng/mL"),
    _t("HCY", "Homocysteine", "Ομοκυστεΐνη", "vitamins", "μmol/L"),
    # Electrolytes
    _t("CA", "Calcium", "Ασβέστιο", "electrolytes", "mg/dL"),
    _t("K", "Potassium", "Κάλιο", "electrolytes", "mmol/L"),
    _t("NA", "Sodium", "Νάτριο", "electrolytes", "mmol/L"),
    # Inflammation
    _t("CRP", "CRP", "CRP", "inflammation", "mg/dL"),
    # Thyroid. one laboratory prints T3 in ng/mL, another in ng/dL: see to_canonical_unit.
    _t("FT3", "Free T3", "Ελεύθερη T3 (FT3)", "thyroid", "pg/mL"),
    _t("FT4", "Free T4", "Ελεύθερη T4 (FT4)", "thyroid", "ng/dL"),
    _t("T3", "T3 (triiodothyronine)", "T3 (τριιωδοθυρονίνη)", "thyroid", "ng/mL"),
    _t("T4", "T4 (thyroxine)", "T4 (θυροξίνη)", "thyroid", "μg/dL"),
    _t("TSH", "TSH", "TSH (θυρεοτρόπος ορμόνη)", "thyroid", "μIU/mL"),
    _t("ATG", "Anti-TG", "Anti-TG", "thyroid", "IU/mL"),
    _t("ATPO", "Anti-TPO", "Anti-TPO", "thyroid", "IU/mL"),
    # Urine
    _t("U_COLOR", "Urine colour", "Χροιά ούρων", "urine", specimen="urine"),
    _t("U_APPEAR", "Urine appearance", "Όψη ούρων", "urine", specimen="urine"),
    _t("U_SG", "Urine specific gravity", "Ειδικό βάρος ούρων", "urine", specimen="urine"),
    _t("U_PH", "Urine pH", "pH ούρων", "urine", specimen="urine"),
    _t("U_PROT", "Urine protein", "Λεύκωμα ούρων", "urine", specimen="urine"),
    _t("U_GLU", "Urine glucose", "Σάκχαρο ούρων", "urine", specimen="urine"),
    _t("U_KET", "Urine ketones", "Οξόνη ούρων", "urine", specimen="urine"),
    _t("U_BLOOD", "Urine blood", "Αιμοσφαιρίνη ούρων", "urine", specimen="urine"),
    _t("U_BIL", "Urine bilirubin", "Χολοχρωστικές ούρων", "urine", specimen="urine"),
    _t("U_URO", "Urobilinogen", "Ουροχολινογόνο", "urine", specimen="urine"),
    _t("U_NIT", "Urine nitrites", "Νιτρώδη ούρων", "urine", specimen="urine"),
    _t("U_LEU", "Leukocyte esterase", "Λευκοκυτταρική εστεράση", "urine", specimen="urine"),
    _t("U_SED", "Urine sediment", "Ίζημα ούρων", "urine", specimen="urine"),
    _t("U_WBC", "Urine white cells", "Πυοσφαίρια", "urine", "κ.ο.π", "urine"),
    _t("U_RBC", "Urine red cells", "Ερυθρά ούρων", "urine", "κ.ο.π", "urine"),
    _t("U_EPI", "Epithelial cells", "Επιθήλια", "urine", specimen="urine"),
    _t("U_MUC", "Mucus", "Βλέννη", "urine", specimen="urine"),
    _t("U_CAST", "Casts", "Κύλινδροι", "urine", specimen="urine"),
    _t("U_CRYST", "Crystals", "Κρύσταλλοι", "urine", specimen="urine"),
    _t("U_AMORPH", "Amorphous salts", "Άμορφα άλατα", "urine", specimen="urine"),
    _t("U_SALT", "Salts", "Άλατα", "urine", specimen="urine"),
    _t("U_MICRO", "Microorganisms", "Μικροοργανισμοί", "urine", specimen="urine"),
    _t("U_FUNGI", "Fungi", "Μύκητες", "urine", specimen="urine"),
    _t("U_LIPID", "Lipiduria", "Λιπιδουρία", "urine", specimen="urine"),
]
BY_CODE: dict[str, Test] = {t.code: t for t in TESTS}
ORDER: dict[str, int] = {t.code: i for i, t in enumerate(TESTS)}


# --- normalisation -----------------------------------------------------------

GR2LAT = str.maketrans({
    "α": "a", "β": "b", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "h", "θ": "th", "ι": "i", "κ": "k",
    "λ": "l", "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t",
    "υ": "y", "φ": "f", "χ": "x", "ψ": "ps", "ω": "o", "µ": "m",
})
# glm-ocr sometimes writes Greek words with Cyrillic look-alike letters.
CYR = str.maketrans({
    "а": "a", "б": "b", "в": "n", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "j", "з": "s", "и": "i",
    "й": "i", "і": "i", "к": "k", "л": "l", "м": "m", "н": "h", "о": "o", "п": "p", "р": "r", "с": "s",
    "т": "t", "у": "y", "ф": "f", "х": "x", "ц": "m", "ч": "x", "ш": "o", "щ": "o", "ъ": "", "ы": "i",
    "ь": "", "э": "e", "ю": "o", "я": "a", "ө": "th", "ү": "y", "ҫ": "s", "ї": "i",
})


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def skel(s: str | None) -> str:
    """Latin skeleton of a printed name: 'Χοληστερόλη' -> 'xolhsterolh'."""
    s = strip_accents((s or "").lower().translate(CYR)).translate(GR2LAT)
    s = s.replace("chol", "xol")
    s = re.sub(r"[.…·:]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# (code, pattern on the skeleton). The first match wins.
RULES: list[tuple[str, str]] = [
    ("U_LEU", r"ester[ah]s|stetras|leuko\w* esteras"),
    ("U_SALT", r"^alata\b"),
    ("U_FUNGI", r"mykht"),
    ("U_LIPID", r"lipidoyr"),
    ("ALP", r"fosfatas|\balp\b|phosphatase"),
    ("ATG", r"anti-? ?tg\b"),
    ("ATPO", r"anti-? ?tpo\b"),
    ("LPA", r"^lp ?\(a\)|lipoprotein\w* ?\(a\)"),
    ("LDL", r"^ldl\b|ldl[ -]?xol|xol\w* ldl"),
    ("U_URO", r"oyroxolin|urobil"),
    ("U_BIL", r"xoloxrost|bilirub"),
    ("U_KET", r"ox[oy]nh|keton"),
    ("U_NIT", r"nitrik|nitrit|nitrod"),
    ("U_WBC", r"pyosfair"),
    ("U_EPI", r"epithhl"),
    ("U_MUC", r"blenn"),
    ("U_CAST", r"kylindr"),
    ("U_CRYST", r"kryst"),
    ("U_AMORPH", r"amorf"),
    ("U_MICRO", r"mikroorgan"),
    ("U_PROT", r"leykoma"),
    ("U_SG", r"eidik\w* bar|specific grav"),
    ("U_PH", r"\bph\b|\brh\b|antidrash"),
    ("U_APPEAR", r"^opsh|appearance"),
    ("U_COLOR", r"xroia|xrosh|colou?r"),
    ("U_SED", r"izhma|sediment"),
    ("HBA1C", r"a1c|\ba1\b|glykozyl|hba1"),
    ("MCHC", r"mchc|sygkentrosh"),
    ("MCH", r"\bmch\b|periektikoth"),
    ("MPV", r"mpv|ogkos aimopet"),
    ("MCV", r"mcv|ogkos erythr"),
    ("PDW", r"pdw|katanomhs aimopet"),
    ("RDW", r"rdw|katanomhs erythr"),
    ("PCT", r"\bpct\b|aimopetaliokrit"),
    ("PLT", r"\bplt\b|aimopetalia"),
    ("HCT", r"\bhct\b|aimatokrit"),
    ("WBC", r"\bwbc\b|leyka aimosfair"),
    ("RBC", r"\brbc\b|erythra aimosfair"),
    ("HGB", r"\bhgb\b|aimosfairinh"),
    ("NEUT", r"polymorfop|polymorphonuc|neutro"),
    ("LYMPH", r"lemfokyt|lymfokyt|lympho"),
    ("MONO", r"monopyr|mononuc|monocyt"),
    ("EOS", r"s[io]+n[oy]fil|eosin"),
    ("BASO", r"baseofil|basofil|basoph"),
    ("HYPO", r"ypoxromia"),
    ("ATH", r"atheromatik|athhromatik|xol ?/ ?hdl"),
    ("HDL", r"\bhdl\b"),
    ("CHOL", r"xolhsterol|xolesterol|xolhsterin"),
    ("TRIG", r"trigl"),
    ("URIC", r"oyrik|uric"),
    ("UREA", r"oyr[p]?ia\b|\burea\b"),
    ("CREA", r"kreatinin|creatin"),
    ("GLU", r"sakxaro|glucose|glykozh"),
    ("AST", r"\bast\b|sgot"),
    ("ALT", r"\balt\b|sgpt"),
    ("GGT", r"g-?gt|glo?yt?amyl|ggt"),
    ("ALB", r"alboymin|\balb\b|albumin"),
    ("FERR", r"fer+[ie]tin|ferit"),
    ("B12", r"b12|b 12"),
    ("FOL", r"fyllik|foli[ck]|folat"),
    ("HCY", r"omokyst|homocyst"),
    ("CA", r"asbest|\bca\b|calcium"),
    ("K", r"kalio|\(k\)|potass"),
    ("NA", r"natrio|\(na\)|sodium"),
    ("VITD", r"d25|vit\w* d|bitamin\w* olik|bitamin\w* d\b|25-?oh"),
    ("CRP", r"\bcrp\b"),
    # Free hormones before the total ones: "Ελεύθερη Τριιωδοθυρονίνη (FT3)".
    ("FT3", r"\bft3\b|free t3|eleyther\w* tri"),
    ("FT4", r"\bft4\b|free t4|eleyther\w* thyrox"),
    ("T3", r"triiodo|triodo|\bt3\b"),
    ("T4", r"thyroxin|\bt4\b"),
    ("TSH", r"\btsh\b|thyreotrop|thyrotrop"),
    ("INS", r"insoylin|insulin"),
    ("ESR", r"t k e|tke\b|t\.k\.e"),
    ("RETIC", r"diktyoerythro"),
]
_RULES_RX = [(code, re.compile(rx)) for code, rx in RULES]
URINE_TWINS = {"GLU": "U_GLU", "HGB": "U_BLOOD", "RBC": "U_RBC"}
TWIN_OF = {v: k for k, v in URINE_TWINS.items()}
DIFF = {"NEUT", "LYMPH", "MONO", "EOS", "BASO"}
EMPTY_VALUES = {"", "-", "—", "null", "none"}


def num(s) -> float | None:
    """'5,5' -> 5.5, '< 0.5' -> 0.5, '12 mg/dL' -> 12.0, 'Όχι' -> None."""
    if s is None:
        return None
    t = str(s).strip().replace(",", ".")
    m = re.fullmatch(r"[<>]?\s*(-?\d+(?:\.\d+)?)(?:\s*[^\d\s-][^\d]*)?", t)
    return float(m.group(1)) if m else None


def is_qual(value) -> bool:
    return num(value) is None


def map_code(name: str, value: str = "", unit: str = "") -> str | None:
    s = skel(name)
    for code, rx in _RULES_RX:
        if rx.search(s):
            u = skel(unit)
            if code in URINE_TWINS and (is_qual(value) or "k o p" in u or "kop" in u.replace(" ", "")):
                return URINE_TWINS[code]
            if code in DIFF:
                if re.search(r"apolyt|absol|#", s) or re.search(r"10\s*\^?\s*[3³]|10e3|10 3", unit or "") \
                        or ("10" in (unit or "") and "%" not in (unit or "")):
                    return code + "_ABS"
            return code
    return None


def base_code(code: str) -> str:
    """The code a text line has to map to for `code` (differential and urine twins fold together)."""
    return code.replace("_ABS", "")


def norm_value(v) -> tuple[str, float | str]:
    """Comparable form of a value: ('n', 5.5) or ('t', 'αρνητικο')."""
    v = (v or "").strip()
    n = num(v)
    if n is not None:
        return ("n", n)
    s = strip_accents(v.lower()).replace(" ", "").replace("κ.ο.π", "").replace("k.o.p", "")
    s = s.translate(str.maketrans({"o": "ο", "x": "χ", "i": "ι"}))  # Latin look-alikes -> Greek
    s = re.sub(r"κ\.?ο\.?[πnη]\.?$", "", s).rstrip(".")
    return ("t", s)


def norm_unit(u: str | None) -> str:
    u = strip_accents((u or "").strip()).lower().replace("µ", "μ").replace(" ", "")
    if re.match(r"u[igm]", u):
        u = "μ" + u[1:]
    u = u.replace("³", "^3").replace("⁶", "^6").replace("*", "").replace("×", "")
    u = re.sub(r"10\^?([36])", r"10^\1", u)
    u = re.sub(r"^x(?=10)", "", u).replace("/ul", "/μl")
    u = u.replace("gr/", "g/").rstrip(".")
    u = u.replace("k.o.p", "κ.ο.π")
    return "" if u in ("-", "none", "null") else u


def is_empty_value(v) -> bool:
    return (v or "").strip().lower() in EMPTY_VALUES


def flag_for(value: str, ref_range: str) -> str:
    """'H', 'L' or '' from a numeric value and a printed range ('4 - 10', '< 200', '> 40')."""
    v = num(value)
    r = (ref_range or "").strip().replace(",", ".")
    if v is None or not r:
        return ""
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)", r)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return "L" if v < lo else "H" if v > hi else ""
    m = re.search(r"([<>≤≥])\s*=?\s*(-?\d+(?:\.\d+)?)", r)
    if m:
        op, bound = m.group(1), float(m.group(2))
        if op in "<≤":
            return "H" if v > bound else ""
        return "L" if v < bound else ""
    return ""


def to_canonical_unit(code: str | None, value: float | None, unit: str) -> tuple[float | None, str]:
    """Hook for unit conversion. Values are stored with the unit as printed.

    TODO: convert to the catalogue unit before values of one test are compared
    across labs. Known case: T3 is printed in ng/mL by one laboratory and in ng/dL by
    another (1 ng/mL = 100 ng/dL).
    """
    return value, unit


# Extra printed names, used only by the fuzzy fallback for OCR-garbled names
# (the pattern rules above are tried first).
ALIASES: dict[str, list[str]] = {
    "RBC": ["Ερυθρά"],
    "WBC": ["Λευκά"],
    "NEUT": ["Ουδετερόφιλα"],
    "MONO": ["Μονοκύτταρα"],
    "PLT": ["Αιμοπετάλια"],
    "GLU": ["Γλυκόζη"],
    "UREA": ["Ουρία"],
    "URIC": ["Ουρικό οξύ"],
    "TRIG": ["Τριγλυκερίδια"],
    "FERR": ["Φερριτίνη"],
    "U_SG": ["Ειδικό βάρος"],
    "U_PROT": ["Λεύκωμα"],
    "U_KET": ["Οξόνη", "Κετόνες"],
    "U_BIL": ["Χολοχρωστικές"],
    "U_URO": ["Ουροχολινογόνο"],
    "U_NIT": ["Νιτρώδη", "Νιτρικά"],
    "U_WBC": ["Πυοσφαίρια"],
    "U_EPI": ["Επιθήλια"],
    "U_COLOR": ["Χροιά"],
    "U_APPEAR": ["Όψη"],
    "U_SED": ["Ίζημα"],
}


def fuzzy_vocabulary() -> list[tuple[str, str]]:
    """(code, skeleton) pairs of the Greek names, for the OCR parser's fuzzy fallback."""
    out: list[tuple[str, str]] = []
    for test in TESTS:
        names = {test.name_el, re.sub(r"\s+ούρων$", "", test.name_el), *ALIASES.get(test.code, [])}
        for name in names:
            s = skel(re.sub(r"\(.*?\)", "", name))
            s = re.sub(r"[^a-z0-9 ]", "", s).strip()
            if len(s) >= 3 and (test.code, s) not in out:
                out.append((test.code, s))
    return out


def display_name(code: str | None, lang: str = "en") -> str:
    test = BY_CODE.get(code or "")
    if not test:
        return ""
    return test.name_el if lang == "el" else test.name_en
