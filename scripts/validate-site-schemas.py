"""Validate emitted synthetic file-analysis schemas and their observed samples."""
import json
from pathlib import Path
from jsonschema import Draft202012Validator

root = Path(__file__).resolve().parents[1]
fixtures = json.loads((root / "test-results/site-analysis-fixtures.json").read_text(encoding="utf-8"))
count = 0
for fixture in fixtures:
    analysis = fixture["analysis"]
    assert analysis["evidence_scope"] == "file_content"
    assert analysis["classification"]["topics"] == []
    assert analysis["classification"]["tasks"] == []
    assert analysis["review"]["status"] == "pending"
    for candidate in analysis["schema"]["candidates"]:
        schema = candidate["json_schema"]
        Draft202012Validator.check_schema(schema)
        if analysis["observed_sample"] is not None:
            Draft202012Validator(schema).validate(analysis["observed_sample"])
        for rule in candidate["selection_criteria"]:
            assert rule["result"] in ("pass", "fail", "unknown")
            assert all(key in rule for key in ("rule_id", "field", "operator", "expected", "reason", "evidence"))
        count += 1
print(f"PASS {count} generated Draft 2020-12 schemas and observed samples")
