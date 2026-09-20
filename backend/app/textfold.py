"""Text folding for search and for checking a model's answer against the text it read.

`fold` makes case, accents and the final sigma not matter (Greek "Ήπαρ", "ηπαρ" and "ΗΠΑΡ" fold to the same
string) and keeps the LENGTH: character i of the folded text is character i of the input, so a match found in the
folded text can be cut out of the original for a snippet. Give it NFC text (`nfc` first): a letter written as a base
letter plus a combining accent would otherwise be two characters.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


@lru_cache(maxsize=8192)
def _fold_char(ch: str) -> str:
    if ch == "ς":  # final sigma
        return "σ"
    base = unicodedata.normalize("NFD", ch)[0]  # the letter without its accents
    low = base.lower()
    return low if len(low) == 1 else base  # a character that lower-cases into two keeps its length


def fold(text: str) -> str:
    """Lower case, without accents, one character out for every character in (text should be NFC)."""
    return "".join(_fold_char(ch) for ch in text)


def fold_words(text: str) -> str:
    """`fold` with runs of white space made one space: for checking that a phrase occurs in a text."""
    return " ".join(fold(nfc(text)).split())
