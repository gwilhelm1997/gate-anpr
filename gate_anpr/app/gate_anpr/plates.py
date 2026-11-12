"""New Zealand registration normalisation. Matching is exact: no character substitution."""

import re

_NOT_ALNUM = re.compile(r"[^A-Z0-9]")
# NZ standard and personalised plates are 1-6 letters/digits.
_NZ_PLATE = re.compile(r"[A-Z0-9]{1,6}")


def normalise(text: str) -> str:
    """Upper-case and drop spaces, dashes and other separators. Never alters characters."""
    return _NOT_ALNUM.sub("", (text or "").upper())


def is_valid_nz(plate: str) -> bool:
    return bool(_NZ_PLATE.fullmatch(plate))
