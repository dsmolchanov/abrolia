"""Fail-closed contract for the privileged production deployment workflow."""

from __future__ import annotations

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-production.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
VERCEL_CONFIG = ROOT / "landing" / "vercel.json"
TEXT = WORKFLOW.read_text(encoding="utf-8")
CI_TEXT = CI_WORKFLOW.read_text(encoding="utf-8")
VERCEL = json.loads(VERCEL_CONFIG.read_text(encoding="utf-8"))

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

EXPECTED_LANDING_PUBLISH_STEP = r'''        id: deploy
        env:
          NO_COLOR: "1"
          VERCEL_TOKEN: ${{ secrets.VERCEL_TOKEN }}
          VERCEL_ORG_ID: ${{ vars.VERCEL_ORG_ID }}
          VERCEL_PROJECT_ID: ${{ vars.VERCEL_PROJECT_ID }}
        run: |
          set -euo pipefail
          : "${VERCEL_TOKEN:?production-landing VERCEL_TOKEN is not configured}"
          : "${VERCEL_ORG_ID:?production-landing VERCEL_ORG_ID is not configured}"
          : "${VERCEL_PROJECT_ID:?production-landing VERCEL_PROJECT_ID is not configured}"

          deployment_url="$(
            vercel deploy --prebuilt --prod --yes --cwd landing --token "$VERCEL_TOKEN"
          )"
          case "$deployment_url" in
            https://*.vercel.app) ;;
            *) echo "Vercel returned an invalid deployment URL" >&2; exit 1 ;;
          esac
          printf 'url=%s\n' "$deployment_url" >> "$GITHUB_OUTPUT"'''

EXPECTED_LANDING_VERIFY_STEP = r'''        env:
          DEPLOYMENT_URL: ${{ steps.deploy.outputs.url }}
        run: |
          set -euo pipefail
          fetch() {
            local url="$1"
            local destination="$2"
            local status
            status="$(
              curl --fail --silent --show-error \
                --retry 5 --retry-all-errors --retry-delay 2 \
                --connect-timeout 10 --max-time 30 \
                --output "$destination" --write-out '%{http_code}' "$url"
            )"
            test "$status" = 200
          }

          protection_headers="$(
            curl --silent --show-error --head \
              --retry 5 --retry-all-errors --retry-delay 2 \
              --connect-timeout 10 --max-time 30 \
              "${DEPLOYMENT_URL%/}/"
          )"
          grep -Eq '^HTTP/[0-9.]+ 302[[:space:]]*$' <<<"$protection_headers"
          grep -Eiq '^location: https://vercel\.com/sso-api\?url=' \
            <<<"$protection_headers"

          canonical_file="$RUNNER_TEMP/abrolia-canonical-index.html"
          canonical_matches=false
          for _attempt in {1..12}; do
            if fetch https://abrolia.com/ "$canonical_file" && \
              cmp --silent landing/index.html "$canonical_file"
            then
              canonical_matches=true
              break
            fi
            sleep 5
          done
          test "$canonical_matches" = true
          grep -F 'data-abrolia-logo="handwritten-a"' "$canonical_file"
          grep -F 'id="join"' "$canonical_file"

          expected_favicon="$(sha256sum landing/favicon.svg | cut -d ' ' -f1)"
          canonical_favicon_file="$RUNNER_TEMP/abrolia-canonical-favicon.svg"
          fetch https://abrolia.com/favicon.svg "$canonical_favicon_file"
          canonical_favicon="$(sha256sum "$canonical_favicon_file" | cut -d ' ' -f1)"
          test "$canonical_favicon" = "$expected_favicon"'''


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


