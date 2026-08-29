---
title: "C6b — a retry path may only catch failures it is prepared for"
status: active
created_at: "2026-08-29"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-23-go-live-checklist.md
scope: c6b
data_policy: synthetic-only-until-explicit-gates
---

# C6b — a retry path may only catch failures it is prepared for

Deferred from C6a, which said auditing this under a slice about activation
authority would be a second change hiding inside the first.

## The defect class

Three defects in the C5 gateway slices were the same shape, and none of them
failed where they happened:

* a `NameError` from a missing import became `lookup_unavailable` — a message
  parked in the WAL and retried forever;
* a `TypeError` from a call site I had missed while changing a signature became
  `runtime_unavailable` — the same;
* both were found by a test disagreeing about a count, not by anything raising.

The shape is specific and it is worth naming rather than treating as three
accidents:

> **A broad `except` around an operation whose failure has a RETRY POLICY
> converts a programming error into a silent, permanent retry.**

It is not "broad excepts are bad". A broad catch whose answer is "log this and
carry on" loses information and nothing else; the `except Exception` guarding
telemetry in `worker.py:273` is correct, and says so. The damage comes from the
combination: catch everything, and then respond with *try again later*. The bug
never surfaces, the work never completes, and the queue grows.

`outcome_unknown` is that response spelled in this codebase's own vocabulary —
it means "come back to this", which for a `TypeError` means forever.

## The invariant

**C1 — Every catch on a retry path names the failures it is prepared for.**
Anything unnamed is a defect rather than a condition, and a defect must be loud.
Where a layer knows what a transport failure looks like, it translates there,
so the layer that decides the retry can catch one named thing.

**A narrowed inner catch is only as strong as the ladder above it.** `_run_once`
ends its exception ladder with `except (OutcomeUnknown, TimeoutError,
ConnectionError)`, so those two builtins are retryable wherever they arise —
narrowing an inner handler does not change that. It is not a gap: they are
transport conditions, and four C6a tests simulate a launch whose connection
died by raising `TimeoutError` and depend on `outcome_unknown`. What the ladder
does NOT catch is `TypeError`, `NameError`, `AttributeError` and `KeyError` —
there is no trailing `except Exception` — which is the class this slice closes.

Worth stating because the rule reads stronger than it is: C1 makes programming
errors loud, and leaves named transport conditions retryable at every level.

That is already how the working code in this repository is written:
`runtimes/chat_client.py` and `privacy/runtime.py` catch
`httpx.TimeoutException, httpx.TransportError` and nothing else, and the Fly
provider wraps its own transport failures into `OutcomeUnknown` and
`ProviderRejected` before they leave it. The provisioning worker is where the
practice was not followed.

## Scope

The four retry-path catches in `ProvisioningWorker`, found by classifying every
`except Exception` in the file by what its handler DOES rather than by reading
each one:

| site | operation | was | named |
| --- | --- | --- | --- |
| `_revoke_consent` | `httpx.post` to the runtime | `outcome_unknown, runtime_unreachable` | `httpx.TimeoutException, httpx.TransportError` |
| `_cleanup_cancelled_result` | `provider.deprovision` | resource `outcome_unknown` | `ProvisioningError` |
| the email-namespace cleanup | `provider.deprovision` | resource `outcome_unknown` | `ProvisioningError` |
| `_finish_runtime` | `secret_sink.install` | `outcome_unknown, secret_install_unknown` | `SecretInstallError`, `ProvisioningError` |

`ProvisioningError` is the right name for the provider ones because the
provider contract already guarantees it: `FlyRuntimeProvisioner` raises
`OutcomeUnknown` for a transport failure and `ProviderRejected` for a refusal,
both subclasses. A provider raising anything else is not reporting a provider
condition — it is broken.

## Deliberately not here

The other sixty-six `except Exception` in the tree. Most are not this shape: a
WSGI handler that must not crash a server, a telemetry write that must not
change a durable outcome, a `finally`-style cleanup. Auditing them would be a
different slice with a different argument, and folding it in here would make
the rule harder to see rather than easier.

## Acceptance

* A programming error raised from each of the four operations propagates
  rather than becoming a retryable outcome.
* The named failures each site is prepared for still produce exactly the
  retryable outcome they produced before.
* Full suite green, ruff and sanitizer clean.
