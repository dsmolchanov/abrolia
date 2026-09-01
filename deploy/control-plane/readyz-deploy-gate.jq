# Is this control plane all right?
#
# Asked at BOTH ends of a deploy: before, "may I mutate this?", and after,
# "did what I just shipped come up?" One file, because two spellings of the
# same question drift — and did: the pre-deploy gate was fixed for the loop
# below while the post-deploy verification kept asking `.status == "ready"`
# behind `curl --fail`, so a deploy that fully succeeded still reported
# failure.
#
# Not the same question as "is it fully ready", and conflating the two is what
# killed production deploys for nine days.
#
# `/readyz` reports `backup_stale` once the newest archive is older than 26h
# (`control_plane/api/app.py`). The archive is written at CONTAINER START, by
# the `migrate --backup-first` step in the Dockerfile CMD, and it cannot be
# written any other way while the service runs: `abrolia-control-plane backup`
# takes the process lock and refuses with "Stop the service first."
#
# So a deploy is the only thing that refreshes the backup, and gating the
# deploy on a fresh backup made that action wait for itself. The last
# successful deploy was 2026-08-22; the first failure was 26 hours later, and
# every deploy since failed on this check.
#
# `backup_stale` is therefore a reason TO deploy, not a reason to refuse one.
# Every other blocker still holds: a database, volume, worker or provider
# problem is a state a deploy would make worse, not better.
#
# Both arms name the status they accept. The exception arm below once tested
# only the blocker list, so `{"blockers":["backup_stale"]}` with no status at
# all — or with `"status":"broken"` — was read as a green light. That is the
# one shape this gate must never guess about: a body that does not state a
# readiness status is not a readiness answer, and a status this gate does not
# know is not one it may treat as the benign case. The stricter post-deploy
# check that used to catch it was replaced by this file, so the exception had
# to grow the status test rather than inherit a permissive one.
# `backup_writer_failed` is excused for the SAME reason and not for a weaker
# one: a deploy cannot fix a directory the service may not write to, so gating
# on it would repeat the nine-day deadlock exactly — refusing the deploy for a
# condition the deploy was never able to clear. It is excused here and NAMED in
# the blocker list, which is the half that was missing: a broken writer used to
# be indistinguishable from a quiet week, because `backup_stale` said both.
(.status == "ready")
or (
  .status == "not_ready"
  and ((.blockers // []) | length) > 0
  and (
    ((.blockers // []) - ["backup_stale", "backup_writer_failed"]) | length
  ) == 0
)
