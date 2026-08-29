# Repository blocking invariants

This file is **owned by this repository**. `plaintalk-dev-agent` installs it once
and never overwrites it, so anything added here survives every fleet-wide policy
refresh.

The fleet-wide invariants live in the `dev-agent:policy` managed block in
`AGENTS.md` and are replaced wholesale on each refresh — do not add
repository-specific entries there, they will be deleted.

## How to use this file

Add an entry when a P1 class keeps coming back; `AGENTS.md`, "Fix the invariant,
not the instance", says at what point. A repeated finding is
a missing rule, not a new discovery: semantic review is an expensive way to
rediscover the same defect, and each round costs a full review generation.

Per the fleet policy, the commit that fixes a recurring P1 should also add the
deterministic check that would have caught it — a lint rule, a test, or a
migration assertion. This list is the human-readable index of those rules, not a
replacement for them.

Each entry should be a closed question a reviewer can answer yes or no. "No route
without an auth dependency" is checkable; "input should be validated" is not, and
an open-ended predicate is satisfiable on any non-trivial diff, which is what
makes a review loop unable to terminate.

## Invariants

<!-- Add entries below. Example shape:

- No handler under `apps/api/routes/` may be registered without a
  `Depends(require_tenant)` argument. Enforced by `tests/test_route_auth.py`.

-->

### Merge authorisation comes only from a verified current-head verdict

- **No `exit 0` in the codex-review-window gate may be reached except from a
  Codex signal bound to the CURRENT head commit and verified to carry no
  BLOCKING marker.** What counts as blocking is round-budgeted since gate v2
  (fleet policy, 2026-08-29): any P0/P1/[BLOCKER] for the first three
  completed review generations, P0 only afterwards — P1s on a budget-exceeded
  round keep their badges and are batch-fixed, but no longer decide the check.
  Absence of a verdict, absence of a severity marker in a comment that is not
  the verdict, a verdict for another head, a signal from any account other
  than the Codex bot, a repository-wide opt-out, and any GitHub API failure
  are all NOT authorisations, on every round. The gate body and its
  executable tests live in `dsmolchanov/codex-review-gate` (this repository
  carries only the pinned calling stub); the exit-0 count, the fail-closed
  paths, and the round-budget behavior are asserted there by
  `tests/test_gate_workflow.py` and `tests/test_gate_behavior.py`.

  Recorded after three instances of this class arrived in a single review, plus
  a fourth found by the tests themselves: a marker-absence predicate that read
  Codex's "create a Codex account" error as approval; a clean signal honoured
  without re-querying for a formal review published moments later; a
  `fast-merge` repository topic that disabled the gate for every pull request
  indefinitely; and an unterminated `printf` that made the clean-comment count
  one short. They are one missing rule, not four findings.

  A corollary worth stating because it cost six days: the gate must ASK from an
  identity Codex will serve. Codex refuses requests from `github-actions[bot]`,
  which has no connected account, so the anchor goes out under
  `CODEX_REQUEST_TOKEN` and an unauthenticated token fails closed rather than
  falling back.


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
  equality is necessary and NOT sufficient, for two measured reasons. A process
  holding an open descriptor rewrites a file in place without the inode moving.
  And a filesystem hands the just-freed inode straight back — Linux did, on the
  CI runner, where an unrelated file written at a name this operation had
  vacated landed on the very same `(st_dev, st_ino)` and an inode-only check
  called it ours. Anything whose correctness depends on WHICH FILE this is
  compares a digest taken at validation time, at every point that asks and not
  only at the reversal. "Every point" has had to be swept twice; the class
  check `test_no_ownership_check_accepts_a_recycled_inode` parameterises the
  sites so the next one is found by running something rather than by reading.

  Ownership is captured when the entry is CREATED, not when it is cleaned up.
  Capturing at cleanup records whoever holds the name by then, which for
  `_read_only_sqlite` meant an interloper's sidecar was recorded as owned and
  unlinked. Where creation is somebody else's to do — SQLite makes `-wal` and
  `-shm` on first read, not at connect — the operation forces it, with a
  statement of its own, before any caller code can run.

  An identity needs something to distinguish it: the volume probes carry random
  bytes because every empty file has the same digest, so identifying them by
  content would be identifying them by nothing.

  This applies to REVERSAL as much as to publication, which is the half that
  was missed first: `_undo` verified the inode alone, so a landed member
  rewritten in place was carried back into the canonical namespace as though it
  were the validated generation, with nothing reported and no fail-safe pause.
  A move records what it published, and anything else at that name — a
  different entry or the same entry with different bytes — is not this
  operation's to move.

  Two corollaries, each of which was a finding before it was a rule. An
  identity is read from ONE descriptor: taking the digest through the open file
  and the inode by pathname afterwards pairs one file's bytes with another
  file's inode. And a failure is not an identity: answering `(inode, None)` for
  a member that would not open made two transiently failing states compare
  EQUAL, so an install or a reversal could be licensed by a pair of failures.
  Absent is an identity; unreadable is not. The identity of a publication is
  taken from the entry the call already OWNS, before the move — read back from
  the destination afterwards, it describes whoever holds that name by then, and
  the cleanup is authorised against their file. And where a path must be handed
  to something that cannot take a descriptor — SQLite, which is given a
  filename — the file lives in a per-invocation directory created 0700 with an
  unpredictable name, because the window cannot be removed but everyone else's
  ability to use it can.
