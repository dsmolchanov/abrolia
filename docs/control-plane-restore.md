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
  --target /data/control-plane.db
```

This was a column of `mv` commands, and it is a command now because three of the
steps have a failure that does not announce itself:

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
- **The install can run out of room.** Staging off the volume only moved where
  the restore was written; moving it in is a *copy*, and renaming the superseded
  database aside frees nothing, because it is the same filesystem and the same
  blocks. The command checks this **before it moves anything** and refuses,
  naming the bytes needed and the bytes free.

**It does not free space for you, by design.** An earlier version archived the
superseded database off-volume and deleted it to make room — which meant a
command you run to move a file could destroy the only copy of every write taken
after the migration. If it refuses for space, do this yourself, in this order,
and only then run it again:

**Stop the service before step 1 and leave it stopped through step 3.** `backup`
and `install-rollback` both take the writer lock and refuse while another
process holds it, so those two are guarded. The `rm` between them is not — it
is a shell command, and nothing stops it unlinking files a running `serve` still
has open, splitting acknowledged writes across an inode nothing will read again.
The two guarded commands bracket it, so a `backup` that succeeded means no
writer held the lock a moment earlier; that is not the same as a guarantee.

```bash
# 1. Archive the superseded database OFF the volume, and verify it opens.
#    `backup` does not migrate — the reason you are here is often a migration
#    that fails, and a command that repeats it would exit before writing.
abrolia-control-plane backup /tmp/control-plane-superseded.cpb
abrolia-control-plane restore /tmp/control-plane-superseded.cpb \
  --target /tmp/verify.db --no-migrate && rm -f /tmp/verify.db /tmp/verify.db*

# 2. Only now release the blocks. ALL FOUR: the pause marker is part of the
#    bundle, and a survivor makes step 3 refuse with "target was not freed"
#    after the destructive step has already run.
rm -f /data/control-plane.db /data/control-plane.db-wal \
      /data/control-plane.db-shm /data/control-plane.db.workers-paused

# 3. Install, telling the command the target is gone on purpose.
abrolia-control-plane install-rollback \
  --restored /tmp/control-plane-rollback.db \
  --target /data/control-plane.db \
  --target-already-freed
```

Never skip step 1. Discarding a restore point to reclaim space is the one move
that turns a disk problem into a data-loss problem, and the pre-migrate archive
covers the state *before* the migration — not the writes taken after it.

Step 3 needs the flag because step 2 deleted the database the command would
otherwise supersede, and a missing `--target` is far more often a typo than a
deliberate state. With the flag it verifies that the whole bundle really is
gone — a leftover `-wal` means the target was not freed — and then installs with
every other check intact. Without step 3's flag the command refuses and says so,
which is how this gap was found: the documented procedure previously ended with
no database and a command that would not run.

Before anything moves, the command also refuses if a control-plane writer still
holds `/data/control-plane.db.writer.lock` (the same lock `serve` takes —
renaming a database out from under a live SQLite connection does not fail
loudly), if the candidate and the target are the same file by inode, if any
bundle member on either side is not a regular file, if the restore does not open
as SQLite or has no pause marker, or if the superseded names it would need are
already taken or too long for the filesystem. Every one of those leaves `/data`
exactly as it was, so the obvious retry works.

It prints what it did, including where the superseded bundle went:

```json
{"sidecars": [], "superseded_kept_as": "/data/control-plane.db.superseded-1800000042",
 "target": "/data/control-plane.db", "workers": "paused"}
```

Nothing is deleted: the superseded database and its sidecars are renamed aside,
intact, under that name.

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
