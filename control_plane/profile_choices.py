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

#: ISO 3166-1 alpha-2 → short English name.
COUNTRIES: tuple[tuple[str, str], ...] = (
    ("AD", "Andorra"), ("AE", "United Arab Emirates"), ("AF", "Afghanistan"),
    ("AG", "Antigua and Barbuda"), ("AI", "Anguilla"), ("AL", "Albania"),
    ("AM", "Armenia"), ("AO", "Angola"), ("AR", "Argentina"),
    ("AS", "American Samoa"), ("AT", "Austria"), ("AU", "Australia"),
    ("AW", "Aruba"), ("AX", "Åland Islands"), ("AZ", "Azerbaijan"),
    ("BA", "Bosnia and Herzegovina"), ("BB", "Barbados"), ("BD", "Bangladesh"),
    ("BE", "Belgium"), ("BF", "Burkina Faso"), ("BG", "Bulgaria"),
    ("BH", "Bahrain"), ("BI", "Burundi"), ("BJ", "Benin"),
    ("BL", "Saint Barthélemy"), ("BM", "Bermuda"), ("BN", "Brunei"),
    ("BO", "Bolivia"), ("BQ", "Caribbean Netherlands"), ("BR", "Brazil"),
    ("BS", "Bahamas"), ("BT", "Bhutan"), ("BW", "Botswana"),
    ("BY", "Belarus"), ("BZ", "Belize"), ("CA", "Canada"),
    ("CC", "Cocos (Keeling) Islands"), ("CD", "Congo (Kinshasa)"),
    ("CF", "Central African Republic"), ("CG", "Congo (Brazzaville)"),
    ("CH", "Switzerland"), ("CI", "Côte d'Ivoire"), ("CK", "Cook Islands"),
    ("CL", "Chile"), ("CM", "Cameroon"), ("CN", "China"), ("CO", "Colombia"),
    ("CR", "Costa Rica"), ("CU", "Cuba"), ("CV", "Cabo Verde"),
    ("CW", "Curaçao"), ("CX", "Christmas Island"), ("CY", "Cyprus"),
    ("CZ", "Czechia"), ("DE", "Germany"), ("DJ", "Djibouti"), ("DK", "Denmark"),
    ("DM", "Dominica"), ("DO", "Dominican Republic"), ("DZ", "Algeria"),
    ("EC", "Ecuador"), ("EE", "Estonia"), ("EG", "Egypt"),
    ("EH", "Western Sahara"), ("ER", "Eritrea"), ("ES", "Spain"),
    ("ET", "Ethiopia"), ("FI", "Finland"), ("FJ", "Fiji"),
    ("FK", "Falkland Islands"), ("FM", "Micronesia"), ("FO", "Faroe Islands"),
    ("FR", "France"), ("GA", "Gabon"), ("GB", "United Kingdom"),
    ("GD", "Grenada"), ("GE", "Georgia"), ("GF", "French Guiana"),
    ("GG", "Guernsey"), ("GH", "Ghana"), ("GI", "Gibraltar"),
    ("GL", "Greenland"), ("GM", "Gambia"), ("GN", "Guinea"),
    ("GP", "Guadeloupe"), ("GQ", "Equatorial Guinea"), ("GR", "Greece"),
    ("GS", "South Georgia and the South Sandwich Islands"), ("GT", "Guatemala"),
    ("GU", "Guam"), ("GW", "Guinea-Bissau"), ("GY", "Guyana"),
    ("HK", "Hong Kong"), ("HN", "Honduras"), ("HR", "Croatia"), ("HT", "Haiti"),
    ("HU", "Hungary"), ("ID", "Indonesia"), ("IE", "Ireland"), ("IL", "Israel"),
    ("IM", "Isle of Man"), ("IN", "India"), ("IO", "British Indian Ocean Territory"),
    ("IQ", "Iraq"), ("IR", "Iran"), ("IS", "Iceland"), ("IT", "Italy"),
    ("JE", "Jersey"), ("JM", "Jamaica"), ("JO", "Jordan"), ("JP", "Japan"),
    ("KE", "Kenya"), ("KG", "Kyrgyzstan"), ("KH", "Cambodia"), ("KI", "Kiribati"),
    ("KM", "Comoros"), ("KN", "Saint Kitts and Nevis"), ("KP", "North Korea"),
    ("KR", "South Korea"), ("KW", "Kuwait"), ("KY", "Cayman Islands"),
    ("KZ", "Kazakhstan"), ("LA", "Laos"), ("LB", "Lebanon"), ("LC", "Saint Lucia"),
    ("LI", "Liechtenstein"), ("LK", "Sri Lanka"), ("LR", "Liberia"),
    ("LS", "Lesotho"), ("LT", "Lithuania"), ("LU", "Luxembourg"), ("LV", "Latvia"),
    ("LY", "Libya"), ("MA", "Morocco"), ("MC", "Monaco"), ("MD", "Moldova"),
    ("ME", "Montenegro"), ("MF", "Saint Martin"), ("MG", "Madagascar"),
    ("MH", "Marshall Islands"), ("MK", "North Macedonia"), ("ML", "Mali"),
    ("MM", "Myanmar"), ("MN", "Mongolia"), ("MO", "Macao"),
    ("MP", "Northern Mariana Islands"), ("MQ", "Martinique"), ("MR", "Mauritania"),
    ("MS", "Montserrat"), ("MT", "Malta"), ("MU", "Mauritius"), ("MV", "Maldives"),
    ("MW", "Malawi"), ("MX", "Mexico"), ("MY", "Malaysia"), ("MZ", "Mozambique"),
    ("NA", "Namibia"), ("NC", "New Caledonia"), ("NE", "Niger"),
    ("NF", "Norfolk Island"), ("NG", "Nigeria"), ("NI", "Nicaragua"),
    ("NL", "Netherlands"), ("NO", "Norway"), ("NP", "Nepal"), ("NR", "Nauru"),
    ("NU", "Niue"), ("NZ", "New Zealand"), ("OM", "Oman"), ("PA", "Panama"),
    ("PE", "Peru"), ("PF", "French Polynesia"), ("PG", "Papua New Guinea"),
    ("PH", "Philippines"), ("PK", "Pakistan"), ("PL", "Poland"),
    ("PM", "Saint Pierre and Miquelon"), ("PN", "Pitcairn"), ("PR", "Puerto Rico"),
    ("PS", "Palestine"), ("PT", "Portugal"), ("PW", "Palau"), ("PY", "Paraguay"),
    ("QA", "Qatar"), ("RE", "Réunion"), ("RO", "Romania"), ("RS", "Serbia"),
    ("RU", "Russia"), ("RW", "Rwanda"), ("SA", "Saudi Arabia"),
    ("SB", "Solomon Islands"), ("SC", "Seychelles"), ("SD", "Sudan"),
    ("SE", "Sweden"), ("SG", "Singapore"), ("SH", "Saint Helena"),
    ("SI", "Slovenia"), ("SJ", "Svalbard and Jan Mayen"), ("SK", "Slovakia"),
    ("SL", "Sierra Leone"), ("SM", "San Marino"), ("SN", "Senegal"),
    ("SO", "Somalia"), ("SR", "Suriname"), ("SS", "South Sudan"),
    ("ST", "São Tomé and Príncipe"), ("SV", "El Salvador"), ("SX", "Sint Maarten"),
    ("SY", "Syria"), ("SZ", "Eswatini"), ("TC", "Turks and Caicos Islands"),
    ("TD", "Chad"), ("TF", "French Southern Territories"), ("TG", "Togo"),
    ("TH", "Thailand"), ("TJ", "Tajikistan"), ("TK", "Tokelau"),
    ("TL", "Timor-Leste"), ("TM", "Turkmenistan"), ("TN", "Tunisia"),
    ("TO", "Tonga"), ("TR", "Türkiye"), ("TT", "Trinidad and Tobago"),
    ("TV", "Tuvalu"), ("TW", "Taiwan"), ("TZ", "Tanzania"), ("UA", "Ukraine"),
    ("UG", "Uganda"), ("UM", "U.S. Outlying Islands"), ("US", "United States"),
    ("UY", "Uruguay"), ("UZ", "Uzbekistan"), ("VA", "Vatican City"),
    ("VC", "Saint Vincent and the Grenadines"), ("VE", "Venezuela"),
    ("VG", "British Virgin Islands"), ("VI", "U.S. Virgin Islands"),
    ("VN", "Vietnam"), ("VU", "Vanuatu"), ("WF", "Wallis and Futuna"),
    ("WS", "Samoa"), ("YE", "Yemen"), ("YT", "Mayotte"), ("ZA", "South Africa"),
    ("ZM", "Zambia"), ("ZW", "Zimbabwe"),
)

LANGUAGE_CODES: frozenset[str] = frozenset(code for code, _ in LANGUAGES)
COUNTRY_CODES: frozenset[str] = frozenset(code for code, _ in COUNTRIES)


def _timezones() -> tuple[str, ...]:
    """IANA zone names, `Region/City` form only.

    `Etc/*`, bare offsets and legacy aliases are real zones but not answers to
    "where does the family live". The tz database comes from the `tzdata`
    package so the production image does not depend on the OS having one.
    """
    zones = zoneinfo.available_timezones()
    return tuple(sorted(
        zone for zone in zones
        if "/" in zone and not zone.startswith(("Etc/", "SystemV/", "US/", "Canada/"))
    ))


TIMEZONES: tuple[str, ...] = _timezones()
TIMEZONE_NAMES: frozenset[str] = frozenset(TIMEZONES)