### A report describes the branch the worker will actually take

- **Any job the dry-run report annotates with `blocked_by` and `table_writes`
  must be one the worker settles WITHOUT resolving a provider, and settling it
  must write exactly the declared tables. While any such annotation is present,
  the report must claim no top-level `table_writes`.** Enforced by
  `tests/control_plane/test_provision_dry_run.py::test_every_annotated_job_agrees_with_one_real_worker_call`,
  which crosses every gated job shape with every invalid receipt state, traces
  one real `run_once()`, and asserts the provider registry was never consulted;
  and by `test_the_gated_shapes_cover_the_worker_s_own_predicate`, which asks
  `requires_current_content_restriction` about every kind the schema allows so a
  newly gated shape cannot slip past that parameter list.

  Recorded after five instances of one class arrived across the E1 review
  rounds. Each was the report describing an operation other than the one the
  worker would perform: a stale consent receipt reported as an ordinary
  provider call; the receipt checked for a runtime job and not for its email
  siblings; a configuration mismatch narrated over a household being deleted or
  an unresolved earlier intent; a cancellation label narrated over a quarantined
  intent that had already reached its provider; and a single top-level write set
  chosen from an unordered scan, which is a claim about which queued job the
  worker reaches first. They are one missing rule, not five findings.

  The rule has two halves and both are load-bearing. A report may state what a
  job's next act writes only where durable state fully determines it — which is
  exactly the case where no provider is consulted — and it may never rank jobs,
  because `JobsRepository.lease` decides that and its answer depends on
  `not_before`, held leases, paused workers and creation order. Every attempt to
  predict the ranking has produced a finding; attaching facts to each job has
  produced none.

### Withdrawal tears down what it can NAME, and never asks the provider

