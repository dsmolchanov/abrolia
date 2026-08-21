"""Fail-closed contract for the privileged production deployment workflow."""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-production.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
CI_TEXT = CI_WORKFLOW.read_text(encoding="utf-8")

CHECKOUT = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
SETUP_NODE = "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020"
SETUP_PYTHON = "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
SETUP_FLY = (
    "superfly/flyctl-actions/setup-flyctl@"
    "ed8efb33836e8b2096c7fd3ba1c8afe303ebbff1"
)

EXPECTED_TRIGGER = """on:
  workflow_run:
    workflows: [ci]
    types: [completed]
    branches: [main]
"""

EXPECTED_GATE = (
    "${{ github.event.workflow_run.conclusion == 'success' && "
    "github.event.workflow_run.event == 'push' && "
    "github.event.workflow_run.head_branch == 'main' && "
    "github.event.workflow_run.head_repository.full_name == github.repository }}"
)

EXPECTED_CONCURRENCY_GROUP = (
    "${{ github.event.workflow_run.conclusion == 'success' && "
    "github.event.workflow_run.event == 'push' && "
    "github.event.workflow_run.head_branch == 'main' && "
    "github.event.workflow_run.head_repository.full_name == github.repository && "
    "'abrolia-production' || "
    "format('abrolia-nondeploy-{0}', github.run_id) }}"
)


def _job(name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)",
        TEXT,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"missing deployment job: {name}"
    return match.group("body")


def _compact(value: str) -> str:
    return " ".join(value.split())


def _top_level_section(name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}:\n(?P<body>.*?)(?=^[a-z][a-z0-9-]*:\n|\Z)",
        TEXT,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"missing top-level workflow section: {name}"
    return f"{name}:\n{match.group('body').rstrip()}\n"


def test_deploy_only_follows_a_successful_first_party_main_ci_push() -> None:
    assert CI_TEXT.startswith("name: ci\n")
    assert _top_level_section("on") == EXPECTED_TRIGGER

    gate = _job("authorize-release")
    condition = re.search(
        r"^    if: >-\n(?P<body>(?:      .+\n)+)", gate, re.MULTILINE
    )
    assert condition
    assert _compact(condition.group("body")) == EXPECTED_GATE

    for name in ("deploy-landing", "deploy-control-plane"):
        job = _job(name)
        assert re.findall(r"^    needs:\s*(\S+)\s*$", job, re.MULTILINE) == [
            "authorize-release"
        ]
        assert "always()" not in job


def test_every_deploy_uses_the_tested_current_main_commit() -> None:
    deploy_jobs = (_job("deploy-landing"), _job("deploy-control-plane"))
    for job in deploy_jobs:
        assert "ref: ${{ github.event.workflow_run.head_sha }}" in job
        assert "persist-credentials: false" in job

    assert "github.sha" not in TEXT
    assert not re.search(r"^\s+ref:\s+main\s*$", TEXT, re.MULTILINE)
    assert TEXT.count('gh api "repos/$GITHUB_REPOSITORY/commits/main" --jq .sha') == 3
    assert TEXT.count('test "$current_sha" = "$DEPLOY_SHA"') == 3


def test_permissions_dependencies_and_production_concurrency_are_immutable() -> None:
    permissions = re.search(
        r"^permissions:\n(?P<body>(?:  .+\n)+)\n", TEXT, re.MULTILINE
    )
    assert permissions
    assert permissions.group("body") == "  contents: read\n"
    assert ": write" not in TEXT

    actions = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", TEXT, re.MULTILINE)
    assert actions == [CHECKOUT, SETUP_NODE, CHECKOUT, SETUP_FLY]
    ci_actions = re.findall(
        r"^\s*(?:-\s*)?uses:\s*(\S+)", CI_TEXT, re.MULTILINE
    )
    assert ci_actions == [CHECKOUT, SETUP_PYTHON, CHECKOUT]

    concurrency = _top_level_section("concurrency")
    group = re.search(
        r"^  group: >-\n(?P<body>(?:    .+\n)+)", concurrency, re.MULTILINE
    )
    assert group
    assert _compact(group.group("body")) == EXPECTED_CONCURRENCY_GROUP
    assert concurrency.count("  cancel-in-progress: false\n") == 1
    assert concurrency.count("  queue: max\n") == 1
    for unsafe in ("continue-on-error", "|| true", "set +e", "@latest"):
        assert unsafe not in TEXT


