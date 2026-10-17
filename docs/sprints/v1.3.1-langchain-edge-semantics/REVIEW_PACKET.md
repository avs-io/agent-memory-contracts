# v1.3.1 LangChain Edge Semantics Review Packet

## Purpose

Close non-blocking GPT Pro findings from the v1.3.0 release review without
changing core schemas, semantic ids, canonicalization, reducer semantics, or
product boundaries.

## Base

- Base: `origin/main` at `7a2f9057a4fd85bbfbca335a41d4c7a4d78b2878`
- Branch: `codex/amc-langchain-edge-semantics`
- Release target: patch/minor follow-up after `v1.3.0`

## Scope

- Validate LangChain numeric limits at construction:
  - `ContractsMemoryConfig.max_bundles`
  - `ContractsMemoryConfig.max_records_per_load`
  - direct `MemoryStore.max_bundles`
- Reject zero, negative, boolean, and non-integer limit values with
  `ValueError`.
- Document that the in-memory LangChain `MemoryStore` is not synchronized for
  concurrent same-session writers; multithreaded applications must serialize
  writes externally or provide a synchronized store.
- Update the stale README development test-count comment.

## Non-Goals

- No locks or concurrent writer implementation in this PR.
- No change to the v1.3.0 shared-store sequential turn-allocation behavior.
- No core schema, ID, canonicalization, runtime, MCP, or package metadata
  changes.
- No product wrapper/control-plane code in `agent-memory-contracts`.

## Files Touched

- `src/agent_memory_contracts/integrations/langchain.py`
- `tests/test_integrations_langchain.py`
- `README.md`
- `CHANGELOG.md`
- `docs/STABILITY.md`
- `docs/specs/sprint_25_langchain_memory.md`
- `docs/sprints/v1.3.1-langchain-edge-semantics/REVIEW_PACKET.md`

## Local Gates

Run from the repository root (a checkout of the `langchain-edge-semantics` worktree).

```bash
PYTHONPATH=src /tmp/amc-release-check-venv312/bin/python -m pytest -q tests/test_integrations_langchain.py
PYTHONPATH=src /tmp/amc-release-check-venv312/bin/python -m pytest --collect-only -q
PYTHONPATH=src /tmp/amc-release-check-venv312/bin/python -m pytest -q
/tmp/amc-release-check-venv312/bin/python -m mypy src/agent_memory_contracts
PYTHONPATH=src /tmp/amc-release-check-venv312/bin/python scripts/audit_public_api.py
PYTHONPATH=src /tmp/amc-release-check-venv312/bin/python -m compileall -q src
git diff --check
/tmp/amc-release-check-venv312/bin/python scripts/check_release_artifacts.py --run-sdist-tests --run-examples
```

Results:

- Targeted LangChain tests: passed, `28` tests.
- Collection count: `852` tests.
- Full pytest: passed with expected Arthashila dataset skips and the
  jsonschema-installed missing-path skip.
- mypy: passed, `39` source files.
- Public API audit: exit `0` with the known `104` STABILITY-vs-`__all__`
  warnings.
- compileall and `git diff --check`: passed.
- Release artifact verifier: passed with sdist tests and examples;
  `agent_memory_contracts-1.3.0.tar.gz` had `204` members and the wheel had
  `69` members.

## External Scout Notes

- Grok and Antigravity were used as read-only scout/reviewer lanes before the
  final patch. Both independently identified the same two edge cases:
  `max_records_per_load=0` loads all records because `episodes[-0:]` is
  `episodes[0:]`, and `MemoryStore(max_bundles=0)` creates an empty black-hole
  deque while negative values fail later.
- Both also flagged that the in-memory LangChain `MemoryStore` should not imply
  thread-safe writer behavior. This PR documents the unsynchronized boundary
  without adding locks.

## Review Focus

1. Does positive-limit validation preserve existing valid usage while failing
   closed for surprising edge values?
2. Is the thread-safety wording honest without implying a new synchronization
   guarantee?
3. Are the new tests narrow and sufficient?
4. Did this PR avoid touching product architecture or v2 schema/ID semantics?
