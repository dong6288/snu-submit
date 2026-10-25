#!/usr/bin/env python3
"""Build a deterministic public-source alpha ZIP of SNU Submit (0.1.0-alpha.3).

    python tools/package_release.py --output <new .zip path>

Only the exact literal paths in ALLOWLIST are packaged; there is no directory
walking or extension globbing, so any other file (credentials, reports, run
records, caches, screenshots) is never included. A new public file must be
added to ALLOWLIST deliberately. Every listed path must be a regular file
inside the repository; symlinks and Windows reparse points on any component
are refused. Members are stored (no compression) in sorted order with fixed
timestamps and permissions under one top folder, followed by SHA256SUMS (the
SHA-256 of every other member's actual bytes). The output must be a new file
that is not a listed path or inside a listed directory. Nothing in the
repository is modified. Exit 0 on success, 1 on refusal.
"""

import argparse
import hashlib
import os
import stat
import sys
import zipfile
from pathlib import Path

VERSION = "0.1.0-alpha.3"
TOP = f"snu-submit-{VERSION}"
SKILL = "plugins/snu-submit/skills/snu-submit"
ALLOWLIST = (
    "README.md",
    "LICENSE",
    "PRIVACY.md",
    "Install-SnuSubmit.ps1",
    "docs/INSTALL.md",
    "tools/package_release.py",
    ".agents/plugins/marketplace.json",
    "plugins/snu-submit/.codex-plugin/plugin.json",
    f"{SKILL}/SKILL.md",
    f"{SKILL}/agents/openai.yaml",
    f"{SKILL}/pyproject.toml",
    f"{SKILL}/requirements.lock",
    f"{SKILL}/references/etl.md",
    f"{SKILL}/references/evidence-api.md",
    f"{SKILL}/references/gmail.md",
    f"{SKILL}/references/profile.md",
    f"{SKILL}/assets/report.html",
    f"{SKILL}/assets/schemas/checks.json",
    f"{SKILL}/assets/schemas/requirements.json",
    f"{SKILL}/assets/schemas/source.json",
    f"{SKILL}/scripts/snu_submit.py",
    f"{SKILL}/scripts/submit_checks/__init__.py",
    f"{SKILL}/scripts/submit_checks/archive_probes.py",
    f"{SKILL}/scripts/submit_checks/file_review.py",
    f"{SKILL}/scripts/submit_checks/core.py",
    f"{SKILL}/scripts/submit_checks/parsers.py",
    f"{SKILL}/scripts/submit_checks/report.py",
    "tests/test_archive_cli.py",
    "tests/test_archive_probes.py",
    "tests/test_file_review.py",
    "tests/test_install.py",
    "tests/test_release_package.py",
)
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024


class PackageError(Exception):
    """Refusal with a short message."""


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _check_path(root, relative):
    """Refuse links/reparse points on every component below root; require a regular file."""
    current, info = root, None
    for part in relative.split("/"):
        current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            raise PackageError(f"allowlisted file missing: {relative}") from None
        if _is_link(info):
            raise PackageError(f"allowlisted path is a link or reparse point: {relative}")
    if not stat.S_ISREG(info.st_mode):
        raise PackageError(f"allowlisted path is not a regular file: {relative}")
    return current


def collect_files(root):
    """Return [(relative_posix_path, absolute_path)] for exactly the ALLOWLIST, sorted."""
    root = Path(root)
    if len(set(ALLOWLIST)) != len(ALLOWLIST):
        raise PackageError("allowlist contains duplicates")
    return sorted((relative, _check_path(root, relative)) for relative in ALLOWLIST)


def _zipinfo(name):
    info = zipfile.ZipInfo(f"{TOP}/{name}", date_time=FIXED_TIME)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_STORED
    return info


def _guard_output(root, target):
    if target.suffix.lower() != ".zip":
        raise PackageError("output must end with .zip")
    if os.path.lexists(target):
        raise PackageError("output already exists; choose a new path")
    if not target.parent.is_dir():
        raise PackageError("output parent directory does not exist")
    out = os.path.normcase(str(target))
    for relative in ALLOWLIST:
        guarded = root / relative
        if out == os.path.normcase(str(guarded)):
            raise PackageError("output must not replace a packaged file")
        parent = guarded.parent
        while parent != root and root in parent.parents:
            if out.startswith(os.path.normcase(str(parent)) + os.sep):
                raise PackageError("output must not be inside a packaged directory")
            parent = parent.parent


def build_package(root, output):
    """Write the package to a NEW output path. Returns {"members", "sha256", "manifest"}."""
    root = Path(root).resolve()
    target = Path(os.path.abspath(output))
    _guard_output(root, target)
    payloads, total, lines = [], 0, []
    for relative, path in collect_files(root):
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise PackageError(f"file too large: {relative}")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise PackageError("package too large")
        payloads.append((relative, data))
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {TOP}/{relative}")
    manifest = ("\n".join(lines) + "\n").encode("utf-8")
    with open(target, "xb") as handle, zipfile.ZipFile(handle, "w") as archive:
        for relative, data in payloads:
            archive.writestr(_zipinfo(relative), data)
        archive.writestr(_zipinfo("SHA256SUMS"), manifest)
    return {"members": [r for r, _ in payloads] + ["SHA256SUMS"],
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "manifest": manifest.decode("utf-8")}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the deterministic SNU Submit alpha source ZIP.")
    parser.add_argument("--output", required=True, help="new .zip path outside the packaged directories")
    args = parser.parse_args(argv)
    try:
        result = build_package(Path(__file__).resolve().parents[1], args.output)
    except (PackageError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"package: {os.path.abspath(args.output)}")
    print(f"members: {len(result['members'])}")
    print(f"sha256: {result['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
