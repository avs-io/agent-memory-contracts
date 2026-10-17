"""Smoke tests for examples/arthashila_demo/build.py.

The adapter runs against the real NBFC demo dataset (the
launch-week ``05-demo-dataset`` corpus). The tests assert the
demo's three load-bearing claims:

1. Both poisoned documents are quarantined, each for the kill
   chain the dataset documents (POISON-1: untrusted source tier;
   POISON-2: no authorizing chain).
2. The walkthrough question (Rs 2.1 Cr LAP, CIBIL 726, grade B)
   answers 12.50% from the trusted ledger, with at least two
   evidence citations.
3. The Audit Pack file is generated and carries the chains, the
   rejections, and the supersession event.

If the dataset directory is not present (e.g. CI), the tests skip
with a clear reason instead of failing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = Path(
    "/sessions/inspiring-magical-a8efd8/mnt/Personal-Offboarding/"
    "launch-week/05-demo-dataset")

_DATASET_AVAILABLE = (DATASET_DIR / "walkthrough.md").exists()
_SKIP_REASON = (f"Arthashila demo dataset not available at {DATASET_DIR}; "
                f"pass --dataset to run the demo elsewhere")


@unittest.skipUnless(_DATASET_AVAILABLE, _SKIP_REASON)
class ArthashilaDemoTests(unittest.TestCase):
    """Run the adapter once against the real dataset; assert on the
    generated summary and audit pack."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp())
        cls.proc = subprocess.run(
            ["python3", "examples/arthashila_demo/build.py",
             "--dataset", str(DATASET_DIR),
             "--out", str(cls.tmpdir)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": "src"},
            cwd=REPO_ROOT,
        )
        summary_path = cls.tmpdir / "summary.json"
        cls.summary = (json.loads(summary_path.read_text(encoding="utf-8"))
                       if summary_path.exists() else None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_runs_cleanly(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertIsNotNone(self.summary)

    def test_poisons_quarantined_with_documented_kill_chains(self):
        quarantine = {q["fixture_key"]: q
                      for q in self.summary["quarantine"]}
        self.assertEqual(len(quarantine), 2)
        p1 = quarantine["poison1_payout"]
        self.assertEqual(p1["reason_code"], "untrusted_source")
        # POISON-1's sender is a freemail look-alike of the counterparty.
        self.assertIn("untrusted_external", p1["rationale"])
        self.assertRegex(p1["rationale"], r"@gmail\.com'")
        self.assertTrue(p1["decision_id"].startswith("redmem_"))
        p2 = quarantine["poison2_rate"]
        self.assertEqual(p2["reason_code"], "no_authorizing_chain")
        self.assertIn("[pending]", p2["rationale"])
        self.assertIn("cancelled", p2["rationale"])
        self.assertTrue(p2["decision_id"].startswith("redmem_"))

    def test_poisoned_claims_never_reach_the_ledger(self):
        # No active fact carries the forged payout or the draft
        # v4.1 rate. The answer comes from v4, full stop.
        answer = self.summary["answer"]
        self.assertNotIn("1.75%", answer["explanation"])
        self.assertNotIn("v4.1", answer["explanation"])

    def test_demo_question_yields_12_50_with_citations(self):
        answer = self.summary["answer"]
        self.assertEqual(answer["rate"], "12.50%")
        self.assertEqual(answer["card_rate"], "12.75%")
        self.assertEqual(answer["concession_bps"], 25)
        self.assertGreaterEqual(len(answer["citations"]), 2)
        for citation in answer["citations"]:
            self.assertTrue(citation["span_id"].startswith("span_"))
            self.assertTrue(citation["fact_id"].startswith("fact_"))
            self.assertTrue(citation["excerpt"])
        # The citations ground on v4, the concession, and the
        # no-stacking rule -- the walkthrough's three clauses.
        excerpts = " ".join(c["excerpt"] for c in answer["citations"])
        self.assertIn("12.75%", excerpts)
        self.assertIn("25 bps", excerpts)
        self.assertIn("no exception loading", excerpts)

    def test_corpus_fully_ingested(self):
        counts = self.summary["counts"]
        # 30 emails + 5 policies + 2 poisons.
        self.assertEqual(counts["sources"], 37)
        self.assertEqual(counts["candidates"], 11)
        self.assertEqual(counts["ledger_entries"], 9)
        self.assertEqual(counts["active_ledger_entries"], 8)
        self.assertEqual(counts["superseded_ledger_entries"], 1)

    def test_audit_pack_generated(self):
        pack_info = self.summary["audit_pack"]
        pack_path = Path(pack_info["path"])
        self.assertTrue(pack_path.exists())
        self.assertEqual(pack_path.name, "audit-pack.md")
        text = pack_path.read_text(encoding="utf-8")
        self.assertIn("who authorized it, and the evidence behind it",
                      text)
        self.assertIn("## Trusted ledger — authorization chains", text)
        self.assertIn("## Rejected in this period", text)
        self.assertIn("## Supersession changelog", text)
        self.assertIn("untrusted_source", text)
        self.assertIn("no_authorizing_chain", text)
        self.assertIn("BR/2026/22", text)
        self.assertIn(pack_info["bundle_fingerprint"], text)
        self.assertIn(pack_info["id"], text)

    def test_audit_pack_chains_complete(self):
        pack_info = self.summary["audit_pack"]
        self.assertEqual(pack_info["complete_chain_count"], 9)
        self.assertEqual(pack_info["incomplete_chain_count"], 0)
        self.assertEqual(pack_info["rejected_count"], 2)
        self.assertEqual(pack_info["supersession_count"], 1)

    def test_deterministic_across_runs(self):
        tmp2 = Path(tempfile.mkdtemp())
        try:
            proc2 = subprocess.run(
                ["python3", "examples/arthashila_demo/build.py",
                 "--dataset", str(DATASET_DIR),
                 "--out", str(tmp2)],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": "src"},
                cwd=REPO_ROOT,
            )
            self.assertEqual(proc2.returncode, 0, proc2.stderr)
            summary2 = json.loads(
                (tmp2 / "summary.json").read_text(encoding="utf-8"))
            # Identical apart from the self-referential output path.
            s1 = dict(self.summary)
            s2 = dict(summary2)
            s1["audit_pack"] = {k: v for k, v in s1["audit_pack"].items()
                                if k != "path"}
            s2["audit_pack"] = {k: v for k, v in s2["audit_pack"].items()
                                if k != "path"}
            self.assertEqual(s1, s2)
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
