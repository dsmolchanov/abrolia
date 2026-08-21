"""Art. 9(4) GDPR determinations, as recorded in `docs/privacy/lawful-bases.md`.

Art. 9(4) lets a member state keep further conditions and limitations on health,
genetic and biometric data. The scan of 2026-08-12 covered the four pilot
countries, and `lawful-bases.md` section 3 states the operative rule plainly:

    Страна household фиксируется в `country_code` профиля. Household из страны,
    по которой результат сюда не внесён, к реальным данным не подключается.

So this module is an ALLOWLIST, not a denylist: a country absent from the table
is refused, because absence means nobody has checked it. Adding an entry here
is a legal determination, not a code change — record the result in
`lawful-bases.md` first, then mirror it here.
"""

from __future__ import annotations

from dataclasses import dataclass

LAWFUL_BASES_DOC = "docs/privacy/lawful-bases.md"


@dataclass(frozen=True)
class Art94Determination:
    """One member state's recorded result."""

    country_code: str
    #: The national provision examined.
    provision: str
    #: None when real family content may be connected today. Otherwise the
    #: prerequisite that is still outstanding, quoted from the determination.
    outstanding_prerequisite: str | None = None

    @property
    def permits_real_content(self) -> bool:
        return self.outstanding_prerequisite is None


DETERMINATIONS: dict[str, Art94Determination] = {
    # § 22 BDSG introduces no additional condition on Art. 9(2)(a); it governs
    # processing WITHOUT consent. Its § 22(2) catalogue guides our TOMs.
    "DE": Art94Determination("DE", "§ 22 BDSG"),
    # UAVG art. 22 ff. keeps the health-data exemption regime and leaves
    # explicit consent available; no additional barrier to Art. 9(2)(a).
    "NL": Art94Determination("NL", "UAVG, art. 22 e.v."),
    # LOPDGDD art. 9.1 bars consent alone where the MAIN PURPOSE is to
    # establish ideology, union membership, religion, sexual orientation,
    # beliefs or racial/ethnic origin. Health is not in that list. Our
    # processing is lawful precisely because identifying those attributes is
    # never a purpose — which is why the extraction ban and the absence of a
    # server-side classifier are a condition of lawfulness in Spain, not
    # hygiene. That ban is enforced in the pipeline, not here.
    "ES": Art94Determination("ES", "LOPDGDD, art. 9.1"),
    # Codice art. 2-septies subjects health data to the Garante's misure di
    # garanzia in addition to an Art. 9(2) condition. The determination requires
    # that reconciliation to be performed and RECORDED before the first Italian
    # household connects, and no such record exists yet — so Italy is refused,
    # fail-closed, until one is written and this entry is cleared.
    # SUSPENDED FROM THE PILOT by owner decision, 2026-08-19. Not merely
    # "awaiting a record": Italy is out of scope until someone chooses to do the
    # Garante reconciliation, which is a decision with a date rather than a task
    # sitting in a queue. Recording it as suspended keeps the register honest —
    # a permanently pending item reads as work in progress, and this is not.
    "IT": Art94Determination(
        "IT",
        "Codice, art. 2-septies",
        outstanding_prerequisite=(
            "Italy is suspended from the pilot (owner decision 2026-08-19). "
            "Art. 2-septies conditions health data on the Garante's misure di "
            "garanzia in addition to an Art. 9(2) condition, and that "
            "reconciliation against our TOMs has not been performed or "
            f"recorded; see {LAWFUL_BASES_DOC} section 3"
        ),
    ),
}


def real_content_refusal(country_code: str | None) -> str | None:
    """Return why this country may not connect to real family content.

    ``None`` means it may. The message is operator-facing: it names the missing
    determination or prerequisite so the reader knows what would unblock it.
    """
    if not country_code:
        return (
            "the household profile has no country_code, so no Art. 9(4) "
            f"determination can apply; see {LAWFUL_BASES_DOC} section 3"
        )
    determination = DETERMINATIONS.get(country_code.upper())
    if determination is None:
        return (
            f"no Art. 9(4) determination is recorded for {country_code.upper()}; "
            f"{LAWFUL_BASES_DOC} section 3 refuses real data for any country "
            "whose result has not been entered"
        )
    if not determination.permits_real_content:
        return (
            f"{determination.country_code} ({determination.provision}): "
            f"{determination.outstanding_prerequisite}"
        )
    return None


def permitted_countries() -> frozenset[str]:
    """Countries that may connect to real family content today."""
    return frozenset(
        code for code, entry in DETERMINATIONS.items() if entry.permits_real_content
    )
