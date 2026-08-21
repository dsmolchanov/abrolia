from __future__ import annotations

import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE_DOCKERFILE = ROOT / "deploy/control-plane/Dockerfile"


def test_runtime_wheel_declares_calendar_dependencies_and_migrations() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = configuration["project"]["dependencies"]
    package_data = configuration["tool"]["setuptools"]["package-data"]

    assert "google-api-python-client>=2.140,<3" in dependencies
    assert "google-auth>=2.34,<3" in dependencies
    assert "core/migrations/*.sql" in package_data["hermes_cloud"]


def test_runtime_wheel_contains_the_control_plane_pwa(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(tmp_path),
            str(ROOT),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wheels = list(tmp_path.glob("abrolia-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        packaged = set(archive.namelist())

    required = {
        "web/index.html",
        "web/manifest.json",
        "web/sw.js",
        "web/static/app.css",
        "web/static/app.js",
        "web/static/favicon.svg",
        "web/static/icon-192.png",
        "web/static/icon-512.png",
    }
    assert required <= packaged, f"PWA assets missing from runtime wheel: {sorted(required - packaged)}"


def test_control_plane_image_copies_the_pwa_before_building_the_wheel() -> None:
    instructions = [
        line.strip()
        for line in CONTROL_PLANE_DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    copy_pwa = instructions.index("COPY web ./web")
    build_wheel = instructions.index("RUN pip wheel --wheel-dir /wheels .")
    assert copy_pwa < build_wheel
