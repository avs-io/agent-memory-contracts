# Contributing

Contributions are welcome. This is a small, focused library; the bar
for additions is "does this make the memory-integrity story stronger
for someone building an AI agent?"

## Setup

```bash
git clone https://github.com/eoniclife/agent-memory-contracts.git
cd agent-memory-contracts
# Install all extras to run the full test suite, the example
# smoke loop, the audit CLI smoke, and the runtime smoke in
# CI. For just the contracts layer, `[jsonschema]` is enough.
pip install -e ".[dev,jsonschema,langchain,mcp]"
pytest -q
```

## What this library is

A schema pack and reference Python implementation for the six memory
planes of an AI agent (evidence, candidate, ledger, taste, state,
ContextPack), with stable ID formats, per-plane validators, and bundle
graph validators. Standard-library only. Apache-2.0.

## What this library is not

- Not an agent runtime. Workers, queues, leases, scheduling live in
  other projects.
- Not a vector store or retrieval layer. The ContextPack plane
  describes what a *bundle* of memory looks like; how you fetch spans
  and project them into a bundle is up to you.
- Not a model-call wrapper. The contracts describe memory; how
  candidates are produced is up to the extractor.

## Ground rules

1. **Standard library only** for runtime code. `jsonschema` is
   allowed for users who want it (in their own code), but the package
   itself depends on nothing.
2. **Schemas and Python contracts are versioned together** at the
   `schema_version` field. Bumping to `1.1.0` is allowed for additive
   changes; breaking changes require a major version bump and a
   deprecation window.
3. **Bundle validators must remain strict.** If a validator starts
   accepting a graph that the README says is invalid, that's a bug.
4. **ID formats are stable.** Once a record type's ID prefix is
   published, changing the algorithm that derives the id is a breaking
   change.
5. **Tests must pass on Python 3.10, 3.11, 3.12** with no warnings
   about unused imports or shadowed names.

## Pull request flow

1. Open an issue first if the change is non-trivial. Most contract
   changes will affect downstream users; we want to discuss the
   design before the code.
2. Fork and branch from `main`.
3. Run `pytest -q` locally; the suite must pass.
4. Run `examples/quickstart.py`; it must produce the expected
   "Bundle validated." output.
5. Open a PR. CI will run on Python 3.10 / 3.11 / 3.12.
6. The `leak-guard` workflow fails a PR if it adds private markers:
   non-allowlisted emails, phone numbers, local home paths, API keys
   or denylisted terms. Run `python scripts/leak_guard.py --tree`
   locally. To get the optional pre-commit hook, run
   `git config core.hooksPath .githooks && chmod +x .githooks/pre-commit`.
   The denylist lives in
   `.github/leak-guard.json`.

## Reporting a bug

Open an issue with:
- The smallest reproduction (which contract, which input, what
  happened vs. what you expected).
- The Python version.
- The output of `python -c "import agent_memory_contracts; print(agent_memory_contracts.__version__)"`.

## Security

If you find a security issue, please email instead of opening a public
issue. (See the `authors` field in `pyproject.toml` for contact.)
