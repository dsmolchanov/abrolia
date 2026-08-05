from __future__ import annotations

import re
import unicodedata

LOCAL_PART = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
RESERVED_LOCAL_PARTS = frozenset({
    "abuse",
    "admin",
    "billing",
    "hello",
    "no-reply",
    "noreply",
    "postmaster",
    "privacy",
    "security",
    "support",
    "team",
    "webmaster",
})
CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}


def transliterate(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    output: list[str] = []
    for character in folded:
        if character in CYRILLIC:
            output.append(CYRILLIC[character])
        elif character.isascii() and character.isalnum():
            output.append(character)
        elif unicodedata.category(character).startswith("M"):
            continue
        else:
            output.append("_")
    return re.sub(r"_+", "_", "".join(output)).strip("_")


def normalize_local_part(value: str) -> str:
    normalized = value.strip().casefold()
    if not 3 <= len(normalized) <= 48 or not LOCAL_PART.fullmatch(normalized):
        raise ValueError("local part must be 3-48 lowercase ASCII characters")
    if normalized in RESERVED_LOCAL_PARTS:
        raise ValueError("local part is reserved")
    return normalized


def suggest_local_part(first_name: str, last_name: str) -> str:
    candidate = "_".join(filter(None, (
        transliterate(first_name), transliterate(last_name)
    )))[:48].rstrip("._-")
    if len(candidate) < 3 or candidate in RESERVED_LOCAL_PARTS:
        candidate = "family_agent"
    return normalize_local_part(candidate)


def collision_candidate(base: str, sequence: int) -> str:
    if sequence < 2:
        return normalize_local_part(base)
    suffix = str(sequence)
    stem = normalize_local_part(base)[: 48 - len(suffix)].rstrip("._-")
    return normalize_local_part(f"{stem}{suffix}")
