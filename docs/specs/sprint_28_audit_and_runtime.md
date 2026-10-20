# Sprint 28 / v1.2.0: Audit pack + Reference runtime

**Status:** planned
**Target version:** `1.2.0` (minor; backwards-compatible, additive)
**Depends on:** v1.1.0 (commit `4948b63`)
**Spec author:** Aditya Singh <avsingkernel@users.noreply.github.com>
**Spec written:** 2026-06-11

## Why this sprint

Two gaps remain between the v1.1.0 contracts and a vettable
product. The audit pack closes the *evidentiary* gap — there
is no current way to answer "what authorized this fact and what
evidence backs it?" without re-walking the bundle by hand. The
reference runtime closes the *execution* gap — the contracts
define what a record is, but there is no implementation of
how a runtime holds and moves them, which means a reviewer
who wants to see the contracts in action has only toy
examples.

The two together are the **pilot-readiness** release: a
vettable demo (the runtime) and a vettable proof (the audit
pack). Both are required for an external reviewer (e.g., a YC
partner) to evaluate the library without writing a 5,000-LOC
runtime of their own.

This sprint is the first that does not originate from a sprint
spec. The work was scoped in the v1.1.0 ADR-by-ADR map
(`docs/ROADMAP-to-product.md`) and the in-tree design notes
(`docs/architecture.md`, the Hermes competitive analysis). The
spec doc was retrofitted to the sprints pattern for the audit
trail.

## What this sprint is

- An `audit` module: `compute_audit_pack(records, *, as_of=None,
  title=...)` walks the full authorization chain for every
  trusted ledger entry. `audit_pack_to_markdown(pack)` renders a
  CFO-readable report. CLI: `audit <path> [--as-of] [--title]
  [--json]`. Mirrors the hygiene module's API shape and id
  discipline.
- A reference runtime: `agent_memory_contracts.runtime` —
  sqlite3 six-plane store, single validated write path, hash-
  chained audit anchors, deterministic pack builds, cite-or-
  refuse answer. 27 public names; implementation-detail
  constants (`_LEDGER_ID_PREFIXES`, `_STATUS_BY_OVERRIDE_DECISION`,
  `_PACK_RANK_WEIGHTS`, `DEFAULT_TENANT`) are private.
- Two new end-to-end demos:
  - `examples/poisoning_demo/` — same attack on a naive
    extract-append-retrieve store (silently poisoned) and a
    contracts-governed store (rejected with a real
    `MemoryReducerDecision` receipt).
  - `examples/arthashila_demo/` — the launch-week Arthashila
    NBFC dataset (30 emails, 5 policy docs, 2 poisoned docs)
    through the contracts. With `--runtime`, the same flow runs
    through the reference runtime.
- 5 differentiator invariants in `tests/invariants/` — the
  ADR-14 acceptance gate for any product-side port. Each
  invariant has positive + adversarial tests.

## What this sprint is not

- **Not a production runtime.** The runtime is a **reference**:
  its purpose is (a) to make the contract semantics executable
  and (b) to serve as the conformance test for any product-side
  port. A product will plug a Postgres backend into
  `StorageBackend`; the invariants are the conformance test.
- **Not an LLM call path.** The runtime's `answer()` is a
  deterministic template answerer (no model calls) so the
  contract is testable. A product will insert the LLM at the
  template boundary; the same `verify_grounding` post-check
  runs on whatever the LLM produces.
- **Not a new schema version.** v1.2.0 is additive; v1.1.0
  bundles continue to validate and load.
- **Not a Postgres port.** That work belongs to the product
  repo. The seams are documented in
  `docs/ROADMAP-to-product.md`.

## Public API

### Audit (8 names)

| Name | Module | Description |
| --- | --- | --- |
| `AuditPack` | `audit` | The pack: chains, rejections, supersessions, counts |
| `AuditChainEntry` | `audit` | One ledger entry's authorization chain |
| `AuditEvidenceRef` | `audit` | One cited span, resolved to its source |
| `AuditRejection` | `audit` | One `reject` reducer decision |
| `AuditSupersession` | `audit` | One governed replacement event |
| `compute_audit_pack` | `audit` | Compute a pack for a bundle |
| `audit_pack_to_markdown` | `audit` | Format a pack as a CFO-readable report |
| `DEFAULT_AUDIT_TITLE` | `audit` | The default pack title |

### Runtime (27 names)

`store` (10): `MemoryStore`, `StorageBackend`, `LedgerSearchHit`,
`ConflictError`, `IdMismatchError`, `NotFoundError`, `StoreError`,
`ValidationRejectedError`, `canonical_json`, `materialize_entry`
(public via re-export).