def _step(job: str, name: str) -> str:
    match = re.search(
        rf"^      - name: {re.escape(name)}\n(?P<body>.*?)(?=^      - name: |\Z)",
        job,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"missing workflow step: {name}"
    return match.group("body").rstrip()


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
    for unsafe in ("continue-on-error", "|| true", "|| :", "set +e", "@latest"):
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
    # `FLY_API_TOKEN` twice, and the same token both times: the boot-critical
    # configuration preflight calls `flyctl secrets list` before the deploy
    # step calls `flyctl deploy`. The point of this assertion is that the
    # workflow reaches for no OTHER secret, so a second use of one already
    # authorized here is a change to record rather than a widening.
    assert secret_references == [
        "VERCEL_TOKEN",
        "VERCEL_TOKEN",
        "FLY_API_TOKEN",
        "FLY_API_TOKEN",
    ]
    assert variable_references == [
        "VERCEL_ORG_ID",
        "VERCEL_PROJECT_ID",
        "VERCEL_ORG_ID",
        "VERCEL_PROJECT_ID",
    ]
    assert landing.count("secrets.VERCEL_TOKEN") == 2
    # Twice: the boot-critical configuration preflight reads the app's secret
    # NAMES before the deploy step mutates anything. Same token, two steps —
    # the isolation this test protects is which credential the job may reach
    # for, not how many times it reaches for it.
    assert control_plane.count("secrets.FLY_API_TOKEN") == 2
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
    assert landing.count('deployment_url="$(') == 1
    assert landing.count("deployment_url=") == 1
    assert (
        'deployment_url="$(\n'
        '            vercel deploy --prebuilt --prod --yes --cwd landing '
        '--token "$VERCEL_TOKEN"\n'
        '          )"'
    ) in landing
    assert 'printf \'url=%s\\n\' "$deployment_url" >> "$GITHUB_OUTPUT"' in landing
    assert _step(landing, "Publish the prebuilt landing artifact") == (
        EXPECTED_LANDING_PUBLISH_STEP
    )


def test_landing_routes_the_public_root_without_a_clean_url_transform() -> None:
    assert set(VERCEL) == {"$schema", "trailingSlash", "rewrites", "headers"}
    assert "cleanUrls" not in VERCEL
    assert VERCEL["rewrites"] == [{"source": "/", "destination": "/index.html"}]
    assert VERCEL["trailingSlash"] is False


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
    # Boot-critical configuration is checked FIRST, and before anything is
    # mutated: `ABROLIA_RUNTIME_MODEL_API_KEY` was required at boot, named in
    # no deploy configuration, and discovered only when a machine tried to
    # start — nine days late, as an outage rather than a red check.
    secrets = raw.index("Require every boot-critical secret before mutation")
    readiness = raw.index("Require a deployable control plane before mutation")
    recheck = raw.index(
        "Recheck main immediately before publishing the control plane"
    )
    deploy = raw.index("flyctl deploy .")
    assert setup < secrets < readiness < recheck < deploy
    preconditions = raw[secrets:readiness]
    # Read from the manifest rather than a list inlined here, so a name added
    # to one is added to both.
    assert "deploy/control-plane/required-runtime-config.txt" in preconditions
    # `.name`, lowercase: `flyctl secrets list --json` returns `name`, and
    # `.Name` yielded null for every entry — which reported EVERY secret
    # missing and would have failed every deploy. Caught by running the check
    # against the real app before shipping it.
    assert "jq -r '.[].name'" in preconditions
    preflight = raw[readiness:recheck]
    assert "https://app.abrolia.com/readyz" in preflight
    # The predicate is NOT inlined here any more. It lives in a file that this
    # gate and `tests/control_plane/test_deploy_gate.py` both read, so the
    # question "may we deploy onto this?" has one answer rather than a copy in
    # a workflow and a copy in a test that can drift apart.
    assert "deploy/control-plane/readyz-deploy-gate.jq" in preflight
    # `curl --fail` would discard the body on the 503 this gate now has to
    # READ: the blocker list is the whole answer. Its absence is the fix, so it
    # is asserted rather than left to survive by luck. Matched on the
    # invocation, not the bare flag, because the step's own comment names it.
    assert "curl --fail" not in preflight
    # A non-readiness answer is still a refusal.
    assert 'test "$code" = 200 || test "$code" = 503' in preflight


def test_both_ends_of_the_deploy_ask_the_same_readiness_question() -> None:
    """The pre-deploy gate and the post-deploy check share one predicate.

    They did not. The gate was corrected for the `backup_stale` loop and the
    verification kept `.status == "ready"` behind `curl --fail`, so a deploy
    that preflighted, gated, mutated and came up healthy still reported
    failure — and the fix looked complete because the half that was tested was
    the half that had been fixed.
    """
    control_plane = _job("deploy-control-plane")
    assert control_plane.count("deploy/control-plane/readyz-deploy-gate.jq") == 2
    # Neither end may go back to demanding a fresh backup. Matched on the
    # INVOCATION, because the steps' comments name the old predicate to explain
    # why it is gone.
    assert """jq -e '.status == "ready"'""" not in control_plane


def test_live_verification_is_required_for_both_frontends() -> None:
    landing = _job("deploy-landing")
    control_plane = _job("deploy-control-plane")

    deploy = landing.index("vercel deploy --prebuilt")
    verify = landing.index("Verify the deployment and canonical landing domain")
    assert deploy < verify
    assert _step(landing, "Verify the deployment and canonical landing domain") == (
        EXPECTED_LANDING_VERIFY_STEP
    )
    assert "${DEPLOYMENT_URL%/}/" in landing
    assert "https://abrolia.com/" in landing
    protection = landing[
        landing.index('protection_headers="$(') : landing.index(
            'canonical_file="$RUNNER_TEMP/'
        )
    ]
    assert "curl --silent --show-error --head" in protection
    assert '"${DEPLOYMENT_URL%/}/"' in protection
    assert "--location" not in protection
    assert "HTTP/[0-9.]+ 302" in protection
    assert r"vercel\.com/sso-api\?url=" in protection
    assert '--output "$destination" --write-out \'%{http_code}\'' in landing
    assert 'test "$status" = 200' in landing
    assert "--location" not in landing
    assert 'canonical_matches=false' in landing
    assert 'cmp --silent landing/index.html "$canonical_file"' in landing
    assert 'test "$canonical_matches" = true' in landing
    assert '[[ "$canonical_body" == "$expected_body" ]]' not in landing
    assert 'expected_favicon="$(sha256sum landing/favicon.svg' in landing
    assert 'fetch https://abrolia.com/favicon.svg "$canonical_favicon_file"' in landing
    assert 'canonical_favicon="$(sha256sum "$canonical_favicon_file"' in landing
    assert 'test "$canonical_favicon" = "$expected_favicon"' in landing
    assert 'data-abrolia-logo=\"handwritten-a\"' in landing
    assert 'id=\"join\"' in landing
    for bypass in (
        "VERCEL_AUTOMATION_BYPASS_SECRET",
        "x-vercel-protection-bypass",
        "--protection-bypass",
        "vercel curl",
    ):
        assert bypass.casefold() not in TEXT.casefold()

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
