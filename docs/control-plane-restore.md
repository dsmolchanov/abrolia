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
