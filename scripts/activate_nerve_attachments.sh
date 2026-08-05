#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: activate_nerve_attachments.sh ORG_ID ACTOR" >&2
  exit 2
fi

org_id="$1"
actor="$2"
nerve_app="${NERVE_CONTROL_PLANE_APP:?NERVE_CONTROL_PLANE_APP is required}"

if [[ ! "${org_id}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]; then
  echo "org id must be a canonical lowercase UUID" >&2
  exit 2
fi
if [[ ! "${actor}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "actor must contain only letters, digits, dot, underscore, or hyphen" >&2
  exit 2
fi

flyctl ssh console --quiet --app "${nerve_app}" \
  --command "env NERVE_FLAGS_ACTOR=${actor} /app/nerve-flags set attachments --org ${org_id} --enabled=true"

echo "Attachments activation was audited. Wait 65 seconds, then resume the email check in Abrolia." >&2