`gate` (3): `MemoryGate`, `IngestReceipt`, `PromoteReceipt`.

`anchors` (6): `AnchorReceipt`, `AnchorDivergence`,
`ChainVerification`, `CoverageReport`, `verify_chain`,
`verify_coverage`, `full_scope` (7 — listed in the audit
script).

`grounding` (8): `answer`, `build_context_pack`,
`related_candidates`, `verify_grounding`, `AnswerResult`,
`PackBuildResult`, `RuntimeBuildReceipt`, `Citation`.

Implementation-detail constants are private: `_LEDGER_ID_PREFIXES`,
`_STATUS_BY_OVERRIDE_DECISION`, `_PACK_RANK_WEIGHTS`,
`DEFAULT_TENANT`. The `canonical_json` helper is public because
tests and the anchor receipts serialize through it.

## Tests

- `tests/test_audit.py` (47 tests): pack assembly, broken
  chains, rejection rendering, supersession rendering,
  markdown formatting, content-derived pack id.
- `tests/test_runtime_anchors.py`: chain building,
  verify_chain, verify_coverage, tamper tests, the
  hand-armed-guard bypass test.
- `tests/test_runtime_gate.py`: idempotent ingest,
  promote, closure validation, conflict error, retract/
  contest/archive paths.
- `tests/test_runtime_grounding.py`: build_context_pack,
  answer refusal threshold, verify_grounding, related
  candidates, deterministic receipts.
- `tests/test_runtime_store.py`: append-only triggers,
  materialize_entry, full_revalidation, active_entries
  (time travel), check_extracted_columns, the SQL-trigger
  drift check.
- `tests/invariants/`: the 5 differentiator tests (see below).
- `tests/test_poisoning_demo.py` (11 tests): A poisoned,
  B clean, receipts present, fingerprint unchanged, report
  generated, deterministic across runs.
- `tests/test_arthashila_demo.py` + `test_arthashila_runtime.py`:
  end-to-end (static + runtime) on the real dataset, with
  full chain + coverage verification.

## ADR-14 differentiator invariants

Each test in `tests/invariants/` is a product claim in code. The
header of each file states the invariant in product language.

1. **no-silent-writes**: every public store/gate method
   fuzzed with trusted-plane payloads. None grows the
   trusted tables except a valid `promote()` (positive
   control). Raw SQL blocked by the schema. A hand-armed
   guard bypass is flagged by coverage. (`tests/invariants/
   test_no_silent_writes.py`, 3 tests)
2. **full-provenance**: chain walk
   entry→decision→candidate→span→source over the synthetic
   universe AND the full seeded Arthashila corpus through
   the runtime store. The audit pack agrees (9/9 complete).
   The walk is proven non-tautological by a hostile deletion.
   Superseded cells keep their full chains. Quarantined
   poisons have decision trails, not entries.
   (`tests/invariants/test_full_provenance.py`, 4 tests
   + 3 conditional on the dataset)
3. **reciprocal-supersession**: materialized reciprocity
   + exact temporal handoff validate via the library. A
   two-thread race on the same target yields exactly one
   edge; the loser gets a clean `ConflictError` naming the
   winner. (`tests/invariants/test_reciprocal_supersession
   .py`, 3 tests)
4. **deterministic-receipts**: same state + request =
   identical pack id = identical fingerprint. Tamper-tested
   against a hand-edited anchor. (`tests/invariants/
   test_deterministic_receipts.py`, 8 tests)
5. **grounded-or-refused**: below-threshold support returns
   a structured refusal with the unreviewed candidates.
   Otherwise a deterministic template answer where every
   sentence carries a resolving `[Fi]` citation by
   construction, post-validated by `verify_grounding`. The
   checker degrades to refusal, never to an uncited answer.
   (`tests/invariants/test_grounded_or_refused.py`, 7
   tests)

If a product-side port must weaken any of these to pass a
feature, the rule is **stop and escalate**. The differentiator
suite is the contract; loosening it dissolves the claim.

## Decisions applied to this sprint

### Small defaults

1. **Audit pack id is content-derived.** Same bundle + same
   `as_of` → same pack id. An attested pack can be re-verified
   later.
2. **Audit pack broken chains are `INCOMPLETE`, not raised.**
   The auditor can render an incomplete pack as a finding; a
   product might be in the middle of ingesting a partial
   batch. The pack never refuses to assemble.
