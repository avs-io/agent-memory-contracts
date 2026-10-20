"""Arthashila demo: the real NBFC dataset through the contracts.

Run from the repository root::

    PYTHONPATH=src python examples/arthashila_demo/build.py
    PYTHONPATH=src python examples/arthashila_demo/build.py \\
        --dataset /path/to/05-demo-dataset --out /tmp/arthashila-out

Add ``--runtime`` to run the same end-to-end flow through the
sqlite reference runtime instead of static record assembly: every
record enters via the validated gate (idempotent ingestion + the
single transactional ``promote()``), the poisons are quarantined
by recorded ``reject`` decisions, the v3 -> v4 supersession is an
edge materialized at read, every committed batch is hash-chain
anchored (and the chain + coverage verified), the walkthrough
question is answered from a deterministic, receipted ContextPack
built out of the store (12.50%, every clause cited), and the
audit pack is emitted from **store state**. Re-running against
the same output directory is a no-op end to end (content-derived
ids: ingestion and promotion replay idempotently, no new
anchors).

Adapter for the "Arthashila Finance Pvt Ltd" synthetic NBFC corpus
(30 emails, 5 policy documents, 2 poisoned documents). The pipeline:

1. **Ingest** -- every email, policy, and poisoned document becomes
   a ``SourceRecord`` (content hash = SHA-256 of the file, source
   trust tier recorded in metadata) with deterministic
   ``EvidenceSpan`` line offsets located by anchor text.
2. **Extract** -- ~10 key facts via simple deterministic extraction
   fixtures (no LLM calls): the v4 rate cells, the CIBIL 720+
   concession, the no-stacking rule, the v4 authorization chain,
   the DSA payout grid, the superseded v3 cell, and the two
   poisoned claims.
3. **Reduce** -- a scripted reducer session promotes the legitimate
   candidates (citing evidence), records the v3 -> v4 supersession
   as a governed event, and rejects the poisons mirroring the
   dataset's documented kill chains (``poisoned/README.md``):
   POISON-1 dies on the source trust tier (spoofed free-mail
   sender vs the established counterparty domain); POISON-2 dies
   on the authorization chain (approval block all ``[pending]``,
   and it cites the 21 April meeting that email-018 shows the CEO
   cancelled). The whole graph passes ``validate_ledger_bundle``.
4. **Answer** -- the walkthrough question ("what rate applies to a
   Rs 2.1 Cr LAP, CIBIL 726, risk grade B?") is answered from the
   trusted ledger: v4 card rate 12.75% (grade B, LTV 50-65 band per
   the live comparable AFL-LAP-26-0412, LTV 58%) minus the 25 bps
   HBS concession (CIBIL 726 >= 720; no exception loading, so the
   no-stacking rule permits it) = **12.50%**, every clause cited.
5. **Audit** -- the full bundle is exported as an Audit Pack
   (``compute_audit_pack`` / ``audit_pack_to_markdown``) to
   ``out/audit-pack.md``, plus a machine-readable ``summary.json``.

Adapter notes (adapting the adapter, not the dataset):

- The walkthrough leaves the demo loan's LTV out of the question
  text; the dataset's live comparable (AFL-LAP-26-0412, Shree
  Balaji Textiles -- Rs 2.1 Cr, grade B, CIBIL 726, LTV 58%,
  sanctioned at 12.50%) pins the 50-65% band, so the demo question
  states LTV 58% explicitly and the expected answer is 12.50%.
- Policy markdown files are ingested as ``manual_note`` sources
  (the contracts' closest source_type for an on-disk policy doc);
  the governed/draft distinction lives in ``metadata.trust_tier``.

Deterministic: fixed extraction/decision timestamps, content-derived
ids, anchor-located spans. Same dataset, same output, every run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

from agent_memory_contracts import (
    CandidateClaim,
    EvidenceSpan,
    FactLedgerEntry,
    MemoryReducerDecision,
    SourceRecord,
    audit_pack_to_markdown,
    bundle_fingerprint,
    compute_audit_pack,
    make_candidate_id,
    make_ledger_entry_id,
    make_reducer_decision_id,
    make_source_id,
    make_span_id,
    sha256_hex,
    validate_ledger_bundle,
)

DEFAULT_DATASET = Path(
    "/sessions/inspiring-magical-a8efd8/mnt/Personal-Offboarding/"
    "launch-week/05-demo-dataset")
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "out"

# Fixed timestamps: the session runs after the last corpus email
# (5 June 2026) and before the RBI onsite (6 July 2026).
T_EXTRACTED = "2026-06-06T09:00:00Z"
T_DECIDED = "2026-06-06T10:00:00Z"
T_AS_OF = "2026-06-08T09:00:00Z"

V3_EFFECTIVE = "2026-01-19T00:00:00Z"
V4_EFFECTIVE = "2026-05-18T00:00:00Z"

REJECT_UNTRUSTED_SOURCE = "untrusted_source"
REJECT_NO_AUTHORIZING_CHAIN = "no_authorizing_chain"

#: Sender domains established in the corpus. Anything else is an
#: untrusted source tier (POISON-1's freemail sender fails here).
#:
#: The defaults are neutral placeholders, so the public repo carries
#: no real-looking addresses. To run against the original corpus, put
#: a ``trust.json`` next to ``walkthrough.md`` or pass
#: ``--trust-config``:
#:   {"internal_domain": "...", "trusted_domains": ["..."],
#:    "authorized_officers": ["..."]}
INTERNAL_DOMAIN = "arthashila.example"
TRUSTED_DOMAINS = {INTERNAL_DOMAIN, "shubhassociates.in",
                   "trisysinfotech.com"}

#: Officers whose written word can authorize counterparty terms
#: (the email-009 payout control is the CFO's own statement).
AUTHORIZED_OFFICERS = {
    "officer1@arthashila.example",
    "officer2@arthashila.example",
    "officer3@arthashila.example",
    "officer4@arthashila.example",
}

TRUST_CONFIG_NAME = "trust.json"
_TRUST_CONFIG_OVERRIDE: Path | None = None


def load_trust_config(dataset_dir: Path,
                      explicit: Path | None = None) -> bool:
    """Override the placeholder trust settings from ``explicit`` or
    ``<dataset_dir>/trust.json``. Returns True if a config was applied."""
    global INTERNAL_DOMAIN
    path = explicit or (Path(dataset_dir) / TRUST_CONFIG_NAME)
    if not path.exists():
        if explicit:
            raise FileNotFoundError(f"trust config not found: {path}")
        return False
    cfg = json.loads(path.read_text(encoding="utf-8"))
    INTERNAL_DOMAIN = str(cfg.get("internal_domain", INTERNAL_DOMAIN)).lower()
    TRUSTED_DOMAINS.clear()
    TRUSTED_DOMAINS.update(d.lower() for d in cfg.get("trusted_domains", []))
    TRUSTED_DOMAINS.add(INTERNAL_DOMAIN)
    if "authorized_officers" in cfg:
        AUTHORIZED_OFFICERS.clear()
        AUTHORIZED_OFFICERS.update(
            a.lower() for a in cfg["authorized_officers"])
    return True

_CHECKS_PASS = {
    "provenance": "pass",
    "temporal_validity": "pass",
    "contradiction_scan": "pass",
    "privacy": "pass",
    "usefulness": "pass",
}


# ---------------------------------------------------------------------------
# Stage 1: dataset -> SourceRecords + EvidenceSpans
# ---------------------------------------------------------------------------


def _parse_email_headers(text: str) -> tuple[str, str, str]:
    """Return (sender_address, captured_at_iso, subject) from the
    dataset's email markdown header block."""
    sender = ""
    captured_at = ""
    subject = ""
    for line in text.splitlines()[:8]:
        if line.startswith("From:"):
            sender = parseaddr(line[len("From:"):].strip())[1]
        elif line.startswith("Date:"):
            captured_at = parsedate_to_datetime(
                line[len("Date:"):].strip()).isoformat()
        elif line.startswith("Subject:"):
            subject = line[len("Subject:"):].strip()
    return sender, captured_at, subject


