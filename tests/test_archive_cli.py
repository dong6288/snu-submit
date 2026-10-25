"""Subprocess tests for scripts/snu_submit.py (archive-profile CLI). Synthetic data and fictional markers only.

Run from the repository root:  python -m unittest tests.test_archive_cli -v
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "plugins" / "snu-submit" / "skills" / "snu-submit" / "scripts" / "snu_submit.py"
REPORT = b"Intro\nBEGIN_SECTION SAMPLE\nmeasured value 7\nEND_SECTION\nOutro\n"


def cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=300)


def cli_legacy_console(*args):
    """Run with a non-UTF-8 console codec (as on a default Korean Windows console) and no UTF-8 mode."""
    env = dict(os.environ, PYTHONIOENCODING="cp949", PYTHONUTF8="0")
    result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, timeout=300, env=env)
    return result.returncode, result.stdout.decode("cp949", errors="replace"), result.stderr.decode("cp949", errors="replace")


def zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buffer.getvalue()


def profile(existing=None):
    rule = {"reason": "fictional rule", "evidence": "synthetic test note"}
    return {"profile_version": "archive-profile/1",
            "required_entries": [dict(rule, path="report.txt"), dict(rule, path="data/table.csv")],
            "nonempty_entries": [dict(rule, path="data/table.csv", encoding=None)],
            "text_sections": [dict(rule, path="report.txt", start_literal="BEGIN_SECTION SAMPLE",
                                   end_literal="END_SECTION", encoding="utf-8")],
            "existing_issues": existing or []}


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.archive = self.dir / "source.zip"
        self.archive.write_bytes(zip_bytes([("report.txt", REPORT), ("data/table.csv", b"a,b\n1,2\n")]))
        self.source_bytes = self.archive.read_bytes()
        self.profile = self.dir / "profile.json"
        self.profile.write_text(json.dumps(profile()), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def scenarios(self, cases, sha=None):
        path = self.dir / "scenarios.json"
        path.write_text(json.dumps({"version": "archive-demo/1", "label": "synthetic test",
                                    "expected_source_sha256": sha or hashlib.sha256(self.source_bytes).hexdigest(),
                                    "cases": cases}), encoding="utf-8")
        return path

    def demo(self, cases, out="demo"):
        result = cli("demo", "--archive", str(self.archive), "--profile", str(self.profile),
                     "--scenarios", str(self.scenarios(cases)), "--output", str(self.dir / out))
        return result

    def test_doctor_reports_json(self):
        result = cli("doctor")
        self.assertIn(result.returncode, (0, 1))
        data = json.loads(result.stdout)
        self.assertTrue(next(c for c in data["checks"] if c["name"] == "python")["ok"])
        self.assertIn("submit_checks.archive_probes", [c["name"] for c in data["checks"]])

    def test_help_works_on_legacy_console_for_every_command(self):
        code, out, err = cli_legacy_console("--help")
        self.assertEqual(code, 0, err)
        for name in ("doctor", "check", "recheck", "sample", "demo", "review"):
            self.assertIn(name, out)
            code, out_sub, err_sub = cli_legacy_console(name, "--help")
            self.assertEqual(code, 0, f"{name}: {err_sub}")
            self.assertIn("usage", out_sub.lower())

    def test_non_ascii_label_on_legacy_console(self):
        label = "가상 라벨 — test \U0001F600"
        code, out, err = cli_legacy_console("check", "--archive", str(self.archive), "--profile", str(self.profile),
                                            "--output", str(self.dir / "lbl"), "--label", label)
        self.assertEqual(code, 0, err)
        report = json.loads((self.dir / "lbl" / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["label"], label)  # files keep the exact UTF-8 text
        code, out, err = cli_legacy_console("doctor")
        self.assertIn(code, (0, 1))
        json.loads(out)

    def test_check_writes_actual_reports(self):
        result = cli("check", "--archive", str(self.archive), "--profile", str(self.profile), "--output", str(self.dir / "c1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.dir / "c1" / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["result"]["overall"], "profile_pass")
        self.assertEqual(report["result"]["archive"]["sha256"], hashlib.sha256(self.source_bytes).hexdigest())
        page = (self.dir / "c1" / "report.html").read_text(encoding="utf-8")
        self.assertNotIn("<script", page.lower())
        self.assertEqual(self.archive.read_bytes(), self.source_bytes)

    def test_demo_outcomes_are_computed_per_case(self):
        result = self.demo([
            {"id": "control", "label": "unchanged", "mutations": []},
            {"id": "missing", "label": "file removed", "mutations": [{"op": "remove_entry", "path": "data/table.csv"}]},
            {"id": "empty", "label": "file emptied", "mutations": [{"op": "empty_entry", "path": "data/table.csv"}]},
            {"id": "no-section", "label": "section removed", "mutations": [
                {"op": "remove_text_section", "path": "report.txt", "start_literal": "BEGIN_SECTION SAMPLE",
                 "end_literal": "END_SECTION"}]},
            {"id": "restored", "label": "restored", "mutations": []}])
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads((self.dir / "demo" / "summary.json").read_text(encoding="utf-8"))
        cases = {c["id"]: c for c in summary["cases"]}
        self.assertEqual(cases["control"]["overall"], "profile_pass")
        self.assertEqual(cases["restored"]["overall"], "profile_pass")
        self.assertEqual((self.dir / "demo" / "archives" / "control.zip").read_bytes(), self.source_bytes)
        self.assertEqual(cases["missing"]["overall"], "needs_changes")
        self.assertIn("data/table.csv", cases["missing"]["missing_entries"])
        self.assertIsNone(cases["missing"]["mutations"][0]["current_entry_sha256"])
        empty_report = json.loads((self.dir / "demo" / cases["empty"]["report_json"]).read_text(encoding="utf-8"))
        self.assertIn("zero_bytes", [p["reason_code"] for p in empty_report["result"]["probes"]])
        section = cases["no-section"]
        self.assertEqual(section["section_findings"][0]["reason_code"], "missing_start")
        self.assertEqual(section["mutations"][0]["removed_lines"], [2, 4])
        self.assertEqual(section["mutations"][0]["removed_segment_sha256"],
                         hashlib.sha256(b"BEGIN_SECTION SAMPLE\nmeasured value 7\nEND_SECTION\n").hexdigest())
        self.assertTrue(summary["source"]["unchanged"])
        self.assertEqual(self.archive.read_bytes(), self.source_bytes)
        index = (self.dir / "demo" / "index.html").read_text(encoding="utf-8")
        ledger = section["mutations"][0]
        self.assertIn(f'{ledger["op"]} {ledger["path"]}', index)
        self.assertIn(f'{ledger["removed_lines"][0]}–{ledger["removed_lines"][1]}', index)
        self.assertIn('href="cases/no-section/report.html"', index)

    def test_existing_unknown_issue_is_preserved_in_every_case(self):
        self.profile.write_text(json.dumps(profile([{"status": "unknown", "reason": "fictional open item",
                                                     "evidence": "synthetic"}])), encoding="utf-8")
        result = self.demo([{"id": "control", "label": "unchanged", "mutations": []},
                            {"id": "missing", "label": "removed",
                             "mutations": [{"op": "remove_entry", "path": "report.txt"}]}])
        self.assertEqual(result.returncode, 0, result.stderr)
        cases = {c["id"]: c for c in json.loads((self.dir / "demo" / "summary.json").read_text("utf-8"))["cases"]}
        self.assertEqual(cases["control"]["overall"], "needs_confirmation")
        self.assertEqual([c["existing_issues"] for c in cases.values()], [1, 1])

    def test_refusals_produce_no_success(self):
        bad_json = self.dir / "dup.json"
        bad_json.write_text('{"profile_version": "archive-profile/1", "profile_version": "x"}', encoding="utf-8")
        refusals = [
            cli("check", "--archive", str(self.archive), "--profile", str(bad_json), "--output", str(self.dir / "r1")),
            self.demo([{"id": "../escape", "label": "bad id", "mutations": []}], out="r2"),
            self.demo([{"id": "absent", "label": "marker absent", "mutations": [
                {"op": "remove_text_section", "path": "report.txt", "start_literal": "BEGIN_SECTION OTHER",
                 "end_literal": "END_SECTION"}]}], out="r3"),
            cli("demo", "--archive", str(self.archive), "--profile", str(self.profile),
                "--scenarios", str(self.scenarios([{"id": "c", "label": "x", "mutations": []}], sha="0" * 64)),
                "--output", str(self.dir / "r4")),
        ]
        for index, result in enumerate(refusals, start=1):
            self.assertEqual(result.returncode, 1, f"case r{index}")
            self.assertFalse((self.dir / f"r{index}").exists(), f"case r{index} created output")
        (self.dir / "taken").mkdir()
        taken = cli("check", "--archive", str(self.archive), "--profile", str(self.profile), "--output", str(self.dir / "taken"))
        self.assertEqual(taken.returncode, 1)
        self.assertEqual(list((self.dir / "taken").iterdir()), [])
        self.assertEqual(self.archive.read_bytes(), self.source_bytes)

    def test_recheck_reports_actual_delta(self):
        first = cli("check", "--archive", str(self.archive), "--profile", str(self.profile), "--output", str(self.dir / "v1"))
        self.assertEqual(first.returncode, 0, first.stderr)
        changed = self.dir / "changed.zip"
        changed.write_bytes(zip_bytes([("report.txt", REPORT)]))
        second = cli("recheck", "--archive", str(changed), "--profile", str(self.profile),
                     "--previous", str(self.dir / "v1" / "report.json"), "--output", str(self.dir / "v2"))
        self.assertEqual(second.returncode, 0, second.stderr)
        report = json.loads((self.dir / "v2" / "report.json").read_text(encoding="utf-8"))
        delta = report["delta"]
        self.assertIn("data/table.csv", delta["entries_removed"])
        self.assertTrue(delta["archive_changed"])
        self.assertTrue(any(c["previous_status"] == "pass" and c["current_status"] == "fail" for c in delta["status_changes"]))
        self.assertEqual(report["result"]["overall"], "needs_changes")

    def test_sample_runs_synthetic_demo(self):
        result = cli("sample", "--output", str(self.dir / "s"))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads((self.dir / "s" / "demo" / "summary.json").read_text(encoding="utf-8"))
        outcomes = {c["id"]: c["overall"] for c in summary["cases"]}
        self.assertEqual(outcomes["control"], "profile_pass")
        self.assertEqual(outcomes["restored"], "profile_pass")
        self.assertEqual(outcomes["missing-file"], "needs_changes")
        self.assertEqual(outcomes["removed-section"], "needs_changes")
        self.assertTrue(summary["source"]["unchanged"])

    def test_demo_refuses_source_with_contradictory_entry_metadata(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("report.txt")
            info.external_attr = 0o040755 << 16  # file name, directory mode
            zf.writestr(info, REPORT)
            zf.writestr("data/table.csv", b"x\n")
        self.archive.write_bytes(buffer.getvalue())
        self.source_bytes = self.archive.read_bytes()
        result = self.demo([{"id": "control", "label": "unchanged", "mutations": []}], out="meta")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.dir / "meta").exists())
        self.assertEqual(self.archive.read_bytes(), self.source_bytes)

    def test_concurrent_source_change_is_reported_incomplete(self):
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("snu_submit_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_run_check = module.run_check
        archive = self.archive

        def run_check_while_source_changes(*args, **kwargs):
            with open(archive, "ab") as handle:  # a real change to the source file during the demo
                handle.write(b"\0")
            return original_run_check(*args, **kwargs)

        scenarios = self.scenarios([{"id": "control", "label": "unchanged", "mutations": []}])
        with mock.patch.object(module, "run_check", run_check_while_source_changes):
            code = module.main(["demo", "--archive", str(self.archive), "--profile", str(self.profile),
                                "--scenarios", str(scenarios), "--output", str(self.dir / "conc")])
        self.assertEqual(code, 1)
        summary = json.loads((self.dir / "conc" / "summary.json").read_text(encoding="utf-8"))
        self.assertFalse(summary["complete"])
        self.assertFalse(summary["source"]["unchanged"])
        self.assertNotEqual(summary["source"]["sha256_after"], summary["source"]["sha256_before"])
        self.assertTrue(summary["notice"].startswith("INCOMPLETE"))

    def test_ambiguous_end_marker_is_still_refused_by_demo(self):
        self.archive.write_bytes(zip_bytes([("report.txt", REPORT + b"END_SECTION\n"), ("data/table.csv", b"x\n")]))
        self.source_bytes = self.archive.read_bytes()
        result = self.demo([{"id": "cut", "label": "remove", "mutations": [
            {"op": "remove_text_section", "path": "report.txt", "start_literal": "BEGIN_SECTION SAMPLE",
             "end_literal": "END_SECTION"}]}], out="amb")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.dir / "amb").exists())


if __name__ == "__main__":
    unittest.main()