def test_provider_credentials_are_fail_closed_and_isolated() -> None:
    landing = _job("deploy-landing")
    control_plane = _job("deploy-control-plane")

    assert "name: production-landing" in landing
    assert "secrets.VERCEL_TOKEN" in landing
    assert "vars.VERCEL_ORG_ID" in landing
    assert "vars.VERCEL_PROJECT_ID" in landing
    assert "FLY_API_TOKEN" not in landing
    assert "VERCEL_TOKEN is not configured" in landing
    assert "VERCEL_ORG_ID is not configured" in landing
    assert "VERCEL_PROJECT_ID is not configured" in landing

    assert "name: production-control-plane" in control_plane
    assert "secrets.FLY_API_TOKEN" in control_plane
    assert "VERCEL_" not in control_plane
    assert "FLY_API_TOKEN is not configured" in control_plane

    secret_references = re.findall(r"secrets\.([A-Z0-9_]+)", TEXT)
    variable_references = re.findall(r"vars\.([A-Z0-9_]+)", TEXT)
    assert secret_references == ["VERCEL_TOKEN", "VERCEL_TOKEN", "FLY_API_TOKEN"]
    assert variable_references == [
        "VERCEL_ORG_ID",
        "VERCEL_PROJECT_ID",
        "VERCEL_ORG_ID",
        "VERCEL_PROJECT_ID",
    ]
    assert landing.count("secrets.VERCEL_TOKEN") == 2
    assert control_plane.count("secrets.FLY_API_TOKEN") == 1
    assert "secrets." not in _job("authorize-release")

    assert "prj_" not in TEXT
    assert "team_" not in TEXT


def test_vercel_builds_in_ci_then_publishes_the_prebuilt_output() -> None:
    landing = _job("deploy-landing")
    assert "npm install --global vercel@48.6.0" in landing
    pull = landing.index(
        'vercel pull --yes --environment=production --cwd landing --token "$VERCEL_TOKEN"'
    )
    build = landing.index(
        'vercel build --prod --cwd landing --token "$VERCEL_TOKEN"'
    )
    recheck = landing.index(
        "Recheck main immediately before publishing the landing page"
    )
    deploy = landing.index(
        'vercel deploy --prebuilt --prod --yes --cwd landing --token "$VERCEL_TOKEN"'
    )
    assert pull < build < recheck < deploy
    assert "https://*.vercel.app" in landing


def test_fly_deploy_uses_the_root_context_and_pinned_target() -> None:
    raw = _job("deploy-control-plane")
    control_plane = _compact(raw.replace("\\\n", " "))
    assert "version: 0.4.58" in control_plane
    assert (
        "flyctl deploy . --app abrolia-control-plane-synthetic "
        "--config deploy/control-plane/fly.toml "
        "--dockerfile deploy/control-plane/Dockerfile "
        "--remote-only --yes --ha=false --wait-timeout 10m"
    ) in control_plane
    setup = raw.index("Install the pinned Fly CLI")
    recheck = raw.index(
        "Recheck main immediately before publishing the control plane"
    )
    deploy = raw.index("flyctl deploy .")
    assert setup < recheck < deploy


def test_live_verification_is_required_for_both_frontends() -> None:
    landing = _job("deploy-landing")
    control_plane = _job("deploy-control-plane")

    deploy = landing.index("vercel deploy --prebuilt")
    verify = landing.index("Verify the deployment and canonical landing domain")
    assert deploy < verify
    assert "${DEPLOYMENT_URL%/}/" in landing
    assert "https://abrolia.com/" in landing
    assert 'test "$deployed_body" = "$expected_body"' in landing
    assert '[[ "$canonical_body" == "$expected_body" ]]' in landing
    assert "sha256sum landing/favicon.svg" in landing
    assert 'data-abrolia-logo=\"handwritten-a\"' in landing
    assert 'id=\"join\"' in landing

    fly_deploy = control_plane.index("flyctl deploy .")
    live_verify = control_plane.index(
        "Verify control-plane health and public frontend assets"
    )
    assert fly_deploy < live_verify
    for path in (
        "/healthz",
        "/readyz",
        "/start",
        "/pwa/index.html",
        "manifest.json",
        "sw.js",
        "static/app.css",
        "static/app.js",
        "static/favicon.svg",
        "static/icon-192.png",
        "static/icon-512.png",
    ):
        assert path in control_plane
    assert '.status == \"healthy\"' in control_plane
    assert '.status == \"ready\"' in control_plane
    assert 'test "$pwa" = "$expected_pwa"' in control_plane
    assert 'sha256sum "web/$asset"' in control_plane
    assert 'data-abrolia-logo=\"handwritten-a\"' in control_plane