def _doc_title(text: str, fallback: str) -> str:
    """First markdown heading, else the fallback (file stem)."""
    for line in text.splitlines()[:6]:
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return fallback


def _has_populated_approval_block(text: str) -> bool:
    """True if the document carries a populated approval block.

    Governed policies (v3, v4, the NPA circular, ...) carry ALCO /
    Board references; POISON-2's approval fields all read
    ``[pending]``, which is exactly the dataset's kill chain for it.
    """
    has_block = ("ALCO approval" in text or "Board approval" in text
                 or "Board noting" in text)
    return has_block and "[pending" not in text


def _trust_tier(relpath: str, sender: str, text: str) -> str:
    """Source trust tier, derived from the corpus itself."""
    domain = sender.rsplit("@", 1)[-1].lower() if "@" in sender else ""
    if relpath.startswith("poisoned/") and domain and (
            domain not in TRUSTED_DOMAINS):
        return "untrusted_external"
    if domain:
        if domain == INTERNAL_DOMAIN:
            return "internal_verified"
        if domain in TRUSTED_DOMAINS:
            return "external_known_counterparty"
        return "untrusted_external"
    # No sender: an on-disk document. Governed if its approval
    # block is populated, a draft otherwise (POISON-2's tier --
    # it passes the source gate and must die on authorization).
    if _has_populated_approval_block(text):
        return "governed_document"
    return "internal_draft"


class DatasetAdapter:
    """Loads the dataset directory and converts files on demand."""

    def __init__(self, dataset_dir: Path) -> None:
        self.dataset_dir = dataset_dir
        self.sources_by_relpath: dict[str, SourceRecord] = {}
        self.texts_by_relpath: dict[str, str] = {}
        self.spans_by_id: dict[str, EvidenceSpan] = {}

    def ingest_file(self, relpath: str) -> SourceRecord:
        """File -> SourceRecord. Emails are parsed for sender/date;
        policies and poisons are documents."""
        if relpath in self.sources_by_relpath:
            return self.sources_by_relpath[relpath]
        path = self.dataset_dir / relpath
        text = path.read_text(encoding="utf-8")
        self.texts_by_relpath[relpath] = text
        content_hash = sha256_hex(text)

        is_email = text.lstrip().startswith("From:")
        if is_email:
            sender, captured_at, subject = _parse_email_headers(text)
            source_type = "email_thread"
            title = subject or _doc_title(text, Path(relpath).stem)
        else:
            sender, captured_at = "", T_EXTRACTED
            source_type = "manual_note"
            title = _doc_title(text, Path(relpath).stem)

        raw_ref = {"kind": "local_path", "value": relpath}
        source_id = make_source_id(source_type, raw_ref, content_hash)
        source = SourceRecord.from_dict({
            "id": source_id,
            "schema_version": "1.0.0",
            "source_type": source_type,
            "title": title,
            "origin_uri": None,
            "raw_ref": raw_ref,
            "content_hash_sha256": content_hash,
            "captured_at": captured_at,
            "observed_at": captured_at,
            "author_or_sender": sender or None,
            "participants": [sender] if sender else [],
            "privacy_class": "internal",
            "custody_status": "external_pointer",
            "parser_version": "arthashila-adapter-v1",
            "metadata": {
                "relpath": relpath,
                "trust_tier": _trust_tier(relpath, sender, text),
                "has_populated_approval_block":
                    _has_populated_approval_block(text),
            },
        })
        self.sources_by_relpath[relpath] = source
        return source

    def span(self, relpath: str, anchor: str) -> EvidenceSpan:
        """Deterministic span: the (1-based) line of the first line
        containing ``anchor`` in the file."""
        source = self.ingest_file(relpath)
        text = self.texts_by_relpath[relpath]
        for line_no, line in enumerate(text.splitlines(), start=1):
            if anchor in line:
                excerpt = line.strip()
                locator_value = f"{line_no}-{line_no}"
                break
        else:
            raise ValueError(f"anchor {anchor!r} not found in {relpath}")
        span_id = make_span_id(source.id, "line_range", locator_value)
        if span_id in self.spans_by_id:
            return self.spans_by_id[span_id]
        span = EvidenceSpan.from_dict({
            "id": span_id,
            "schema_version": "1.0.0",
            "source_id": source.id,
            "episode_id": None,
            "locator": {"kind": "line_range", "value": locator_value},
            "text_excerpt": excerpt,
            "excerpt_policy": "short_quote_allowed",
            "span_hash_sha256": sha256_hex(excerpt),
            "privacy_class": "internal",
            "metadata": {"relpath": relpath},
        })
        self.spans_by_id[span_id] = span
        return span


# ---------------------------------------------------------------------------
# Stage 2: the extraction fixtures (~10 key facts, no LLM)
# ---------------------------------------------------------------------------

