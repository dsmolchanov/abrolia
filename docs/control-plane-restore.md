# Control-plane backup and restore

The control-plane backup is separate from every household runtime backup. It
contains encrypted account/onboarding metadata, jobs, config manifests, consent
receipts, and tombstones; it never contains runtime provider secrets. Field
encryption and lookup/token HMAC keys are restored through an independently
protected secret procedure and are never embedded in the archive.

## Backup boundary

- Take a SQLite online snapshot from the one local `/data` volume.
- Encrypt it with the dedicated `ABROLIA_CONTROL_PLANE_BACKUP_KEY`, not the
  field-encryption or runtime backup key.
- Store archives in EU object storage with access logging and policy-defined
  rotation. Store the backup key and the three application keys separately.
- Monitor last successful backup age. A volume snapshot without an exercised
  restore is not a release gate.

Use an exact output path:

```bash
abrolia-control-plane backup /data/backups/control-plane-20260804.cpb
```

The archive uses authenticated AES-256-GCM. Secret values are read from the
environment/Fly secret namespace; they are absent from argv, archive metadata,
SQLite, and logs.

## Backup before migrate (migrate-on-start)

The container entrypoint runs the migration step before `serve`:

```bash
python -m control_plane.db migrate --backup-first
```

When at least one `control_plane/migrations/*.sql` is pending, it first writes
`/data/control-plane.db.pre-migrate-<last-applied-rev>-<epoch>.bak`, in the same
authenticated archive format as `abrolia-control-plane backup` and with the same
dedicated `ABROLIA_CONTROL_PLANE_BACKUP_KEY`, mode `0600`. It fails closed: a
missing or invalid backup key, or any snapshot error, exits non-zero **before**
touching the schema, and the container stops instead of serving. A migration
failure also exits non-zero and prints the archive path to restore from; the
whole pending batch is applied in ONE transaction, so a failure leaves no
partial schema — not within a file and not across files. That second half
matters on restart: a partially upgraded database would be snapshotted by the
next boot and recorded as the new pre-migrate archive, silently replacing the
good restore point with one taken of the broken schema. When nothing is
pending, no archive is written.

A failing migration restarts the container, and each boot reaches this step
again. It does **not** snapshot again: an existing `pre-migrate-<rev>-*` archive
for the unchanged revision is reused, so a restart loop cannot fill `/data` and
cost you the ability to take a restore point at all. Reuse requires that nothing
has been written since — the database and its WAL are checked against the
archive's timestamp — because a migration can be dropped from the next image,
the container can then serve at the same revision and take writes, and an
archive matching only by revision would be a restore point that silently loses
them. Nothing is ever deleted to reclaim space; discarding a restore point is
the one move that turns a disk problem into a data-loss problem.

Restore a pre-migrate archive with `--no-migrate`. The normal restore applies
pending migrations, which from the new image would immediately reapply — or fail
again on — the migration the archive exists to undo:

```bash
abrolia-control-plane restore /data/control-plane.db.pre-migrate-0008-1800000000.bak \
  --target /data/control-plane-rollback.db --no-migrate
```

The restored database keeps the archived schema and stays worker-paused.

**Then put it where the rolled-back image will look.** `deploy/control-plane/fly.toml`
pins `ABROLIA_CONTROL_PLANE_DB = "/data/control-plane.db"`, so rolling the image
back on its own reopens the migrated database this archive exists to undo — the
restore sits at `/data/control-plane-rollback.db` and is never read. The worker
pause lives beside the database it belongs to, as
`<db path>.workers-paused`, so it has to travel with it: moving the database
alone silently resumes the workers on a database that has not been reconciled.

Move both, with the service stopped and the original kept:

```bash
# The WAL and shared-memory sidecars belong to the database they sit beside. A
# service that was killed rather than stopped cleanly leaves a populated
# control-plane.db-wal; opening the RESTORED file at the same path would replay
# those migrated pages into it, and the superseded database would be separated
# from its own latest committed pages. Move all three together, always.
mv /data/control-plane.db     /data/control-plane.db.superseded-<epoch>
mv /data/control-plane.db-wal /data/control-plane.db.superseded-<epoch>-wal 2>/dev/null || true
mv /data/control-plane.db-shm /data/control-plane.db.superseded-<epoch>-shm 2>/dev/null || true
mv /data/control-plane.db.workers-paused /data/control-plane.db.workers-paused.superseded-<epoch> 2>/dev/null || true

mv /data/control-plane-rollback.db     /data/control-plane.db
mv /data/control-plane-rollback.db-wal /data/control-plane.db-wal 2>/dev/null || true
mv /data/control-plane-rollback.db-shm /data/control-plane.db-shm 2>/dev/null || true
mv /data/control-plane-rollback.db.workers-paused /data/control-plane.db.workers-paused
```

Confirm no sidecar from the superseded database is left at the canonical path
before starting anything — one that survives will be replayed into the restore:

```bash
ls /data/control-plane.db-wal /data/control-plane.db-shm 2>/dev/null
```

Confirm the pause survived the move before starting anything:

```bash
test -f /data/control-plane.db.workers-paused && echo "workers paused"
```

The alternative is to leave the file where it is and point the rolled-back
release at it by setting `ABROLIA_CONTROL_PLANE_DB=/data/control-plane-rollback.db`.
That works and it changes what a later reader of `fly.toml` believes about the
volume, so prefer the move unless you are keeping both databases deliberately.

Only then roll the image back to the pre-upgrade release, and only after
verifying the restore run `resume-jobs`.

## Isolated restore rehearsal

1. Stop the API/embedded worker and snapshot the original volume.
2. Attach a new isolated volume to a staging Machine with no public route and no
   real-provider flags.
3. Restore to a new path; never overwrite the only copy:

   ```bash
   abrolia-control-plane restore /restore/control-plane.cpb --target /data/control-plane.db
   ```

4. Verify AES-GCM authentication, `PRAGMA integrity_check`, migration history,
   foreign keys, WAL/FULL settings, table classification, and row counts.
5. Start the API with workers paused. Confirm login, masked `/me`, onboarding
   snapshot, export, health counters, and that no token/session/bootstrap hash
   appears in an export. The pause gates both provisioning leases and the
   sessionless pending-deletion orchestrator; no external delete resumes here.
6. Reconcile `running`/`outcome_unknown` jobs against exact provider refs. Do not
   re-run an external create based only on database status.
7. Explicitly resume jobs only after the review:

   ```bash
   abrolia-control-plane resume-jobs
   ```

8. Run one synthetic onboarding smoke, then destroy only the exact isolated
   staging resources.

A restore must reject a wrong key, tampered archive, corrupt SQLite file, or an
existing target. A deleted household is not restored into service: tombstones
and the deletion register are checked before jobs/bootstrap, and expired backup
objects are removed according to the privacy policy.

Record the rehearsal date, archive digest/size, backup age, restore duration,
schema versions, counts, reconciled job IDs, and final synthetic cleanup status
in the protected operations journal—never direct identifiers or credentials.
