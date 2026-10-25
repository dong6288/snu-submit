"""Synthetic tests for Install-SnuSubmit.ps1. No real Codex, Python install, account or network is used.

Function checks dot-source the installer (which then only defines functions) and need PowerShell.
Full-run refusal checks use fake codex/python .cmd files with call logs and need Windows.
If the local execution policy blocks running scripts, the tests are skipped (the policy is not bypassed).
Run from the repository root:  python -m unittest tests.test_install -v
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "Install-SnuSubmit.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
POLICY_MARKERS = ("running scripts is disabled", "PSSecurityException", "UnauthorizedAccess")

ROW = {"name": "snu-submit", "marketplaceName": "snu-submit", "pluginId": "snu-submit@snu-submit",
       "version": "0.1.0-alpha.1", "installed": True, "enabled": True, "source": {"type": "local"}}


def run_ps(args, env=None, cwd=None):
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", *args], capture_output=True, text=True,
                            timeout=120, env=env, cwd=cwd)
    if any(marker in result.stderr for marker in POLICY_MARKERS):
        raise unittest.SkipTest("execution policy blocks local scripts; not bypassed")
    return result


def ps_quote(path):
    return "'" + str(path).replace("'", "''") + "'"


@unittest.skipUnless(POWERSHELL, "PowerShell is not available")
class InstallerFunctionTests(unittest.TestCase):
    def evaluate(self, function, cases, extra=""):
        env = dict(os.environ, SNU_CASES=json.dumps(cases))
        command = (f". {ps_quote(INSTALLER)}; $cases = $env:SNU_CASES | ConvertFrom-Json; "
                   f"foreach ($c in $cases) {{ {function} $c {extra} }}")
        result = run_ps(["-Command", command], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def test_installed_identity_requires_exact_structured_row(self):
        def doc(rows, available=()):
            return json.dumps({"installed": list(rows), "available": list(available)})
        positive = doc([ROW])
        negatives = [
            doc([dict(ROW, enabled=False)]),
            doc([dict(ROW, installed=False)]),
            doc([], available=[ROW]),
            doc([dict(ROW, name="snu-submit-other")]),
            doc([dict(ROW, name="SNU-SUBMIT")]),
            doc([dict(ROW, marketplaceName="elsewhere")]),
            doc([dict(ROW, installed="true", enabled="true")]),
            doc([{k: v for k, v in ROW.items() if k != "enabled"}]),
            json.dumps({"installed": [], "note": "snu-submit snu-submit installed enabled"}),
            json.dumps({"available": [ROW]}),
            doc([]),  # valid shape, nothing installed
            json.dumps({"installed": ROW, "available": []}),  # single object instead of an array
            json.dumps({"installed": None, "available": []}),
            json.dumps({"installed": "snu-submit", "available": []}),
            json.dumps({"installed": True, "available": []}),
            json.dumps({"installed": [ROW]}),  # "available" array missing
            json.dumps({"installed": [ROW], "available": None}),
            json.dumps([{"installed": [ROW], "available": []}]),  # top level is an array
            json.dumps(None),
            "snu-submit installed",
            "{not json",
        ]
        results = self.evaluate("Test-InstalledIdentity", [positive] + negatives)
        self.assertEqual(results, ["True"] + ["False"] * len(negatives))

    def test_marketplace_action_detects_conflict_before_changes(self):
        root = str(REPO)
        cases = [
            json.dumps({"marketplaces": []}),
            json.dumps({"marketplaces": [{"name": "other", "root": "C:\\elsewhere"}]}),
            json.dumps({"marketplaces": [{"name": "snu-submit", "root": root}]}),
            json.dumps({"marketplaces": [{"name": "snu-submit", "root": os.path.join(root, "different")}]}),
            json.dumps({"marketplaces": [{"name": "snu-submit"}]}),
            json.dumps({"other": []}),
            "not json",
            json.dumps({"marketplaces": None}),
            json.dumps({"marketplaces": False}),
            json.dumps({"marketplaces": "snu-submit"}),
            json.dumps({"marketplaces": {"name": "snu-submit", "root": root}}),  # object instead of an array
            json.dumps({"marketplaces": [None]}),
            json.dumps({"marketplaces": ["snu-submit"]}),
            json.dumps([{"marketplaces": []}]),  # top level is an array
            json.dumps("marketplaces"),
        ]
        results = self.evaluate("Get-MarketplaceAction", cases, extra=ps_quote(root))
        self.assertEqual(results, ["add", "add", "reuse", "conflict", "conflict", "invalid", "invalid"] + ["invalid"] * 8)

    def test_tree_check_walks_nested_entries_without_following_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "tree"
            (tree / "Lib" / "site-packages" / "pkg").mkdir(parents=True)
            (tree / "Lib" / "site-packages" / "pkg" / "mod.py").write_text("x = 1\n", encoding="ascii")
            outside = Path(tmp) / "outside"
            outside.mkdir()
            self.assertEqual(self.evaluate("Test-TreeReparseFree", [str(tree)]), ["True"])
            nested = tree / "Lib" / "site-packages" / "linked"
            try:
                os.symlink(outside, nested, target_is_directory=True)
            except (OSError, NotImplementedError):
                if os.name != "nt":
                    self.skipTest("symlink creation is not permitted here; plain-tree validation still ran")
                try:
                    import _winapi
                    _winapi.CreateJunction(str(outside), str(nested))
                except (ImportError, OSError, AttributeError):
                    self.skipTest("neither symlink nor junction creation is available; plain-tree validation still ran")
            self.assertEqual(self.evaluate("Test-TreeReparseFree", [str(tree)]), ["False"])

    def test_reparse_check_accepts_plain_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain"
            plain.mkdir()
            results = self.evaluate("Test-ReparseFree", [str(plain), str(plain / "not-yet-created")])
            self.assertEqual(results, ["True", "True"])
            link = Path(tmp) / "link"
            try:
                os.symlink(plain, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is not permitted here; plain-path validation still ran")
            self.assertEqual(self.evaluate("Test-ReparseFree", [str(link / "child")]), ["False"])


@unittest.skipUnless(POWERSHELL and os.name == "nt", "full installer refusal runs need Windows PowerShell")
class InstallerRefusalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "package"
        skill = self.root / "plugins" / "snu-submit" / "skills" / "snu-submit"
        for relative, text in ((".agents/plugins/marketplace.json", "{}"), ("plugins/snu-submit/.codex-plugin/plugin.json", "{}"),
                               (f"{skill.relative_to(self.root).as_posix()}/requirements.lock", "# synthetic\n"),
                               (f"{skill.relative_to(self.root).as_posix()}/scripts/snu_submit.py", "print('synthetic')\n")):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        shutil.copyfile(INSTALLER, self.root / "Install-SnuSubmit.ps1")
        self.fakes = Path(self.tmp.name) / "fakes"
        self.fakes.mkdir()
        (self.fakes / "fake_codex.cmd").write_text(
            '@echo off\r\necho %*>>"%~dp0codex_calls.log"\r\n'
            'if "%~1 %~2 %~3"=="plugin marketplace list" (type "%~dp0marketplaces.json" & exit /b 0)\r\nexit /b 0\r\n',
            encoding="ascii")
        (self.fakes / "fake_python.cmd").write_text(
            '@echo off\r\necho %*>>"%~dp0python_calls.log"\r\necho 3.12\r\nexit /b 0\r\n', encoding="ascii")

    def tearDown(self):
        self.tmp.cleanup()

    def run_installer(self, marketplaces):
        (self.fakes / "marketplaces.json").write_text(json.dumps(marketplaces), encoding="ascii")
        return run_ps(["-File", str(self.root / "Install-SnuSubmit.ps1"),
                       "-CodexExe", str(self.fakes / "fake_codex.cmd"),
                       "-PythonExe", str(self.fakes / "fake_python.cmd")])

    def calls(self, name):
        log = self.fakes / name
        return log.read_text(encoding="ascii", errors="replace").splitlines() if log.exists() else []

    def test_conflicting_marketplace_refuses_before_any_change(self):
        result = self.run_installer({"marketplaces": [{"name": "snu-submit", "root": "C:\\elsewhere\\snu-submit"}]})
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / ".venv").exists())
        self.assertEqual(self.calls("python_calls.log"), [])
        self.assertEqual([c.strip() for c in self.calls("codex_calls.log")], ["plugin marketplace list --json"])

    def test_unknown_existing_venv_directory_is_not_reused(self):
        (self.root / ".venv").mkdir()
        (self.root / ".venv" / "keep.txt").write_text("unknown content", encoding="ascii")
        result = self.run_installer({"marketplaces": []})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls("python_calls.log"), [])
        self.assertEqual(self.calls("codex_calls.log"), [])
        self.assertEqual((self.root / ".venv" / "keep.txt").read_text(encoding="ascii"), "unknown content")

    def test_nested_site_packages_junction_is_refused_before_python_or_pip(self):
        venv = self.root / ".venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Lib").mkdir()
        (venv / "pyvenv.cfg").write_text("home = synthetic\n", encoding="ascii")
        (venv / "Scripts" / "python.exe").write_bytes(b"synthetic stub, never executed")
        outside = Path(self.tmp.name) / "outside-site-packages"
        outside.mkdir()
        (outside / "existing.txt").write_text("external", encoding="ascii")
        try:
            import _winapi
            _winapi.CreateJunction(str(outside), str(venv / "Lib" / "site-packages"))
        except (ImportError, OSError, AttributeError):
            self.skipTest("junction creation is not available here; the nested-junction guard was NOT exercised")
        result = self.run_installer({"marketplaces": []})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls("python_calls.log"), [])
        self.assertEqual(self.calls("codex_calls.log"), [])  # refused in stage 0, before Codex or pip
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["existing.txt"])
        self.assertEqual((outside / "existing.txt").read_text(encoding="ascii"), "external")

    def test_junction_venv_is_refused_without_touching_target(self):
        target = Path(self.tmp.name) / "elsewhere"
        target.mkdir()
        (target / "pyvenv.cfg").write_text("home = synthetic\n", encoding="ascii")
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(self.root / ".venv"))
        except (ImportError, OSError, AttributeError):
            self.skipTest("junction creation is not available here")
        result = self.run_installer({"marketplaces": []})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls("python_calls.log"), [])
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["pyvenv.cfg"])


if __name__ == "__main__":
    unittest.main()
