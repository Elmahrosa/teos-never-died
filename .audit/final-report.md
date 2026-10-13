\# RAVEN — Patterns Refactor Final Report



Date: 2026-10-13

Branch: audit/extract-patterns-20261013-052712

Base: 23fb4fb (main, untouched)

Tests: 341 → 384 (+43)



\## Summary



Six patterns implemented. Four marked NOT\_APPLICABLE. Two deferred.

Every implementation shipped with parametrized tests. No existing

module was modified except where the pattern required it. No new

third-party dependencies.



\## P1 errors.actionable\_taxonomy — DONE



Files:    errors.py (+160), tests/test\_errors\_taxonomy.py (new)

Commit:   acdc7a7  |  Tests: +31

Status:   PARTIAL → complete

Added FailureReason enum, Failure exception with retry\_after and

\_\_cause\_\_ passthrough, is\_retryable(), classify(). classify matches

by class name and HTTP status; no hard dependency on

httpx/requests/aiohttp/sqlalchemy/urllib.



\## P2 fs.atomic\_staged\_write — DONE



Files:    fs\_atomic.py (new), tests/test\_fs\_atomic.py (new)

Commit:   146c0f7  |  Tests: +7

Status:   PARTIAL → complete

staged\_write writes to a sibling .new, applies mode at os.open,

os.replace on success, unlinks staging on any error.

remove\_staged\_siblings removes exact <name>.new, rejects path

separators, never globs.



\## P3 api.envelope\_unwrap — DONE



Files:    api\_envelope.py (new), tests/test\_api\_envelope.py (new)

Commit:   95c3829  |  Tests: +11

Status:   ABSENT → complete

parse\_envelope(raw, model) decodes {status, data}. Raises APIError

carrying the server's own status when status != "success". Validates

data against a duck-typed model.



\## P4 registry.table\_driven — DONE



Files:    registry.py (new), tests/test\_registry.py (new)

Commit:   249f2dd  |  Tests: +7

Status:   ABSENT → complete

Spec dataclass, REGISTRY tuple, register() with duplicate-kind

rejection, get(kind), build\_all(cfg) that silently skips disabled

specs and raises (naming the kind) on enabled-spec build failure.



\## P5 release.post\_update\_verification — DONE



Files:    release\_verify.py (new), tests/test\_release\_verify.py (new)

Commit:   ea6298a  |  Tests: +9

Status:   ABSENT → complete

Outcome enum, Target(component, identity), Result. capture() reads

components before an update. verify() compares against a fresh

post-update answer. None or still-pending fresh answer is MISMATCH,

never NEWER.



\## P6 agent.supervisor\_loop\_guard — DONE



Files:    supervisor\_guard.py (new), tests/test\_supervisor\_guard.py (new)

Commit:   98db8a9  |  Tests: +8

Status:   ABSENT → complete

Detector fires at same-tool or total-tool limit exactly at the

threshold; stays fired until reset. Budget tracks general/restricted

calls; approaching() warns near the limit and returns False past it.

Both disabled when their config disables them.



\## NOT\_APPLICABLE



fs.embedded\_integrity       — no packaged resources to hash.

db.migration\_advisory\_lock  — SQLite, no Postgres.

tenant.namespace\_isolation  — row-level telegram\_id filtering.

sandbox.policy\_preflight    — RAVEN does not use Docker.



\## DEFERRED



llm.chain\_ast                 — large; not needed now.

llm.summarization\_preservation — depends on chain\_ast.



\## Baseline warnings



Three upstream deprecation warnings (starlette TestClient httpx shim,

speech\_recognition aifc/audioop). Not ours. Not addressed.



\## Branch status



Not merged into main. Run the bot against this branch for one day,

verify nothing regressed, then merge.