3. **The runtime uses sqlite3, not Postgres.** Stdlib-only
   is a house rule. The semantics (append-only triggers,
   partial unique indexes, single-writer transactions
   standing in for `FOR SHARE`) are implemented sqlite-
   natively. `StorageBackend` is the protocol seam; the
   invariants are the conformance test for any other
   backend.
4. **`MemoryGate` is the only write path.** There is no
   `insert_ledger_entry()` API. The gate accepts decisions +
   entries + supersessions as one transactional closure.
5. **The first-promoter-wins for conflicts.** A double
   promotion, a double supersession, or a double override
   all return `ConflictError` with the winning decision id.
   No automatic merge, ever.
6. **The runtime synthesizes a `ProjectStateSnapshot` per
   pack.** The library compiler requires a state reference.
   The runtime derives a deterministic, content-addressed
   snapshot from the pack's selection. The state reducer
   is product scope; this is the deliberate adaptation
   that fixes the predecessor draft's never-runnable pack
   path.
7. **`answer()` is a deterministic template answerer.** No
   LLM. A product inserts the LLM at the template boundary;
   the same `verify_grounding` post-check runs on whatever
   the LLM produces.
8. **`answer()` returns a structured refusal below
   threshold**, with `related_candidate_ids` (the
   unreviewed candidates the user might want to consider).
   Refusal is the feature, not the failure mode.
9. **Every committed batch appends a hash-chained
   `AnchorReceipt`** with a replayable scope. The runtime
   appends the anchor inside the gate transaction; nothing
   in the trusted tables can be written without an anchor.

### Bigger defaults

10. **Status overrides (retract/contest/archive) live in
    an edge table, not on the entry.** The contracts
    support these statuses; the ADRs only specced
    supersession against append-only rows. The runtime
    extends the same edge-materialized pattern (one
    terminal override per entry, first commit wins).
11. **`_LEDGER_ID_PREFIXES`, `_STATUS_BY_OVERRIDE_DECISION`,
    `_PACK_RANK_WEIGHTS`, `DEFAULT_TENANT` are private
    (prefixed `_`).** They are implementation details of
    the sqlite3 reference; a Postgres port would not
    reuse them. The frozen public surface is the classes,
    errors, and 7 verbs.
12. **Tamper tests drop the SQL triggers.** The hostile-DBA
    threat model: an attacker with `sqlite_master` write
    access can drop the append-only triggers. The
    invariants' `verify_coverage()` flags any trusted row
    that no anchor accounts for, which is the
    defense-in-depth that survives the trigger loss. The
    ADR-6 chain is the second layer; together they are the
    *what gets detected, not what gets prevented*
    claim.

## Definition of done

- [x] `src/agent_memory_contracts/audit.py` (955 LOC) with
      8 public names.
- [x] CLI: `audit <path> [--as-of] [--title] [--json]`.
- [x] `src/agent_memory_contracts/runtime/` (4 modules,
      3,778 LOC total) with 27 public names.
- [x] `examples/poisoning_demo/` — 4 files, runnable.
- [x] `examples/arthashila_demo/` — 1,189-LOC adapter,
      with `--runtime` flag.
- [x] `tests/invariants/` — 5 files, 26 tests.
- [x] `tests/test_audit.py` (47 tests) +
      `tests/test_runtime_*.py` (98 tests) +
      `tests/test_poisoning_demo.py` (11 tests) +
      `tests/test_arthashila_*.py` (~20 tests).
- [x] `docs/STABILITY.md` — two new sections (Audit pack,
      Runtime).
- [x] `docs/ROADMAP-to-product.md` — successor map.
- [x] README — Runtime + Audit packs sections; updated
      badges and counts.
- [x] CHANGELOG — v1.2.0 section.
- [x] CI — install all extras; example smoke loop
      includes `examples/*/run.py`.
- [ ] TestPyPI publish — to be actioned after commit.

## Bottom line

The library was feature-complete at v1.1.0; v1.2.0 makes it
*vettable*. An external reviewer can:

- Read the contracts (frozen at v1.0.0).
- Read the audit pack spec (one function, one dataclass).
- Run the reference runtime against the seed corpus
  (one command, deterministic, no LLM).
- Run the differentiator invariants (one command, 26
  tests, positive + adversarial).
- Run the poisoning demo (one command, two stores,
  byte-identical fingerprints before and after the attack).
- Run the Arthashila demo end to end (one command, the
  real launch-week dataset through the runtime, anchor
  chain and coverage verified).

That is a complete external-review surface for a
YC-style vetter. The product port (Postgres/FastAPI/console)
is the remaining work; the seam is documented ADR-by-ADR.
