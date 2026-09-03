"""The closed vocabularies a household profile is chosen from.

The profile form used to take language, country and timezone as free text and
validate them afterwards — `country_code` against `^[A-Z]{2}$`, language by
length, timezone not at all — so a tester who typed "Czechia" or described the
family's languages in a sentence got a 422 the page did not show. The form now
offers exactly these values, and the contract accepts exactly these values,
so what the browser sends is what the server stores.

Codes are the durable value; names are display only. `family_language` is an
ISO 639-1 code (what the runtime manifest already carried as `en`),
`country_code` is ISO 3166-1 alpha-2, `timezone` is an IANA zone name.
"""

from __future__ import annotations

import zoneinfo

#: ISO 639-1 code → English name. Deliberately a broad set rather than the
#: full 184: a family picks the language it speaks at home, and a code the
#: runtime cannot render a reply in is not a useful choice.
LANGUAGES: tuple[tuple[str, str], ...] = (
    ("ar", "Arabic"),
    ("bg", "Bulgarian"),
    ("bn", "Bengali"),
    ("ca", "Catalan"),
    ("cs", "Czech"),
    ("da", "Danish"),
    ("de", "German"),
    ("el", "Greek"),
    ("en", "English"),
    ("es", "Spanish"),
    ("et", "Estonian"),
    ("fa", "Persian"),
    ("fi", "Finnish"),
    ("fr", "French"),
    ("he", "Hebrew"),
    ("hi", "Hindi"),
    ("hr", "Croatian"),
    ("hu", "Hungarian"),
    ("hy", "Armenian"),
    ("id", "Indonesian"),
    ("it", "Italian"),
    ("ja", "Japanese"),
    ("ka", "Georgian"),
    ("kk", "Kazakh"),
    ("ko", "Korean"),
    ("lt", "Lithuanian"),
    ("lv", "Latvian"),
    ("nl", "Dutch"),
    ("no", "Norwegian"),
    ("pl", "Polish"),
    ("pt", "Portuguese"),
    ("ro", "Romanian"),
    ("ru", "Russian"),
    ("sk", "Slovak"),
    ("sl", "Slovenian"),
    ("sr", "Serbian"),
    ("sv", "Swedish"),
    ("th", "Thai"),
    ("tr", "Turkish"),
    ("uk", "Ukrainian"),
    ("ur", "Urdu"),
    ("uz", "Uzbek"),
    ("vi", "Vietnamese"),
    ("zh", "Chinese"),
)

#: ISO 3166-1 alpha-2 → short English name. Europe only, by owner decision
#: (2026-09-03): the product is for families living in Europe, and a list of
#: 249 states hid the fifty that apply. Council of Europe members plus the
#: European states outside it, and the transcontinental ones a family in
#: Europe may actually live in.
COUNTRIES: tuple[tuple[str, str], ...] = (
    ("AD", "Andorra"), ("AL", "Albania"), ("AM", "Armenia"), ("AT", "Austria"),
    ("AZ", "Azerbaijan"), ("BA", "Bosnia and Herzegovina"), ("BE", "Belgium"),
    ("BG", "Bulgaria"), ("BY", "Belarus"), ("CH", "Switzerland"), ("CY", "Cyprus"),
    ("CZ", "Czechia"), ("DE", "Germany"), ("DK", "Denmark"), ("EE", "Estonia"),
    ("ES", "Spain"), ("FI", "Finland"), ("FR", "France"), ("GB", "United Kingdom"),
    ("GE", "Georgia"), ("GR", "Greece"), ("HR", "Croatia"), ("HU", "Hungary"),
    ("IE", "Ireland"), ("IS", "Iceland"), ("IT", "Italy"), ("LI", "Liechtenstein"),
    ("LT", "Lithuania"), ("LU", "Luxembourg"), ("LV", "Latvia"), ("MC", "Monaco"),
    ("MD", "Moldova"), ("ME", "Montenegro"), ("MK", "North Macedonia"),
    ("MT", "Malta"), ("NL", "Netherlands"), ("NO", "Norway"), ("PL", "Poland"),
    ("PT", "Portugal"), ("RO", "Romania"), ("RS", "Serbia"), ("RU", "Russia"),
    ("SE", "Sweden"), ("SI", "Slovenia"), ("SK", "Slovakia"), ("SM", "San Marino"),
    ("TR", "Türkiye"), ("UA", "Ukraine"), ("VA", "Vatican City"),
)

LANGUAGE_CODES: frozenset[str] = frozenset(code for code, _ in LANGUAGES)
COUNTRY_CODES: frozenset[str] = frozenset(code for code, _ in COUNTRIES)


#: Zones outside `Europe/*` that a family in one of the countries above lives
#: in: the Atlantic islands of Portugal, Spain, Denmark and Iceland, and the
#: Caucasus and Cyprus, whose IANA zones sit under `Asia/` and `Atlantic/`.
_EUROPEAN_ZONES_ELSEWHERE = frozenset({
    "Atlantic/Azores", "Atlantic/Canary", "Atlantic/Faroe", "Atlantic/Madeira",
    "Atlantic/Reykjavik", "Asia/Baku", "Asia/Nicosia", "Asia/Tbilisi",
    "Asia/Yerevan",
})


def _timezones() -> tuple[str, ...]:
    """IANA zone names for the countries offered, `Region/City` form only.

    The same owner decision that limits the countries to Europe limits the
    zones: a family in one of those countries lives in `Europe/*` or one of
    the few zones listed above, and four hundred others hid them. The tz
    database comes from the `tzdata` package so the production image does not
    depend on the OS having one.
    """
    zones = zoneinfo.available_timezones()
    return tuple(sorted(
        zone for zone in zones
        if zone.startswith("Europe/") or zone in _EUROPEAN_ZONES_ELSEWHERE
    ))


TIMEZONES: tuple[str, ...] = _timezones()
TIMEZONE_NAMES: frozenset[str] = frozenset(TIMEZONES)