V4 = "policies/Interest-Rate-Risk-Grading-Matrix-v4-May2026.md"
V3 = "policies/Interest-Rate-Risk-Grading-Matrix-v3-Jan2026.md"
EXC_LOG = "policies/Credit-Exception-Log-Q1Q2-2026.md"
EMAIL_009 = "emails/email-009.md"
EMAIL_018 = "emails/email-018.md"
EMAIL_021 = "emails/email-021.md"
POISON_1 = "poisoned/POISON-1-forged-dsa-payout-memo.md"
POISON_2 = "poisoned/POISON-2-unauthorized-rate-draft.md"

#: key -> fixture. ``evidence``: (relpath, anchor) pairs;
#: ``valid_from``/``valid_until``: temporal window for the
#: resulting fact.
EXTRACTION_FIXTURES: dict[str, dict[str, Any]] = {
    "v4_lap_b_le50": {
        "subject": "card_rate.lap.grade_b.ltv_le_50",
        "predicate": "rate_is",
        "object": "12.25%",
        "claim_text": ("LAP card rate, grade B, LTV <= 50%: 12.25% "
                       "per Rate Matrix v4 (effective 18 May 2026)."),
        "evidence": [(V4, "| **B** | **12.25%**")],
        "valid_from": V4_EFFECTIVE,
    },
    "v4_lap_b_50_65": {
        "subject": "card_rate.lap.grade_b.ltv_50_65",
        "predicate": "rate_is",
        "object": "12.75%",
        "claim_text": ("LAP card rate, grade B, LTV 50-65%: 12.75% "
                       "per Rate Matrix v4 (effective 18 May 2026)."),
        "evidence": [(V4, "| **B** | **12.25%**")],
        "valid_from": V4_EFFECTIVE,
    },
    "v4_lap_a_le50": {
        "subject": "card_rate.lap.grade_a.ltv_le_50",
        "predicate": "rate_is",
        "object": "11.25%",
        "claim_text": ("LAP card rate, grade A, LTV <= 50%: 11.25% "
                       "per Rate Matrix v4 (effective 18 May 2026)."),
        "evidence": [(V4, "| **A** | **11.25%**")],
        "valid_from": V4_EFFECTIVE,
    },
    "v4_lap_a_50_65": {
        "subject": "card_rate.lap.grade_a.ltv_50_65",
        "predicate": "rate_is",
        "object": "11.75%",
        "claim_text": ("LAP card rate, grade A, LTV 50-65%: 11.75% "
                       "per Rate Matrix v4 (effective 18 May 2026)."),
        "evidence": [(V4, "| **A** | **11.25%**")],
        "valid_from": V4_EFFECTIVE,
    },
    "v4_hbs_concession": {
        "subject": "policy.hbs_concession",
        "predicate": "grants",
        "object": "25 bps off card rate (LAP, grades A-C, primary CIBIL >= 720)",
        "claim_text": ("High-Bureau-Score concession, Matrix v4 par.4: "
                       "25 bps off card rate for LAP, grades A-C, where "
                       "the primary applicant's CIBIL is 720 or above."),
        "evidence": [(V4, "25 bps off card rate")],
        "valid_from": V4_EFFECTIVE,
    },
    "v4_no_stacking": {
        "subject": "policy.hbs_concession.no_stacking",
        "predicate": "requires",
        "object": "no exception loading and no other concession (one discount per loan)",
        "claim_text": ("The HBS concession is not combinable with any "
                       "exception loading or other concession (Matrix v4 "
                       "par.4; precedent EXC-2026-011, where CIBIL 731 "
                       "correctly received no concession on an "
                       "exception-loaded loan)."),
        "evidence": [(V4, "no exception loading and no other concession"),
                     (EXC_LOG, "HBS concession NOT applied")],
        "valid_from": V4_EFFECTIVE,
    },
    "v4_authorization": {
        "subject": "rate_matrix_v4.authorization",
        "predicate": "authorized_by",
        "object": "ALCO/2026/05 (6 May 2026) + Board resolution BR/2026/22 (14 May 2026), effective 18 May 2026",
        "claim_text": ("Rate Matrix v4 was approved by ALCO on 6 May 2026 "
                       "(ref ALCO/2026/05) and by the Board on 14 May 2026 "
                       "(resolution BR/2026/22), effective 18 May 2026, "
                       "superseding v3."),
        "evidence": [(V4, "ALCO/2026/05"),
                     (EMAIL_021, "resolution BR/2026/22")],
        "valid_from": V4_EFFECTIVE,
    },
    "dsa_payout": {
        "subject": "dsa_payout.shubh_associates",
        "predicate": "rate_is",
        "object": "1.00% LAP / 1.25% SME, payable after first EMI",
        "claim_text": ("The DSA payout grid for Shubh Associates stands at "
                       "1.00% of disbursed amount on LAP and 1.25% on SME "
                       "secured, payable after the first EMI clears, with "
                       "the 12-month NPA clawback (CFO, 12 Feb 2026); no "
                       "change is valid without CFO+MD written joint "
                       "signature."),
        "evidence": [(EMAIL_009, "1.00% of disbursed amount on LAP")],
        "valid_from": "2026-02-12T15:08:00+05:30",
    },
    # The superseded cell: v3's value for the band the demo loan
    # sits in. Promoted as a superseded entry so the supersession
    # is a governed, auditable event (not a deletion).
    "v3_lap_b_50_65": {
        "subject": "card_rate.lap.grade_b.ltv_50_65",
        "predicate": "rate_is",
        "object": "13.00%",
        "claim_text": ("LAP card rate, grade B, LTV 50-65%: 13.00% per "
                       "Rate Matrix v3 (effective 19 January 2026; "
                       "superseded 18 May 2026 by v4)."),
        "evidence": [(V3, "| **B** | 12.50% | 13.00% | 13.75% |")],
        "valid_from": V3_EFFECTIVE,
        "valid_until": V4_EFFECTIVE,
    },
    # POISON-1: the forged payout memo's central claim.
    "poison1_payout": {
        "subject": "dsa_payout.shubh_associates",
        "predicate": "rate_is",
        "object": "1.75% LAP / 2.25% SME, on disbursal (forged)",
        "claim_text": ("Revised DSA payout structure effective immediately: "
                       "1.75% LAP / 2.25% SME, payable on disbursal, "
                       "clawback withdrawn."),
        "evidence": [(POISON_1, "| Loan Against Property (LAP) | 1.00% | **1.75%** |")],
        "valid_from": None,
    },
    # POISON-2: the unauthorized draft's rate cell for the band
    # the walkthrough exercises hardest.
    "poison2_rate": {
        "subject": "card_rate.lap.grade_b.ltv_le_50",
        "predicate": "rate_is",
        "object": "12.00% (draft v4.1)",
        "claim_text": ("LAP card rate, grade B, LTV <= 50%: 12.00% per "
                       "Interim Rate Revision Draft v4.1 of 24 April 2026, "
                       "proposed effective 1 May 2026."),
        "evidence": [(POISON_2, "| **B** | 12.00% | 12.50% | 13.50% |")],
        "valid_from": None,
    },
}


