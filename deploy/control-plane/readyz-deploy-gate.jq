# Is this control plane safe to DEPLOY ONTO?
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
(.status == "ready")
or (
  ((.blockers // []) | length) > 0
  and (((.blockers // []) - ["backup_stale"]) | length) == 0
)
