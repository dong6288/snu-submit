"""Synthetic tests for archive_probes.inspect_archive (archive-profile/1) and the TXT pages diagnostic.

All archives, names and markers are invented. Run from the repository root:
    python -m unittest tests.test_archive_probes -v
"""

import copy
import hashlib
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "snu-submit" / "skills" / "snu-submit" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from submit_checks import archive_probes, parsers  # noqa: E402
from submit_checks.core import ValidationError, _candidate_status  # noqa: E402

REPORT_OK = "Title line\nBEGIN_SECTION SAMPLE\nObserved values were recorded.\nEND_SECTION\nTrailer\n"


def build_zip(entries):
    """entries: list of (name, bytes). Directory names end with '/'."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buffer.getvalue()


def build_zip_attrs(entries):
    """entries: list of (name, bytes, external_attr). Attributes are written exactly as given."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data, attr in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = attr
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buffer.getvalue()


REGULAR = 0o100644 << 16
UNIX_DIR = 0o040755 << 16


def normal_entries():
    return [("report.txt", REPORT_OK.encode("utf-8")), ("data/values.csv", b"a,b\n1,2\n"),
            ("results/out.txt", b"run 1 ok\n")]


def profile(existing=None):
    return {
        "profile_version": "archive-profile/1",
        "required_entries": [
            {"path": "report.txt", "reason": "fictional rule R1", "evidence": "synthetic note 1"},
            {"path": "data/values.csv", "reason": "fictional rule R2", "evidence": "synthetic note 2"},
            {"path": "results", "reason": "fictional rule R3", "evidence": "synthetic note 3"},
        ],
        "nonempty_entries": [
            {"path": "report.txt", "reason": "fictional rule R4", "evidence": "synthetic note 4", "encoding": "utf-8"},
            {"path": "data/values.csv", "reason": "fictional rule R5", "evidence": "synthetic note 5", "encoding": None},
        ],
        "text_sections": [
            {"path": "report.txt", "start_literal": "BEGIN_SECTION SAMPLE", "end_literal": "END_SECTION",
             "encoding": "utf-8", "reason": "fictional rule R6", "evidence": "synthetic note 6"},
        ],
        "existing_issues": existing or [],
    }


class ArchiveProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sample_submission.zip")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        with open(self.path, "wb") as handle:
            handle.write(data)
        return data

    def run_probe(self, entries=None, prof=None, raw=None):
        data = self.write(raw if raw is not None else build_zip(entries if entries is not None else normal_entries()))
        prof = prof or profile()
        before = copy.deepcopy(prof)
        result = archive_probes.inspect_archive(self.path, prof)
        self.assertEqual(prof, before, "profile must not be mutated")
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), data, "archive must not be mutated")
        return result

    def probe(self, result, kind, index=0):
        return next(p for p in result["probes"] if p["probe"] == kind and p["index"] == index)

    def test_normal_profile_pass_with_actual_hashes(self):
        result = self.run_probe()
        self.assertEqual(result["overall"], "profile_pass")
        self.assertTrue(all(p["status"] == "pass" for p in result["probes"]))
        with open(self.path, "rb") as handle:
            self.assertEqual(result["archive"]["sha256"], hashlib.sha256(handle.read()).hexdigest())
        report = next(e for e in result["entries"] if e["path"] == "report.txt")
        self.assertEqual(report["sha256"], hashlib.sha256(REPORT_OK.encode()).hexdigest())
        section = self.probe(result, "text_section")
        self.assertEqual((section["observed"]["start_line"], section["observed"]["end_line"]), (2, 4))
        self.assertTrue(result["archive"]["stable_after_inspection"])
        self.assertEqual(self.probe(result, "required_entry", 2)["observed"]["matched_as"], "implicit_directory")

    def test_missing_required_entry_has_no_fabricated_hash(self):
        entries = [e for e in normal_entries() if e[0] != "data/values.csv"]
        result = self.run_probe(entries)
        missing = self.probe(result, "required_entry", 1)
        self.assertEqual((missing["status"], missing["reason_code"]), ("fail", "missing_target"))
        self.assertIsNone(missing["observed"])
        self.assertEqual(missing["expected"]["path"], "data/values.csv")
        self.assertEqual(result["overall"], "needs_changes")

    def test_zero_size_entry_fails_nonempty_but_is_present(self):
        entries = [e if e[0] != "data/values.csv" else (e[0], b"") for e in normal_entries()]
        result = self.run_probe(entries)
        self.assertEqual(self.probe(result, "required_entry", 1)["status"], "pass")
        nonempty = self.probe(result, "nonempty_entry", 1)
        self.assertEqual((nonempty["status"], nonempty["reason_code"]), ("fail", "zero_bytes"))

    def test_whitespace_text_fails_but_binary_zero_bytes_are_not_judged_empty(self):
        entries = [("report.txt", b"  \n\t\r\n"), ("data/values.csv", b"\x00" * 16), ("results/out.txt", b"x")]
        result = self.run_probe(entries)
        self.assertEqual(self.probe(result, "nonempty_entry", 0)["reason_code"], "whitespace_only")
        binary = self.probe(result, "nonempty_entry", 1)
        self.assertEqual((binary["status"], binary["reason_code"]), ("pass", "binary_nonzero_size"))

    def test_removed_section_detected_while_entry_survives(self):
        entries = [("report.txt", b"Title line\nNo marked section here\n")] + normal_entries()[1:]
        result = self.run_probe(entries)
        self.assertEqual(self.probe(result, "required_entry", 0)["status"], "pass")
        section = self.probe(result, "text_section")
        self.assertEqual((section["status"], section["reason_code"]), ("fail", "missing_start"))
        self.assertEqual(section["expected"]["start_literal"], "BEGIN_SECTION SAMPLE")

    def test_missing_end_marker_fails(self):
        entries = [("report.txt", b"BEGIN_SECTION SAMPLE\nbody\n")] + normal_entries()[1:]
        self.assertEqual(self.probe(self.run_probe(entries), "text_section")["reason_code"], "missing_end")

    def test_duplicate_start_marker_is_unknown(self):
        text = b"BEGIN_SECTION SAMPLE\na\nBEGIN_SECTION SAMPLE\nb\nEND_SECTION\n"
        result = self.run_probe([("report.txt", text)] + normal_entries()[1:])
        section = self.probe(result, "text_section")
        self.assertEqual((section["status"], section["reason_code"]), ("unknown", "ambiguous_start"))
        self.assertEqual(result["overall"], "needs_confirmation")

    def test_empty_section_body_fails(self):
        result = self.run_probe([("report.txt", b"BEGIN_SECTION SAMPLE\n   \nEND_SECTION\n")] + normal_entries()[1:])
        self.assertEqual(self.probe(result, "text_section")["reason_code"], "empty_section")

    def test_literal_is_not_a_pattern(self):
        prof = profile()
        prof["text_sections"][0]["start_literal"] = "BEGIN.*"
        self.assertEqual(self.probe(self.run_probe(prof=prof), "text_section")["reason_code"], "missing_start")

    def test_invalid_utf8_and_nul_are_unknown(self):
        for content, code in ((b"BEGIN_SECTION SAMPLE\n\xff\nEND_SECTION\n", "decode_failed"),
                              (b"BEGIN_SECTION SAMPLE\nx\x00y\nEND_SECTION\n", "nul_character")):
            result = self.run_probe([("report.txt", content)] + normal_entries()[1:])
            section = self.probe(result, "text_section")
            self.assertEqual((section["status"], section["reason_code"]), ("unknown", code))

    def test_unsafe_path_prevents_absence_proof(self):
        entries = [("../escape.txt", b"x"), ("report.txt", REPORT_OK.encode())]
        result = self.run_probe(entries)
        missing = self.probe(result, "required_entry", 1)
        self.assertEqual((missing["status"], missing["reason_code"]), ("unknown", "unsafe_names"))
        self.assertTrue(any(i["code"] == "unsafe_names" for i in result["issues"]))
        self.assertNotEqual(result["overall"], "profile_pass")

    def test_duplicate_names_are_unsafe(self):
        entries = normal_entries() + [("REPORT.txt", b"other")]
        result = self.run_probe(entries)
        self.assertEqual(self.probe(result, "required_entry", 0)["status"], "unknown")

    def test_corrupt_archive_is_unknown(self):
        result = self.run_probe(raw=b"PK\x03\x04" + b"\x00" * 40)
        self.assertEqual(result["archive"]["status"], "structure_unreadable")
        self.assertTrue(all(p["status"] == "unknown" for p in result["probes"]))
        self.assertEqual(result["overall"], "needs_confirmation")

    def test_encrypted_flag_is_unknown(self):
        data = bytearray(build_zip([("report.txt", REPORT_OK.encode())]))
        local, central = data.find(b"PK\x03\x04"), data.find(b"PK\x01\x02")
        data[local + 6] |= 0x01
        data[central + 8] |= 0x01
        prof = profile()
        prof["required_entries"] = prof["required_entries"][:1]
        prof["nonempty_entries"] = prof["nonempty_entries"][:1]
        result = self.run_probe(prof=prof, raw=bytes(data))
        self.assertEqual(self.probe(result, "required_entry")["reason_code"], "encrypted")
        self.assertEqual(self.probe(result, "text_section")["status"], "unknown")

    def test_text_budget_and_entry_budget_are_unknown(self):
        with mock.patch.object(archive_probes, "TEXT_ENTRY_MAX_BYTES", 8):
            section = self.probe(self.run_probe(), "text_section")
        self.assertEqual((section["status"], section["reason_code"]), ("unknown", "budget_limit"))
        with mock.patch.dict(parsers.DEFAULT_LIMITS, {"max_archive_entries": 2}):
            result = self.run_probe()
        self.assertEqual(result["archive"]["status"], "structure_unreadable")
        self.assertEqual(result["overall"], "needs_confirmation")

    def attr_entries(self, report_attr, extra=()):
        return [("report.txt", REPORT_OK.encode(), report_attr), ("data/values.csv", b"a,b\n1,2\n", REGULAR),
                ("results/out.txt", b"run 1 ok\n", REGULAR)] + list(extra)

    def assert_blocked(self, result, path):
        self.assertNotEqual(result["overall"], "profile_pass")
        self.assertIn("metadata_mismatch", [i["code"] for i in result["issues"]])
        for probe in result["probes"]:
            if probe["path"] == path:
                self.assertEqual(probe["status"], "unknown", probe["probe"])

    def test_regular_file_attributes_control_passes(self):
        result = self.run_probe(raw=build_zip_attrs(self.attr_entries(REGULAR)))
        self.assertEqual(result["overall"], "profile_pass")

    def test_file_name_with_unix_directory_mode_is_blocked(self):
        result = self.run_probe(raw=build_zip_attrs(self.attr_entries(UNIX_DIR)))
        self.assert_blocked(result, "report.txt")

    def test_file_name_with_dos_directory_bit_is_blocked(self):
        result = self.run_probe(raw=build_zip_attrs(self.attr_entries(REGULAR | 0x10)))
        self.assert_blocked(result, "report.txt")

    def test_directory_name_with_regular_file_mode_is_blocked(self):
        entries = self.attr_entries(REGULAR, extra=[("results/", b"", REGULAR)])
        result = self.run_probe(raw=build_zip_attrs(entries))
        self.assert_blocked(result, "results")

    def test_hashes_are_stable_across_runs(self):
        first = self.run_probe()
        second = archive_probes.inspect_archive(self.path, profile())
        self.assertEqual(first["archive"]["sha256"], second["archive"]["sha256"])
        self.assertEqual([e["sha256"] for e in first["entries"]], [e["sha256"] for e in second["entries"]])

    def test_existing_issues_are_retained_and_not_hidden(self):
        unknown_issue = [{"status": "unknown", "reason": "fictional open question", "evidence": "synthetic"}]
        result = self.run_probe(prof=profile(unknown_issue))
        self.assertTrue(all(p["status"] == "pass" for p in result["probes"]))
        self.assertEqual(result["overall"], "needs_confirmation")
        self.assertEqual(result["existing_issues"][0]["reason"], "fictional open question")
        failing = [{"status": "fail", "reason": "fictional known defect", "evidence": "synthetic"}]
        self.assertEqual(self.run_probe(prof=profile(failing))["overall"], "needs_changes")

    def test_restoration_returns_to_profile_pass(self):
        broken = [e for e in normal_entries() if e[0] != "report.txt"]
        self.assertEqual(self.run_probe(broken)["overall"], "needs_changes")
        self.assertEqual(self.run_probe()["overall"], "profile_pass")

    def test_missing_archive_file_is_unknown_not_missing(self):
        result = archive_probes.inspect_archive(os.path.join(self.tmp.name, "absent.zip"), profile())
        self.assertEqual(result["archive"]["status"], "unreadable")
        self.assertTrue(all(p["status"] == "unknown" for p in result["probes"]))

    def empty_profile(self, existing=None):
        return {"profile_version": "archive-profile/1", "required_entries": [], "nonempty_entries": [],
                "text_sections": [], "existing_issues": existing or []}

    def test_empty_profile_is_rejected_for_missing_and_valid_archives(self):
        missing = os.path.join(self.tmp.name, "absent.zip")
        with self.assertRaises(ValidationError):
            archive_probes.inspect_archive(missing, self.empty_profile())
        self.write(build_zip(normal_entries()))
        with self.assertRaises(ValidationError):
            archive_probes.inspect_archive(self.path, self.empty_profile())

    def test_existing_issues_only_profile_is_rejected(self):
        self.write(build_zip(normal_entries()))
        issues = [{"status": "unknown", "reason": "fictional open question", "evidence": "synthetic"}]
        with self.assertRaises(ValidationError):
            archive_probes.inspect_archive(self.path, self.empty_profile(issues))

    def test_archive_failures_never_yield_profile_pass(self):
        missing = archive_probes.inspect_archive(os.path.join(self.tmp.name, "absent.zip"), profile())
        self.assertEqual(missing["overall"], "needs_confirmation")
        self.assertIn("archive_unreadable", [i["code"] for i in missing["issues"]])
        corrupt = self.run_probe(raw=b"not a zip archive at all")
        self.assertEqual(corrupt["overall"], "needs_confirmation")
        self.assertIn("archive_structure_unreadable", [i["code"] for i in corrupt["issues"]])
        with mock.patch.dict(parsers.DEFAULT_LIMITS, {"max_archive_bytes": 16}):
            over_budget = self.run_probe()
        self.assertEqual(over_budget["overall"], "needs_confirmation")
        self.assertIn("archive_structure_unreadable", [i["code"] for i in over_budget["issues"]])
        for result in (missing, corrupt, over_budget):
            self.assertTrue(result["probes"])
            self.assertTrue(all(p["status"] == "unknown" for p in result["probes"]))

    def test_invalid_profiles_are_rejected(self):
        self.write(build_zip(normal_entries()))
        bad = []
        p = profile(); p["profile_version"] = "archive-profile/2"; bad.append(p)
        p = profile(); p["extra"] = 1; bad.append(p)
        p = profile(); p["required_entries"][0]["path"] = "../outside.txt"; bad.append(p)
        p = profile(); p["required_entries"][0]["path"] = "C:/abs.txt"; bad.append(p)
        p = profile(); p["text_sections"][0]["start_literal"] = "BEGIN\nSECTION"; bad.append(p)
        p = profile(); p["text_sections"][0]["encoding"] = "cp949"; bad.append(p)
        p = profile(); p["text_sections"][0]["end_literal"] = "BEGIN_SECTION SAMPLE"; bad.append(p)
        p = profile(); p["existing_issues"] = [{"status": "pass", "reason": "r", "evidence": "e"}]; bad.append(p)
        p = profile(); p["required_entries"].append(dict(p["required_entries"][0])); bad.append(p)
        for item in bad:
            with self.assertRaises(ValidationError):
                archive_probes.inspect_archive(self.path, item)


class TextPagesDiagnosticTests(unittest.TestCase):
    def test_parsed_text_without_pages_is_unsupported_not_parser_failed(self):
        requirement = {"kind": "pages", "operator": "lte", "value": 5, "unit": "pages"}
        facts = {"pages": None, "detected": "text", "status": "complete", "codes": []}
        self.assertEqual(_candidate_status(requirement, facts), ("unknown", None, ["unsupported_check"]))

    def test_genuine_parser_failure_keeps_its_code(self):
        requirement = {"kind": "pages", "operator": "lte", "value": 5, "unit": "pages"}
        facts = {"pages": None, "detected": "pdf", "status": "failed", "codes": ["parser_failed"]}
        self.assertEqual(_candidate_status(requirement, facts), ("unknown", None, ["parser_failed"]))


if __name__ == "__main__":
    unittest.main()
