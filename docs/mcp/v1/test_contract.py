"""Offline contract checks; these do not certify a running MCP backend."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

import build_contract

ROOT = Path(__file__).resolve().parent
BUNDLE = json.loads((ROOT / "contract.schema.json").read_text(encoding="utf-8"))
EXAMPLES = json.loads((ROOT / "examples.json").read_text(encoding="utf-8"))


def validator(tool, direction):
    return Draft202012Validator({**BUNDLE, "$ref": "#/$defs/" + tool + direction},
                                format_checker=FormatChecker())


class ContractTest(unittest.TestCase):
    def test_schema_and_all_six_examples(self):
        Draft202012Validator.check_schema(BUNDLE)
        self.assertEqual(len(EXAMPLES), 6)
        self.assertEqual({e["tool"] for e in EXAMPLES}, set(build_contract.inputs))
        for example in EXAMPLES:
            for direction in ("Input", "Output"):
                with self.subTest(tool=example["tool"], direction=direction):
                    validator(example["tool"], direction).validate(example[direction.lower()])

    def test_generated_files_are_reproducible(self):
        previous = build_contract.ROOT
        try:
            with tempfile.TemporaryDirectory() as directory:
                build_contract.ROOT = Path(directory)
                for name, value in (("contract.schema.json", build_contract.bundle),
                                    ("examples.json", build_contract.examples)):
                    build_contract.write(name, value)
                    self.assertEqual((ROOT / name).read_bytes(), (Path(directory) / name).read_bytes())
        finally:
            build_contract.ROOT = previous

    def test_rejects_bad_arguments(self):
        cases = [
            ("get_incident", {}), ("get_incident", {"incidentId": "INC-742001"}),
            ("get_incident", {"incidentId": "INC000001", "raw": True}),
            ("list_incidents", {"limit": 0}), ("list_incidents", {"limit": 101}),
            ("list_incidents", {"filters": {"priorityIds": [5]}}),
            ("list_incidents", {"filters": {"unassigned": "true"}}),
            ("list_incidents", {"filters": {"states": []}}),
            ("list_incident_changes", {}), ("list_incident_changes", {"cursor": ""}),
            ("find_assignees", {"groupId": ""}),
        ]
        for tool, data in cases:
            with self.subTest(tool=tool, data=data):
                self.assertFalse(validator(tool, "Input").is_valid(data))

    def test_empty_search_is_not_missing_card(self):
        empty = copy.deepcopy(EXAMPLES[0]["output"])
        empty["data"]["items"] = []
        validator("list_incidents", "Output").validate(empty)
        self.assertFalse(validator("get_incident", "Output").is_valid(empty))
        missing = {"contractVersion": "1.0.0", "ok": False,
                   "error": {"code": "NOT_FOUND", "message": "Карточка не найдена",
                             "retryable": False, "recovery": "none"}}
        validator("get_incident", "Output").validate(missing)
        missing["data"] = {}
        self.assertFalse(validator("get_incident", "Output").is_valid(missing))

    def test_stale_partial_and_cursor_expiry(self):
        partial = copy.deepcopy(EXAMPLES[4]["output"])
        partial["meta"]["completeness"] = "partial"
        partial["meta"]["sources"][0]["status"] = "stale"
        partial["meta"]["warnings"] = ["Справочник устарел"]
        partial["data"].update(items=[], recommendedGroupId=None, reason="directory_stale")
        validator("find_resolver_groups", "Output").validate(partial)
        expired = {"contractVersion": "1.0.0", "ok": False,
                   "error": {"code": "CURSOR_EXPIRED", "message": "Получите новый снимок",
                             "retryable": False, "recovery": "resync_snapshot"}}
        validator("list_incident_changes", "Output").validate(expired)

    def test_responsibility_requires_directory_evidence(self):
        result = copy.deepcopy(EXAMPLES[4]["output"])
        result["data"]["items"][0]["evidence"][0]["kind"] = "history"
        self.assertFalse(validator("find_resolver_groups", "Output").is_valid(result))
        result = copy.deepcopy(EXAMPLES[4]["output"])
        result["data"]["reason"] = "ambiguous_owner"
        self.assertFalse(validator("find_resolver_groups", "Output").is_valid(result))
        result = copy.deepcopy(EXAMPLES[5]["output"])
        result["data"]["items"][0]["onDuty"] = "yes"
        self.assertFalse(validator("find_assignees", "Output").is_valid(result))

    def test_event_cursor_and_sequence_boundaries(self):
        result = copy.deepcopy(EXAMPLES[2]["output"])
        for bad in (101, "01", "0", "-1"):
            result["data"]["events"][0]["eventSeq"] = bad
            self.assertFalse(validator("list_incident_changes", "Output").is_valid(result))
        result["data"]["events"] = []
        result["data"]["hasMore"] = False
        validator("list_incident_changes", "Output").validate(result)
        result["data"]["nextCursor"] = None
        self.assertFalse(validator("list_incident_changes", "Output").is_valid(result))

    def test_timestamps_and_minimal_card(self):
        for timestamp in ("2026-13-01T00:00:00Z", "2026-11-22T12:00:00+03:00"):
            result = copy.deepcopy(EXAMPLES[1]["output"])
            result["meta"]["generatedAt"] = timestamp
            self.assertFalse(validator("get_incident", "Output").is_valid(result))
        result = copy.deepcopy(EXAMPLES[1]["output"])
        del result["data"]["deadline"]
        self.assertFalse(validator("get_incident", "Output").is_valid(result))


if __name__ == "__main__":
    unittest.main()