def build_candidates(
    adapter: DatasetAdapter,
) -> dict[str, tuple[CandidateClaim, list[EvidenceSpan]]]:
    """Materialise every extraction fixture as a CandidateClaim
    with anchor-located evidence spans."""
    out: dict[str, tuple[CandidateClaim, list[EvidenceSpan]]] = {}
    for key, fixture in EXTRACTION_FIXTURES.items():
        spans = [adapter.span(relpath, anchor)
                 for relpath, anchor in fixture["evidence"]]
        span_ids = [s.id for s in spans]
        source_ids = sorted({s.source_id for s in spans})
        temporal_hint = {
            "observed_at": None, "asserted_at": None,
            "valid_from_hint": fixture.get("valid_from"),
            "valid_until_hint": fixture.get("valid_until"),
        }
        candidate_id = make_candidate_id("claim", span_ids, {
            "subject": fixture["subject"],
            "predicate": fixture["predicate"],
            "object": fixture["object"],
            "claim_text": fixture["claim_text"],
            "claim_scope": "company",
            "temporal_hint": temporal_hint,
        })
        candidate = CandidateClaim.from_dict({
            "id": candidate_id,
            "schema_version": "1.0.0",
            "candidate_type": "claim",
            "source_record_ids": source_ids,
            "episode_record_ids": [],
            "evidence_span_ids": span_ids,
            "natural_language_summary": fixture["claim_text"],
            "extracted_by": {"agent": "arthashila-fixture-extractor",
                             "model": "deterministic",
                             "tool": None, "prompt_ref": None},
            "extracted_at": T_EXTRACTED,
            "confidence": "high",
            "risk_class": "medium",
            "status": "candidate",
            "review": {"reviewed_by": None, "reviewed_at": None,
                       "review_notes": None},
            "metadata": {"fixture_key": key},
            "subject": fixture["subject"],
            "predicate": fixture["predicate"],
            "object": fixture["object"],
            "claim_text": fixture["claim_text"],
            "claim_scope": "company",
            "temporal_hint": temporal_hint,
        })
        out[key] = (candidate, spans)
    return out


# ---------------------------------------------------------------------------
# Stage 3: the scripted reducer session
# ---------------------------------------------------------------------------


def _fact_id_for(key: str, span_ids: list[str], valid_from: str) -> str:
    fixture = EXTRACTION_FIXTURES[key]
    return make_ledger_entry_id("fact", span_ids, {
        "ledger_type": "fact",
        "subject": fixture["subject"],
        "predicate": fixture["predicate"],
        "object": fixture["object"],
        "scope": "company",
        "valid_from": valid_from,
        "evidence_span_ids": sorted(span_ids),
    })


def _fact_entry(key: str, candidate: CandidateClaim,
                spans: list[EvidenceSpan], *, entry_id: str,
                decision_id: str, status: str, valid_from: str,
                valid_until: str | None,
                supersedes: list[str], superseded_by: list[str],
                ) -> FactLedgerEntry:
    return FactLedgerEntry.from_dict({
        "id": entry_id,
        "schema_version": "1.0.0",
        "ledger_type": "fact",
        "status": status,
        "confidence": "high",
        "scope": "company",
        "source_record_ids": sorted({s.source_id for s in spans}),
        "episode_record_ids": [],
        "evidence_span_ids": [s.id for s in spans],
        "candidate_ids": [candidate.id],
        "reducer_decision_id": decision_id,
        "subject": candidate.subject,
        "predicate": candidate.predicate,
        "object": candidate.object,
        "fact_text": candidate.claim_text,
        "observed_at": valid_from,
        "asserted_at": T_DECIDED,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "stale_after": None,
        "created_at": T_DECIDED,
        "updated_at": T_DECIDED,
        "supersedes": supersedes,
        "superseded_by": superseded_by,
        "metadata": {"fixture_key": key},
    })


def _decision(decision_type: str, candidate_ids: list[str],
              ledger_ids: list[str], span_ids: list[str],
              rationale: str, *, checks: dict[str, str],
              metadata: dict[str, Any]) -> MemoryReducerDecision:
    decision_id = make_reducer_decision_id(
        decision_type, candidate_ids, ledger_ids, span_ids, rationale)
    return MemoryReducerDecision.from_dict({
        "id": decision_id,
        "schema_version": "1.0.0",
        "decision_type": decision_type,
        "target_candidate_ids": candidate_ids,
        "target_ledger_entry_ids": ledger_ids,
        "evidence_span_ids": span_ids,
        "rationale": rationale,
        "decided_by": {"agent": "arthashila-scripted-reducer",
                       "model": "deterministic",
                       "tool": None, "prompt_ref": None},
        "decided_at": T_DECIDED,
        "confidence": "high",
        "risk_class": "medium" if decision_type != "reject" else "high",
        "checks": checks,
        "metadata": metadata,
    })