- **No code path that exists because a consent went away may call a provider to
  discover what that provider holds. It must reach every reference the control
  plane can name from durable state or from arithmetic on identifiers it
  already has: the job's request, its `external_ref_ciphertext`, and — for an
  email identity — `email_org_external_ref(household_id, identity_id)`, which is
  a pure function computed before the first provider call.** Enforced by
  `tests/control_plane/test_provisioning_jobs.py::test_a_shutdown_tears_down_what_it_can_name_without_asking_the_provider`,
  which parameterises the durable and derived carriers and asserts the
  provisioner is never called.

  Recorded after this class arrived three times. First: `inspect` was believed
  read-only and is not — `NerveManagedEmailProvisioner.inspect` deletes and
  reissues the API key and rotates the webhook, and
  `GoogleOAuthEmailProvisioner.inspect_intent` calls `ensure` — so a shutdown
  mutated a withdrawn household's provider state to find out what it had. Then:
  refusing to look at all abandoned the teardown the quarantine exists for.
  Then: reading only the immutable request missed the reference Google OAuth and
  Nerve BYO persist through `settle`, leaving a known binding or domain live.

  A derived reference counts only if it is the reference the job's OWN provider
  accepts for teardown. Google's `deprovision` takes
  `google-oauth:<identity_id>`, which is arithmetic on the request. Nerve's
  decodes a `_Refs` of provider-assigned org, webhook and key ids, which nothing
  here can construct — and scheduling an `email_org_external_ref` lookup key in
  its place produces a cleanup the deprovisioner refuses: a failed job standing
  in for a teardown, which is worse than scheduling nothing, because the inbox
  is live either way and one of the two says it was handled. Where no acceptable
  reference exists the job stays quarantined and says so — and where the
  provider can resolve a computed reference with a READ-ONLY lookup, the
  teardown is built rather than the capability disabled. Both Nerve routes
  accept `nerve-org:<org_external_ref>` and resolve it through `get_org`, a
  plain GET, deleting what is found under the org. A worker-level test can only
  assert the string it scheduled; that the concrete provisioner ACCEPTS that
  string is a separate assertion and belongs beside the provisioner.

  The rule is not "never look" and not "ask the provider" — it is that looking
  means reading what this side already knows. A reference computed before a
  provider call is knowable even when that call created state and then timed out
  without recording anything, which is the case an earlier version of
  `_shutdown_probe` called undiscoverable in its own docstring. It is derivable,
  and a docstring admitting a gap is not a substitute for closing it.

### A precondition is enforced where the provider is CALLED, not where work is planned

