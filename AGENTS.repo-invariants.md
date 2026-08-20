
### A validated pathname is not a claim on the entry behind it

- **No recovery operation may delete, publish over, or install a filesystem
  entry on the strength of a check made at an earlier moment. A removal is
  authorised only while the entry is still the one this call published or
  copied; an install is authorised only while the member is byte-identical to
  what preflight validated; and a file this module intends to trust is opened
  ONCE, with `O_NOFOLLOW`, and every later question about it — size, digest,
  contents — is answered from that descriptor.** Enforced by
  `tests/control_plane/test_migrate_on_start.py::test_no_move_primitive_deletes_an_entry_it_stopped_owning`
  (every move primitive × regular-file, hard-link and symlink sentinels),
  `::test_no_validated_member_is_installed_after_it_changes` (in-place rewrite
  through a descriptor, which leaves the inode unchanged) and
  `::test_a_landed_member_removed_before_success_is_not_a_success`.

  Recorded after this class arrived in seven separate rounds on one pull
  request. The instances: a read-only validation deleting a dangling sidecar
  symlink it never created; the volume probe unlinking both names
  unconditionally after one had been vacated; `_undo` moving a destination
  another process had replaced; restore's cleanup unlinking a pause marker
  belonging to somebody else's restore; `create_backup` archiving a database
  renamed after it was authenticated; the fail-safe pause following a symlink
  and truncating its target; and the install loop consuming candidate
  pathnames rather than the entries that passed preflight. They are one
  missing rule, not seven findings.

  The general shape is TOCTOU, and the general remedy is that identity travels
  with the operation rather than being re-derived from the filesystem. Inode
  equality is necessary and NOT sufficient: a process holding an open
  descriptor rewrites a file in place without the inode moving, so anything
  whose correctness depends on the CONTENTS compares a digest taken at
  validation time.
