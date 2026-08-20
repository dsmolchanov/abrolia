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

The container entrypoint runs the migration step before `serve`. It takes the
same writer lock `serve` takes and refuses if another process holds it: a
snapshot that overlaps a live writer can miss a commit that lands between the
archive and the migration, leaving a restore point that silently omits data.

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

Both the snapshot and the rollback restore stream through disk in bounded
chunks, so neither is limited by the Machine's 512 MiB of RAM. The intermediate
image is written to ephemeral storage rather than beside the database — putting
an image and an archive, each about the size of the database, on the same 1 GiB
volume would exhaust it while trying to take a backup. Set
`ABROLIA_BACKUP_SCRATCH_DIR` if the machine's default temporary directory is not
where you want that image.

A failing migration restarts the container, and each boot reaches this step
again. It does **not** snapshot again: an existing `pre-migrate-<rev>-*` archive
for the unchanged revision is reused, so a restart loop cannot fill `/data` and
cost you the ability to take a restore point at all. Reuse requires that nothing
has been written since, and that is decided by CONTENT: the archive is decrypted
and hashed against a fresh image of the database. Timestamps were the obvious
signal and the wrong one — a migration that fails still opens a transaction and
checkpoints on close, so the mtimes advance even though nothing was committed,
every restart looked like a change, and each one wrote another archive. The
comparison has to be by content anyway, because a migration can be dropped from
the next image, the container can then serve at the same revision and take
writes, and an archive matching only by revision would be a restore point that
silently loses them. Nothing is ever deleted to reclaim space; discarding a restore point is
the one move that turns a disk problem into a data-loss problem.

Restore a pre-migrate archive with `--no-migrate`. The normal restore applies
pending migrations, which from the new image would immediately reapply — or fail
again on — the migration the archive exists to undo:

**Check the space first.** At this point `/data` already holds the live
database and the archive. Restoring beside them puts a third full-size file on
the same 1 GiB volume, so a database between roughly a third and half of the
volume fails with `ENOSPC` — with a perfectly good backup sitting right there.
The restore is the moment that must not fail for want of room.

```bash
df -h /data
ls -l /data/control-plane.db /data/control-plane.db.pre-migrate-*.bak
```

If the live database, the archive and one more copy all fit, restore beside the
database, which keeps everything on the volume that survives a Machine
replacement:

```bash
abrolia-control-plane restore /data/control-plane.db.pre-migrate-0008-1800000000.bak \
  --target /data/control-plane-rollback.db --no-migrate
```

If they do not, stage the restore off the volume. `/tmp` trades durability for
room — it does not survive a Machine restart — so complete the install below in
the same session:

```bash
abrolia-control-plane restore /data/control-plane.db.pre-migrate-0008-1800000000.bak \
  --target /tmp/control-plane-rollback.db --no-migrate
```

The restored database keeps the archived schema and stays worker-paused.

**Then install it where the rolled-back image will look.**
`deploy/control-plane/fly.toml` pins `ABROLIA_CONTROL_PLANE_DB = "/data/control-plane.db"`,
so rolling the image back on its own reopens the migrated database this archive
exists to undo — a restore left at `/tmp/control-plane-rollback.db` is never
read.

```bash
abrolia-control-plane install-rollback \
  --restored /tmp/control-plane-rollback.db \
  --target /data/control-plane.db \
  --superseded-to /tmp
```

This was a column of `mv` commands, and it is a command now because three of the
steps have a failure that does not announce itself:

- **The install itself can run out of room.** Staging off the volume only moved
  where the restore was written; moving it in is a *copy*, and renaming the
  superseded database aside frees nothing, because it is the same filesystem and
  the same blocks. So when the copy will not fit, `install-rollback` writes the
  superseded database to `--superseded-to` as an authenticated archive in the
  same format as every other backup, **reads that archive back**, and only then
  releases the blocks. Nothing is deleted before its replacement has been shown
  to open; freeing space by dropping a restore point is the one move that turns
  a disk problem into a data-loss problem. If `--superseded-to` is on the volume
  being freed, or the restore still does not fit afterwards, the command stops
  and tells you where everything is.
- **A `-wal` left behind is replayed into the restore.** A service that was
  killed rather than stopped leaves committed frames in `control-plane.db-wal`;
  beside the installed rollback, SQLite replays them and quietly reapplies the
  migrated pages you are rolling back. The sidecars move with the database they
  belong to, in both directions, and the command refuses to finish if one is
  still at the canonical path.
- **The worker pause is a sibling file**, `<db path>.workers-paused`, so it does
  not follow the database unless something moves it. Moving the database alone
  resumes the workers against data nobody has reconciled. The command installs
  the marker and fails if it did not arrive.

It refuses before touching anything if a control-plane writer still holds
`/data/control-plane.db.writer.lock` — the same lock `serve` takes. Renaming a
database and its sidecars out from under a live SQLite connection does not fail
loudly; the process keeps its descriptors on the superseded inode and its writes
land in a file nothing will read again. **Stop the service first.**

It also validates everything before the live database moves: that the candidate
and the target are genuinely different files (by inode, so a hard link cannot
pass as a rollback), that the target is a regular file, that the restore opens
as SQLite and passes `integrity_check` and `foreign_key_check`, that a worker
pause marker sits beside it — and, when the install will need to free space,
that `--superseded-to` exists, is writable, is off the volume, and has no
archive already under the name this run would take. A bundle that cannot be
installed is refused with `/data` exactly as it was, so the obvious retry works.

The space it reserves covers the whole bundle — database, sidecars **and** the
pause marker — in allocated blocks. A gate that only covered the database could
pass, copy the database, and then fail on the marker, leaving a restored
database at the canonical path with nothing pausing the workers.

Both `restore` and `install-rollback` read `ABROLIA_CONTROL_PLANE_BACKUP_KEY`
and nothing else. They do not need the field-encryption keys, the HMAC keys, or
any Fly setting — recovery must not require the deployment it is recovering to
be intact.

It prints what it did, including where the superseded database ended up:

```json
{"superseded_kept_as": "/tmp/control-plane.db.superseded-1800000042.cpb",
 "sidecars": [], "target": "/data/control-plane.db", "workers": "paused"}
```

Recover from that archive with the ordinary restore command and the same backup
key, should you need anything written after the migration:

```bash
abrolia-control-plane restore /tmp/control-plane.db.superseded-1800000042.cpb \
  --target /data/control-plane-superseded.db --no-migrate
```

The alternative to installing at all is to leave the restore where it is and
point the rolled-back release at it by setting
`ABROLIA_CONTROL_PLANE_DB=/data/control-plane-rollback.db`. That works, and it
changes what a later reader of `fly.toml` believes about the volume, so prefer
the install unless you are keeping both databases deliberately.

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