- **Any condition that must be able to stop external work — a consent, a kill
  switch, an operator brake — is read on the path that reaches
  `provider.ensure` / `provider.inspect`, at the time it reaches it, and at
  every such entry point. A check only at the planning or selection layer does
  not enforce it.** Enforced for the MVP email switches by
  `tests/control_plane/test_email_option_flags.py::test_disabling_an_option_stops_work_that_is_already_queued`,
  which enqueues with the option ON, turns it OFF, and asserts across a fresh
  `ensure`, a user-triggered `inspect` and a reclaimed job that the recording
  provider was never called; by
  `test_operator_reconcile_also_stops_at_a_disabled_option`, because `reconcile`
  is a second entry point that dispatches to the same provider methods; and by
  `test_an_enabled_option_still_reaches_its_provider`, without which every one
  of those passes just as well when the worker stops for an unrelated reason.

  Recorded after the second instance. The Art. 9(2)(a) content restriction was
  the first: `onboarding.provision` reported the precondition while the worker
  was the thing that had to hold it, and the fix put the check in `_run_once`
  and shared the predicate through `requires_current_content_restriction` so the
  two layers ask one question. The MVP kill switches repeated it exactly —
  `_assert_email_rollout` refused a disabled option at `select`, and every job
  already queued kept provisioning, which is the incident the switch exists for.

  Three halves, all load-bearing.

  **Read at call time, not at plan time.** A value captured when the job was
  created is the value the operator is trying to change. The queue is the whole
  problem: between selection and the provider call there is an unbounded delay
  during which the answer can flip, and jobs are re-inspected and reclaimed long
  after they are planned.

  **Exempt shutdown, never teardown.** A brake that also stops deprovisioning
  strands exactly the external resources pulling it is meant to remove. Ask
  through `_is_shutdown_action` rather than restating it; the reasoning it
  carries for the consent precondition is the same reasoning, and a second
  spelling is a second thing to get wrong.

  **A brake must not be implemented by removing the adapter.** The sharpest
  instance of that half, found 2026-08-22: `container.py` registered the Nerve
  adapters only `if config.real_email_enabled`, so turning the brake on deleted
  the very objects teardown resolves. Teardown looks a provider up by the job's
  durable `provider` column, so after
  enable -> provision -> disable -> restart, every cleanup and reconcile raised
  `ProviderRejected` and `DeletionService` swallowed it into `unknown` — an
  erasure request that could never complete while the inbox it named stayed
  live. Registration follows CREDENTIALS (`nerve_configured`); the brake lives
  at forward dispatch, which is the only layer that can tell `ensure` from
  `deprovision`. Enforced by
  `tests/control_plane/test_real_email_wiring.py::test_teardown_still_reaches_the_provider_after_the_brake_goes_on`,
  `::test_forward_work_is_braked_while_teardown_is_not`, and
  `::test_household_deletion_completes_with_the_brake_on`, over both Nerve
  providers.

  **Teardown re-runs `deprovision`; it never probes with `inspect`.** Found in
  the same round. The reconcile branch probed with `inspect` for every resource
  except runtime, and `inspect` is not contractually read-only:
  `NerveManagedEmailProvisioner.inspect` is a RECOVERY path that reissues the
  API key and rotates the webhook. Reconciling an uncertain email cleanup
  therefore handed a withdrawn household's inbox fresh live credentials instead
  of removing it. The runtime case had the right behaviour for a narrower
  stated reason — that inspecting the shared app cannot distinguish an absent
  workload from its retained secret namespace — which hid the general one, so
  every other resource type was one adapter away from the same defect.
  `deprovision` is the operation whose idempotence teardown already depends on;
  the branch delegated to it anyway once `inspect` returned READY. Enforced by
  `tests/control_plane/test_real_email_wiring.py::test_reconciling_a_cleanup_tears_down_instead_of_probing`,
  parameterised over both Nerve providers and both cleanup origins, with a stub
  whose `inspect` counts credential reissues.

  **Reconcile dispatches exhaustively; adapter shape gates nothing about
  teardown.** Found in the same round's design pass, as the residue of the
  finding above: the shutdown route still lived INSIDE the email branch gated
  on `callable(getattr(provider, "reconcile", None))` — an attribute the
  `Provisioner` protocol never declared — so an adapter omitting the method
  sent quarantined teardown work to a tail whose first act was
  `provider.inspect`, and every schema kind without its own branch fell into
  the same tail by omission. All adapters defining `reconcile` was a property
  of the adapters, not a guarantee of the worker. The worker now owns the
  whole decision: shutdown routing for email jobs sits above every
  adapter-shaped check; forward reconcile REQUIRES `reconcile` and fails
  closed (`provider_cannot_reconcile`) rather than substituting a probe; the
  protocol declares the method; and the end of `_reconcile` is an explicit
  refusal (`reconcile_unsupported`), not a tail, so a future kind cannot
  silently acquire an inspect path. The runtime branch keeps its read-only
  inspect deliberately — crash recovery for live households' forward work is
  the one place the recovery-inspect contract is legitimate. Enforced by
  `tests/control_plane/test_real_email_wiring.py::test_a_quarantined_job_reconciles_without_the_adapters_reconcile_method`,
  `::test_forward_reconcile_without_the_method_fails_closed` (both
  parameterised over all four adapters including the fake, against a stub
  whose `inspect` raises), and
  `::test_a_kind_without_a_reconcile_path_is_refused_not_probed` over the two
  kinds Phase E has not created yet.

  **Whatever `build` constructs, `validate` must already have vetted.** Widening
  what the container builds silently widens what the config must accept.
  Registering the Nerve adapters on `nerve_configured` rather than
  `real_email_enabled` meant a brake-off deployment with
  `ABROLIA_NERVE_BASE_URL=http://...` passed `validate` and then raised
  `ValueError` inside `NerveAdminClient` — a dormant provider turned into a
  startup outage. Structural checks (HTTPS origin, canonical UUIDs) therefore
  bind to CONSTRUCTIBILITY and run under `nerve_configured`; enablement checks
  (the household allowlist, completeness) stay under `real_email_enabled`.
  Enforced by
  `tests/control_plane/test_real_email_wiring.py::test_malformed_nerve_settings_are_refused_with_the_brake_either_way`
  across three malformations with the brake both ways, paired with
  `::test_a_valid_nerve_block_builds_and_keeps_both_adapters` — without which
  the rule is satisfied by refusing every Nerve configuration, which would take
  teardown away again.

  **A brake subtracts; it never adds.** The live environment value is ANDed with
  the boot-time authorization from the validated configuration. Reading the
  variable alone made `0 -> 1` an authorization rather than a release: a process
  booted with the brake on — and therefore with a household allowlist that was
  never consulted for the household in question — would dispatch its durable
  Nerve job the moment the variable flipped, routing around the frozen
  configuration the worker was built from. The allowlist is enforced where it
  was validated. Enforced by
  `tests/control_plane/test_real_email_wiring.py::test_the_env_brake_cannot_authorize_what_the_config_refused`.

  **A brake carries the whole authorization, not a summary of it.** The worker
  was given `real_email_enabled` as one boolean, which discarded
  `real_email_household_allowlist`: a household queued while allowlisted kept
  its authorization after the allowlist was narrowed to exclude it, because the
  durable job carries only its own `household_id` and dispatch asked whether
  real email was on *anywhere*. Selection checks the allowlist, but a durable
  job outlives its selection. The worker now holds the frozen SET and asks
  membership. Enforced by
  `test_a_household_removed_from_the_allowlist_cannot_still_dispatch`,
  `test_membership_is_what_the_brake_asks`, and
  `test_the_container_authorizes_nobody_while_the_brake_is_on` — the last
  because handing the list through with the brake on would let `0 -> 1`
  authorize households the configuration never did.

  **Erasure owns its ambiguity, and says so durably.** Deletion cancels only
  `pending` and `waiting_user`, correctly: an `outcome_unknown` job may have
  created something upstream and must be reconciled, not discarded. But
  reconciliation re-applies every forward precondition, so a job the kill switch
  settled carried `real_email_disabled` — no reconciliation suffix — and the
  brake blocked it again forever. Erasure could not reach the provider for
  exactly the resource whose creation was uncertain. Deletion now reclassifies
  its OWN household's ambiguous jobs to `deletion_requires_reconciliation`, and
  `_reconcile` routes those to `_shutdown_probe` ahead of the email branch,
  whose `secret_namespace_not_ready` early return would otherwise stop them —
  deletion has already swept the namespace, it being an external resource like
  any other.

  The exemption is a ROUTE, not a hole in the brake. It sits above
  `_blocked_by_email_kill_switch` in `_reconcile`, never inside it: that
  predicate is shared with `_run_once`, where the job may be forward work. A
  pending job leased just before erasure began is `running`, deletion leaves
  `running` jobs alone deliberately, and exempting deletion-owned work inside
  the predicate let a resumed worker call `ensure` with the brake off — creating
  new upstream email state during an erasure, on all three email routes at once.
  Only a path that cannot create anything may pass a brake. Enforced by
  `test_erasure_never_lets_forward_work_past_the_brake`, over
  `nerve-managed`, `nerve-byo-domain` and `google-oauth`.

  **Erasure joins the lifecycle it depends on.** Scheduling a cleanup is not
  finishing one. `_delete_email_binding_secret` deletes the provider secret only
  for an identity marked `disconnecting` — the state the ordinary disconnect
  flow sets — and deletion never entered that flow, so its own cleanup settled
  `outcome_unknown/secret_cleanup_unknown`, the parent job was never settled,
  and `resume` saw unresolved work forever with the provider resource and the
  secret namespace both already gone. Deletion now performs the same transition
  the disconnect path uses, in the same durable transaction. This only shows up
  when the deletion does NOT complete: the completed path drops the household
  row and cascades the identity away, leaving nothing to settle. Enforced by
  `test_erasure_teardown_reaches_a_terminal_state`, whose fixture deliberately
  keeps the deletion open.

  Ownership is asked of the HOUSEHOLD's durable status, never of the job's error
  code. Stamping a code onto each ambiguous job in one transaction — the first
  attempt — left two holes: a job already carrying
  `withdrawal_requires_reconciliation` was skipped by the guard against
  overwriting an operator's quarantine reason, and a `running` call that timed
  out AFTER that statement was never stamped at all. `status = 'deleting'` is
  written in the same transaction that makes the deletion durable, so there is
  no window, nothing to re-run on resume, and the job keeps the reason it had.

  It must still not suffix the brake's own error code — that code is written for
  live households too, and suffixing it would exempt every braked job from its
  own brake — and it must not reach beyond the household being erased, which
  would make one deletion a global release. Enforced by
  `test_deletion_can_tear_down_a_job_the_brake_settled`,
  `test_erasure_owns_every_ambiguous_job_however_it_got_there` (braked,
  pre-quarantined, and ambiguous-only-after-deletion),
  `test_deletion_reclassifies_only_its_own_households_jobs`, and
  `test_the_brake_code_alone_is_never_reconciliation_work`.

  The route is deliberately narrow — deletion-owned work only. Withdrawal,
  cancel and reset leave the namespace in place and their reconciliation is a
  settled contract; widening it to all shutdown work breaks
  `test_late_waiting_response_after_cancel_stays_reconcilable` and is a separate
  question.

  This also settles where such a brake may be read. Registration cannot carry
  it, and the worker holds no `ControlPlaneConfig`, so it is read from the
  environment at call time — a deliberate second reader of a variable the
  config also parses. That is the one case where the usual "one spelling" rule
  yields, and it yields because the alternative breaks erasure.

  **Braking is not failing.** Settling a braked job terminally is only safe
  where it is known never to have reached a provider — a first attempt that was
  not reclaimed. Anywhere else `failed` erases durable uncertainty AND moves the
  job out of `outcome_unknown`, the only status `reconcile` accepts, so the
  brake strands exactly the external org, domain or binding that turning the
  brake off exists to clean up. An already-ambiguous job is left untouched,
  because rewriting it replaces the reconcilable state an operator is holding
  with the brake's own, less informative, reason. Enforced by
  `test_a_braked_job_survives_to_be_reconciled_when_the_flag_returns`, which
  brakes, re-enables, and asserts the provider is then actually reached — the
  second half being what distinguishes a preserved job from a differently-broken
  one. And the brake's error code must NOT carry `RECONCILIATION_SUFFIX`: that
  suffix means quarantined, `_is_shutdown_action` treats quarantined as exempt,
  and a braked job would be exempt from its own brake on the very next pass.

  The selection-layer check still earns its place — it keeps a cut option off
  the screen instead of failing after a user picks it — but it is the courtesy,
  not the enforcement. Where both layers ask, they must ask from one table:
  `CUT_EMAIL_OPTIONS` is keyed by selection kind and by provider name because
  those are the two vocabularies, and two hand-written maps would drift into an
  option cut from one and not the other.


