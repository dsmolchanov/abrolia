from __future__ import annotations

import json
import struct
import xml.etree.ElementTree as ElementTree
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LOGO_MARKER = 'data-abrolia-logo="handwritten-a"'
LOGO_PATH = (
    "M18 21 C31 8 50 8 58 18 C67 29 59 43 45 50 C31 57 14 54 10 45 "
    "C6 36 16 27 29 25 C43 23 54 29 59 41 C65 54 77 55 91 43"
)
OLD_ARROW_PATHS = (
    "M8 27V14.5C8 10.91 10.91 8 14.5 8H28",
    "M16 48V27c0-6.08 4.92-11 11-11h21",
)


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def _png_dimensions(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", payload[16:24])


def test_every_control_plane_page_renders_the_shared_logo(api_harness) -> None:
    world = api_harness.create_principal("brand-owner@family.test")
    pages = [
        api_harness.client.get("/start"),
        api_harness.client.get("/auth/verify"),
    ]
    api_harness.authenticate(world)
    pages.append(api_harness.client.get("/onboarding"))

    for page in pages:
        assert page.status_code == 200
        assert page.text.count(LOGO_MARKER) == 1
        assert LOGO_PATH in page.text
        assert '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">' in page.text

    favicon = api_harness.client.get("/static/favicon.svg")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")


def test_all_frontend_shells_use_the_handwritten_a_and_no_old_arrow() -> None:
    landing = _read("landing/index.html")
    web_chat = _read("web/index.html")
    control_plane_base = _read("control_plane/web/templates/base.html")

    assert landing.count(LOGO_MARKER) == 3
    assert web_chat.count(LOGO_MARKER) == 1
    assert control_plane_base.count(LOGO_MARKER) == 1
    for source in (landing, web_chat, control_plane_base, _read("landing/og-image.svg")):
        assert LOGO_PATH in source
        for old_path in OLD_ARROW_PATHS:
            assert old_path not in source

    template_root = REPOSITORY_ROOT / "control_plane/web/templates"
    child_templates = sorted(path for path in template_root.glob("*.html") if path.name != "base.html")
    assert child_templates
    for template in child_templates:
        assert '{% extends "base.html" %}' in template.read_text(encoding="utf-8")


def test_logo_favicons_and_pwa_icons_are_valid_brand_assets() -> None:
    favicon_paths = (
        "landing/favicon.svg",
        "control_plane/web/static/favicon.svg",
        "web/static/favicon.svg",
    )
    for relative_path in favicon_paths:
        ElementTree.parse(REPOSITORY_ROOT / relative_path)
        source = _read(relative_path)
        assert "#6E8BFF" in source.upper()
        assert "#FFB09F" in source.upper()
        for old_path in OLD_ARROW_PATHS:
            assert old_path not in source

    manifest = json.loads(_read("web/manifest.json"))
    assert manifest["start_url"] == "./index.html"
    assert manifest["scope"] == "./"
    assert manifest["background_color"] == "#f7fafc"
    assert manifest["theme_color"] == "#18222d"
    assert [icon["src"] for icon in manifest["icons"]] == [
        "static/icon-192.png",
        "static/icon-512.png",
    ]
    assert _png_dimensions(REPOSITORY_ROOT / "web/static/icon-192.png") == (192, 192)
    assert _png_dimensions(REPOSITORY_ROOT / "web/static/icon-512.png") == (512, 512)


def test_pwa_serves_the_logo_and_relative_brand_assets(api_harness) -> None:
    page = api_harness.client.get("/pwa/index.html")
    assert page.status_code == 200
    assert page.text.count(LOGO_MARKER) == 1
    assert LOGO_PATH in page.text
    assert 'href="manifest.json"' in page.text
    assert 'href="static/favicon.svg"' in page.text

    manifest = api_harness.client.get("/pwa/manifest.json")
    favicon = api_harness.client.get("/pwa/static/favicon.svg")
    app_icon = api_harness.client.get("/pwa/static/icon-192.png")
    assert manifest.status_code == 200
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert app_icon.status_code == 200
    assert app_icon.headers["content-type"] == "image/png"
