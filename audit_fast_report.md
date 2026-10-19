# RAVEN — Pattern Audit Fast Report

Date: 2026-10-14
Branch: audit/extract-patterns-20261013-052712
Base: main (untouched)

## Patterns

P1 errors.actionable_taxonomy: PRESENT
P2 fs.atomic_staged_write: PRESENT
P3 api.envelope_unwrap: PRESENT
P4 registry.table_driven: PRESENT
P5 release.post_update_verification: PRESENT
P6 agent.supervisor_loop_guard: PRESENT
P7 llm.chain_ast: ABSENT
P8 llm.summarization_preservation: ABSENT (depends on P7)
P9 tenant.namespace_isolation: ABSENT
P10 fs.embedded_integrity: NOT_APPLICABLE (no packaged resources)
P11 db.migration_advisory_lock: NOT_APPLICABLE (SQLite, no Postgres)
P12 sandbox.policy_preflight: NOT_APPLICABLE (no Docker usage)

## Summary

PRESENT: 6
PARTIAL: 0
ABSENT: 3
NOT_APPLICABLE: 3

All patterns from P1 to P6 are present and complete.

Note: Working tree is clean. No uncommitted changes.