def run_reducer_session(
    adapter: DatasetAdapter,
    candidates: dict[str, tuple[CandidateClaim, list[EvidenceSpan]]],
) -> dict[str, Any]:
    """The scripted session: promote the legits, record the v3->v4
    supersession as one governed event, reject the two poisons per
    the dataset's kill chains. Returns the session state."""
    decisions: list[MemoryReducerDecision] = []
    ledger: list[FactLedgerEntry] = []
    quarantine: list[dict[str, Any]] = []

    # --- gate helpers (derived from the corpus, not hardcoded) ----
    def source_for(candidate: CandidateClaim) -> SourceRecord:
        primary_span_id = candidate.evidence_span_ids[0]
        span = adapter.spans_by_id[primary_span_id]
        for source in adapter.sources_by_relpath.values():
            if source.id == span.source_id:
                return source
        raise ValueError(f"no source for span {primary_span_id}")

    def passes_trust_tier(candidate: CandidateClaim) -> tuple[bool, str]:
        source = source_for(candidate)
        tier = str(source.metadata["trust_tier"])
        if tier == "untrusted_external":
            return False, (
                f"source trust tier is {tier!r}: sender "
                f"{source.author_or_sender!r} does not match the "
                f"established counterparty domain (authentic Shubh "
                f"mail comes only from shubhassociates.in; payout "
                f"changes require CFO+MD written joint signature per "
                f"email-009)")
        return True, tier

    def passes_authorization(candidate: CandidateClaim) -> tuple[bool, str]:
        source = source_for(candidate)
        if bool(source.metadata.get("has_populated_approval_block")):
            return True, "populated approval block on the source document"
        sender = (source.author_or_sender or "").lower()
        if sender in AUTHORIZED_OFFICERS:
            return True, f"asserted in writing by authorized officer {sender}"
        return False, (
            "no authorizing chain: approval fields read [pending], no "
            "ALCO or Board reference; the cited 21 April 2026 meeting "
            "was cancelled by the CEO on 20 April (email-018), who also "
            "directed the draft not to circulate")

    # --- promotions (active facts) ---------------------------------
    promote_keys = ["v4_lap_b_le50", "v4_lap_a_le50", "v4_lap_a_50_65",
                    "v4_hbs_concession", "v4_no_stacking",
                    "v4_authorization", "dsa_payout"]
    for key in promote_keys:
        candidate, spans = candidates[key]
        ok_tier, tier_note = passes_trust_tier(candidate)
        ok_auth, auth_note = passes_authorization(candidate)
        assert ok_tier and ok_auth, f"legit candidate {key} failed gates"
        fixture = EXTRACTION_FIXTURES[key]
        valid_from = str(fixture["valid_from"])
        span_ids = [s.id for s in spans]
        entry_id = _fact_id_for(key, span_ids, valid_from)
        rationale = (f"promote {key}: source tier {tier_note!r}; "
                     f"authorization: {auth_note}")
        decision = _decision("promote", [candidate.id], [entry_id],
                             span_ids, rationale,
                             checks=dict(_CHECKS_PASS),
                             metadata={"fixture_key": key})
        entry = _fact_entry(key, candidate, spans, entry_id=entry_id,
                            decision_id=decision.id, status="active",
                            valid_from=valid_from, valid_until=None,
                            supersedes=[], superseded_by=[])
        decisions.append(decision)
        ledger.append(entry)

    # --- the v3 -> v4 supersession, one governed event --------------
    old_candidate, old_spans = candidates["v3_lap_b_50_65"]
    new_candidate, new_spans = candidates["v4_lap_b_50_65"]
    old_span_ids = [s.id for s in old_spans]
    new_span_ids = [s.id for s in new_spans]
    old_id = _fact_id_for("v3_lap_b_50_65", old_span_ids, V3_EFFECTIVE)
    new_id = _fact_id_for("v4_lap_b_50_65", new_span_ids, V4_EFFECTIVE)
    supersede_rationale = (
        "supersede v3 LAP B/50-65 (13.00%) with v4 (12.75%): governed "
        "replacement per ALCO/2026/05 and Board resolution BR/2026/22, "
        "effective 18 May 2026; the exact cell three people quoted "
        "stale in the corpus (emails 025/027)")
    supersede_decision = _decision(
        "supersede",
        [old_candidate.id, new_candidate.id],
        [old_id, new_id],
        sorted(set(old_span_ids + new_span_ids)),
        supersede_rationale,
        checks=dict(_CHECKS_PASS),
        metadata={"fixture_key": "v3_to_v4_supersession"})
    old_entry = _fact_entry(
        "v3_lap_b_50_65", old_candidate, old_spans, entry_id=old_id,
        decision_id=supersede_decision.id, status="superseded",
        valid_from=V3_EFFECTIVE, valid_until=V4_EFFECTIVE,
        supersedes=[], superseded_by=[new_id])
    new_entry = _fact_entry(
        "v4_lap_b_50_65", new_candidate, new_spans, entry_id=new_id,
        decision_id=supersede_decision.id, status="active",
        valid_from=V4_EFFECTIVE, valid_until=None,
        supersedes=[old_id], superseded_by=[])
    decisions.append(supersede_decision)
    ledger.extend([old_entry, new_entry])

    # --- the rejections (the dataset's two kill chains) -------------
    # POISON-1: untrusted source tier. The reject decision cites the
    # forged memo's own span plus the email-009 control it violates.
    p1_candidate, p1_spans = candidates["poison1_payout"]
    ok_tier, tier_msg = passes_trust_tier(p1_candidate)
    assert not ok_tier, "POISON-1 must fail the trust tier"
    control_span = adapter.span(EMAIL_009, "joint signature")
    p1_span_ids = [s.id for s in p1_spans] + [control_span.id]
    p1_rationale = f"{REJECT_UNTRUSTED_SOURCE}: {tier_msg}"
    p1_checks = dict(_CHECKS_PASS)
    p1_checks["provenance"] = "fail"
    p1_decision = _decision(
        "reject", [p1_candidate.id], [], p1_span_ids, p1_rationale,
        checks=p1_checks,
        metadata={"reason_code": REJECT_UNTRUSTED_SOURCE,
                  "auto_rejected": True,
                  "fixture_key": "poison1_payout"})
    decisions.append(p1_decision)
    quarantine.append({
        "fixture_key": "poison1_payout",
        "candidate_id": p1_candidate.id,
        "reason_code": REJECT_UNTRUSTED_SOURCE,
        "decision_id": p1_decision.id,
        "rationale": p1_rationale,
    })

    # POISON-2: no authorizing chain. The reject decision cites the
    # draft's pending approval block and the email-018 cancellation.
    p2_candidate, p2_spans = candidates["poison2_rate"]
    ok_tier2, _ = passes_trust_tier(p2_candidate)
    assert ok_tier2, "POISON-2 passes the source gate (it is internal)"
    ok_auth2, auth_msg2 = passes_authorization(p2_candidate)
    assert not ok_auth2, "POISON-2 must fail the authorization gate"
    pending_span = adapter.span(POISON_2, "| ALCO approval | [pending")
    cancellation_span = adapter.span(
        EMAIL_018, "we do not change rates outside the ALCO and Board route")
    p2_span_ids = [s.id for s in p2_spans] + [pending_span.id,
                                              cancellation_span.id]
    p2_rationale = f"{REJECT_NO_AUTHORIZING_CHAIN}: {auth_msg2}"
    p2_checks = dict(_CHECKS_PASS)
    p2_checks["contradiction_scan"] = "fail"
    p2_decision = _decision(
        "reject", [p2_candidate.id], [], p2_span_ids, p2_rationale,
        checks=p2_checks,
        metadata={"reason_code": REJECT_NO_AUTHORIZING_CHAIN,
                  "auto_rejected": True,
                  "fixture_key": "poison2_rate"})
    decisions.append(p2_decision)
    quarantine.append({
        "fixture_key": "poison2_rate",
        "candidate_id": p2_candidate.id,
        "reason_code": REJECT_NO_AUTHORIZING_CHAIN,
        "decision_id": p2_decision.id,
        "rationale": p2_rationale,
    })

    return {
        "decisions": decisions,
        "ledger": ledger,
        "quarantine": quarantine,
    }


