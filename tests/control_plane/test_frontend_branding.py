from __future__ import annotations

import json
import struct
import xml.etree.ElementTree as ElementTree
from html.parser import HTMLParser
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


class _FrontendHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self.stylesheets: list[str] = []
        self.scripts: list[str] = []
        self.inline_styles = 0
        self.inline_scripts = 0
        self.inline_attributes: list[str] = []
        self.join_targets = 0
        self.pilot_form_actions: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id") == "join":
            self.join_targets += 1
        self.inline_attributes.extend(
            name for name, _value in attrs if name == "style" or name.startswith("on")
        )
        if tag == "a":
            self._anchor_href = attributes.get("href", "")
            self._anchor_text = []
        elif tag == "form" and "data-pilot-form" in attributes:
            self.pilot_form_actions.append(attributes.get("action", ""))
        elif tag == "style":
            self.inline_styles += 1
        elif tag == "script":
            source = attributes.get("src")
            if source:
                self.scripts.append(source)
            else:
                self.inline_scripts += 1
        elif tag == "link" and "stylesheet" in (attributes.get("rel") or "").split():
            self.stylesheets.append(attributes.get("href", ""))

    def handle_data(self, data: str) -> None:
        if self._anchor_href is not None:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor_href is not None:
            label = " ".join("".join(self._anchor_text).split())
            self.anchors.append((self._anchor_href, label))
            self._anchor_href = None
            self._anchor_text = []


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def _png_dimensions(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", payload[16:24])


def _parse_html(source: str) -> _FrontendHTMLParser:
    parser = _FrontendHTMLParser()
    parser.feed(source)
    return parser


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


def test_every_early_access_link_targets_the_public_request_flow() -> None:
    landing = _parse_html(_read("landing/index.html"))
    early_access_hrefs = [
        href for href, label in landing.anchors if label.casefold().startswith("request early access")
    ]

    assert len(early_access_hrefs) >= 2
    assert set(early_access_hrefs) == {"#join"}
    assert landing.join_targets == 1
    public_email_hrefs = {href for href, _label in landing.anchors if href.startswith("mailto:")}
    assert len(public_email_hrefs) == 1
    assert len(landing.pilot_form_actions) == 1
    assert set(landing.pilot_form_actions) == public_email_hrefs


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

    content_security_policy = page.headers["content-security-policy"]
    assert "style-src 'self'" in content_security_policy
    assert "script-src 'self'" in content_security_policy
    assert "'unsafe-inline'" not in content_security_policy
    shell = _parse_html(page.text)
    assert shell.stylesheets == ["static/app.css"]
    assert shell.scripts == ["static/app.js"]
    assert shell.inline_styles == 0
    assert shell.inline_scripts == 0
    assert shell.inline_attributes == []

    manifest = api_harness.client.get("/pwa/manifest.json")
    favicon = api_harness.client.get("/pwa/static/favicon.svg")
    app_icon = api_harness.client.get("/pwa/static/icon-192.png")
    stylesheet = api_harness.client.get("/pwa/static/app.css")
    script = api_harness.client.get("/pwa/static/app.js")
    service_worker = api_harness.client.get("/pwa/sw.js")
    anonymous_message = api_harness.client.post("/api/web/message", json={"text": "hello"})
    # Same-origin gate fires before authentication (house order), so a bare
    # anonymous post is refused with 403, not 401.
    assert anonymous_message.status_code == 403
    assert manifest.status_code == 200
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert app_icon.status_code == 200
    assert app_icon.headers["content-type"] == "image/png"
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert ".brand-stroke" in stylesheet.text
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert 'form.addEventListener("submit"' in script.text
    assert 'fetch("/api/web/message"' in script.text
    assert 'navigator.serviceWorker.register("./sw.js")' in script.text
    assert service_worker.status_code == 200
    assert "javascript" in service_worker.headers["content-type"]
    assert anonymous_message.headers["cache-control"] == "no-store"
