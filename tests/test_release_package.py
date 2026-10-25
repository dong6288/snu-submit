"""Tests for tools/package_release.py. Synthetic trees only; the real repository is only read.

Run from the repository root:  python -m unittest tests.test_release_package -v
"""

import hashlib
import importlib.util
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("package_release_under_test", REPO / "tools" / "package_release.py")
pkg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pkg)

# Fictional files that must never be packaged, including same-extension files inside packaged folders.
INTRUDERS = (
    ".git/config", ".venv/pyvenv.cfg", "credentials.json", "screenshot.png", "work/report.html",
    f"{pkg.SKILL}/assets/credentials.json", f"{pkg.SKILL}/assets/runs/report.json",
    f"{pkg.SKILL}/assets/schemas/extra.json", f"{pkg.SKILL}/assets/nested/deeper/report.html",
    f"{pkg.SKILL}/scripts/secret_helper.py", f"{pkg.SKILL}/scripts/submit_checks/__pycache__/core.cpython-312.pyc",
    f"{pkg.SKILL}/scripts/submit_checks/local_override.py", f"{pkg.SKILL}/references/private-notes.md",
    f"{pkg.SKILL}/references/nested/notes.md", f"{pkg.SKILL}/.snu-submit/runs/r1/report.json",
    "tests/test_private_data.py", "tests/fixtures/credentials.json", "tests/nested/test_hidden.py",
    "docs/private.md", "tools/other_tool.py",
)


def make_tree(root):
    for relative in pkg.ALLOWLIST:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"synthetic public file {relative}\n".encode())
    for relative in INTRUDERS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"fictional intruder {relative}\n".encode())


def read_package(path):
    with zipfile.ZipFile(path) as archive:
        return {info.filename: (info, archive.read(info.filename)) for info in archive.infolist()}, archive.namelist()


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "tree"
        self.root.mkdir()
        make_tree(self.root)
        self.out = Path(self.tmp.name) / "out"
        self.out.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_member_set_and_every_hash(self):
        result = pkg.build_package(self.root, self.out / "a.zip")
        members, order = read_package(self.out / "a.zip")
        expected = {f"{pkg.TOP}/{r}" for r in pkg.ALLOWLIST} | {f"{pkg.TOP}/SHA256SUMS"}
        self.assertEqual(set(members), expected)
        self.assertEqual(order[-1], f"{pkg.TOP}/SHA256SUMS")
        self.assertEqual(order[:-1], sorted(order[:-1]))
        manifest = members[f"{pkg.TOP}/SHA256SUMS"][1].decode("utf-8").splitlines()
        listed = {}
        for line in manifest:
            digest, name = line.split("  ", 1)
            listed[name] = digest
        self.assertEqual(set(listed), expected - {f"{pkg.TOP}/SHA256SUMS"})
        for name, digest in listed.items():
            relative = name.split("/", 1)[1]
            self.assertEqual(digest, hashlib.sha256(members[name][1]).hexdigest())
            self.assertEqual(members[name][1], (self.root / relative).read_bytes())
        for info, _ in members.values():
            self.assertEqual(info.date_time, pkg.FIXED_TIME)
            self.assertEqual(info.external_attr, 0o100644 << 16)
            self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
        self.assertEqual(result["members"], [n.split("/", 1)[1] for n in order])

    def test_intruders_never_appear(self):
        pkg.build_package(self.root, self.out / "a.zip")
        members, _ = read_package(self.out / "a.zip")
        for relative in INTRUDERS:
            self.assertNotIn(f"{pkg.TOP}/{relative}", members)
        blob = (self.out / "a.zip").read_bytes()
        self.assertNotIn(b"fictional intruder", blob)

    def test_repeat_builds_are_byte_identical(self):
        first = pkg.build_package(self.root, self.out / "a.zip")
        second = pkg.build_package(self.root, self.out / "b.zip")
        self.assertEqual((self.out / "a.zip").read_bytes(), (self.out / "b.zip").read_bytes())
        self.assertEqual(first["sha256"], second["sha256"])

    def test_real_repository_package_matches_allowlist_exactly(self):
        a, b = self.out / "real-a.zip", self.out / "real-b.zip"
        pkg.build_package(REPO, a)
        pkg.build_package(REPO, b)
        self.assertEqual(a.read_bytes(), b.read_bytes())
        members, _ = read_package(a)
        self.assertEqual({n.split("/", 1)[1] for n in members}, set(pkg.ALLOWLIST) | {"SHA256SUMS"})

    def test_existing_output_is_not_overwritten(self):
        target = self.out / "taken.zip"
        target.write_bytes(b"keep")
        with self.assertRaises(pkg.PackageError):
            pkg.build_package(self.root, target)
        self.assertEqual(target.read_bytes(), b"keep")

    def test_output_inside_packaged_directory_is_refused(self):
        for relative in ("tests/pkg.zip", f"{pkg.SKILL}/assets/pkg.zip", "docs/pkg.zip"):
            with self.assertRaises(pkg.PackageError):
                pkg.build_package(self.root, self.root / relative)
            self.assertFalse((self.root / relative).exists())

    def test_missing_allowlisted_file_is_refused(self):
        (self.root / pkg.SKILL / "scripts" / "submit_checks" / "core.py").unlink()
        with self.assertRaises(pkg.PackageError):
            pkg.build_package(self.root, self.out / "m.zip")
        self.assertFalse((self.out / "m.zip").exists())

    def test_symlinked_allowlisted_path_is_refused(self):
        outside = Path(self.tmp.name) / "outside.py"
        outside.write_bytes(b"secret = 1\n")
        target = self.root / pkg.SKILL / "scripts" / "submit_checks" / "report.py"
        target.unlink()
        try:
            os.symlink(outside, target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not permitted on this platform/account")
        with self.assertRaises(pkg.PackageError):
            pkg.build_package(self.root, self.out / "s.zip")
        self.assertFalse((self.out / "s.zip").exists())

    def test_directory_in_place_of_allowlisted_file_is_refused(self):
        target = self.root / "LICENSE"
        target.unlink()
        target.mkdir()
        with self.assertRaises(pkg.PackageError):
            pkg.build_package(self.root, self.out / "d.zip")

    def test_cli_entry_point_refuses_existing_output(self):
        target = self.out / "cli.zip"
        target.write_bytes(b"keep")
        self.assertEqual(pkg.main(["--output", str(target)]), 1)
        self.assertEqual(target.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
