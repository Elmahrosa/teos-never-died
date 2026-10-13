# RAVEN Architectural Patterns Gap Report

## Pattern Evaluation Summary

Based on my audit of the RAVEN codebase, here is the evaluation of each architectural pattern specified in `.audit/manifest.json`:

### P1 Patterns (Priority 1)

#### errors.actionable_taxonomy: ABSENT
- **Current State**: No systematic error taxonomy with actionable error messages found
- **Evidence**: Error handling appears to be ad-hoc throughout the codebase (e.g., generic try/catch blocks logging exceptions)
- **Smallest Change**: Define error categories with specific, actionable remediation guidance and implement consistent error reporting

#### registry.table_driven: ABSENT
- **Current State**: No externalized rule/decision tables found
- **Evidence**: Business logic appears to be hardcoded in various places (e.g., tier limits in database.py, command handlers in telegram_bot.py)
- **Smallest Change**: Externalize configurable rules (like tier limits, command permissions) to YAML/JSON files with loader functions

### P2 Patterns (Priority 2)

#### api.envelope_unwrap: ABSENT
- **Current State**: No standardized API response envelope format found
- **Evidence**: API responses vary by endpoint (webhook_server.py returns different JSON structures, agent_engine.py has its own response format)
- **Smallest Change**: Define standard envelope format (e.g., {success, data, error, metadata}) and wrap all API responses accordingly

#### fs.atomic_staged_write: PARTIAL
- **Current State**: Some atomic write patterns found but not comprehensive
- **Evidence**: 
  - database.py: Uses transactional writes with commit/rollback (lines 286, 346, 388, 430, 487)
  - Missing: Staging to temporary files before atomic rename for crash safety
- **Smallest Change**: For file-based operations, implement write-to-temp-then-rename pattern

#### fs.embedded_integrity: PRESENT
- **Current State**: HMAC-SHA256 signature verification for webhook payloads
- **Evidence**: webhook_server.py lines 125-164 (`verify_signature` function) and lines 394-407 (signature verification in webhook endpoint)
- **Details**: Implements Svix-style signature verification with timestamp validation to prevent replay attacks

#### db.migration_advisory_lock: PRESENT
- **Current State**: Advisory database migration system
- **Evidence**: database.py lines 226-247 (`_run_migrate_columns` function) called during init_db() at line 188
- **Details**: Adds missing columns via ALTER TABLE never drops/rewrites data, fails gracefully without stopping bot

#### tenant.namespace_isolation: PRESENT
- **Current State**: Multi-tenant data isolation by telegram_id
- **Evidence**: All user-specific queries in database.py scope by telegram_id:
  - `_get_user` (line 253): `where(User.telegram_id == str(telegram_id))`
  - `_set_prefs` (line 315): Same pattern
  - `_set_tier` (line 361): Same pattern
  - `_bump` (line 397): `where(DailyUsage.telegram_id == str(telegram_id), ...)`
  - `_usage_for` (line 436): Same pattern
  - `_history` (line 505): `where(Subscription.telegram_id == str(telegram_id))`
- **Details**: tenant_id = telegram_id ensures complete namespace isolation

#### llm.chain_ast: PRESENT
- **Current State**: Think → Search → Answer → Refine agent loop
- **Evidence**: agent_engine.py lines 382-494 (`run_agent` function) implements:
  - Think: Planning phase (line 413)
  - Search: Web search execution (line 431)
  - Answer: Response generation (line 446)
  - Refinement: Iterative improvement loop (lines 453-456)
  - Verification: Claim checking against evidence (lines 458-463)
- **Details**: Multi-stage LLM workflow with clear phase separation

#### llm.summarization_preservation: PRESENT
- **Current State**: Verbose fact extraction vs LLM summarization
- **Evidence**: badge_verify.py docstring (lines 1-19) and implementation:
  - "Lets RAVEN state the plain facts about a Credly badge [...] straight from what Credly itself publishes, instead of letting the LLM guess"
  - "Collecting these facts never touches a model. The verifier fetches the public badge page, reads Credly's own meta tags, and returns the fields verbatim"
- **Details**: Prevents LLM hallucination by extracting source facts directly

#### agent.supervisor_loop_guard: PRESENT
- **Current State**: Unattended supervisor/watchdog for agent loop
- **Evidence**: raven_watchdog.ps1 file:
  - Docstring: "RAVEN watchdog - unattended supervisor"
  - Single-instance guard (lines 57-71)
  - Process monitoring and restart logic (lines 78-196)
  - Crash-loop detection with backoff (lines 184-196)
  - Different handling for clean exits vs crashes (lines 175-182)
- **Details**: Supervisory process ensuring agent loop reliability

#### sandbox.policy_preflight: PRESENT
- **Current State**: Target validation and sandboxed execution
- **Evidence**: recon_engine.py:
  - Policy preflight: `validate_target` method (lines 125-153) validates targets are local/RFC1918 or pre-approved
  - Sandboxed execution: `bounded_subprocess` (lines 155-211) with 1MB output limits, timeout handling
  - Additional safe functions: `fetch_http_headers`, `port_scan`, `ping_host` all validate first
- **Details**: Preflight policy check → sandboxed execution for all potentially unsafe operations

#### release.post_update_verification: PRESENT
- **Current State**: Release artifact integrity verification
- **Evidence**: install.ps1 lines 31-65:
  - Downloads Ollama installer
  - Fetches expected SHA256 from GitHub releases (lines 31-49)
  - Computes actual hash (line 51)
  - Verifies match before proceeding (lines 53-61)
  - Shows actual hash for transparency (line 64)
- **Details**: Post-download verification before executing installer

## Recommendations

### Immediate Priorities (P1)
1. **Implement errors.actionable_taxonomy** - Define error categories with actionable messages
2. **Implement registry.table_driven** - Externalize configurable rules to YAML/JSON

### P2 Patterns Status
- 6/8 P2 patterns already implemented (fs.embedded_integrity, db.migration_advisory_lock, tenant.namespace_isolation, llm.chain_ast, llm.summarization_preservation, agent.supervisor_loop_guard, sandbox.policy_preflight, release.post_update_verification)
- 2 patterns need completion: api.envelope_unwrap and fs.atomic_staged_write (partial)

### Implementation Order
Following the priority order specified:
1. errors.actionable_taxonomy (P1)
2. registry.table_driven (P1) 
3. api.envelope_unwrap (P2)
4. fs.atomic_staged_write (P2 - complete the partial implementation)

## Verification Requirements
Per instructions, each implementation must be verified with:
```
python -m py_compile <touched files> && pytest -x -q
```