### A stored identity is the string the channel's ingest actually produces

- **No row in `channel_bindings` may name an identity the runtime will not
  recognise. `actor_id` and `external_id` are two readings of ONE value — the
  transport sender — and must be equal; `chat_id` is the conversation that
  channel's ingest reports, and is never derived from either.** Enforced by
  `ChannelBindingsRepository._reject_actor_that_is_not_the_sender`, called from
  `_insert` so no write path can go around it and from `issue_challenge` so
  nobody is handed a code that could not have redeemed, and by
  `tests/control_plane/test_channel_bindings.py::test_a_bound_whatsapp_member_is_authorized_by_a_real_inbound_turn`,
  which drives a webhook-shaped inbound through `as_eml` and
  `trusted_run_context` and asserts BOTH halves of the resulting pair match
  the binding the control plane wrote.

  Recorded after this class arrived twice in two review rounds on one pull
  request. First the CHAT half: `issue_challenge` defaulted `chat_id` to
  `external_id`, documented as "the truth for WhatsApp, where a 1:1 thread IS
  the number". Then the ACTOR half: the control plane invented internal actor
  names (`synthetic-second-adult`) while `DesiredSpecPlanner.issue` seeded the
  sender column from onboarding's `chat_id`. They are one missing rule, not two
  findings.

  What makes this class expensive is that every layer the control plane can see
  reports success. The row is written, the revision is published, the rollout is
  scheduled, the endpoint answers 200 — and then every real inbound turn from
  that member is classified `unknown` and gets no family capabilities. Nothing
  reports it, because nothing in the control plane can observe an authorization
  that never happens. Only a test that goes through the real ingest can.

  The identities are not the control plane's to choose. `hermes_cloud/channels/
  telegram.py` builds its context from `message.from.id` and `message.chat.id`;
  `hermes_cloud/ingest/whatsapp_webhook.py` normalizes the sender to `+999…`
  and reports the conversation as the provider's `remote_jid`
  (`…@s.whatsapp.net`, or `@g.us` for a group). `Household.knows_binding`
  compares the resulting pair BY STRING. So a value that is plausible, or
  derivable, or "the same number", is not thereby the right value.

  Deriving one identity from another is the specific temptation, and it has now
  produced both instances. It looks safe whenever the two coincide in the case
  in front of you and it is wrong in the case you did not picture: the WhatsApp
  1:1 thread whose JID is not its number, the group whose suffix is `@g.us`, the
  Telegram chat that is not its member's user ID. Where a real mapping is
  genuinely needed — one person holding one identity across channels — that is a
  verified sender-to-actor mapping carried through the manifest, a design this
  code does not have. Until it exists, the binding is REFUSED rather than
  written under a name the runtime cannot honour.

  A consequence worth stating because it looks like a regression and is not:
  one human reachable on two channels is two members of `actors.family`. The
  system has no cross-channel notion of a person, and the internal actor names
  made it look as though it did.

### A household's fallback owner is decided in one place

- **Any code that chooses which account receives a household's email fallback,
  or asks whether an address belongs to one, calls `control_plane/owners.py`.
  No other module may join `accounts` to `household_memberships`, and "active
  owner" means the membership AND the account are both `active`.** Enforced by
  `tests/control_plane/test_owner_predicate.py`: one case greps the control
  plane for the join and names any module that performs it, the other gives a
  household a locked owner beside an active one and asserts the locked account
  is neither chosen as the fallback nor treated as a mailbox collision.

  Recorded after the FOURTH instance, which is three later than
  `AGENTS.md` allows, and the four are worth listing because every one of them
  passed its own tests and each looked like a different bug:

  1. `ChannelPreferencesRepository` refused a pairing nothing had asked it
     about — the table had no writer at all, so the rule was unreachable.
  2. The refusal moved to `EmailIdentityService.select` for the managed and
     own-domain mailboxes, because the repository's own refusal lands after
     the provider has created the inbox, where a family cannot correct it.
  3. `gmail_agent` walked past that: its address does not exist until Google
     grants one, so `select` sees `address = None`, and the callback compared
     the grant with the INITIATING account rather than with every owner.
  4. The predicate itself was incomplete — membership status without account
     status — and the planner chose an owner with an unordered `LIMIT 1`, so a
     LOCKED account could become the fallback and be refused afterwards, from
     inside a provisioning job whose provider had already succeeded.

  **The shape they share** is that four call sites each needed the same
  question answered and each answered it locally, correctly for the case in
  front of them. That is not a review failure; it is what happens when a rule
  lives in prose. It lives in `owners.py` now, and the queries are returned as
  SQL and parameters rather than executed, so a caller inside a transaction
  and a caller holding no connection can both ask it without the rule being
  written twice.

  **Both lifecycles, in both directions.** A locked account cannot receive a
  fallback, so it must not be chosen as one — and a mailbox equal to its
  address must not be refused on its behalf either. Only the first direction
  is a delivery failure; the second silently blocks an onboarding that had
  nothing wrong with it, which is the harder one to find.

  **The choice is ordered.** `LIMIT 1` over an unordered set makes which owner
  is the fallback depend on storage order, and `config_sha256` would then move
  for a household that changed nothing — the same reason `verified()` orders
  the bindings it projects.