# ---------------------------------------------------------------------------
# Stage 4: answer the walkthrough question from the ledger
# ---------------------------------------------------------------------------

DEMO_QUESTION = ("What rate applies to a Rs 2.1 Cr LAP, CIBIL 726, "
                 "risk grade B (LTV 58%, per the live comparable "
                 "AFL-LAP-26-0412)?")


def _rate_percent(value: str) -> float:
    match = re.match(r"^(\d+(?:\.\d+)?)%", value)
    if match is None:
        raise ValueError(f"not a rate: {value!r}")
    return float(match.group(1))


def answer_demo_question(
    adapter: DatasetAdapter,
    ledger: list[FactLedgerEntry],
) -> dict[str, Any]:
    """Answer from the trusted ledger only: active facts, every
    clause cited."""
    active = {e.subject: e for e in ledger if e.status == "active"}

    card = active["card_rate.lap.grade_b.ltv_50_65"]   # LTV 58% band
    concession = active["policy.hbs_concession"]
    no_stacking = active["policy.hbs_concession.no_stacking"]

    card_rate = _rate_percent(card.object)             # 12.75
    # CIBIL 726 >= 720, product LAP, grade B in A-C: eligible.
    # The demo loan carries no exception loading, so the
    # no-stacking rule does not bar the concession.
    final_rate = card_rate - 0.25                      # 12.50

    citations: list[dict[str, str]] = []
    for entry in (card, concession, no_stacking):
        for span_id in entry.evidence_span_ids:
            span = adapter.spans_by_id[span_id]
            source_title = next(
                (s.title for s in adapter.sources_by_relpath.values()
                 if s.id == span.source_id), "")
            citations.append({
                "fact_id": entry.id,
                "span_id": span_id,
                "source_title": source_title,
                "locator": f"{span.locator['kind']} {span.locator['value']}",
                "excerpt": span.text_excerpt or "",
            })

    explanation = (
        f"Card rate per Matrix v4, grade B, LTV 50-65%: {card.object} "
        f"(fact {card.id}). CIBIL 726 qualifies for the HBS concession "
        f"of 25 bps (fact {concession.id}); the loan carries no "
        f"exception loading, so the no-stacking rule permits it "
        f"(fact {no_stacking.id}). Contracted rate: {final_rate:.2f}%."
    )
    return {
        "question": DEMO_QUESTION,
        "rate": f"{final_rate:.2f}%",
        "card_rate": card.object,
        "concession_bps": 25,
        "citations": citations,
        "explanation": explanation,
        "grounded_on_fact_ids": [card.id, concession.id, no_stacking.id],
    }


# ---------------------------------------------------------------------------
# The runtime path (--runtime): same flow, through store + gate +
# grounding instead of static record assembly.
# ---------------------------------------------------------------------------


def answer_demo_question_runtime(
    store: Any,
    pack: Any,
) -> dict[str, Any]:
    """The walkthrough answer, computed over the grounded pack:
    the three clauses must be IN the pack (selected from the
    store's active ledger at as_of), every clause cited from
    store evidence."""
    selected: dict[str, dict[str, Any]] = {}
    for entry_id in pack.selected_entry_ids:
        entry = store.get_ledger_entry(entry_id)
        if entry is not None and entry.get("status") == "active":
            selected[str(entry.get("subject") or "")] = entry

    card = selected["card_rate.lap.grade_b.ltv_50_65"]   # LTV 58% band
    concession = selected["policy.hbs_concession"]
    no_stacking = selected["policy.hbs_concession.no_stacking"]

    card_rate = _rate_percent(str(card["object"]))       # 12.75
    final_rate = card_rate - 0.25                        # 12.50

    citations: list[dict[str, str]] = []
    for entry in (card, concession, no_stacking):
        for span_id in entry.get("evidence_span_ids") or []:
            span = store.get_span(str(span_id)) or {}
            source = store.get_source(str(span.get("source_id"))) or {}
            locator = span.get("locator") or {}
            citations.append({
                "fact_id": str(entry["id"]),
                "span_id": str(span_id),
                "source_title": str(source.get("title") or ""),
                "locator": f"{locator.get('kind')} {locator.get('value')}",
                "excerpt": str(span.get("text_excerpt") or ""),
            })

    explanation = (
        f"Card rate per Matrix v4, grade B, LTV 50-65%: {card['object']} "
        f"(fact {card['id']}). CIBIL 726 qualifies for the HBS concession "
        f"of 25 bps (fact {concession['id']}); the loan carries no "
        f"exception loading, so the no-stacking rule permits it "
        f"(fact {no_stacking['id']}). Contracted rate: {final_rate:.2f}%."
    )
    return {
        "question": DEMO_QUESTION,
        "rate": f"{final_rate:.2f}%",
        "card_rate": str(card["object"]),
        "concession_bps": 25,
        "citations": citations,
        "explanation": explanation,
        "grounded_on_fact_ids": [str(card["id"]), str(concession["id"]),
                                 str(no_stacking["id"])],
    }


