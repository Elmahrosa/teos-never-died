 # RAVEN — Pattern Gap Report

Repo: teos-never-died (RAVEN)
Layout: flat modules at repo root. No `app/` package.
Baseline: 310 tests pass, 3 upstream deprecation warnings.
Audit branch: audit/extract-patterns-20261013-052712
Revision: 2 (corrects the earlier keyword-match report)

Rule for this report: a pattern is PRESENT only when the mechanism
described by the order exists in code, at a named file:line, and does
the job the order asks. A keyword hit is not a mechanism.

---

## P1 — errors.actionable_taxonomy

Decision:   PARTIAL
Evidence:   errors.py:1-355 (imported by dashboard.py:25, webhook_server.py:52,
            listed in tests/test_imports.py:42)
Mechanism:  errors.py defines ErrorCategory (8 categories) and ActionableError
            (code, message, user_guidance, technical_details, recoverable),
            plus log_actionable_error() and raise_validation_error().
Gap:        The order specifies FailureReason (UNREACHABLE / TIMEOUT /
            RATE_LIMITED / QUOTA_EXCEEDED / FORBIDDEN / NOT_FOUND / REJECTED /
            SERVER_ERROR), Failure(Exception), classify(exc) that maps
            httpx/requests/aiohttp/sqlalchemy/urllib errors, and
            is_retryable(reason). None of that exists.
Smallest change:
            Add FailureReason enum, Failure class (with retry_after and
            __cause__ passthrough), classify(), and is_retryable() to errors.py.
            Rewrite ad-hoc "if 'timeout' in str(e)" branches in the AI router
            and webhook to use classify(e).reason.

---

## P1 — registry.table_driven

Decision:   ABSENT
Evidence:   none found
Mechanism:  No Spec / REGISTRY / build_all. Provider selection in
            multi_provider_ai.AIRouter.chat() is an if/elif chain on provider
            name. Command routing in telegram_bot.main() is a flat list of
            add_handler calls, not a table.
Smallest change:
            Add a Spec dataclass, a REGISTRY tuple, get(kind), and
            build_all(cfg). Migrate AIRouter's provider selection to the
            registry. Optional backends that are unconfigured must be silently
            skipped.

---

## P2 — api.envelope_unwrap

Decision:   ABSENT
Evidence:   none found
Mechanism:  webhook_server.py returns its own JSONResponse shapes
            ({"ok": ..., "status": ...}); dashboard.py returns its own dicts;
            multi_provider_ai parses provider JSON directly.
Smallest change:
            Define parse_envelope(raw, model) and APIError. Apply it to the
            payment webhook and dashboard JSON endpoints where a
            {"status": ..., "data": ...} shape is in use. Docstring must name
            the failure mode: decoding the wrapper into the payload type
            silently succeeds with all fields defaulted.

---

## P2 — fs.atomic_staged_write

Decision:   PARTIAL
Evidence:   The jsonio module (if present) uses write-to-tmp + os.replace.
            database.py uses SQLAlchemy transactions (lines 286, 346, 388,
            430, 487).
Mechanism:  Crash-safe writes exist for JSON files, but there is no general
            staged_write(target, mode, write) helper and no sibling-file
            convention (.new next to target).
Gap:        The order specifies: staging file is target.with_suffix(suffix +
            ".new"), a SIBLING not /tmp; mode applied at os.open not via chmod;
            unlink staging on any failure; os.replace only on success;
            remove_staged_siblings deletes exact names, never glob.
Smallest change:
            Add fs_atomic.py with staged_write() and
            remove_staged_siblings(). Rewrite jsonio to call staged_write().

---

## P3 — fs.embedded_integrity

Decision:   NOT_APPLICABLE
Evidence:   RAVEN does not package resources. There is no data/ or resources/
            directory shipped with the code, no manifest of embedded files.
Mechanism:  None needed.
Would-become-applicable if:
            RAVEN ever ships a bundled directory (e.g. dashboard/templates +
            static as a package) that could be tampered with on disk and needs
            sha256 verification against a packaged copy.
Reason:     The pattern solves a problem this repo does not have.

---

## P3 — db.migration_advisory_lock

