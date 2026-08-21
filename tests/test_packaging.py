from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_wheel_declares_calendar_dependencies_and_migrations() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = configuration["project"]["dependencies"]
    package_data = configuration["tool"]["setuptools"]["package-data"]

    assert "google-api-python-client>=2.140,<3" in dependencies
    assert "google-auth>=2.34,<3" in dependencies
    assert "core/migrations/*.sql" in package_data["hermes_cloud"]
