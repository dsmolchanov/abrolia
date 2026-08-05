from __future__ import annotations

import pytest

from control_plane.email.local_part import (
    collision_candidate,
    normalize_local_part,
    suggest_local_part,
)


def test_suggestion_transliterates_and_normalizes_names() -> None:
    assert suggest_local_part("Дмитрий", "Молчанов") == "dmitrii_molchanov"
    assert suggest_local_part("Éva", "Novák") == "eva_novak"
    assert collision_candidate("eva_novak", 2) == "eva_novak2"
    assert normalize_local_part("UpperCase") == "uppercase"


@pytest.mark.parametrize(
    "value",
    [
        "hello",
        "admin",
        "аdmin",
        "two..dots",
        "ab",
        "x" * 49,
    ],
)
def test_local_part_policy_rejects_reserved_unicode_and_ambiguous_shapes(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_local_part(value)