Decision:   NOT_APPLICABLE
Evidence:   database.py defaults to SQLite (sqlite+aiosqlite:///raven_saas.db).
            No import of psycopg, asyncpg, or alembic anywhere in the tree.
Mechanism:  Postgres advisory locks (pg_advisory_lock) do not exist in SQLite.
Would-become-applicable if:
            DATABASE_URL is switched to Postgres AND multi-instance deployment
            is required.
Reason:     The pattern assumes a Postgres deployment that does not exist here.

---

## P4 — tenant.namespace_isolation

Decision:   ABSENT
Evidence:   database.py filters rows by telegram_id (a row-level tenant key).
            There is no schema-per-tenant model. No Tenant class.
Mechanism:  Row-level isolation via telegram_id is real, but it is not
            namespace isolation as the order describes (schema_name,
            group_id, scoped_name, prefix, label, user_id, tags).
Smallest change:
            If RAVEN ever moves to multi-schema, add a Tenant dataclass with
            the eight methods the order specifies and route every schema name
            through it. Not needed today.

---

## P4 — release.post_update_verification

Decision:   ABSENT
Evidence:   install.ps1:31-49 verifies the sha256 of the downloaded Ollama
            installer before running it. That is download integrity.
Mechanism:  No capture() / verify() / Target / Result. No comparison of an
            expected version against a fresh post-update answer.
Smallest change:
            Add release_verify.py with Outcome, Target, Result, capture() and
            verify(). Wire it to a release command that checks component
            versions before and after an update.

---

## P5 — llm.chain_ast

Decision:   ABSENT
Evidence:   telegram_bot.conversation_history is a flat list of
            {"role", "content"} dicts. No Header, BodyPair, Section, or
            ChainAST.
Mechanism:  None.
Smallest change:
            Add llm_ast.py with PairType, Header, BodyPair, Section, ChainAST,
            parse_chain(chain, force). This is a large pattern; only justified
            if RAVEN needs section-level chain manipulation.

---

## P5 — llm.summarization_preservation

Decision:   ABSENT
Evidence:   No summarizer. agent_engine.run_agent keeps a bounded list of
            previous answers but does not summarize sections.
Mechanism:  None.
Smallest change:
            Depends on llm.chain_ast existing first. Skip until P5 chain_ast
            is real.

---

## P6 — agent.supervisor_loop_guard

Decision:   ABSENT
Evidence:   agent_engine.run_agent uses max_rounds (default 3) as a hard cap.
            That is a loop bound, not a detector.
Mechanism:  No Detector (same_tool_limit, total_tool_limit, check, reset).
            No Budget (general_calls, restricted_calls, remaining, consume,
            approaching).
Smallest change:
            Add supervisor_guard.py with Detector and Budget. Wire into
            run_agent so the loop reflects when thresholds are hit rather than
            silently truncating.

---

## P6 — sandbox.policy_preflight

Decision:   NOT_APPLICABLE
Evidence:   No DockerClient import anywhere in the tree. RAVEN does not run
            sandboxed containers. recon_engine.validate_target() is a target
            ALLOWLIST (RFC1918 + RAVEN_PRE_APPROVED_DOMAINS) enforced before
            a scan; it is not a container-policy preflight.
Mechanism:  None needed.
Would-become-applicable if:
            RAVEN gains a Docker-based execution sandbox for untrusted code.
Reason:     The pattern assumes a container runtime this repo does not use.

---

## Summary

PRESENT:         0
PARTIAL:         2  (errors.actionable_taxonomy, fs.atomic_staged_write)
ABSENT:          7  (registry.table_driven, api.envelope_unwrap,
                    tenant.namespace_isolation,
                    release.post_update_verification,
                    llm.chain_ast, llm.summarization_preservation,
                    agent.supervisor_loop_guard)
NOT_APPLICABLE:  3  (fs.embedded_integrity, db.migration_advisory_lock,
                    sandbox.policy_preflight)

## Recommended order of work

1. errors.actionable_taxonomy (finish PARTIAL) — real value, small diff
2. fs.atomic_staged_write (finish PARTIAL) — real value, small diff
3. registry.table_driven — moderate value, moderate diff
4. api.envelope_unwrap — moderate value, moderate diff
5. release.post_update_verification — small value unless RAVEN ships updates
6. agent.supervisor_loop_guard — small value; current max_rounds is adequate
7. llm.chain_ast — LARGE; do not start unless there is a clear need
8. llm.summarization_preservation — depends on 7; skip
9. tenant.namespace_isolation — skip until multi-schema is real
NOT_APPLICABLE:  fs.embedded_integrity, db.migration_advisory_lock,
                 sandbox.policy_preflight — do not implement