"""Kill-switch defaults for the suites that exercise gated email options.

`ABROLIA_BYO_EMAIL_ENABLED` and `ABROLIA_GMAIL_ENABLED` are fail-closed and
default to off, so every test that selects `family_domain` or `gmail_agent` has
to turn its option on — which is the point: the flag is only a kill switch if
the code refuses without it.

Enabled here rather than in each test because the contract under test in these
modules is the provider lifecycle, not the flag. The flag's own behaviour is
asserted in `tests/control_plane/test_email_option_flags.py`, which deliberately
does NOT use this fixture.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _enable_gated_email_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABROLIA_BYO_EMAIL_ENABLED", "1")
    monkeypatch.setenv("ABROLIA_GMAIL_ENABLED", "1")
