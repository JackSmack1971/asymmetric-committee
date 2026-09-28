import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).parents[1] / "scripts" / "validate_audit.py"
spec = importlib.util.spec_from_file_location("validate_audit", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
validate = module.validate


def base_doc():
    return {
        "schema_version": "1.0",
        "scope": {"mode": "change", "targets": ["src/example.py"], "baseline": "main"},
        "findings": [],
        "verification": [{"command": "pytest -q", "status": "PASSED", "exit_code": 0, "notes": None}],
        "framework_claims": [],
        "blockers": [],
        "verdict": "PASS",
    }


class ValidateAuditTests(unittest.TestCase):
    def test_valid_pass(self):
        self.assertEqual(validate(base_doc()), [])

    def test_pass_rejects_blocking_finding(self):
        doc = base_doc()
        doc["findings"] = [{
            "id": "AUD-001", "severity": "HIGH", "confidence": "HIGH",
            "category": "correctness", "location": "src/example.py:10",
            "claim": "input can bypass required validation", "evidence": ["source: line 10"],
            "impact": "invalid state can be persisted", "remediation": "enforce validation at the write boundary",
        }]
        self.assertTrue(any("PASS is inconsistent" in e for e in validate(doc)))

    def test_placeholder_rejected(self):
        doc = base_doc()
        doc["scope"]["targets"] = ["TBD"]
        self.assertTrue(any("scope.targets" in e for e in validate(doc)))

    def test_not_run_requires_null_exit_code(self):
        doc = base_doc()
        doc["verification"] = [{"command": "integration suite", "status": "NOT_RUN", "exit_code": 0, "notes": "tool unavailable"}]
        self.assertTrue(any("exit_code must be null" in e for e in validate(doc)))

    def test_red_requires_adverse_evidence(self):
        doc = base_doc()
        doc["verdict"] = "RED"
        self.assertTrue(any("RED requires" in e for e in validate(doc)))

    def test_inconclusive_accepts_blocker(self):
        doc = base_doc()
        doc["blockers"] = ["required staging credentials are unavailable"]
        doc["verification"] = [{"command": "staging integration test", "status": "NOT_RUN", "exit_code": None, "notes": "credentials unavailable"}]
        doc["verdict"] = "INCONCLUSIVE"
        self.assertEqual(validate(doc), [])

    def test_valid_v11_review_provenance(self):
        doc = base_doc()
        doc["schema_version"] = "1.1"
        doc["findings"] = [{
            "id": "AUD-001", "severity": "MEDIUM", "confidence": "HIGH",
            "category": "correctness", "location": "src/example.py:10",
            "claim": "input can bypass required validation", "evidence": ["source: line 10"],
            "impact": "invalid state can be persisted", "remediation": "enforce validation at the write boundary",
        }]
        doc["finding_reviews"] = [{
            "finding_id": "AUD-001", "reviewer_role": "audit-reviewer",
            "status": "CONFIRMED", "rationale": "source directly demonstrates bypass",
            "evidence": ["source: line 10"], "unresolved_assumptions": [],
        }]
        self.assertEqual(validate(doc), [])

    def test_review_provenance_requires_v11(self):
        doc = base_doc()
        doc["finding_reviews"] = []
        self.assertTrue(any("requires schema_version '1.1'" in e for e in validate(doc)))

    def test_review_must_reference_existing_finding(self):
        doc = base_doc()
        doc["schema_version"] = "1.1"
        doc["finding_reviews"] = [{
            "finding_id": "AUD-404", "reviewer_role": "audit-reviewer",
            "status": "REJECT", "rationale": "finding is unsupported",
            "evidence": ["source: no matching path"], "unresolved_assumptions": [],
        }]
        self.assertTrue(any("must reference an existing finding" in e for e in validate(doc)))


if __name__ == "__main__":
    unittest.main()
