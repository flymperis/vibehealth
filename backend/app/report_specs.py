"""What is asked of the model for each kind of text report: prompts and JSON schemas, in one place.

This is the file to edit when the answers need tuning. `SPECS` maps a document kind to a `Spec`; every other kind
(today: `other` read as a narrative report) uses `GENERIC`. Nothing here decides what is stored or shown: report.py
cleans the answer (every field is optional and checked, see there), the API and the UI show what is left.

To add a field: put it in the prompt and in `properties`, then read it in `report.clean_details` and show it in
`DocumentReview.tsx`. The answer is one text-only call to reader A on the transcribed text.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import DocumentKind


@dataclass(frozen=True)
class Spec:
    name: str  # what the prompt calls the document
    prompt: str  # the system prompt
    schema: dict  # the JSON schema Ollama constrains the answer with
    num_predict: int  # the answer's length limit in tokens


_STR = {"type": "string"}


def _strings(limit: int) -> dict:
    return {"type": "array", "items": _STR, "maxItems": limit}


def _objects(fields: list[str], limit: int) -> dict:
    return {"type": "array", "maxItems": limit, "items": {
        "type": "object", "properties": {f: _STR for f in fields}, "required": fields}}


def _schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties)}


_INTRO = ("Below is the text of a medical document ({name}), read from scanned pages: it may contain small reading "
          "errors and may be in Greek or English.\nReturn JSON with these fields:\n")
_RULES = """
Rules:
- Use only what the text says. Never add a diagnosis, advice, name, date or number that is not in the text.
- Copy names, numbers and units exactly as printed. Write summaries in the language of the text.
- A field the text does not state stays an empty string or an empty list. Do not guess.
- If the text is empty or has nothing to report, return every field empty."""

_CONCLUSION = ('- conclusion: the document\'s own conclusion, impression or opinion (e.g. "Συμπέρασμα", "Γνωμάτευση", '
               '"Impression"), copied or lightly shortened. If it has none, a summary of the document in 1 to 3 sentences.')
_FINDINGS = ("- key_findings: at most 8 short strings, one per important finding (what was found, where, with sizes or "
             "numbers as printed). Leave out normal boilerplate and repeated headers.")


def _spec(name: str, fields: str, properties: dict, num_predict: int = 1536) -> Spec:
    return Spec(name, _INTRO.format(name=name) + fields + _RULES, _schema(properties), num_predict)


GENERIC = _spec(
    "medical document",
    _CONCLUSION + "\n" + _FINDINGS,
    {"conclusion": _STR, "key_findings": _strings(8)},
    num_predict=1024,
)

IMAGING = _spec(
    "imaging report",
    "\n".join([
        "- modality: one of ultrasound, mri, ct, xray, other (ultrasound includes Doppler and triplex; xray includes "
        "mammography), or an empty string if the text does not say.",
        '- regions: at most 6 body regions examined, as named in the text (e.g. "upper abdomen", "right carotid").',
        '- measurements: at most 12 measurements that the text states with a number, as {label, value, unit}. '
        'Example: "polyp, maximum diameter 3.5 mm" gives label "polyp, maximum diameter", value "3.5", unit "mm". '
        "value is the number exactly as printed. Skip anything without an explicit number. Never calculate or estimate.",
        _CONCLUSION,
        _FINDINGS,
    ]),
    {"modality": _STR, "regions": _strings(6), "measurements": _objects(["label", "value", "unit"], 12),
     "conclusion": _STR, "key_findings": _strings(8)},
)

REPORT = _spec(
    "medical opinion",
    "\n".join([
        "- doctor: the name of the doctor who wrote or signed it, as printed (with title if printed).",
        "- specialty: the doctor's specialty, only if the text names it.",
        "- diagnoses: at most 8 diagnoses or clinical impressions, as the doctor states them.",
        "- recommendations: at most 8 recommendations, treatments or tests the doctor advises, as stated.",
        "- follow_up: {text, date}. text: the follow-up or re-examination instruction as stated (e.g. \"review in 6 months\"). "
        "date: only a specific date printed in the text, written YYYY-MM-DD; otherwise an empty string.",
        _CONCLUSION,
        _FINDINGS,
    ]),
    {"doctor": _STR, "specialty": _STR, "diagnoses": _strings(8), "recommendations": _strings(8),
     "follow_up": _schema({"text": _STR, "date": _STR}), "conclusion": _STR, "key_findings": _strings(8)},
)

PRESCRIPTION = _spec(
    "prescription",
    "\n".join([
        "- prescriber: the prescribing doctor's name, as printed.",
        "- date: the prescription's date if printed, written YYYY-MM-DD; otherwise an empty string.",
        "- medications: at most 20 prescribed medicines, as {name, active_substance, strength, dose_instruction, "
        "duration_or_quantity}. Copy every field exactly as printed (a printed e-prescription lists columns such as "
        '"Φάρμακο", "Δραστική ουσία", "Δοσολογία", "Ποσότητα"). name: the medicine as printed. active_substance: only if '
        "printed. strength: e.g. \"500 mg\", as printed. dose_instruction: how to take it, as printed. "
        "duration_or_quantity: the duration or the quantity (packs, tablets), as printed.",
        "Never fill a field from your own knowledge: do not add the active substance of a brand name, a usual dose "
        "or a strength that the text does not print. An unclear or missing field stays an empty string.",
    ]),
    {"prescriber": _STR, "date": _STR,
     "medications": _objects(["name", "active_substance", "strength", "dose_instruction", "duration_or_quantity"], 20)},
    num_predict=2048,
)

SPECS: dict[DocumentKind, Spec] = {
    DocumentKind.IMAGING: IMAGING,
    DocumentKind.REPORT: REPORT,
    DocumentKind.PRESCRIPTION: PRESCRIPTION,
}


def spec_for(kind: DocumentKind | str) -> Spec:
    return SPECS.get(kind, GENERIC)  # type: ignore[arg-type]