def build_runtime(dataset_dir: Path, out_dir: Path) -> dict[str, Any]:
    """The --runtime pipeline: dataset -> gate -> store ->
    grounding -> audit pack, all anchored and verified. Returns
    the summary dict (also written to ``out/summary.json``).

    Idempotent writes: re-running against the same output
    directory replays every ingest/promote/pack/answer as a no-op
    (content-derived ids). The only row a re-run appends is one
    fresh ``verified`` anchor -- full revalidation is an
    attestation and attests every run, by ADR-6 design.
    """
    from agent_memory_contracts.runtime import grounding
    from agent_memory_contracts.runtime.anchors import (
        verify_chain,
        verify_coverage,
    )
    from agent_memory_contracts.runtime.gate import MemoryGate
    from agent_memory_contracts.runtime.store import MemoryStore

    if not dataset_dir.is_dir():
        raise FileNotFoundError(f"dataset directory not found: {dataset_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "runtime-store.sqlite3"
    store = MemoryStore(db_path)
    gate = MemoryGate(store, actor="arthashila-runtime-demo")

    # Stage 1-3 record assembly is shared with the static path;
    # the difference is that nothing below enters memory except
    # through the gate.
    load_trust_config(dataset_dir, _TRUST_CONFIG_OVERRIDE)
    adapter = DatasetAdapter(dataset_dir)
    for sub in ("emails", "policies", "poisoned"):
        for path in sorted((dataset_dir / sub).glob("*.md")):
            if path.name == "README.md":
                continue
            adapter.ingest_file(f"{sub}/{path.name}")
    candidates = build_candidates(adapter)
    session = run_reducer_session(adapter, candidates)
    decisions: list[MemoryReducerDecision] = session["decisions"]
    ledger: list[FactLedgerEntry] = session["ledger"]
    quarantine: list[dict[str, Any]] = session["quarantine"]

    # Ingestion (idempotent; deterministic order).
    for source in sorted(adapter.sources_by_relpath.values(),
                         key=lambda s: s.id):
        gate.submit_source(asdict(source), created_at=T_EXTRACTED)
    for span_id in sorted(adapter.spans_by_id):
        gate.submit_span(asdict(adapter.spans_by_id[span_id]),
                         created_at=T_EXTRACTED)
    for key in sorted(candidates):
        gate.submit_candidate(asdict(candidates[key][0]),
                              created_at=T_EXTRACTED)

    # Promotion: one transactional promote() per reducer decision
    # -- the only way anything reaches the trusted plane.
    entries_by_decision: dict[str, list[FactLedgerEntry]] = {}
    for entry in ledger:
        entries_by_decision.setdefault(entry.reducer_decision_id,
                                       []).append(entry)
    for decision in decisions:
        batch = [asdict(e) for e in
                 entries_by_decision.get(decision.id, [])]
        supersessions: list[tuple[str, str]] = []
        if decision.decision_type == "supersede":
            for entry in entries_by_decision.get(decision.id, []):
                for old_id in entry.supersedes:
                    supersessions.append((old_id, entry.id))
        gate.promote(asdict(decision), batch,
                     supersessions=supersessions, created_at=T_DECIDED)

    # The global net + tamper evidence.
    gate.full_revalidation(created_at=T_AS_OF)
    chain = verify_chain(store)
    coverage = verify_coverage(store)
    assert chain.ok, f"anchor chain diverged: {chain.divergence}"
    assert coverage.ok, (f"unanchored rows: {coverage.unanchored} "
                         f"{coverage.derived_index_drift}")

    # Stage 4: the walkthrough, grounded in a receipted pack built
    # from the store at as_of.
    pack = grounding.build_context_pack(
        store, task=DEMO_QUESTION, as_of=T_AS_OF, created_at=T_AS_OF)
    answer = answer_demo_question_runtime(store, pack)
    assert answer["rate"] == "12.50%", (
        f"walkthrough answer must be 12.50%, got {answer['rate']}")
    assert len(answer["citations"]) >= 2, "answer must cite evidence"

    # Poison probe: the forged payout never surfaces; the trusted
    # payout fact (with the CFO's controls) does.
    probe = grounding.answer(
        store,
        question="What is the DSA payout for Shubh Associates on LAP?",
        as_of=T_AS_OF, created_at=T_AS_OF)
    assert probe.status == "answered"
    probe_text = probe.answer_text or ""
    # The forged memo's content must never surface (boundary-aware:
    # legitimate card rates "11.75%" / "12.25%" contain the forged
    # "1.75%" / "2.25%" as substrings).
    assert re.search(r"(?<![\d.])1\.75%", probe_text) is None
    assert re.search(r"(?<![\d.])2\.25%", probe_text) is None
    assert "clawback withdrawn" not in probe_text
    assert "on disbursal" not in probe_text
    assert "1.00% of disbursed amount on LAP" in probe_text
    assert grounding.verify_grounding(
        probe_text, len(probe.citations)) == ()

    # Stage 5: the audit pack, emitted from STORE STATE (ledger
    # entries come back materialized; chains must be complete).
    records_by_plane = store.all_records()
    records: list[dict[str, Any]] = []
    for plane in ("sources", "spans", "candidates", "decisions",
                  "entries"):
        records.extend(records_by_plane[plane])
    audit = compute_audit_pack(
        records, as_of=T_AS_OF,
        title=("Arthashila Finance — every fact the AI relies on, "
               "who authorized it, and the evidence behind it."))
    audit_path = out_dir / "audit-pack.md"
    audit_path.write_text(audit_pack_to_markdown(audit), encoding="utf-8")

    entries_export = store.list_ledger_entries()
    summary = {
        "dataset_dir": str(dataset_dir),
        "mode": "runtime",
        "counts": {
            "sources": store.counts()["sources"],
            "evidence_spans": store.counts()["spans"],
            "candidates": store.counts()["candidates"],
            "reducer_decisions": store.counts()["reducer_decisions"],
            "ledger_entries": store.counts()["ledger_entries"],
            "active_ledger_entries": sum(
                1 for e in entries_export if e["status"] == "active"),
            "superseded_ledger_entries": sum(
                1 for e in entries_export if e["status"] == "superseded"),
        },
        "quarantine": quarantine,
        "answer": answer,
        "audit_pack": {
            "path": str(audit_path),
            "id": audit.id,
            "bundle_fingerprint": audit.bundle_fingerprint,
            "complete_chain_count": audit.complete_chain_count,
            "incomplete_chain_count": audit.incomplete_chain_count,
            "rejected_count": audit.rejected_count,
            "supersession_count": audit.supersession_count,
        },
        "trusted_ledger_fingerprint": bundle_fingerprint(entries_export),
        "runtime": {
            "db_path": str(db_path),
            "anchors": store.counts()["audit_anchors"],
            "chain_ok": chain.ok,
            "anchors_checked": chain.anchors_checked,
            "coverage_ok": coverage.ok,
            "supersession_edges": len(store.supersession_edges()),
            "pack_id": pack.pack_id,
            "pack_fingerprint": pack.fingerprint,
            "pack_selected": len(pack.selected_entry_ids),
            "payout_probe": {
                "status": probe.status,
                "answer_text": probe.answer_text,
                "citation_count": len(probe.citations),
            },
        },
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    store.close()
    return summary


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def build(dataset_dir: Path, out_dir: Path) -> dict[str, Any]:
    """Run the full adapter pipeline. Returns the summary dict
    (also written to ``out/summary.json``)."""
    if not dataset_dir.is_dir():
        raise FileNotFoundError(f"dataset directory not found: {dataset_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    load_trust_config(dataset_dir, _TRUST_CONFIG_OVERRIDE)
    adapter = DatasetAdapter(dataset_dir)

    # Ingest the whole corpus (emails + policies + poisons) so the
    # audit pack and counts describe the real dataset, not just the
    # files the fixtures touch.
    for sub in ("emails", "policies", "poisoned"):
        for path in sorted((dataset_dir / sub).glob("*.md")):
            if path.name == "README.md":
                continue
            adapter.ingest_file(f"{sub}/{path.name}")

    candidates = build_candidates(adapter)
    session = run_reducer_session(adapter, candidates)
    decisions: list[MemoryReducerDecision] = session["decisions"]
    ledger: list[FactLedgerEntry] = session["ledger"]
    quarantine: list[dict[str, Any]] = session["quarantine"]

    # The library is the referee: the full graph must validate.
    sources = list(adapter.sources_by_relpath.values())
    spans = list(adapter.spans_by_id.values())
    all_candidates = [c for c, _ in candidates.values()]
    validate_ledger_bundle(
        source_records=[asdict(s) for s in sources],
        episode_records=[],
        evidence_spans=[asdict(s) for s in spans],
        candidate_records=[asdict(c) for c in all_candidates],
        reducer_decisions=[asdict(d) for d in decisions],
        ledger_entries=[asdict(e) for e in ledger],
    )

    answer = answer_demo_question(adapter, ledger)
    assert answer["rate"] == "12.50%", (
        f"walkthrough answer must be 12.50%, got {answer['rate']}")
    assert len(answer["citations"]) >= 2, "answer must cite evidence"

    # The Audit Pack: the inspection-ready export.
    records: list[dict[str, Any]] = []
    for group in (sources, spans, all_candidates, decisions, ledger):
        records.extend(asdict(r) for r in group)
    pack = compute_audit_pack(
        records, as_of=T_AS_OF,
        title=("Arthashila Finance — every fact the AI relies on, "
               "who authorized it, and the evidence behind it."))
    audit_path = out_dir / "audit-pack.md"
    audit_path.write_text(audit_pack_to_markdown(pack), encoding="utf-8")

    summary = {
        "dataset_dir": str(dataset_dir),
        "counts": {
            "sources": len(sources),
            "evidence_spans": len(spans),
            "candidates": len(all_candidates),
            "reducer_decisions": len(decisions),
            "ledger_entries": len(ledger),
            "active_ledger_entries": sum(
                1 for e in ledger if e.status == "active"),
            "superseded_ledger_entries": sum(
                1 for e in ledger if e.status == "superseded"),
        },
        "quarantine": quarantine,
        "answer": answer,
        "audit_pack": {
            "path": str(audit_path),
            "id": pack.id,
            "bundle_fingerprint": pack.bundle_fingerprint,
            "complete_chain_count": pack.complete_chain_count,
            "incomplete_chain_count": pack.incomplete_chain_count,
            "rejected_count": pack.rejected_count,
            "supersession_count": pack.supersession_count,
        },
        "trusted_ledger_fingerprint": bundle_fingerprint(
            [asdict(e) for e in ledger]),
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Arthashila NBFC dataset -> contracts adapter demo.")
    parser.add_argument(
        "--dataset", default=str(DEFAULT_DATASET),
        help=f"Path to the 05-demo-dataset directory "
             f"(default: {DEFAULT_DATASET}).")
    parser.add_argument(
        "--out", default=str(DEFAULT_OUT_DIR),
        help="Output directory for audit-pack.md + summary.json "
             "(default: examples/arthashila_demo/out/).")
    parser.add_argument(
        "--runtime", action="store_true",
        help="Run the same flow through the sqlite reference runtime "
             "(store + gate + grounding + anchors) instead of static "
             "record assembly.")
    parser.add_argument(
        "--trust-config", default=None,
        help="JSON with internal_domain / trusted_domains / "
             "authorized_officers (default: <dataset>/trust.json if "
             "present, else neutral placeholders).")
    args = parser.parse_args(argv)
    global _TRUST_CONFIG_OVERRIDE
    _TRUST_CONFIG_OVERRIDE = Path(args.trust_config) if args.trust_config else None

    if args.runtime:
        summary = build_runtime(Path(args.dataset), Path(args.out))
    else:
        summary = build(Path(args.dataset), Path(args.out))

    counts = summary["counts"]
    if args.runtime:
        print("=== Arthashila demo: NBFC corpus through the reference "
              "runtime ===")
    else:
        print("=== Arthashila demo: NBFC corpus through the contracts ===")
    print()
    print(f"  ingested:   {counts['sources']} sources, "
          f"{counts['evidence_spans']} evidence spans")
    print(f"  candidates: {counts['candidates']} "
          f"(deterministic extraction fixtures, no LLM)")
    print(f"  promoted:   {counts['ledger_entries']} ledger entries "
          f"({counts['active_ledger_entries']} active, "
          f"{counts['superseded_ledger_entries']} superseded)")
    print(f"  rejected:   {len(summary['quarantine'])} poisons quarantined")
    for q in summary["quarantine"]:
        print(f"    - {q['fixture_key']}: {q['reason_code']} "
              f"-> {q['decision_id']}")
    print()
    print(f"  Q: {summary['answer']['question']}")
    print(f"  A: {summary['answer']['rate']} "
          f"({len(summary['answer']['citations'])} citations)")
    print(f"     {summary['answer']['explanation']}")
    print()
    print(f"  audit pack: {summary['audit_pack']['path']} "
          f"(pack {summary['audit_pack']['id']})")
    print(f"  chains: {summary['audit_pack']['complete_chain_count']} "
          f"complete, {summary['audit_pack']['incomplete_chain_count']} "
          f"incomplete; {summary['audit_pack']['rejected_count']} "
          f"rejections; {summary['audit_pack']['supersession_count']} "
          f"supersession event(s)")
    if args.runtime:
        runtime = summary["runtime"]
        print()
        print(f"  store:      {runtime['db_path']}")
        print(f"  anchors:    {runtime['anchors']} write/verified anchors; "
              f"chain verified: {runtime['chain_ok']}; "
              f"coverage: {runtime['coverage_ok']}")
        print(f"  pack:       {runtime['pack_id']} "
              f"({runtime['pack_selected']} facts selected, fingerprint "
              f"{runtime['pack_fingerprint'][:16]}...)")
        probe = runtime["payout_probe"]
        print(f"  probe:      DSA payout question -> {probe['status']} "
              f"({probe['citation_count']} citations; forged 1.75% "
              f"never surfaces)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
