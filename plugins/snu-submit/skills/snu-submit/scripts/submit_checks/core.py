"""Core checker helpers (0.1.0-alpha.1, offline-preview).

Currently provides one bounded strict JSON reader and structural validation
against the three packaged schemas. load_json_strict() opens
the given path with ordinary open(); it does NOT establish input-root
containment, no-follow handling or evidence trust. A later safe-IO stage must
route every actual path before calling it.
"""

import copy
import ctypes
import hashlib
import json
import math
import os
import re
import stat as statmod
import unicodedata
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_INT_DIGITS = 32
MAX_ERROR_CHARS = 300
_READ_CHUNK = 64 * 1024


class ValidationError(ValueError):
    """Bounded, payload-free error for unreadable or invalid input."""

    def __init__(self, message):
        super().__init__(str(message)[:MAX_ERROR_CHARS])


def _reject_constant(name):
    raise ValidationError(f"non-finite JSON constant {name} is not allowed")


def _parse_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValidationError("JSON number overflows to a non-finite float")
    return value


def _parse_int(text):
    if len(text.lstrip("-")) > MAX_JSON_INT_DIGITS:
        raise ValidationError(f"JSON integer longer than {MAX_JSON_INT_DIGITS} digits")
    return int(text)


def _unique_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValidationError("duplicate JSON object key")  # key text is never echoed
        obj[key] = value
    return obj


def _check_depth(text):
    """Single left-to-right O(n) nesting guard run before the recursive decoder.

    Tracks only in-string/escape state; brackets inside strings are ignored.
    It is not a JSON grammar: malformed input (e.g. an unterminated string)
    passes through here and is rejected by json.loads afterwards.
    """
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "[" or ch == "{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValidationError(f"JSON nesting deeper than {MAX_JSON_DEPTH}")
        elif ch == "]" or ch == "}":
            depth -= 1


def _check_strings(value):
    """Reject strings (including keys) that cannot be encoded as UTF-8, e.g. lone surrogates."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                stack.append(key)
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                raise ValidationError("JSON string contains an unpaired surrogate escape") from None


def load_json_strict(path, max_bytes=MAX_JSON_BYTES):
    """Read at most max_bytes from path and return the strictly decoded JSON value."""
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_JSON_BYTES:
        raise ValidationError(f"max_bytes must be a positive integer <= {MAX_JSON_BYTES}")
    try:
        path = os.fspath(path)
    except TypeError:
        raise ValidationError("path must be a str or os.PathLike") from None
    if type(path) is not str:
        raise ValidationError("path must be a text path")
    chunks, total = [], 0
    try:
        with open(path, "rb") as handle:
            while total <= max_bytes:
                chunk = handle.read(min(_READ_CHUNK, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
    except (OSError, ValueError) as exc:  # ValueError: e.g. embedded NUL in path
        raise ValidationError(f"cannot read JSON file: {type(exc).__name__}") from None
    if total > max_bytes:
        raise ValidationError(f"JSON file exceeds {max_bytes} bytes")
    raw = b"".join(chunks)
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValidationError("UTF-8 byte order mark is not allowed in JSON")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"invalid UTF-8 at byte {exc.start}") from None
    _check_depth(text)
    try:
        value = json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant,
                           parse_float=_parse_float, parse_int=_parse_int)
    except ValidationError:
        raise
    except json.JSONDecodeError as exc:
        raise ValidationError(f"malformed JSON at line {exc.lineno} column {exc.colno}: {exc.msg}") from None
    except (ValueError, RecursionError) as exc:
        raise ValidationError(f"JSON rejected: {type(exc).__name__}") from None
    _check_strings(value)
    return value


# ---------------------------------------------------------------------------
# Packaged schema validation (closed set of three trusted local schemas)
# ---------------------------------------------------------------------------

SCHEMA_NAMES = frozenset({"source.json", "requirements.json", "checks.json"})
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
MAX_SCHEMA_BYTES = 1024 * 1024
MAX_SCHEMA_DEPTH = 128
_SAFE_PATH_PART = re.compile(r"[A-Za-z0-9_$.-]{1,64}")
_VALIDATORS = {}  # lazily filled; at most one entry per SCHEMA_NAMES member


def _schema_dir():
    """Installed skill root/assets/schemas, independent of the working directory."""
    return Path(__file__).resolve().parents[2] / "assets" / "schemas"


def _check_local_refs(schema):
    """Allow only local-fragment $ref/$dynamicRef; refuse $recursiveRef. Iterative, depth-bounded."""
    stack = [(schema, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_SCHEMA_DEPTH:
            raise ValidationError("packaged schema nesting too deep")
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("$ref", "$dynamicRef"):
                    if type(value) is not str or not value.startswith("#"):
                        raise ValidationError(f"packaged schema has a non-local {key}")
                elif key == "$recursiveRef":
                    raise ValidationError("packaged schema uses unsupported $recursiveRef")
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)


def _packaged_validator(schema_name):
    validator = _VALIDATORS.get(schema_name)
    if validator is not None:
        return validator
    schema = load_json_strict(_schema_dir() / schema_name, max_bytes=MAX_SCHEMA_BYTES)
    if type(schema) is not dict or schema.get("$schema") != SCHEMA_DIALECT:
        raise ValidationError(f"packaged schema {schema_name} is not a Draft 2020-12 schema")
    _check_local_refs(schema)
    try:
        import jsonschema
        import referencing
    except ImportError:
        raise ValidationError("jsonschema is not installed; install the hash-locked requirements") from None
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        # Empty registry without a retrieve callback: nothing outside this schema can be loaded.
        validator = jsonschema.Draft202012Validator(schema, registry=referencing.Registry())
    except Exception as exc:
        raise ValidationError(f"packaged schema {schema_name} is invalid: {type(exc).__name__}") from None
    _VALIDATORS[schema_name] = validator
    return validator


def _error_location(error):
    parts = []
    for part in list(error.absolute_path)[:16]:
        if type(part) is int:
            parts.append(str(part))
        elif type(part) is str and _SAFE_PATH_PART.fullmatch(part):
            parts.append(part)
        else:
            parts.append("?")  # never echo arbitrary instance keys
    return "/" + "/".join(parts)


def validate_document(data, schema_name):
    """Structurally validate data against one packaged schema; return data unchanged.

    schema_name must be exactly 'source.json', 'requirements.json' or
    'checks.json'. Only the first failure is reported, as a bounded message
    naming the JSON location and schema keyword, never instance values.
    Structural validity does not establish evidence truth, freshness or
    calendar-valid timestamps (no format checker is applied).
    """
    if type(schema_name) is not str or schema_name not in SCHEMA_NAMES:
        raise ValidationError("schema_name must be one of source.json, requirements.json, checks.json")
    validator = _packaged_validator(schema_name)
    try:
        error = next(iter(validator.iter_errors(data)), None)
    except Exception as exc:
        raise ValidationError(f"{schema_name} validation could not complete: {type(exc).__name__}") from None
    if error is not None:
        keyword = error.validator if type(error.validator) is str else "?"
        raise ValidationError(f"{schema_name} validation failed at {_error_location(error)}: {keyword[:40]}")
    return data


# ---------------------------------------------------------------------------
# Requirement identity
# ---------------------------------------------------------------------------

_JSON_SCALAR_TYPES = (str, int, float, bool, type(None))
MAX_IDENTITY_DEPTH = 256


def _check_json_types(value):
    """Reject values json.dumps would silently coerce (tuples, non-str keys, subclasses).

    Depth-bounded, so a cyclic structure terminates with an error.
    """
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_IDENTITY_DEPTH:
            raise ValidationError("requirement nesting too deep or cyclic")
        kind = type(item)
        if kind is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValidationError("requirement object keys must be strings")
                stack.append((child, depth + 1))
        elif kind is list:
            stack.extend((child, depth + 1) for child in item)
        elif kind not in _JSON_SCALAR_TYPES:
            raise ValidationError(f"requirement contains a non-JSON value of type {kind.__name__[:40]}")
        elif kind is float and not math.isfinite(item):
            raise ValidationError("requirement contains a non-finite number")


def canonical_requirement_sha256(requirement):
    """SHA-256 (lowercase hex) of the FULL requirement record in canonical JSON.

    Encoding: json.dumps(requirement, ensure_ascii=False, sort_keys=True,
    separators=(',', ':'), allow_nan=False) as UTF-8. Every field is included
    as given; nothing is omitted or Unicode-normalized, and the input is not
    modified. Non-JSON values, non-finite numbers, cycles and strings that are
    not encodable as UTF-8 raise a bounded ValidationError. This is identity
    only; requirement semantics are validated elsewhere.
    """
    if type(requirement) is not dict:
        raise ValidationError("requirement must be a JSON object")
    _check_json_types(requirement)
    try:
        canonical = json.dumps(requirement, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        payload = canonical.encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        # ValueError covers NaN/Infinity and circular references; TypeError covers
        # non-JSON values and non-string/mixed keys that cannot be sorted.
        raise ValidationError(f"requirement is not canonical JSON: {type(exc).__name__}") from None
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Windows bounded safe read (POSIX is not implemented here)
# ---------------------------------------------------------------------------

MAX_SAFE_READ_BYTES = 50 * 1024 * 1024
_SAFE_READ_CHUNK = 64 * 1024
_MAX_RELATIVE_CHARS = 2048
_MAX_COMPONENT_CHARS = 255
_FORBIDDEN_NAME_CHARS = frozenset('<>:"|?*\\/')
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{c}" for c in "123456789\u00b9\u00b2\u00b3"} | {f"lpt{c}" for c in "123456789\u00b9\u00b2\u00b3"})
_DRIVE_ROOT_RE = re.compile(r"[A-Za-z]:\\")

_GENERIC_READ = 0x80000000
_FILE_LIST_DIRECTORY = 0x0001
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2  # FILE_SHARE_DELETE is never granted: held components cannot be renamed/deleted
_OPEN_EXISTING = 3
_FLAG_BACKUP_SEMANTICS = 0x02000000
_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FLAG_SEQUENTIAL_SCAN = 0x08000000
_ATTR_DIRECTORY = 0x10
_ATTR_DEVICE = 0x40
_ATTR_REPARSE_POINT = 0x400
_ATTR_UNSAFE_FILE = 0x40 | 0x400 | 0x1000 | 0x40000 | 0x400000  # device, reparse, offline, recall-on-open/data
_FILE_TYPE_DISK = 1
_INFO_BASIC, _INFO_STANDARD, _INFO_ID = 0, 1, 18
_FILETIME_UNIX_EPOCH = 116444736000000000
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_KERNEL32 = None


class _FileBasicInfo(ctypes.Structure):
    _fields_ = [("CreationTime", ctypes.c_int64), ("LastAccessTime", ctypes.c_int64),
                ("LastWriteTime", ctypes.c_int64), ("ChangeTime", ctypes.c_int64),
                ("FileAttributes", ctypes.c_uint32)]


class _FileStandardInfo(ctypes.Structure):
    _fields_ = [("AllocationSize", ctypes.c_int64), ("EndOfFile", ctypes.c_int64),
                ("NumberOfLinks", ctypes.c_uint32), ("DeletePending", ctypes.c_ubyte),
                ("Directory", ctypes.c_ubyte)]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_uint64), ("FileId", ctypes.c_ubyte * 16)]


def _kernel32():
    """Lazily bind the few kernel32 functions used, with explicit 64-bit-safe prototypes."""
    global _KERNEL32
    if _KERNEL32 is None:
        from ctypes import wintypes as wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
        k32.CreateFileW.restype = wt.HANDLE
        k32.GetFileInformationByHandleEx.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD]
        k32.GetFileInformationByHandleEx.restype = wt.BOOL
        k32.GetFinalPathNameByHandleW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD, wt.DWORD]
        k32.GetFinalPathNameByHandleW.restype = wt.DWORD
        k32.GetFileType.argtypes = [wt.HANDLE]
        k32.GetFileType.restype = wt.DWORD
        k32.ReadFile.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
        k32.ReadFile.restype = wt.BOOL
        k32.CloseHandle.argtypes = [wt.HANDLE]
        k32.CloseHandle.restype = wt.BOOL
        _KERNEL32 = k32
    return _KERNEL32


def _check_name(name, what):
    if not name or len(name) > _MAX_COMPONENT_CHARS:
        raise ValidationError(f"{what} has an empty or overlong component")
    if name in (".", ".."):
        raise ValidationError(f"{what} has a dot or dot-dot component")
    if any(ord(ch) < 32 or ord(ch) == 127 or ch in _FORBIDDEN_NAME_CHARS for ch in name):
        raise ValidationError(f"{what} has a control, reserved or stream character")
    if name[-1] in ". ":
        raise ValidationError(f"{what} has a component ending in dot or space")
    if name.split(".")[0].rstrip(" ").casefold() in _RESERVED_DEVICE_NAMES:
        raise ValidationError(f"{what} has a reserved device name")


def _split_root(root):
    if type(root) is not str or not 3 <= len(root) <= 1024:
        raise ValidationError("root must be an absolute local drive path")
    text = root.replace("/", "\\")
    if not _DRIVE_ROOT_RE.match(text):
        raise ValidationError("root must be an absolute local drive path such as C:\\folder")
    rest = text[3:]
    if rest.endswith("\\"):
        rest = rest[:-1]
    parts = rest.split("\\") if rest else []
    for part in parts:
        _check_name(part, "root")
    return text[0].upper() + ":", parts


def _split_relative(relative_path):
    if type(relative_path) is not str or not 0 < len(relative_path) <= _MAX_RELATIVE_CHARS:
        raise ValidationError("relative_path must be a non-empty bounded string")
    if relative_path.startswith("/") or "\\" in relative_path:
        raise ValidationError("relative_path must be a forward-slash path relative to root")
    parts = relative_path.split("/")
    for part in parts:
        _check_name(part, "relative_path")
    return parts


def _win_error(message):
    return ValidationError(f"{message} (winerror {ctypes.get_last_error()})")


def _open_handle(k32, path, directory):
    if directory:
        access, share, flags = _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES, _FILE_SHARE_READ | _FILE_SHARE_WRITE, \
            _FLAG_BACKUP_SEMANTICS | _FLAG_OPEN_REPARSE_POINT
    else:
        access, share, flags = _GENERIC_READ | _FILE_READ_ATTRIBUTES, _FILE_SHARE_READ, \
            _FLAG_OPEN_REPARSE_POINT | _FLAG_SEQUENTIAL_SCAN
    handle = k32.CreateFileW("\\\\?\\" + path, access, share, None, _OPEN_EXISTING, flags, None)
    if handle is None or handle == _INVALID_HANDLE:
        raise _win_error("cannot open path component without following links, or it is in use")
    return handle


def _query(k32, handle, info_class, struct_type):
    info = struct_type()
    if not k32.GetFileInformationByHandleEx(handle, info_class, ctypes.byref(info), ctypes.sizeof(info)):
        raise _win_error("file information unavailable")
    return info


def _filetime_ns(ticks):
    value = (int(ticks) - _FILETIME_UNIX_EPOCH) * 100
    if value < 0:
        raise ValidationError("timestamp before 1970 is not supported")
    return value


def _handle_stat(k32, handle):
    """Return (stat dict, standard info) for an open handle."""
    basic = _query(k32, handle, _INFO_BASIC, _FileBasicInfo)
    standard = _query(k32, handle, _INFO_STANDARD, _FileStandardInfo)
    if standard.EndOfFile < 0:
        raise ValidationError("file size unavailable")
    stat = {"size_bytes": int(standard.EndOfFile), "mtime_ns": _filetime_ns(basic.LastWriteTime),
            "ctime_ns": _filetime_ns(basic.ChangeTime), "attributes": int(basic.FileAttributes)}
    return stat, standard


def _handle_identity(k32, handle):
    info = _query(k32, handle, _INFO_ID, _FileIdInfo)
    raw = bytes(info.FileId)
    if not any(raw):
        raise ValidationError("file identity unavailable")
    return {"volume_serial": int(info.VolumeSerialNumber), "file_id": raw.hex()}


def _final_path(k32, handle):
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    length = k32.GetFinalPathNameByHandleW(handle, buffer, size, 0)  # normalized, DOS volume name
    if length == 0 or length >= size:
        raise _win_error("final path of opened handle unavailable")
    return buffer.value


def _require_plain_directory(k32, handle):
    stat, standard = _handle_stat(k32, handle)
    if not stat["attributes"] & _ATTR_DIRECTORY or not standard.Directory:
        raise ValidationError("path component is not a directory")
    if stat["attributes"] & _ATTR_REPARSE_POINT:
        raise ValidationError("path component is a reparse point (link, junction or cloud placeholder)")


def _require_regular_file(k32, handle):
    if k32.GetFileType(handle) != _FILE_TYPE_DISK:
        raise ValidationError("target is not a regular disk file")
    stat, standard = _handle_stat(k32, handle)
    if stat["attributes"] & (_ATTR_DIRECTORY | _ATTR_UNSAFE_FILE) or standard.Directory:
        raise ValidationError("target is a directory, device, reparse point or offline/cloud placeholder")
    if standard.NumberOfLinks != 1 or standard.DeletePending:
        raise ValidationError("target has multiple hard links or is pending deletion")


def _read_handle(k32, handle, max_bytes):
    """Read to EOF, at most max_bytes + 1 bytes; refuse excess before returning anything."""
    from ctypes import wintypes as wt
    buffer = ctypes.create_string_buffer(_SAFE_READ_CHUNK)
    count = wt.DWORD()
    digest = hashlib.sha256()
    chunks, total = [], 0
    while True:
        want = min(_SAFE_READ_CHUNK, max_bytes + 1 - total)
        if not k32.ReadFile(handle, buffer, want, ctypes.byref(count), None):
            raise _win_error("file read failed")
        n = count.value
        if n == 0:
            break
        total += n
        if total > max_bytes:
            raise ValidationError(f"file exceeds {max_bytes} bytes")
        chunk = buffer.raw[:n]
        digest.update(chunk)
        chunks.append(chunk)
    return b"".join(chunks), digest.hexdigest()


_POSIX_MAX_DIR_ENTRIES = 100_000


def _posix_capable():
    return (os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
            and os.stat in os.supports_follow_symlinks and os.scandir in os.supports_fd
            and all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK")))


def _split_posix_root(root):
    if type(root) is not str or not 1 <= len(root) <= 4096 or not root.startswith("/"):
        raise ValidationError("root must be an absolute POSIX path")
    rest = root[1:]
    if rest.endswith("/"):
        rest = rest[:-1]
    parts = rest.split("/") if rest else []
    for part in parts:
        if part in ("", ".", "..") or len(part) > _MAX_COMPONENT_CHARS or any(ord(ch) < 32 or ord(ch) == 127 for ch in part):
            raise ValidationError("root has an empty, dot, dot-dot, overlong or control-character component")
    return parts


def _posix_stat(st):
    if st.st_size < 0 or st.st_mtime_ns < 0 or st.st_ctime_ns < 0:
        raise ValidationError("file size or timestamp unsupported")
    return {"size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns, "attributes": st.st_mode}


def _posix_identity(st):
    if st.st_dev < 0 or st.st_ino < 0:
        raise ValidationError("file identity unavailable")
    return {"volume_serial": st.st_dev, "file_id": format(st.st_ino, "x")}


def _posix_open(parent_fd, name, directory):
    """lstat the entry relative to parent_fd, then open it with O_NOFOLLOW and confirm the same object."""
    try:
        seen = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise ValidationError(f"cannot inspect path component (errno {exc.errno})") from None
    wanted = statmod.S_ISDIR if directory else statmod.S_ISREG
    if not wanted(seen.st_mode):
        raise ValidationError("path component is a link, special file or of the wrong type")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0)
    if directory:
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ValidationError(f"cannot open path component without following links (errno {exc.errno})") from None
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (seen.st_dev, seen.st_ino) or not wanted(opened.st_mode):
            raise ValidationError("path component changed while opening")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _posix_require_exact_name(dir_fd, name):
    """Refuse case-insensitive or normalization aliases: the exact name must be a directory entry."""
    try:
        with os.scandir(dir_fd) as entries:
            for count, entry in enumerate(entries):
                if count >= _POSIX_MAX_DIR_ENTRIES:
                    raise ValidationError("directory too large to verify exact entry name")
                if entry.name == name:
                    return
    except OSError as exc:
        raise ValidationError(f"cannot list directory (errno {exc.errno})") from None
    raise ValidationError("path component does not match an exact directory entry name")


def _posix_read(fd, max_bytes):
    digest = hashlib.sha256()
    chunks, total = [], 0
    while True:
        chunk = os.read(fd, min(_SAFE_READ_CHUNK, max_bytes + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValidationError(f"file exceeds {max_bytes} bytes")
        digest.update(chunk)
        chunks.append(chunk)
    return b"".join(chunks), digest.hexdigest()


def _read_file_bound_posix(root, rel_parts, max_bytes):
    """POSIX branch: descriptor-relative O_NOFOLLOW walk from '/', all descriptors held until the end."""
    if not _posix_capable():
        raise ValidationError("safe IO not implemented for this platform")
    root_parts = _split_posix_root(root)
    fds, chain = [], []  # chain: (parent_fd, name, fd) for every opened component below '/'
    try:
        fds.append(os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        if not statmod.S_ISDIR(os.fstat(fds[0]).st_mode):
            raise ValidationError("filesystem root is not a directory")
        for name in root_parts:
            child = _posix_open(fds[-1], name, directory=True)
            chain.append((fds[-1], name, child))
            fds.append(child)
        root_fd = fds[-1]
        root_seen = os.fstat(root_fd)
        root_identity = _posix_identity(root_seen)
        root_stat_before = _posix_stat(root_seen)

        for depth, name in enumerate(rel_parts):
            is_leaf = depth == len(rel_parts) - 1
            _posix_require_exact_name(fds[-1], name)
            child = _posix_open(fds[-1], name, directory=not is_leaf)
            chain.append((fds[-1], name, child))
            fds.append(child)
        leaf_fd = fds[-1]
        leaf_seen = os.fstat(leaf_fd)
        if not statmod.S_ISREG(leaf_seen.st_mode) or leaf_seen.st_nlink != 1:
            raise ValidationError("target is not a single-link regular file")
        file_identity = _posix_identity(leaf_seen)
        stat_before = _posix_stat(leaf_seen)
        if stat_before["size_bytes"] > max_bytes:
            raise ValidationError(f"file exceeds {max_bytes} bytes")

        data, sha256 = _posix_read(leaf_fd, max_bytes)

        leaf_after = os.fstat(leaf_fd)
        stat_after = _posix_stat(leaf_after)
        if stat_after != stat_before or len(data) != stat_before["size_bytes"] or _posix_identity(leaf_after) != file_identity:
            raise ValidationError("file changed during read")
        # Every name must still bind to the object held open (no rename/replace during the read).
        for parent_fd, name, child in chain:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            opened = os.fstat(child)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValidationError("path component was renamed or replaced during read")
        root_after = os.fstat(root_fd)
        root_stat_after = _posix_stat(root_after)
        if _posix_identity(root_after) != root_identity or root_stat_after != root_stat_before:
            raise ValidationError("root changed during read")
    except OSError as exc:
        raise ValidationError(f"safe read failed (errno {exc.errno})") from None
    finally:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass
    return {
        "data": data,
        "sha256": sha256,
        "size_bytes": len(data),
        "relative_path": "/".join(rel_parts),
        "file_identity": file_identity,
        "root_identity": root_identity,
        "stat_before": stat_before,
        "stat_after": stat_after,
        "root_stat_before": root_stat_before,
        "root_stat_after": root_stat_after,
    }


def read_file_bound(root, relative_path, max_bytes=MAX_SAFE_READ_BYTES):
    """Bounded read of one regular file strictly inside an explicit root (Windows and POSIX).

    POSIX: root must be an absolute path; every component from '/' is lstat'ed
    and opened relative to its parent descriptor with O_NOFOLLOW (directories
    with O_DIRECTORY), all descriptors are held, names inside the root must be
    exact directory entries, the leaf must be a single-link regular file, and
    every name is re-checked against its held descriptor after the read.

    Windows: every root ancestor, the root and each relative component is opened
    without following reparse points and held open (no delete sharing) until
    the read ends; each must be a plain directory. Inside the root, every
    opened handle's normalized final path must equal the root's final path
    plus the exact requested names (case and 8.3 aliases are refused). The
    leaf must be a single-link regular disk file. Identity and stat of the
    root and file are compared before and after the read. Returns only a fully
    read stable snapshot; any doubt raises a bounded ValidationError. Callers
    must re-check after parsing: this does not prove later freshness.
    """
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_SAFE_READ_BYTES:
        raise ValidationError(f"max_bytes must be a positive integer <= {MAX_SAFE_READ_BYTES}")
    if os.name == "posix":
        return _read_file_bound_posix(root, _split_relative(relative_path), max_bytes)
    if os.name != "nt":
        raise ValidationError("safe IO not implemented for this platform")
    drive, root_parts = _split_root(root)
    rel_parts = _split_relative(relative_path)
    k32 = _kernel32()
    handles = []
    try:
        current = drive + "\\"
        for index in range(len(root_parts) + 1):
            if index:
                current = current.rstrip("\\") + "\\" + root_parts[index - 1]
            handles.append(_open_handle(k32, current, directory=True))
            _require_plain_directory(k32, handles[-1])
        root_handle = handles[-1]
        root_final = _final_path(k32, root_handle)
        if not root_final.startswith("\\\\?\\") or root_final.startswith("\\\\?\\UNC\\"):
            raise ValidationError("root must be on a local volume")
        root_identity = _handle_identity(k32, root_handle)
        root_stat_before = _handle_stat(k32, root_handle)[0]

        expected = root_final.rstrip("\\")
        for depth, part in enumerate(rel_parts):
            current = current.rstrip("\\") + "\\" + part
            expected += "\\" + part
            is_leaf = depth == len(rel_parts) - 1
            handles.append(_open_handle(k32, current, directory=not is_leaf))
            if not is_leaf:
                _require_plain_directory(k32, handles[-1])
            if _final_path(k32, handles[-1]) != expected:
                raise ValidationError("path component does not resolve to its exact name inside root")
        leaf = handles[-1]
        _require_regular_file(k32, leaf)
        file_identity = _handle_identity(k32, leaf)
        stat_before = _handle_stat(k32, leaf)[0]
        if stat_before["size_bytes"] > max_bytes:
            raise ValidationError(f"file exceeds {max_bytes} bytes")

        data, sha256 = _read_handle(k32, leaf, max_bytes)

        stat_after = _handle_stat(k32, leaf)[0]
        if stat_after != stat_before or len(data) != stat_before["size_bytes"]:
            raise ValidationError("file changed during read")
        if _handle_identity(k32, leaf) != file_identity or _final_path(k32, leaf) != expected:
            raise ValidationError("file identity or location changed during read")
        root_stat_after = _handle_stat(k32, root_handle)[0]
        if (_handle_identity(k32, root_handle) != root_identity or root_stat_after != root_stat_before
                or _final_path(k32, root_handle) != root_final):
            raise ValidationError("root changed during read")
    finally:
        for handle in reversed(handles):
            k32.CloseHandle(handle)
    return {
        "data": data,
        "sha256": sha256,
        "size_bytes": len(data),
        "relative_path": "/".join(rel_parts),
        "file_identity": file_identity,
        "root_identity": root_identity,
        "stat_before": stat_before,
        "stat_after": stat_after,
        "root_stat_before": root_stat_before,
        "root_stat_after": root_stat_after,
    }


# ---------------------------------------------------------------------------
# Shared read budget and literal text source binding
# ---------------------------------------------------------------------------

DEFAULT_READ_BUDGET_BYTES = 200 * 1024 * 1024
TEXT_SOURCE_MAX_BYTES = 1024 * 1024
TEXT_SOURCE_MAX_CHARS = 100_000
TEXT_NORMALIZATION_VERSION = "nfc-lf-utf8-v1"
_SOURCE_MAX_COMBINING_RUN = 64


class ReadBudget:
    """One shared budget of actual bytes read through read_file_bound.

    Pass the same object to every later stage (selection, recheck); it is
    never reset per helper or source. Each read reserves
    min(remaining, max_bytes + 1) before calling the safe helper, which may
    read at most that many bytes (reserved - 1 allowed plus the excess probe).
    A successful read refunds the unused part of the reservation; a failed
    read keeps the full reservation charged, because its actual consumption
    is not reported.
    """

    def __init__(self, total_bytes=DEFAULT_READ_BUDGET_BYTES):
        if type(total_bytes) is not int or not 0 < total_bytes <= DEFAULT_READ_BUDGET_BYTES:
            raise ValidationError(f"total_bytes must be a positive integer <= {DEFAULT_READ_BUDGET_BYTES}")
        self.total_bytes = total_bytes
        self.remaining = total_bytes
        self.charged = 0

    def read(self, root, relative_path, max_bytes):
        """read_file_bound under the budget. Raises ValidationError (exhausted or read failure)."""
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_SAFE_READ_BYTES:
            raise ValidationError(f"max_bytes must be a positive integer <= {MAX_SAFE_READ_BYTES}")
        reserved = min(self.remaining, max_bytes + 1)
        allowed = reserved - 1
        if allowed < 1:
            raise ValidationError("read budget exhausted")
        self.remaining -= reserved
        self.charged += reserved
        result = read_file_bound(root, relative_path, allowed)  # failure: reservation stays charged
        refund = reserved - result["size_bytes"]
        self.remaining += refund
        self.charged -= refund
        return result


def _source_parent_problems(sources):
    """Unique ids, existing non-self parents and no parent cycles; raise on any violation."""
    ids = {}
    for source in sources:
        if source["id"] in ids:
            raise ValidationError("duplicate source id in bundle")
        ids[source["id"]] = source["parent_source_id"]
    for source_id, parent in ids.items():
        if parent is not None and (parent == source_id or parent not in ids):
            raise ValidationError("source parent_source_id is missing or self-referential")
    for source_id in ids:
        seen, current = set(), source_id
        while current is not None:
            if current in seen:
                raise ValidationError("source parent_source_id chain contains a cycle")
            seen.add(current)
            current = ids[current]


def _normalize_literal_text(raw):
    """nfc-lf-utf8-v1: strict UTF-8 of the whole artifact, CRLF/CR -> LF, then NFC. Returns (text, reason)."""
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, "artifact_not_utf8"
    if len(text) > TEXT_SOURCE_MAX_CHARS:
        return None, "text_too_long"
    run = 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Mc", "Me"):
            run += 1
            if run > _SOURCE_MAX_COMBINING_RUN:
                return None, "combining_run_too_long"
        else:
            run = 0
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    if len(text) > TEXT_SOURCE_MAX_CHARS:
        return None, "text_too_long"
    return text, None


def verify_sources(bundle, artifact_root, budget=None):
    """Bind literal text sources to locally read bytes; report, never repair.

    Returns {"sources": {id: entry}, "bytes_read": int, "failures": [codes]}.
    entry = {"source": deep copy of the original record, "verified": bool,
    "normalized_text": str | None, "reasons": [codes], "artifact_sha256":
    actual | None, "normalized_text_sha256": actual | None}. Only
    normalization_version 'nfc-lf-utf8-v1' over UTF-8 artifacts is supported;
    anything else stays unverified. verified=True means the bytes and
    normalized text match and the record says complete; it says nothing about
    coverage, applicability, dates or freshness.
    """
    validate_document(bundle, "source.json")
    if type(artifact_root) is not str:
        raise ValidationError("artifact_root must be a string path")
    if budget is None:
        budget = ReadBudget()
    elif type(budget) is not ReadBudget:
        raise ValidationError("budget must be a ReadBudget")
    sources = bundle["sources"]
    _source_parent_problems(sources)
    charged_before = budget.charged
    results, failures = {}, []

    def budget_read(relative_path, reasons, code):
        # exhausted: nothing can be read; limited: the cap was lowered by the budget, so a
        # failure may be budget-caused rather than a property of the file.
        if budget.remaining < 2:
            budget_code = "read_budget_exhausted"
        elif budget.remaining < TEXT_SOURCE_MAX_BYTES + 1:
            budget_code = "read_budget_limited"
        else:
            budget_code = None
        try:
            return budget.read(artifact_root, relative_path, TEXT_SOURCE_MAX_BYTES)
        except ValidationError:
            reasons.append(budget_code or code)
            if budget_code and budget_code not in failures:
                failures.append(budget_code)
            return None

    for source in sources:
        reasons = []
        entry = {"source": copy.deepcopy(source), "verified": False, "normalized_text": None,
                 "reasons": reasons, "artifact_sha256": None, "normalized_text_sha256": None}
        results[source["id"]] = entry
        if source["collection_status"] != "complete":
            reasons.append("collection_" + source["collection_status"])
        if any(region["may_contain_requirements"] for region in source["unread_regions"]):
            reasons.append("unread_regions_present")
        if source["normalization_version"] != TEXT_NORMALIZATION_VERSION:
            reasons.append("normalization_unsupported")
        if source["artifact_ref"] is None:
            reasons.append("artifact_missing")
            continue
        raw = budget_read(source["artifact_ref"], reasons, "artifact_unreadable")
        if raw is None:
            continue
        entry["artifact_sha256"] = raw["sha256"]
        bytes_ok = True
        if source["artifact_sha256"] != raw["sha256"]:
            reasons.append("artifact_hash_undeclared" if source["artifact_sha256"] is None else "artifact_hash_mismatch")
            bytes_ok = False
        if source["original_bytes_sha256"] is not None and source["original_bytes_sha256"] != raw["sha256"]:
            reasons.append("original_bytes_hash_mismatch")
            bytes_ok = False
        if source["normalized_text_ref"] is None:
            reasons.append("normalized_text_missing")
            continue
        stored = budget_read(source["normalized_text_ref"], reasons, "normalized_text_unreadable")
        if stored is None:
            continue
        entry["normalized_text_sha256"] = stored["sha256"]
        if source["normalized_text_sha256"] != stored["sha256"]:
            reasons.append("normalized_text_hash_undeclared" if source["normalized_text_sha256"] is None
                           else "normalized_text_hash_mismatch")
            bytes_ok = False
        if source["normalization_version"] != TEXT_NORMALIZATION_VERSION:
            continue
        text, problem = _normalize_literal_text(raw["data"])
        if problem:
            reasons.append(problem)
            continue
        if text.encode("utf-8") != stored["data"]:
            reasons.append("normalized_text_mismatch")
            continue
        if bytes_ok:
            entry["normalized_text"] = text
            entry["verified"] = not reasons
    return {"sources": results, "bytes_read": budget.charged - charged_before, "failures": failures}


# ---------------------------------------------------------------------------
# Explicit selection binding (no selector matching or evaluation here)
# ---------------------------------------------------------------------------

SELECTION_MAX_FILE_BYTES = MAX_SAFE_READ_BYTES
_PARSER_NAMES = frozenset({"pdf", "docx", "pptx", "xlsx", "zip", "text"})
_DETECTED_FORMATS = frozenset({"pdf", "docx", "pptx", "xlsx", "zip", "text", "hwp", "hwpx", "png", "jpeg", "empty", "unknown"})
_UNREAD_KINDS = frozenset({"image", "scanned_page", "embedded_object", "encrypted", "truncated",
                           "budget_limit", "unsupported_format", "parse_error", "other"})
_LOCATION_LIMITS = {"page": (1, 100000), "paragraph": (1, 1000000), "table": (1, 100000), "row": (1, 100000),
                    "column": (1, 10000), "char_start": (0, 2147483647), "char_end": (0, 2147483647)}
# Same grammar as the schemas' rel_path definition.
_SCHEMA_REL_PATH_RE = re.compile(r'(?!/)(?![A-Za-z]:)(?!.*//)(?!.*/$)(?!(?:.*/)?\.{1,2}(?:/|$))[^\\:*?"<>|\x00-\x1f]{1,1024}')


def _now_rfc3339():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _public_location(location):
    if type(location) is not dict:
        return None
    out = {}
    for key, (low, high) in _LOCATION_LIMITS.items():
        value = location.get(key)
        if type(value) is int and low <= value <= high:
            out[key] = value
    label = location.get("label")
    if type(label) is str and label:
        out["label"] = label[:256]
    return out or None


def _public_archive_entry(entry):
    name = entry.get("name") if type(entry.get("name")) is str else ""
    path = entry.get("entry_path")
    safe = type(path) is str and _SCHEMA_REL_PATH_RE.fullmatch(path) is not None and not entry.get("unsafe_name")
    sha = entry.get("sha256")
    return {
        "name": name[:1024] or "(empty name)",
        "entry_path": path if safe else None,
        "is_dir": bool(entry.get("is_dir")),
        "compressed_size": max(0, int(entry.get("compressed_size") or 0)),
        "declared_size": max(0, int(entry.get("declared_size") or 0)),
        "bytes_read": max(0, int(entry.get("bytes_read") or 0)),
        "crc_ok": entry.get("crc_ok") if type(entry.get("crc_ok")) is bool else None,
        "encrypted": bool(entry.get("encrypted")),
        "nested_archive": bool(entry.get("nested_archive")),
        "unsafe_name": not safe,
        "sha256": sha if type(sha) is str and re.fullmatch(r"[0-9a-f]{64}", sha) else None,
    }


def _public_parser(result):
    """Map raw parse_bytes facts onto checks.$defs.parser without upgrading anything."""
    fmt = result.get("format")
    status = result.get("status")
    reason = result.get("reason") or ""
    unread = []
    for region in (result.get("unread_regions") or [])[:1000]:
        kind = region.get("kind") if region.get("kind") in _UNREAD_KINDS else "other"
        detail = region.get("detail")
        unread.append({"kind": kind, "location": _public_location(region.get("location")),
                       "detail": detail[:2048] if type(detail) is str else None})
    kinds = {region["kind"] for region in unread}
    if status == "complete":
        parser_status = "ok"
    elif status == "failed":
        parser_status = "failed"
    elif "timed out" in reason:
        parser_status = "timeout"
    elif "encrypted" in kinds:
        parser_status = "encrypted"
    elif fmt not in _PARSER_NAMES or ("unsupported_format" in kinds and not result.get("text")):
        parser_status = "unsupported"
    elif "budget_limit" in kinds:
        parser_status = "budget_exceeded"
    else:
        parser_status = "partial"
    limits = set()
    texts = [reason] + [region["detail"] or "" for region in unread if region["kind"] == "budget_limit"]
    for text in texts:
        if "timed out" in text or "time budget" in text:
            limits.add("time")
        if "uncompressed" in text:
            limits.add("zip_uncompressed_bytes")
        if "entries" in text and "budget" in text:
            limits.add("zip_entries")
        if "pages" in text and "budget" in text:
            limits.add("pages")
        if "exceeds parser budget" in text:
            limits.add("file_bytes")
        if "IPC" in text:
            limits.add("worker_output")
    errors = []
    if parser_status in ("failed", "timeout"):
        errors.append({"code": "parser_timeout" if parser_status == "timeout" else "parser_failed",
                       "detail": reason[:2048] or None})
    metrics = result.get("metrics") or {}

    def count(key, value=None):
        value = metrics.get(key) if value is None else value
        return value if type(value) is int and 0 <= value <= 1000000000000 else None

    encoding = metrics.get("encoding")
    return {
        "name": fmt if fmt in _PARSER_NAMES else "none",
        "status": parser_status,
        "metrics": {
            "pages": count("pages", result.get("pages")),
            "text_chars": count("text_chars"),
            "text_chars_no_whitespace": count("text_chars_no_whitespace"),
            "text_words": count("text_words"),
            "paragraphs": count("paragraphs"),
            "tables": count("tables"),
            "images": count("images"),
            "entries": count("entries"),
            "uncompressed_bytes_read": count("uncompressed_bytes_read"),
            "encoding": encoding if type(encoding) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", encoding) else None,
        },
        "unread_regions": unread,
        "limits_hit": sorted(limits),
        "errors": errors,
    }


def _not_run_parser():
    return {"name": "none", "status": "not_run", "metrics": {key: None for key in (
        "pages", "text_chars", "text_chars_no_whitespace", "text_words", "paragraphs", "tables",
        "images", "entries", "uncompressed_bytes_read", "encoding")},
        "unread_regions": [], "limits_hit": [], "errors": []}


def _selection_semantics(selection):
    """Validate the fragment with the packaged schema plus duplicate/collision/overlap rules."""
    validate_document({"schema_version": "1.0", "requirements": [], "selection": selection}, "requirements.json")
    ids, keys = set(), set()
    for item in selection["files"]:
        key = unicodedata.normalize("NFC", item["path"]).casefold()
        if item["file_id"] in ids:
            raise ValidationError("duplicate file_id in selection")
        if key in keys:
            raise ValidationError("duplicate or case-colliding path in selection")
        ids.add(item["file_id"])
        keys.add(key)
    for item in selection["excluded"]:
        if unicodedata.normalize("NFC", item["path"]).casefold() in keys:
            raise ValidationError("a selected path is also listed as excluded")


def bind_selection(selection, submission_root, budget=None):
    """Bind explicitly selected files to safely read bytes and one parser run each.

    Returns {"selection", "files", "parsed", "failures", "bytes_read"}:
    selection matches checks.$defs.selection, files contains checks
    file_record objects only for files actually read, parsed maps file_id to
    private parser output (text stays internal), failures lists
    {"file_id", "code"} facts. A read failure never implies the file is
    missing: such files keep their declared id/path with a null hash and no
    file_record. Every file is re-read after parsing through the same budget;
    any difference invalidates the binding. Selector matching, requirement
    evaluation and verdicts are not done here.
    """
    _selection_semantics(selection)
    if type(submission_root) is not str:
        raise ValidationError("submission_root must be a string path")
    if budget is None:
        budget = ReadBudget()
    elif type(budget) is not ReadBudget:
        raise ValidationError("budget must be a ReadBudget")
    from .parsers import parse_bytes  # lazy: the parser spawns worker processes

    charged_before = budget.charged
    public_files, records, parsed, failures, problems = [], [], {}, [], []
    if not selection["resolved"]:
        problems.append("input selection not resolved")
    if selection["unresolved_candidates"]:
        problems.append("ambiguous candidates remain")
        failures.append({"file_id": None, "code": "unresolved_candidates"})

    def safe_read(path):
        limited = budget.remaining < SELECTION_MAX_FILE_BYTES + 1
        try:
            return budget.read(submission_root, path, SELECTION_MAX_FILE_BYTES), None
        except ValidationError:
            return None, "read_budget_limited" if limited else "read_unverified"

    for item in selection["files"]:
        file_id, path = item["file_id"], item["path"]
        public = {"file_id": file_id, "path": path, "role": item["role"], "sha256": None}
        public_files.append(public)
        started = _now_rfc3339()
        first, code = safe_read(path)
        if first is None:
            failures.append({"file_id": file_id, "code": code})  # unknown, never "missing"
            continue
        before = {"size": first["stat_before"]["size_bytes"], "mtime_ns": first["stat_before"]["mtime_ns"],
                  "sha256": first["sha256"]}
        ext = path.rsplit("/", 1)[-1]
        ext = ext[ext.rfind("."):].lower() if ext.rfind(".") > 0 else None
        record = {"file_id": file_id, "path": path, "role": item["role"], "exists": True,
                  "sha256": first["sha256"], "bytes": first["size_bytes"], "detected_format": "unknown",
                  "declared_extension": ext if ext and len(ext) <= 32 else None,
                  "inspection_started_at": started, "inspection_finished_at": None,
                  "before": before, "after": None, "changed_during_inspection": False,
                  "parser": _not_run_parser(), "archive_entries": []}
        records.append(record)
        if item.get("sha256") is not None and item["sha256"] != first["sha256"]:
            failures.append({"file_id": file_id, "code": "sha256_mismatch"})
            record["inspection_finished_at"] = _now_rfc3339()
            continue
        result = parse_bytes(first["data"], path.rsplit("/", 1)[-1])
        first = dict(first, data=None)  # drop the bytes as soon as parsing is done
        fmt = result.get("format")
        record["detected_format"] = fmt if fmt in _DETECTED_FORMATS else "unknown"
        record["parser"] = _public_parser(result)
        record["archive_entries"] = [_public_archive_entry(e) for e in (result.get("archive_entries") or [])[:1000]]
        second, code = safe_read(path)
        record["inspection_finished_at"] = _now_rfc3339()
        if second is None:
            failures.append({"file_id": file_id, "code": "post_parse_" + code})
            continue
        record["after"] = {"size": second["stat_after"]["size_bytes"], "mtime_ns": second["stat_after"]["mtime_ns"],
                           "sha256": second["sha256"]}
        unchanged = (second["sha256"] == first["sha256"] and second["file_identity"] == first["file_identity"]
                     and second["root_identity"] == first["root_identity"]
                     and second["stat_before"] == first["stat_after"])
        if not unchanged:
            record["changed_during_inspection"] = True
            failures.append({"file_id": file_id, "code": "changed_during_inspection"})
            continue
        public["sha256"] = first["sha256"]
        parsed[file_id] = {"sha256": first["sha256"], "result": result}

    manifest = None
    if public_files and all(item["sha256"] for item in public_files):
        canonical = json.dumps(sorted(public_files, key=lambda item: item["file_id"]), ensure_ascii=False,
                               sort_keys=True, separators=(",", ":"), allow_nan=False)
        manifest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        claimed = selection["manifest_sha256"]
        if claimed is not None and claimed != manifest:
            problems.append("manifest hash mismatch")
            failures.append({"file_id": None, "code": "manifest_mismatch"})
    elif not public_files:
        problems.append("no files selected")
    else:
        problems.append("one or more selected files not bound")
    public_selection = {"resolved": manifest is not None and not problems, "manifest_sha256": manifest,
                        "files": public_files, "unresolved_reason": "; ".join(problems)[:2048] or None}
    return {"selection": public_selection, "files": records, "parsed": parsed, "failures": failures,
            "bytes_read": budget.charged - charged_before}


# ---------------------------------------------------------------------------
# Requirement evidence, scope and supersession binding (no file evaluation)
# ---------------------------------------------------------------------------

_DISCOVERY_ONLY_KINDS = frozenset({"email", "email_attachment"})
_SCOPE_FIELDS = ("course_id", "assignment_id", "section_id", "audience")


def _bound_text(source_id, bundle_sources, binding):
    """Return (text, record, reasons) for a source usable as literal evidence."""
    record = bundle_sources.get(source_id)
    entry = binding.get(source_id)
    if record is None or entry is None:
        return None, record, ["source_missing"]
    reasons = []
    if type(entry) is not dict or entry.get("source") != record:
        return None, record, ["source_binding_mismatch"]
    text, digest = entry.get("normalized_text"), entry.get("normalized_text_sha256")
    if entry.get("verified") is not True or type(text) is not str or type(digest) is not str:
        return None, record, ["source_unverified"]
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest or record["normalized_text_sha256"] != digest:
        return None, record, ["source_hash_mismatch"]
    if record["kind"] in _DISCOVERY_ONLY_KINDS:
        reasons.append("discovery_only_source")
    return text, record, reasons


def _quote_matches(text, quote, span):
    start, end = span["start"], span["end"]
    return type(start) is int and type(end) is int and 0 <= start < end <= len(text) and text[start:end] == quote


def _derive_applicability(assignment, source_record, requirement):
    """applies only with established alignment; explicit difference -> not_applicable; else unknown."""
    claimed = requirement["applicability"]
    reasons, verdicts = [], []
    for field in _SCOPE_FIELDS:
        values = {v for v in (claimed[field], source_record[field] if source_record else None) if v is not None}
        if len(values) > 1:
            reasons.append("scope_conflict_" + field)
            verdicts.append("unknown")
            continue
        value = next(iter(values), None)
        expected = assignment[field]
        if field in ("course_id", "assignment_id"):
            if value is None:
                reasons.append(field + "_unverified")  # course-wide rows have no explicit marker yet
                verdicts.append("unknown")
            else:
                verdicts.append("applies" if value == expected else "not_applicable")
        elif field == "section_id":
            if value is None:
                verdicts.append("applies")  # bound through the matching assignment_id
            elif expected is None:
                reasons.append("section_unverified")
                verdicts.append("unknown")
            else:
                verdicts.append("applies" if value == expected else "not_applicable")
        else:
            if value is None or value == expected:
                verdicts.append("applies")
            else:
                reasons.append("audience_unverified")  # membership (extension, group) cannot be proven here
                verdicts.append("unknown")
    if "not_applicable" in verdicts:
        result = "not_applicable"
    elif "unknown" in verdicts:
        result = "unknown"
    else:
        result = "applies"
    claims = {claimed["status"], source_record["applicability"] if source_record else "unknown"}
    if result == "applies" and claims != {"applies"}:
        reasons.append("applicability_claim_disagrees")
        result = "unknown"
    return result, reasons


def _target_signature(row):
    return (row["axis"], row["kind"], row["target_scope"], json.dumps(row["selector"], sort_keys=True),
            json.dumps(row["cardinality"], sort_keys=True), row["quantifier"])


def verify_requirements(requirements_doc, bundle, source_binding):
    """Bind each requirement to verified literal evidence, derived scope and verified supersession.

    Returns {"requirements": {rid: {"requirement": deep copy, "evidence_verified": bool,
    "applicability": "applies"|"not_applicable"|"unknown", "active": True|False|None,
    "reasons": [codes]}}, "failures": [codes]}. Every row is kept. Nothing here
    resolves targets, executes operators or decides readiness; ambiguous/conflict
    states and discovery-only (mail) evidence stay blocked via reasons.
    """
    validate_document(requirements_doc, "requirements.json")
    validate_document(bundle, "source.json")
    if type(source_binding) is not dict or type(source_binding.get("sources")) is not dict:
        raise ValidationError("source_binding must be the result of verify_sources")
    rows = requirements_doc["requirements"]
    by_id = {}
    for row in rows:
        if row["id"] in by_id:
            raise ValidationError("duplicate requirement id")
        by_id[row["id"]] = row
    bundle_sources = {}
    for record in bundle["sources"]:
        if record["id"] in bundle_sources:
            raise ValidationError("duplicate source id in bundle")
        bundle_sources[record["id"]] = record
    binding = source_binding["sources"]
    failures = []
    if set(binding) != set(bundle_sources):
        failures.append("source_binding_incomplete")
    if not rows:
        failures.append("no_requirements")

    out = {}
    for row in rows:
        reasons = []
        text, record, source_reasons = _bound_text(row["source_id"], bundle_sources, binding)
        reasons.extend(source_reasons)
        if text is not None and not _quote_matches(text, row["quote"], row["span"]):
            reasons.append("quote_span_mismatch")
        evidence = text is not None and not reasons
        applicability, scope_reasons = _derive_applicability(bundle["assignment"], record, row)
        reasons.extend(scope_reasons)
        if row["state"] in ("ambiguous", "conflict"):
            reasons.append("state_" + row["state"])
        out[row["id"]] = {"requirement": copy.deepcopy(row), "evidence_verified": evidence,
                          "applicability": applicability, "active": True, "reasons": reasons}

    # Declared supersession edges new -> old, each verified independently.
    incoming = {rid: [] for rid in by_id}
    edges = {rid: [] for rid in by_id}
    for row in rows:
        for claim in row["supersedes"]:
            old_id = claim["requirement_id"]
            if old_id not in by_id or old_id == row["id"]:
                out[row["id"]]["reasons"].append("correction_target_missing")
                continue
            edges[row["id"]].append(old_id)
            new_entry, old_entry = out[row["id"]], out[old_id]
            # Only a confirmed successor may suppress; ambiguous/conflict/superseded never qualify.
            ok = row["state"] == "confirmed"
            ok = ok and new_entry["evidence_verified"] and old_entry["evidence_verified"]
            ok = ok and new_entry["applicability"] == "applies" and old_entry["applicability"] == "applies"
            ok = ok and claim["scope_matched"] is True
            old_row = by_id[old_id]
            ok = ok and all(row["applicability"][f] == old_row["applicability"][f] for f in _SCOPE_FIELDS)
            ok = ok and _target_signature(row) == _target_signature(old_row)
            c_text, c_record, c_reasons = _bound_text(claim["correction_source_id"], bundle_sources, binding)
            ok = ok and c_text is not None and not c_reasons
            ok = ok and _quote_matches(c_text, claim["correction_quote"], claim["correction_span"])
            # The correction source's OWN ids must match the selected assignment (never filled from
            # the new row's claims); then section/audience/claims must align via the shared helper.
            assignment = bundle["assignment"]
            ok = ok and c_record is not None
            ok = ok and c_record["course_id"] is not None and c_record["course_id"] == assignment["course_id"]
            ok = ok and c_record["assignment_id"] is not None and c_record["assignment_id"] == assignment["assignment_id"]
            ok = ok and _derive_applicability(assignment, c_record, row)[0] == "applies"
            if not ok:
                new_entry["reasons"].append("correction_unverified")
            incoming[old_id].append((row["id"], ok))

    # Cycle detection over all declared edges (iterative DFS colouring).
    in_cycle, colour = set(), {rid: 0 for rid in by_id}
    for start in by_id:
        if colour[start]:
            continue
        stack, path = [(start, iter(edges[start]))], [start]
        colour[start] = 1
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                colour[node] = 2
                stack.pop()
                path.pop()
            elif colour[child] == 1:
                in_cycle.update(path[path.index(child):])
            elif colour[child] == 0:
                colour[child] = 1
                stack.append((child, iter(edges[child])))
                path.append(child)

    for rid, row in by_id.items():
        entry, claims = out[rid], incoming[rid]
        if rid in in_cycle:
            entry["active"] = None
            entry["reasons"].append("supersession_cycle")
        elif not claims:
            if row["state"] == "superseded":
                entry["active"] = None
                entry["reasons"].append("supersession_unverified")
        elif len(claims) > 1:
            entry["active"] = None
            entry["reasons"].append("supersession_ambiguous")
        elif not claims[0][1]:
            entry["active"] = None
            entry["reasons"].append("supersession_unverified")
        elif row["state"] != "superseded":
            entry["active"] = None
            entry["reasons"].append("supersession_state_inconsistent")
        else:
            entry["active"] = False
            entry["reasons"].append("superseded")
        entry["reasons"] = list(dict.fromkeys(entry["reasons"]))[:32]
    return {"requirements": out, "failures": failures}


# ---------------------------------------------------------------------------
# Explicit target resolution (no operator evaluation or verdict)
# ---------------------------------------------------------------------------

_EMPTY_SELECTION = {"resolved": False, "files": [], "excluded": [], "unresolved_candidates": [], "manifest_sha256": None}


def _segment_match(pattern, name):
    """Case-sensitive wildcard match of one path segment ('*' and '?' only), O(len*len), no regex."""
    p = n = 0
    star, mark = -1, 0
    while n < len(name):
        if p < len(pattern) and (pattern[p] == "?" or pattern[p] == name[n]):
            p += 1
            n += 1
        elif p < len(pattern) and pattern[p] == "*":
            star, mark = p, n
            p += 1
        elif star != -1:
            p = star + 1
            mark += 1
            n = mark
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def _glob_match(pattern, path):
    if ("**" in pattern or any(ch in pattern for ch in "[]{}\\:") or pattern.startswith("/")
            or any(seg in ("", ".", "..") for seg in pattern.split("/"))):
        raise ValidationError("unsafe glob pattern")
    pattern_parts, path_parts = pattern.split("/"), path.split("/")
    return len(pattern_parts) == len(path_parts) and all(
        _segment_match(pat, seg) for pat, seg in zip(pattern_parts, path_parts))


def _file_matches(selector, item):
    mode = selector["mode"]
    basename = item["path"].rsplit("/", 1)[-1]
    if mode == "all":
        return True
    if mode == "role":
        return item["role"] == selector["role"]
    if mode == "exact_name":
        return basename == selector["exact_name"]
    if mode == "safe_glob":
        return _glob_match(selector["safe_glob"], item["path"])
    if mode == "archive_entry":  # selects the outer container
        if selector["archive_name"] is not None and basename != selector["archive_name"]:
            return False
        return selector["role"] is None or item["role"] == selector["role"]
    return False


def _cardinality_status(cardinality, count):
    if count is None:
        return "unknown"
    high = cardinality["max"]
    return "within" if cardinality["min"] <= count and (high is None or count <= high) else "outside"


def _unresolved(code, reason):
    return {"type": "unresolved", "reason_code": code, "reason": reason[:2048]}


def _entry_verified(entry):
    return (type(entry) is dict and not entry.get("unsafe_name") and not entry.get("encrypted")
            and not entry.get("is_dir") and entry.get("crc_ok") is True
            and type(entry.get("sha256")) is str and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is not None
            and type(entry.get("bytes_read")) is int and entry.get("bytes_read") == entry.get("declared_size"))


def resolve_targets(requirement, selection_binding):
    """Resolve one requirement's selector against an explicit bind_selection result.

    Returns {"target", "candidates", "count", "cardinality_status", "reasons"}.
    target fits checks.$defs.target (resolved_files / submission_set /
    missing_target / unresolved); candidates are internal bound facts
    including the private parser result. Absence is claimed only when it can
    be proved from the explicit selection (and, for archive entries, from a
    container whose parse status is complete); otherwise the target is
    unresolved and count is None. No operator is evaluated here.
    """
    validate_document({"schema_version": "1.0", "requirements": [requirement], "selection": _EMPTY_SELECTION},
                      "requirements.json")
    cardinality, selector, scope = requirement["cardinality"], requirement["selector"], requirement["target_scope"]
    if cardinality["max"] is not None and cardinality["min"] > cardinality["max"]:
        raise ValidationError("cardinality min is greater than max")
    if (type(selection_binding) is not dict or type(selection_binding.get("selection")) is not dict
            or type(selection_binding["selection"].get("files")) is not list
            or type(selection_binding.get("parsed")) is not dict or type(selection_binding.get("failures")) is not list):
        raise ValidationError("selection_binding must be the result of bind_selection")
    public = selection_binding["selection"]
    parsed = selection_binding["parsed"]
    declared_complete = (not any(f.get("code") == "unresolved_candidates" for f in selection_binding["failures"]
                                 if type(f) is dict)
                         and "input selection not resolved" not in (public.get("unresolved_reason") or ""))
    reasons, candidates = [], []
    bound_matches, unbound_matches = [], []
    for item in public["files"]:
        if not _file_matches(selector, item):
            continue
        private = parsed.get(item["file_id"])
        if (type(item.get("sha256")) is str and type(private) is dict and private.get("sha256") == item["sha256"]
                and type(private.get("result")) is dict):
            bound_matches.append((item, private["result"]))
        else:
            unbound_matches.append(item)

    def done(target, count):
        return {"target": target, "candidates": candidates, "count": count,
                "cardinality_status": _cardinality_status(cardinality, count),
                "reasons": list(dict.fromkeys(reasons))[:32]}

    if unbound_matches:
        reasons.append("candidate_unbound")
    if not declared_complete:
        reasons.append("selection_declaration_incomplete")

    if scope == "submission_set":
        manifest = public.get("manifest_sha256")
        if public.get("resolved") is not True or type(manifest) is not str or unbound_matches or not declared_complete:
            reasons.append("selection_unresolved")
            return done(_unresolved("selection_unresolved", "submission set is not fully bound"), None)
        for item, result in bound_matches:
            candidates.append({"file_id": item["file_id"], "path": item["path"], "role": item["role"],
                               "sha256": item["sha256"], "entry_path": None, "entry": None, "result": result})
        file_ids = [item["file_id"] for item, _ in bound_matches]
        return done({"type": "submission_set", "manifest_sha256": manifest, "file_ids": file_ids,
                     "count": len(file_ids)}, len(file_ids))

    if scope == "file":
        for item, result in bound_matches:
            candidates.append({"file_id": item["file_id"], "path": item["path"], "role": item["role"],
                               "sha256": item["sha256"], "entry_path": None, "entry": None, "result": result})
        if unbound_matches or not declared_complete:
            return done(_unresolved("selection_unresolved", "a possibly matching selected file is not bound"), None)
        if not candidates:
            reasons.append("no_matching_selected_file")
            return done({"type": "missing_target", "scope": "file",
                         "expected_role": selector["role"] if selector["mode"] == "role" else None,
                         "expected_name": selector["exact_name"] if selector["mode"] == "exact_name" else None,
                         "expected_path": None, "container": None}, 0)
        return done({"type": "resolved_files", "scope": "file", "files": [
            {"file_id": c["file_id"], "path": c["path"], "sha256": c["sha256"], "entry_path": None,
             "entry_sha256": None} for c in candidates]}, len(candidates))

    # archive_entry: exact outer container(s), exact internal path.
    entry_path = selector["entry_path"]
    if unbound_matches or not declared_complete:
        return done(_unresolved("selection_unresolved", "a possibly matching archive is not bound"), None)
    if not bound_matches:
        reasons.append("no_matching_container")
        return done({"type": "missing_target", "scope": "archive_entry",
                     "expected_role": selector["role"], "expected_name": selector["archive_name"],
                     "expected_path": entry_path, "container": None}, 0)
    unknown = None
    for item, result in bound_matches:
        entries = result.get("archive_entries") if type(result.get("archive_entries")) is list else []
        found = False
        for entry in entries:
            if type(entry) is not dict:
                continue
            raw_name = entry.get("name") if type(entry.get("name")) is str else ""
            if entry.get("entry_path") == entry_path:
                found = True
                if _entry_verified(entry):
                    candidates.append({"file_id": item["file_id"], "path": item["path"], "role": item["role"],
                                       "sha256": item["sha256"], "entry_path": entry_path,
                                       "entry": copy.deepcopy(entry), "result": result})
                else:
                    unknown = unknown or ("encrypted" if entry.get("encrypted") else "unread_region",
                                          "matching archive entry not fully read and CRC-verified")
            elif entry.get("unsafe_name") and raw_name.rstrip("/") == entry_path:
                found = True
                unknown = unknown or ("unsafe_archive_entry", "an unsafe archive entry may be the target")
        if not found and result.get("status") != "complete":
            code = "parser_failed" if result.get("status") == "failed" else "unread_region"
            unknown = unknown or (code, "archive not completely inspected; absence of the entry cannot be proved")
    if unknown is not None:
        reasons.append("archive_entry_unverified")
        return done(_unresolved(unknown[0], unknown[1]), None)
    if not candidates:
        reasons.append("archive_entry_absent")
        container = None
        if len(bound_matches) == 1:
            container = {"file_id": bound_matches[0][0]["file_id"], "sha256": bound_matches[0][0]["sha256"]}
        return done({"type": "missing_target", "scope": "archive_entry", "expected_role": selector["role"],
                     "expected_name": selector["archive_name"], "expected_path": entry_path,
                     "container": container}, 0)
    return done({"type": "resolved_files", "scope": "archive_entry", "files": [
        {"file_id": c["file_id"], "path": c["path"], "sha256": c["sha256"], "entry_path": entry_path,
         "entry_sha256": c["entry"]["sha256"]} for c in candidates]}, len(candidates))


# ---------------------------------------------------------------------------
# Mechanical condition evaluation (content/deadline unsupported here)
# ---------------------------------------------------------------------------

_NUMERIC_OPS = frozenset({"eq", "lt", "lte", "gt", "gte"})
_ALLOWED_OPS = {"exists": {"exists"}, "count": _NUMERIC_OPS, "filename": {"eq", "contains"}, "extension": {"eq"},
                "format": {"eq"}, "size_bytes": _NUMERIC_OPS, "pages": _NUMERIC_OPS, "text_chars": _NUMERIC_OPS,
                "text_words": _NUMERIC_OPS, "content": {"contains"}, "deadline": {"lte"}}
_UNIT_BYTES = {"bytes": [Fraction(1)], "KiB": [Fraction(1024)], "MiB": [Fraction(1048576)],
               "MB_unspecified": [Fraction(1000000), Fraction(1048576)]}
_TEXT_METRIC = {"characters": "text_chars", "characters_no_whitespace": "text_chars_no_whitespace",
                "whitespace_words": "text_words"}
_PARSER_STATUS_CODE = {"failed": "parser_failed", "timeout": "parser_timeout", "encrypted": "encrypted",
                       "budget_exceeded": "budget_exceeded", "unsupported": "unsupported_format",
                       "partial": "unread_region", "not_run": "parser_failed"}
_REASON_CODE = {"source_missing": "source_missing", "source_hash_mismatch": "source_hash_mismatch",
                "source_unverified": "source_hash_mismatch", "source_binding_mismatch": "source_hash_mismatch",
                "quote_span_mismatch": "quote_span_mismatch", "discovery_only_source": "collection_incomplete"}


def _number(value):
    """Exact Fraction for a finite int/float; None for booleans, non-finite or non-numbers."""
    if type(value) is int:
        return Fraction(value)
    if type(value) is float and math.isfinite(value):
        return Fraction(value)
    return None


def _compare(op, observed, expected):
    return {"eq": observed == expected, "lt": observed < expected, "lte": observed <= expected,
            "gt": observed > expected, "gte": observed >= expected}[op]


def _numeric_status(op, observed, expected_values):
    """pass/fail only when every interpretation of the expected value agrees; else unknown."""
    if observed is None or not expected_values or any(v is None for v in expected_values):
        return "unknown"
    outcomes = {_compare(op, observed, expected) for expected in expected_values}
    return "unknown" if len(outcomes) != 1 else ("pass" if outcomes.pop() else "fail")


def _format_status(expected, detected, parser_status):
    binary = {"pdf", "zip", "png", "jpeg", "ole_compound"}
    if expected in ("pdf", "zip", "png", "jpeg"):
        if detected == expected or (expected == "zip" and detected == "docx"):  # DOCX carries a ZIP signature
            return "pass"
        return "fail" if detected in binary | {"text", "empty", "unknown", "docx"} else "unknown"
    if expected in ("docx", "pptx", "xlsx"):
        if detected == expected:
            return "pass" if parser_status != "failed" else "unknown"
        return "fail" if detected in {"pdf", "png", "jpeg", "ole_compound", "text", "empty"} else "unknown"
    if expected == "text":
        if detected == "text":
            return "pass" if parser_status == "complete" else "unknown"
        return "fail" if detected in binary | {"docx", "empty"} else "unknown"
    # hwp/hwpx: contents are not identified by this alpha parser
    return "fail" if detected in {"pdf", "png", "jpeg", "text", "empty"} else "unknown"


def _candidate_facts(candidate, records):
    """Facts for one candidate: entry facts for archive entries, never container metrics."""
    entry = candidate["entry"]
    if entry is not None:
        content = entry.get("content") if type(entry.get("content")) is dict else None
        name = candidate["entry_path"].rsplit("/", 1)[-1]
        return {"name": name, "size": entry.get("bytes_read") if _entry_verified(entry) else None,
                "detected": content.get("format") if content else None,
                "status": content.get("status") if content else None,
                "pages": content.get("pages") if content else None,
                "metrics": content or {}, "codes": [] if content else ["unsupported_format"],
                "format_pass_blocked": not content or content.get("status") != "complete"}
    result = candidate["result"]
    record = records.get(candidate["file_id"])
    size = record["bytes"] if record and record.get("sha256") == candidate["sha256"] else None
    public = _public_parser(result)
    codes = [] if public["status"] == "ok" else [_PARSER_STATUS_CODE.get(public["status"], "other")]
    # A matching signature never passes if parsing failed, timed out, was encrypted, hit a budget
    # or was not attempted; png/jpeg have no parser, so their signature is the only fact.
    blocked = public["status"] in ("failed", "timeout", "encrypted", "budget_exceeded", "not_run") or (
        public["status"] == "unsupported" and result.get("format") not in ("png", "jpeg"))
    return {"name": candidate["path"].rsplit("/", 1)[-1], "size": size, "detected": result.get("format"),
            "status": result.get("status"), "pages": result.get("pages") if result.get("format") == "pdf" else None,
            "metrics": result.get("metrics") or {}, "codes": codes, "format_pass_blocked": blocked}


def _candidate_status(requirement, facts):
    """Return (status, observed_value, codes) for one candidate."""
    kind, op, value, unit = requirement["kind"], requirement["operator"], requirement["value"], requirement["unit"]
    if kind == "exists":
        return "pass", True, []
    if kind == "filename":
        name = facts["name"]
        if type(value) is not str:
            return "unknown", name, ["unsupported_check"]
        return ("pass" if (name == value if op == "eq" else value in name) else "fail"), name, []
    if kind == "extension":
        name = facts["name"]
        ext = name[name.rfind("."):] if name.rfind(".") > 0 else ""
        return ("pass" if ext.casefold() == str(value).casefold() else "fail"), ext, []
    if kind == "format":
        if facts["detected"] is None:
            return "unknown", None, facts["codes"] or ["unsupported_format"]
        status = _format_status(value, facts["detected"], facts["status"])
        if status == "pass" and facts["format_pass_blocked"]:
            return "unknown", facts["detected"], facts["codes"] or ["parser_failed"]
        return status, facts["detected"], ([] if status != "unknown" else facts["codes"] or ["unsupported_format"])
    if kind == "size_bytes":
        observed = _number(facts["size"])
        expected = [None if _number(value) is None else _number(value) * m for m in _UNIT_BYTES.get(unit, [])]
        status = _numeric_status(op, observed, expected)
        codes = [] if status != "unknown" else (["ambiguous_unit"] if observed is not None and unit == "MB_unspecified"
                                                else ["unread_region"])
        return status, facts["size"], codes
    if kind == "pages":
        pages = facts["pages"]
        if facts["detected"] == "docx":
            return "unknown", None, ["unsupported_check"]  # DOCX pagination is never estimated
        if type(pages) is not int:
            if facts["codes"]:
                return "unknown", None, facts["codes"]  # genuine parser failure/limits keep their codes
            # A successful parse that has no page concept (e.g. plain text) is unsupported, not failed.
            return "unknown", None, ["unsupported_check" if facts["status"] == "complete" else "parser_failed"]
        status = _numeric_status(op, Fraction(pages), [_number(value)])
        if status == "pass" and facts["status"] != "complete":
            return "unknown", pages, facts["codes"] or ["unread_region"]  # no parser-dependent pass when incomplete
        return status, pages, ([] if status != "unknown" else ["other"])
    if kind in ("text_chars", "text_words"):
        key = _TEXT_METRIC.get(unit)
        if key is None or (kind == "text_words") != (key == "text_words"):
            return "unknown", None, ["unsupported_check"]
        metric = facts["metrics"].get(key)
        if facts["status"] != "complete" or type(metric) is not int:
            return "unknown", None, facts["codes"] or ["unread_region"]
        status = _numeric_status(op, Fraction(metric), [_number(value)])
        return status, metric, ([] if status != "unknown" else ["other"])
    return "unknown", None, ["unsupported_check"]


def _content_candidates(requirement, resolution):
    """Internal literal-occurrence facts for the later content-confirmation step (never a pass)."""
    needle = requirement["value"] if type(requirement["value"]) is str else ""
    found = []
    for candidate in resolution["candidates"]:
        text = candidate["result"].get("text") if candidate["entry"] is None else None
        if not needle or type(text) is not str:
            continue
        start = text.find(needle)
        while start != -1 and len(found) < 50:
            found.append({"file_id": candidate["file_id"], "char_start": start, "char_end": start + len(needle),
                          "excerpt": text[max(0, start - 100):start + len(needle) + 100][:512]})
            start = text.find(needle, start + 1)
    return found


def _mechanical_result(status, observed=None, reason=None, codes=(), remediation=None, method="deterministic",
                       content_candidates=()):
    return {"status": status, "method": method, "observed": observed,
            "reason": None if reason is None else str(reason)[:2048],
            "unknown_codes": list(dict.fromkeys(codes))[:32] if status == "unknown" else [],
            "remediation": None if remediation is None else str(remediation)[:2048],
            "content_candidates": list(content_candidates)}


def evaluate_mechanical(requirement_entry, resolution, selection_binding):
    """Evaluate one requirement mechanically against resolved, bound targets.

    Returns {status, method, observed, reason, unknown_codes, remediation,
    content_candidates}. Gates (evidence, state, supersession, applicability,
    target) come first and keep the result unknown; not_applicable only for a
    proved different scope or a verified supersession. content and deadline
    kinds are unsupported here and stay unknown. No public check, verdict or
    report is assembled.
    """
    if type(requirement_entry) is not dict or type(requirement_entry.get("requirement")) is not dict:
        raise ValidationError("requirement_entry must come from verify_requirements")
    if type(resolution) is not dict or type(resolution.get("target")) is not dict:
        raise ValidationError("resolution must come from resolve_targets")
    if type(selection_binding) is not dict or type(selection_binding.get("parsed")) is not dict:
        raise ValidationError("selection_binding must come from bind_selection")
    requirement = requirement_entry["requirement"]
    kind, op, unit = requirement["kind"], requirement["operator"], requirement["unit"]
    target = resolution["target"]

    if requirement_entry.get("active") is False and requirement_entry.get("evidence_verified") is True:
        return _mechanical_result("not_applicable", reason="superseded by a verified explicit correction")
    if requirement_entry.get("applicability") == "not_applicable" and requirement_entry.get("evidence_verified") is True:
        return _mechanical_result("not_applicable", reason="requirement scope explicitly differs from the selected assignment")

    codes = [_REASON_CODE.get(r, "other") for r in requirement_entry.get("reasons", [])
             if r in _REASON_CODE] if requirement_entry.get("evidence_verified") is not True else []
    if requirement_entry.get("evidence_verified") is not True and not codes:
        codes.append("quote_span_mismatch")
    if requirement["state"] in ("ambiguous", "conflict") or requirement_entry.get("active") is None:
        codes.append("conflict_unresolved")
    if requirement_entry.get("applicability") != "applies":
        codes.append("applicability_unknown")
    if kind not in _ALLOWED_OPS or op not in _ALLOWED_OPS[kind]:
        codes.append("unsupported_check")
    if kind == "content":
        return _mechanical_result("unknown", reason="content presence needs specific user confirmation",
                                  codes=codes + ["requires_user_confirmation", "unsupported_check"],
                                  remediation="해당 내용이 파일의 어느 위치에 있는지 직접 확인해 주세요.",
                                  method="content_review", content_candidates=_content_candidates(requirement, resolution))
    if kind == "deadline":
        return _mechanical_result("unknown", reason="deadline evaluation is not implemented in this step",
                                  codes=codes + ["unsupported_check", "date_unresolved"],
                                  remediation="마감 시각과 제출 기간은 eTL 화면에서 직접 확인해 주세요.")
    if codes:
        return _mechanical_result("unknown", reason="evidence, state, scope or check kind not established", codes=codes,
                                  remediation="요구사항 근거(원문 인용·출처·적용 대상)를 다시 확인해 주세요.")

    observed = {"operator": op, "expected": requirement["value"], "unit": unit, "values": []}
    if target["type"] == "unresolved":
        return _mechanical_result("unknown", reason=target["reason"], codes=[target["reason_code"]],
                                  remediation="대상 파일 선택과 파일 상태를 확인해 주세요.")
    if target["type"] == "missing_target":
        expected = [v for v in (target["expected_role"], target["expected_name"], target["expected_path"]) if v]
        if requirement["cardinality"]["min"] >= 1 or kind == "exists":
            return _mechanical_result("fail", observed=observed, reason="required target is absent from the explicit selection",
                                      remediation=f"요구된 제출 대상({', '.join(expected) or '조건에 맞는 파일'})을 제출 파일 목록에 추가하세요.")
        return _mechanical_result("unknown", reason="no target to evaluate (zero allowed by cardinality)", codes=["other"])
    if resolution.get("cardinality_status") == "outside":
        card = requirement["cardinality"]
        upper = "" if card["max"] is None else ", " + str(card["max"]) + "개 이하"
        return _mechanical_result("fail", observed=observed, reason=f"{resolution['count']} matching target(s) outside cardinality",
                                  remediation=f"조건에 맞는 제출 대상을 {card['min']}개 이상{upper}로 맞추세요 "
                                              f"(현재 {resolution['count']}개).")
    if resolution.get("cardinality_status") != "within":
        return _mechanical_result("unknown", reason="target count not established", codes=["selection_unresolved"])

    public_sha = {f["file_id"]: f.get("sha256") for f in selection_binding["selection"]["files"]}
    records = {r["file_id"]: r for r in selection_binding.get("files", []) if type(r) is dict}
    parsed = selection_binding["parsed"]
    for candidate in resolution["candidates"]:
        private = parsed.get(candidate["file_id"])
        if (public_sha.get(candidate["file_id"]) != candidate["sha256"] or type(private) is not dict
                or private.get("sha256") != candidate["sha256"]):
            return _mechanical_result("unknown", reason="candidate is not bound to current bytes", codes=["file_changed"])

    if target["type"] == "submission_set":
        if kind == "count":
            status = _numeric_status(op, Fraction(target["count"]), [_number(requirement["value"])])
            observed["values"].append({"file_id": None, "entry_path": None, "value": target["count"], "status": status})
        elif kind == "size_bytes":
            sizes = [records[i]["bytes"] if i in records and records[i].get("sha256") == public_sha.get(i) else None
                     for i in target["file_ids"]]
            total = None if any(type(s) is not int for s in sizes) else sum(sizes)
            expected = [None if _number(requirement["value"]) is None else _number(requirement["value"]) * m
                        for m in _UNIT_BYTES.get(unit, [])]
            status = _numeric_status(op, _number(total), expected)
            observed["values"].append({"file_id": None, "entry_path": None, "value": total, "status": status})
        else:
            return _mechanical_result("unknown", reason="kind not supported for a submission set", codes=["unsupported_check"])
        if status == "unknown":
            return _mechanical_result("unknown", observed=observed, reason="submission set value not established",
                                      codes=["ambiguous_unit" if unit == "MB_unspecified" else "other"])
        return _mechanical_result(status, observed=observed, reason=f"submission set {kind} {op} {requirement['value']}",
                                  remediation=None if status == "pass" else
                                  f"제출 파일 전체의 {kind} 조건({op} {requirement['value']} {unit or ''})을 만족하도록 파일 구성을 수정하세요.")

    statuses, unknown_codes = [], []
    for candidate in resolution["candidates"]:
        status, value, codes = _candidate_status(requirement, _candidate_facts(candidate, records))
        statuses.append(status)
        unknown_codes.extend(codes if status == "unknown" else [])
        shown = value if value is None or type(value) in (int, float, bool) else str(value)[:1024]
        observed["values"].append({"file_id": candidate["file_id"], "entry_path": candidate["entry_path"],
                                   "value": shown, "status": status})
    quantifier = requirement["quantifier"]
    if quantifier == "exists":
        folded = "pass" if "pass" in statuses else ("fail" if statuses and all(s == "fail" for s in statuses) else "unknown")
    else:  # each / all: every candidate must pass; one proven failure fails
        folded = "fail" if "fail" in statuses else ("pass" if statuses and all(s == "pass" for s in statuses) else "unknown")
    if folded == "unknown":
        return _mechanical_result("unknown", observed=observed, reason="one or more targets could not be evaluated",
                                  codes=unknown_codes or ["other"], remediation="파일 내용이 끝까지 읽히는 형식인지 확인해 주세요.")
    failing = [v for v in observed["values"] if v["status"] == "fail"][:3]  # concrete repair targets
    remediation = None if folded == "pass" else "; ".join(
        f"{v['entry_path'] or v['file_id']}: 현재 {v['value']} — 요구 조건 {op} {requirement['value']} {unit or ''}에 맞게 수정하세요."
        for v in failing)
    return _mechanical_result(folded, observed=observed, reason=f"{kind} {op} {requirement['value']} ({quantifier})",
                              remediation=remediation)


# ---------------------------------------------------------------------------
# Thin check assembly (one schema-valid check per requirement row; no report)
# ---------------------------------------------------------------------------

_FRAGMENT_VALIDATORS = {}
_EVIDENCE_DOWNGRADE = {"source_missing": "source_missing", "source_hash_mismatch": "source_hash_mismatch",
                       "collection_incomplete": "discovery_only_source", "quote_span_mismatch": "quote_span_mismatch"}


def _fragment_validator(schema_name, def_name):
    """Validator for one $defs entry of a packaged schema (same local-only, no-retrieval rules)."""
    key = (schema_name, def_name)
    if key not in _FRAGMENT_VALIDATORS:
        base = _packaged_validator(schema_name).schema
        fragment = {"$schema": SCHEMA_DIALECT, "$ref": "#/$defs/" + def_name, "$defs": base["$defs"]}
        _check_local_refs(fragment)
        import jsonschema
        import referencing
        try:
            _FRAGMENT_VALIDATORS[key] = jsonschema.Draft202012Validator(fragment, registry=referencing.Registry())
        except Exception as exc:
            raise ValidationError(f"schema fragment {def_name} unavailable: {type(exc).__name__}") from None
    return _FRAGMENT_VALIDATORS[key]


def _validate_check(check):
    try:
        error = next(iter(_fragment_validator("checks.json", "check").iter_errors(check)), None)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(f"check validation could not complete: {type(exc).__name__}") from None
    if error is not None:
        keyword = error.validator if type(error.validator) is str else "?"
        raise ValidationError(f"check failed schema at {_error_location(error)}: {keyword[:40]}")


def _current_evidence(row, source_binding):
    """Re-verify a row's literal evidence against the current source binding. Returns (ok, code)."""
    entry = source_binding["sources"].get(row["source_id"])
    if type(entry) is not dict or type(entry.get("source")) is not dict:
        return False, "source_missing"
    record, text, digest = entry["source"], entry.get("normalized_text"), entry.get("normalized_text_sha256")
    if entry.get("verified") is not True or type(text) is not str or type(digest) is not str:
        return False, "source_hash_mismatch"
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest or record.get("normalized_text_sha256") != digest:
        return False, "source_hash_mismatch"
    if record.get("kind") in _DISCOVERY_ONLY_KINDS:
        return False, "collection_incomplete"
    if not _quote_matches(text, row["quote"], row["span"]):
        return False, "quote_span_mismatch"
    return True, None


def _public_content_candidates(internal, parsed):
    out = []
    for item in internal[:50]:
        location = {"char_start": item["char_start"], "char_end": item["char_end"]}
        private = parsed.get(item["file_id"])
        result = private.get("result") if type(private) is dict and type(private.get("result")) is dict else {}
        for loc in result.get("locations") or []:
            if (type(loc) is dict and type(loc.get("char_start")) is int and type(loc.get("char_end")) is int
                    and loc["char_start"] <= item["char_start"] < loc["char_end"]):
                for key in ("page", "paragraph", "table", "row", "column"):
                    if key in loc:
                        location[key] = loc[key]
                break
        out.append({"file_id": item["file_id"], "location": _public_location(location),
                    "excerpt": str(item["excerpt"])[:512]})
    return out


def _supersession_still_valid(old_id, requirement_binding, source_binding):
    """Re-check, against current bytes, the single edge that made old_id inactive."""
    edges = []
    for other in requirement_binding["requirements"].values():
        row = other.get("requirement") if type(other) is dict else None
        for claim in (row.get("supersedes") or []) if type(row) is dict else []:
            if type(claim) is dict and claim.get("requirement_id") == old_id:
                edges.append((row, claim))
    if len(edges) != 1:
        return False
    row, claim = edges[0]
    correction = {"source_id": claim.get("correction_source_id"), "quote": claim.get("correction_quote"),
                  "span": claim.get("correction_span")}
    return _current_evidence(row, source_binding)[0] and _current_evidence(correction, source_binding)[0]


def _check_id(requirement_id, used):
    candidate = "chk:" + requirement_id
    if len(candidate) > 128:
        candidate = "chk:" + hashlib.sha256(requirement_id.encode("utf-8")).hexdigest()[:40]
    if candidate in used:
        raise ValidationError("check_id collision")
    used.add(candidate)
    return candidate


def _fallback_unknown(check, reason):
    """Schema-valid fail-closed replacement that keeps identity and evidence fields."""
    return dict(check, status="unknown", method="deterministic", observed=None,
                target={"type": "unresolved", "reason_code": "other", "reason": reason[:2048]},
                reason=reason[:2048], unknown_codes=["other"], remediation=None, content_candidates=[])


def assemble_checks(requirement_binding, source_binding, selection_binding):
    """Return one checks.$defs.check per original requirement row, in row order.

    Evidence is re-verified against the current source binding (a stale
    evidence flag is never trusted); targets come from resolve_targets and
    results from evaluate_mechanical. Every check is schema-validated; a
    helper or validation failure becomes a schema-valid unknown check, or a
    ValidationError if even that cannot be produced. No report, verdict,
    coverage or freshness is assembled, and the list makes no readiness claim.
    """
    if type(requirement_binding) is not dict or type(requirement_binding.get("requirements")) is not dict:
        raise ValidationError("requirement_binding must come from verify_requirements")
    if type(source_binding) is not dict or type(source_binding.get("sources")) is not dict:
        raise ValidationError("source_binding must come from verify_sources")
    if type(selection_binding) is not dict or type(selection_binding.get("parsed")) is not dict:
        raise ValidationError("selection_binding must come from bind_selection")
    checks, used = [], set()
    for requirement_id, original in requirement_binding["requirements"].items():
        if type(original) is not dict or type(original.get("requirement")) is not dict:
            raise ValidationError("requirement entry malformed")
        entry = copy.deepcopy(original)
        row = entry["requirement"]
        if row.get("id") != requirement_id:
            raise ValidationError("requirement entry id mismatch")
        ok, code = _current_evidence(row, source_binding)
        if not ok:
            entry["evidence_verified"] = False
            entry["reasons"] = list(entry.get("reasons", [])) + [_EVIDENCE_DOWNGRADE[code]]
        # Current evidence must still support any supersession before not_applicable is allowed.
        if entry.get("active") is False and not _supersession_still_valid(requirement_id, requirement_binding,
                                                                          source_binding):
            entry["active"] = None
            entry["reasons"] = list(entry.get("reasons", [])) + ["supersession_unverified"]
        verified = ok and entry.get("evidence_verified") is True
        check = {
            "check_id": _check_id(requirement_id, used),
            "requirement_id": requirement_id,
            "requirement_sha256": canonical_requirement_sha256(row),
            "axis": row["axis"], "mandatory": row["mandatory"], "kind": row["kind"],
            "evidence": {"source_id": row["source_id"], "quote": row["quote"], "span": copy.deepcopy(row["span"]),
                         "location": copy.deepcopy(row["source_location"]), "verified": verified},
        }
        try:
            resolution = resolve_targets(row, selection_binding)
            mech = evaluate_mechanical(entry, resolution, selection_binding)
            check.update({"status": mech["status"], "method": mech["method"], "target": copy.deepcopy(resolution["target"]),
                          "observed": copy.deepcopy(mech["observed"]), "reason": mech["reason"],
                          "unknown_codes": list(mech["unknown_codes"]), "remediation": mech["remediation"],
                          "content_candidates": _public_content_candidates(mech["content_candidates"],
                                                                           selection_binding["parsed"])})
        except ValidationError as exc:
            checks.append(_fallback_unknown(check, f"check could not be evaluated: {exc}"))
            _validate_check(checks[-1])
            continue
        # Fail-closed consistency with the schema's implications (never promote).
        if check["status"] in ("pass", "fail") and not verified:
            check.update(status="unknown", unknown_codes=["quote_span_mismatch"], remediation=check["remediation"])
        if check["status"] == "pass" and (check["target"]["type"] not in ("resolved_files", "submission_set")
                                          or check["unknown_codes"]):
            check.update(status="unknown", unknown_codes=check["unknown_codes"] or ["other"])
        if check["status"] == "fail" and not check["remediation"]:
            check.update(status="unknown", unknown_codes=["other"])
        if check["status"] == "unknown" and not check["unknown_codes"]:
            check["unknown_codes"] = ["other"]
        if check["status"] != "unknown":
            check["unknown_codes"] = []
        try:
            _validate_check(check)
        except ValidationError as exc:
            check = _fallback_unknown(check, f"check failed schema validation: {exc}")
            _validate_check(check)
        checks.append(check)
    return checks


# ---------------------------------------------------------------------------
# Conservative report mapping (never emits ready; freshness proof not implemented)
# ---------------------------------------------------------------------------

REPORT_RULES_VERSION = "rules-0.1.0-alpha.1"
SYNTHETIC_LABEL = "가상 과제 재현"
_SELECTION_FAILURE_CODE = {"read_unverified": "selection_unresolved", "read_budget_limited": "budget_exceeded",
                           "sha256_mismatch": "file_changed", "changed_during_inspection": "file_changed",
                           "post_parse_read_unverified": "file_changed", "post_parse_read_budget_limited": "budget_exceeded",
                           "unresolved_candidates": "selection_unresolved", "manifest_mismatch": "selection_unresolved"}
_HASH_MISMATCH_REASONS = {"artifact_hash_mismatch", "artifact_hash_undeclared", "original_bytes_hash_mismatch",
                          "normalized_text_hash_mismatch", "normalized_text_hash_undeclared", "normalized_text_mismatch"}
_UNREAD_REASONS = {"artifact_missing", "artifact_unreadable", "normalized_text_missing", "normalized_text_unreadable",
                   "read_budget_exhausted", "read_budget_limited"}


def _source_summary(record, entry):
    """Public summary; hashes only from the current verified binding, never from host claims."""
    reasons = set(entry.get("reasons", [])) if type(entry) is dict else set()
    actual_artifact = entry.get("artifact_sha256") if type(entry) is dict else None
    actual_text = entry.get("normalized_text_sha256") if type(entry) is dict else None
    if (actual_artifact and actual_text and actual_artifact == record["artifact_sha256"]
            and actual_text == record["normalized_text_sha256"] and not reasons & _HASH_MISMATCH_REASONS):
        verification = "verified"
    elif reasons & _HASH_MISMATCH_REASONS:
        verification = "mismatch"
    elif record["artifact_ref"] is None or record["collection_status"] == "failed":
        verification = "not_collected"
    else:
        verification = "missing_artifact"
    verified = verification == "verified"
    return {"source_id": record["id"], "kind": record["kind"], "collection_status": record["collection_status"],
            "observed_at": record["observed_at"], "artifact_ref": record["artifact_ref"],
            "artifact_sha256": actual_artifact if verified else None,
            "normalized_text_sha256": actual_text if verified else None,
            "original_bytes_sha256": actual_artifact if verified and record["original_bytes_sha256"] is not None else None,
            "hash_verification": verification}


def _derive_coverage(bundle, binding, requirement_failures):
    """Re-derive coverage from current host records and the verified binding; never upgrade.

    Returns (coverage, gaps) where gaps are (coverage_failure_code, source_id, detail).
    The basis is the host's collection records plus local byte binding, not
    independent network proof.
    """
    claimed = bundle["coverage"]
    coverage = copy.deepcopy(claimed)
    gaps = []

    def verified(source_id):
        entry = binding.get(source_id) if type(source_id) is str else None
        return type(entry) is dict and entry.get("verified") is True

    if claimed["semester_start"] is None or not verified(claimed["semester_start_source_id"]):
        gaps.append(("semester_start_unknown", None, "semester start not established by a verified source"))
    if claimed["through_at"] is None:
        gaps.append(("other", None, "collection end time not recorded"))
    if claimed["notice_cap_reached"]:
        gaps.append(("cap_reached", None, "notice list cap reached"))
    if claimed["notice_has_more"] is not False:
        gaps.append(("has_more", None, "more notices may exist"))
    if claimed["body_truncated"]:
        gaps.append(("body_truncated", None, "a notice body was truncated"))
    if claimed["pinned_notices"] not in ("complete", "not_present"):
        gaps.append(("pinned_notice_missing", None, "pinned notices not fully collected"))
    if claimed["common_notices"] not in ("complete", "not_present"):
        gaps.append(("common_notice_missing", None, "common notices not fully collected"))
    if claimed["referenced_materials"] not in ("complete", "not_present"):
        gaps.append(("fetch_failed", None, "referenced materials not fully collected"))
    mail = claimed["mail"]  # optional discovery: not_used alone is not a gap
    if mail["status"] in ("incomplete", "failed") or mail["cap_reached"] or mail["has_more"] is True:
        gaps.append(("has_more", None, "attempted mail discovery is incomplete or capped"))
    for source_id in claimed["unread_required_sources"][:64]:
        gaps.append(("unread_region", source_id, "required source unread"))
    for failure in claimed["failures"][:64]:
        gaps.append((failure["code"], failure["source_id"], failure["detail"] or "host-recorded collection failure"))
    if "source_binding_incomplete" in requirement_failures:
        gaps.append(("other", None, "source binding does not cover every source"))
    assignment = bundle["assignment"]
    selected = [r for r in bundle["sources"] if r["kind"] == "assignment"
                and r["course_id"] == assignment["course_id"] and r["assignment_id"] == assignment["assignment_id"]]
    if not any(verified(r["id"]) for r in selected):
        gaps.append(("not_collected", None, "selected assignment detail not collected and verified"))
    for record in bundle["sources"]:
        if record["kind"] in ("email", "email_attachment", "user_confirmation"):
            continue
        if record["collection_status"] != "complete":
            gaps.append(("fetch_failed", record["id"], "source collection incomplete or failed"))
        elif not verified(record["id"]):
            gaps.append(("other", record["id"], "source bytes or normalized text not verified"))
        if any(region["may_contain_requirements"] for region in record["unread_regions"]):
            gaps.append(("unread_region", record["id"], "source has unread regions that may hold requirements"))
        if record["applicability"] == "unknown":
            gaps.append(("other", record["id"], "source applicability to the selected assignment unknown"))
    if gaps and coverage["state"] == "complete":
        coverage["state"] = "incomplete"
    if coverage["state"] == "incomplete":
        known = {(f["code"], f["source_id"]) for f in coverage["failures"]}
        for code, source_id, detail in gaps:
            if (code, source_id) not in known and len(coverage["failures"]) < 256:
                coverage["failures"].append({"code": code, "source_id": source_id, "detail": str(detail)[:2048]})
                known.add((code, source_id))
        if not gaps:
            gaps.append(("other", None, "host recorded coverage as incomplete"))
    return coverage, gaps[:256]


def _file_blockers(selection_binding):
    """Global selection/file/parser blockers, independent of individual check results."""
    blockers = []
    selection = selection_binding["selection"]
    parsed = selection_binding.get("parsed") or {}
    records = {r.get("file_id"): r for r in selection_binding["files"] if type(r) is dict}
    if not selection.get("files"):
        blockers.append(("selection_unresolved", None, "no files selected"))
    for item in selection.get("files", []):
        file_id, sha = item.get("file_id"), item.get("sha256")
        record = records.get(file_id)
        private = parsed.get(file_id)
        if type(sha) is not str or type(private) is not dict or private.get("sha256") != sha:
            blockers.append(("selection_unresolved", file_id, "selected file not bound to current bytes"))
            continue
        if (record is None or record.get("exists") is not True or record.get("sha256") != sha
                or record.get("changed_during_inspection") or type(record.get("after")) is not dict
                or record["after"].get("sha256") != sha):
            blockers.append(("file_changed", file_id, "file not stably inspected"))
            continue
        parser = record.get("parser") or {}
        status = parser.get("status")
        if status != "ok":
            blockers.append((_PARSER_STATUS_CODE.get(status, "parser_failed"), file_id, f"parser status {status}"))
        if parser.get("unread_regions"):
            blockers.append(("unread_region", file_id, "file has unread regions"))
        if parser.get("limits_hit"):
            blockers.append(("budget_exceeded", file_id, "parser budget limit reached"))
        if any(e.get("unsafe_name") or e.get("encrypted") or e.get("nested_archive") or e.get("crc_ok") is False
               for e in record.get("archive_entries", [])):
            blockers.append(("unsafe_archive_entry", file_id, "archive has unsafe, encrypted, nested or corrupt entries"))
    return blockers[:1024]


def _stale_requirement_ids(requirement_binding, changed_sources):
    """Rows whose evidence, or whose supersession edge, depends on a changed source."""
    if not changed_sources:
        return set()
    stale = set()
    for rid, entry in requirement_binding["requirements"].items():
        row = entry.get("requirement") if type(entry) is dict else None
        if type(row) is not dict:
            continue
        if row.get("source_id") in changed_sources:
            stale.add(rid)
        for claim in row.get("supersedes") or []:
            if type(claim) is dict and (row.get("source_id") in changed_sources
                                        or claim.get("correction_source_id") in changed_sources):
                stale.add(rid)
                stale.add(claim.get("requirement_id"))
    return stale


def assemble_report(bundle, source_binding, requirement_binding, selection_binding, checks, *, mode, run_id,
                    started_at, finished_at):
    """Map existing facts into one schema-valid checks.json report snapshot.

    Freshness comes from evaluate_freshness(now=finished_at); checks whose
    evidence or supersession depends on a changed source become unknown.
    Coverage is re-derived from host records and the verified binding and
    only ever downgraded; global selection/file/parser blockers are gathered
    independently of check results. Totals are recomputed: any mandatory fail
    gives needs_changes; ready only when freshness is proven fresh, coverage
    is complete, the selection is fully bound and inspected, every applicable
    mandatory check (at least one) passes and no global blocker exists;
    otherwise needs_confirmation. Window and receipt stay separate. Nothing is
    written; inputs are not modified; private parsed text never appears.
    """
    validate_document(bundle, "source.json")
    if mode != bundle["mode"]:
        raise ValidationError("mode does not match the source bundle")
    if type(source_binding) is not dict or type(source_binding.get("sources")) is not dict:
        raise ValidationError("source_binding must come from verify_sources")
    if type(requirement_binding) is not dict or type(requirement_binding.get("requirements")) is not dict:
        raise ValidationError("requirement_binding must come from verify_requirements")
    if (type(selection_binding) is not dict or type(selection_binding.get("selection")) is not dict
            or type(selection_binding.get("files")) is not list):
        raise ValidationError("selection_binding must come from bind_selection")
    if type(checks) is not list:
        raise ValidationError("checks must be the list from assemble_checks")
    binding = source_binding["sources"]
    reasons = []

    def reason(code, detail, requirement_id=None, source_id=None, file_id=None):
        if len(reasons) < 4096:
            reasons.append({"code": code, "requirement_id": requirement_id, "source_id": source_id,
                            "file_id": file_id, "detail": None if detail is None else str(detail)[:2048]})

    # Sources and coverage (coverage is never upgraded).
    summaries, unverified = [], []
    for record in bundle["sources"]:
        entry = binding.get(record["id"])
        summaries.append(_source_summary(record, entry))
        if type(entry) is not dict or entry.get("verified") is not True:
            unverified.append(record["id"])
            codes = set(entry.get("reasons", [])) if type(entry) is dict else set()
            code = ("source_missing" if type(entry) is not dict else
                    "source_hash_mismatch" if codes & _HASH_MISMATCH_REASONS else "collection_incomplete")
            reason(code, "source not verified against local bytes", source_id=record["id"])
    coverage, gaps = _derive_coverage(bundle, binding, requirement_binding.get("failures", []))
    for code, source_id, detail in gaps:
        reason("collection_incomplete", f"coverage ({code}): {detail}", source_id=source_id)

    # Requirement-level and selection-level failures.
    for code in requirement_binding.get("failures", []):
        reason({"no_requirements": "empty_requirements"}.get(code, "source_missing"), f"requirement binding: {code}")
    selection = copy.deepcopy(selection_binding["selection"])
    if selection.get("resolved") is not True:
        reason("selection_unresolved", selection.get("unresolved_reason") or "selection not resolved")
    for failure in selection_binding.get("failures", []):
        if type(failure) is dict:
            reason(_SELECTION_FAILURE_CODE.get(failure.get("code"), "other"), f"selection: {failure.get('code')}",
                   file_id=failure.get("file_id") if type(failure.get("file_id")) is str else None)
    for code, file_id, detail in _file_blockers(selection_binding):
        reason(code, detail, file_id=file_id)

    # Freshness from the completed helper; host 'fresh' claims are never taken as proof.
    fresh = evaluate_freshness(bundle, source_binding, mode=mode, now=finished_at)
    freshness = fresh["freshness"]
    for item in fresh["reasons"]:
        reason(item["code"], item["detail"], source_id=item["source_id"])
    stale = _stale_requirement_ids(requirement_binding, set(fresh["changed_source_ids"]))

    # Checks: every row kept; stale conclusions on changed sources become unknown; totals recomputed.
    public_checks = copy.deepcopy(checks)
    counts = {"pass": 0, "fail": 0, "unknown": 0, "not_applicable": 0}
    for check in public_checks:
        if type(check) is not dict or check.get("status") not in counts:
            raise ValidationError("checks contain a malformed item")
        if check.get("requirement_id") in stale and check["status"] != "unknown":
            check.update(status="unknown", unknown_codes=["source_changed"],
                         reason="source changed since the evidence was recorded; re-extract and re-check")
            check["evidence"]["verified"] = False
        elif check.get("requirement_id") in stale and "source_changed" not in check["unknown_codes"]:
            check["unknown_codes"] = (check["unknown_codes"] + ["source_changed"])[:32]
        if check.get("mandatory") is True:
            counts[check["status"]] += 1
            if check["status"] == "unknown":
                for code in check.get("unknown_codes", []):
                    reason(code, "mandatory check unknown", requirement_id=check.get("requirement_id"))
    mandatory_total = sum(counts.values())
    if not public_checks:
        reason("empty_requirements", "no requirement checks")
    if mandatory_total - counts["not_applicable"] == 0:
        reason("no_mandatory_requirements", "no applicable mandatory requirement was evaluated")
    if len(public_checks) != len(requirement_binding["requirements"]):
        reason("other", "check list does not match requirement rows")

    # Window and receipt: separate axes; kept only when their source is currently verified.
    def verified_source(source_id):
        entry = binding.get(source_id) if type(source_id) is str else None
        return type(entry) is dict and entry.get("verified") is True

    window = copy.deepcopy(bundle["submission_window"])
    if window["state"] != "unknown" and not verified_source(window["source_id"]):
        window = {"state": "unknown", "deadline_at": None, "deadline_text": None, "timezone": None, "source_id": None,
                  "reason": "window source not verified"}
    receipt = copy.deepcopy(bundle["server_receipt"])
    if receipt["status"] != "unknown" and not verified_source(receipt["source_id"]):
        receipt = {"status": "unknown", "observed_at": None, "source_id": None, "observed_filenames": [],
                   "reason": "receipt source not verified"}

    # Verdict: any mandatory fail first; ready only with no global blocker at all.
    applicable = mandatory_total - counts["not_applicable"]
    ready = (not reasons and fresh["state"] == "fresh" and coverage["state"] == "complete"
             and selection.get("resolved") is True and selection.get("files") and mode in ("synthetic", "online")
             and applicable >= 1 and counts["pass"] == applicable and counts["unknown"] == 0 and counts["fail"] == 0)
    if counts["fail"]:
        overall, label = "needs_changes", "수정 필요"
    elif ready:
        overall, label = "ready", "확인된 요구사항 기준 파일 준비됨"
    else:
        overall, label = "needs_confirmation", "확인 필요"
    freshness_note = ("출처 최신성은 호스트 수집 기록과 로컬 해시의 일치로만 확인했으며 독립적인 네트워크 재확인은 아닙니다."
                      if fresh["state"] == "fresh" else "출처 최신성은 확인되지 않았습니다(offline-preview).")
    notice = ("이 보고서는 검사 시점의 스냅샷입니다. 이후 파일·출처 변경, 마감·제출 기간, 서버 제출 상태를 반영하지 않으며 "
              "실제 제출 가능 여부나 제출 접수를 뜻하지 않습니다. " + freshness_note)
    if mode == "synthetic":
        notice = f"[{SYNTHETIC_LABEL}] 실제 과제의 준비 상태가 아닙니다. " + notice
    report = {
        "report_schema_version": "1.0", "tool_version": "0.1.0-alpha.2", "rules_version": REPORT_RULES_VERSION,
        "run_id": run_id, "generated_at": finished_at, "inspection_started_at": started_at,
        "inspection_finished_at": finished_at, "mode": mode,
        "synthetic_label": SYNTHETIC_LABEL if mode == "synthetic" else None, "snapshot_notice": notice,
        "assignment": copy.deepcopy(bundle["assignment"]), "sources": summaries, "selection": selection,
        "files": copy.deepcopy(selection_binding["files"]), "checks": public_checks, "coverage": coverage,
        "freshness": freshness,
        "verdict": {"overall": overall, "label": label,
                    "summary": (f"필수 조건 {mandatory_total}개 중 통과 {counts['pass']}, 위반 {counts['fail']}, "
                                f"확인 필요 {counts['unknown']}, 해당 없음 {counts['not_applicable']}. "
                                f"확인되지 않은 항목 {len(reasons)}건이 있습니다."),
                    "mandatory_total": mandatory_total, "mandatory_pass": counts["pass"],
                    "mandatory_fail": counts["fail"], "mandatory_unknown": counts["unknown"],
                    "mandatory_not_applicable": counts["not_applicable"],
                    "freshness": fresh["state"] if fresh["state"] in ("fresh", "changed") else "unknown"},
        "unknown_reasons": reasons, "submission_window": window, "server_receipt": receipt,
        "delta": {"previous_run_id": None, "files_added": [], "files_removed": [], "files_changed": [],
                  "files_renamed": [], "sources_added": [], "sources_removed": [], "sources_changed": [],
                  "requirements_added": [], "requirements_removed": [], "requirements_changed": [],
                  "check_status_changes": []},
    }
    validate_document(report, "checks.json")
    return report


# ---------------------------------------------------------------------------
# Calendar-aware source freshness (no READY wiring, no delta)
# ---------------------------------------------------------------------------

FRESHNESS_MAX_AGE_SECONDS = 600
_RFC3339_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})")
_FRESHNESS_EXEMPT_KINDS = frozenset({"email", "email_attachment", "user_confirmation"})


def _parse_aware(text):
    """Real calendar parse of an RFC 3339 timestamp with explicit offset; None if invalid."""
    if type(text) is not str:
        return None
    match = _RFC3339_RE.fullmatch(text)
    if match is None:
        return None
    base, fraction, offset = match.groups()
    if offset != "Z":
        # Reject before fromisoformat can normalize e.g. +00:60 into +01:00; '-00:00' means
        # "offset unknown" (RFC 3339) and does not prove timezone provenance.
        if offset == "-00:00" or int(offset[1:3]) >= 24 or int(offset[4:6]) >= 60:
            return None
    micro = (fraction or "").ljust(6, "0")[:6]
    try:
        value = datetime.fromisoformat(f"{base}.{micro}{'+00:00' if offset == 'Z' else offset}")
    except ValueError:
        return None  # impossible date/time or offset
    return value if value.tzinfo is not None and value.utcoffset() is not None else None


def evaluate_freshness(bundle, source_binding, *, mode, now):
    """Re-evaluate the host's final re-collection receipt against local bytes and real time.

    Returns {"freshness": checks.$defs.freshness, "state": "fresh"|"changed"|"unknown",
    "reasons": [unknown_reason-shaped dicts], "changed_source_ids": [ids]}.
    Local bytes prove only that the host's recorded hashes are consistent with
    what is on disk; they do not prove independent network re-collection or
    authentication. Offline mode is always unknown. Every relevant non-mail
    source must have exactly one refresh item, observed at most 600 seconds
    before `now` (inclusive) and not after the receipt time, with the bound
    source record being that same final observation (same URL, timestamp and
    hashes). A verified hash difference is reported as changed; anything
    unproven is unknown. Claimed states and statuses are never trusted.
    """
    validate_document(bundle, "source.json")
    if mode != bundle["mode"]:
        raise ValidationError("mode does not match the source bundle")
    if type(source_binding) is not dict or type(source_binding.get("sources")) is not dict:
        raise ValidationError("source_binding must come from verify_sources")
    now_dt = _parse_aware(now)
    if now_dt is None:
        raise ValidationError("now must be a valid timezone-aware RFC 3339 timestamp")
    reasons, changed = [], []

    def reason(code, detail, source_id=None):
        if len(reasons) < 512:
            reasons.append({"code": code, "requirement_id": None, "source_id": source_id, "file_id": None,
                            "detail": str(detail)[:2048]})

    def unknown(detail, state="not_performed"):
        reason("freshness_unknown", detail)
        return {"freshness": {"state": state, "refreshed_at": None, "items": [], "reason": detail[:2048]},
                "state": "unknown", "reasons": reasons, "changed_source_ids": []}

    if mode == "offline":
        return unknown("offline mode: no host re-collection in this run")
    claimed = bundle["freshness"]
    refreshed = _parse_aware(claimed["refreshed_at"])
    if refreshed is None:
        return unknown("no valid final re-collection receipt time")
    age = (now_dt - refreshed).total_seconds()
    if age < 0 or age > FRESHNESS_MAX_AGE_SECONDS:
        return unknown("final re-collection receipt is in the future or older than 600 seconds", "failed")

    items_by_id, duplicates = {}, set()
    for item in claimed["items"]:
        if item["source_id"] in items_by_id:
            duplicates.add(item["source_id"])
        items_by_id[item["source_id"]] = item
    records = {record["id"]: record for record in bundle["sources"]}
    for source_id in sorted(set(items_by_id) - set(records))[:16]:
        reason("other", "refresh item names an unknown source")
    relevant = [r for r in bundle["sources"] if r["kind"] not in _FRESHNESS_EXEMPT_KINDS]
    if not relevant:
        return unknown("no non-mail source whose freshness could be proven", "failed")

    binding = source_binding["sources"]
    out_items, unproven = [], 0
    for record in relevant:
        sid = record["id"]
        item = items_by_id.get(sid)
        entry = binding.get(sid)
        verified = (type(entry) is dict and entry.get("verified") is True and entry.get("source") == record
                    and type(entry.get("normalized_text")) is str
                    and hashlib.sha256(entry["normalized_text"].encode("utf-8")).hexdigest() == entry.get("normalized_text_sha256")
                    and entry.get("artifact_sha256") == record["artifact_sha256"])
        current_artifact = entry["artifact_sha256"] if verified else None
        current_text = entry["normalized_text_sha256"] if verified else None
        status, problem = "not_refreshed", None
        observed = _parse_aware(item["observed_at"]) if item else None
        if item is None or sid in duplicates:
            problem = ("freshness_unknown", "source has no unique refresh item")
        elif item["status"] in ("failed", "not_refreshed"):
            status, problem = item["status"], ("collection_incomplete", "host reports the re-collection failed")
        elif not verified:
            status, problem = "failed", ("source_hash_mismatch", "source is not bound to verified local bytes")
        elif mode == "online" and record["kind"] == "synthetic":
            status, problem = "failed", ("freshness_unknown", "synthetic record is not live collection")
        elif record["collection_status"] != "complete" or record["original_url"] is None:
            status, problem = "failed", ("freshness_unknown", "same-source provenance (complete record, URL) missing")
        elif observed is None or _parse_aware(record["observed_at"]) != observed:
            status, problem = "failed", ("freshness_unknown", "refresh time missing, invalid or not the bound record's observation")
        elif observed > refreshed or (now_dt - observed).total_seconds() > FRESHNESS_MAX_AGE_SECONDS:
            status, problem = "failed", ("freshness_unknown", "observation after the receipt or older than 600 seconds")
        elif None in (item["previous_artifact_sha256"], item["previous_normalized_text_sha256"]):
            status, problem = "failed", ("freshness_unknown", "previous hashes missing; change cannot be proven")
        elif (item["current_artifact_sha256"], item["current_normalized_text_sha256"]) != (current_artifact, current_text):
            status, problem = "failed", ("source_hash_mismatch", "refresh item does not match locally bound bytes")
        elif (item["previous_artifact_sha256"], item["previous_normalized_text_sha256"]) == (current_artifact, current_text):
            status = "unchanged"
        else:
            status = "changed"
            changed.append(sid)
            reason("source_changed", "source bytes changed since the previous observation", sid)
        if problem:
            unproven += 1
            reason(problem[0], problem[1], sid)
        if len(out_items) < 128:
            out_items.append({"source_id": sid,
                              "previous_artifact_sha256": item["previous_artifact_sha256"] if item else None,
                              "current_artifact_sha256": current_artifact,
                              "previous_normalized_text_sha256": item["previous_normalized_text_sha256"] if item else None,
                              "current_normalized_text_sha256": current_text,
                              "observed_at": item["observed_at"] if item and observed else None,
                              "status": status})

    note = ("local bytes match host records; this is not independent network re-collection"
            + ("; synthetic records (가상 과제 재현), not live collection" if mode == "synthetic" else ""))
    if changed:
        state, schema_state = "changed", "changed"
    elif unproven:
        state, schema_state = "unknown", "failed"
        reason("freshness_unknown", "one or more sources could not be proven fresh")
    else:
        state, schema_state = "fresh", "fresh"
    freshness = {"state": schema_state, "refreshed_at": claimed["refreshed_at"], "items": out_items,
                 "reason": (note if state == "fresh" else f"{note}; {state}")[:2048]}
    return {"freshness": freshness, "state": state, "reasons": reasons, "changed_source_ids": changed}


# ---------------------------------------------------------------------------
# Immutable report delta (record comparison only; no recheck, no reuse)
# ---------------------------------------------------------------------------

def _report_index(report, label):
    """Validate one report and index its identities; duplicates are rejected, never chosen."""
    validate_document(report, "checks.json")
    if report["rules_version"] != REPORT_RULES_VERSION:
        raise ValidationError(f"{label} report rules_version is not supported")

    def unique(items, key, what):
        index = {}
        for item in items:
            if item[key] in index:
                raise ValidationError(f"{label} report has duplicate {what}")
            index[item[key]] = item
        return index

    unique(report["checks"], "check_id", "check_id")
    unique(report["files"], "file_id", "file record")
    return {"files": unique(report["selection"]["files"], "file_id", "selected file_id"),
            "sources": unique(report["sources"], "source_id", "source_id"),
            "checks": unique(report["checks"], "requirement_id", "requirement_id")}


def _real_sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _differs(old, new):
    """True only for two real hashes that differ; unknown (null) never proves a change."""
    return _real_sha(old) and _real_sha(new) and old != new


def compare_report_delta(previous, current):
    """Return checks.$defs.delta comparing two immutable report records.

    previous=None gives the initial delta (previous_run_id null, everything in
    current listed as added, every current check with previous_status null).
    Identities are explicit: file_id, source_id, requirement_id. A file or
    source is changed only when both sides carry real, different hashes; a
    rename needs the same file_id with a different path, and same_bytes is
    true only for two real equal hashes. A still-declared but unbound file is
    neither removed nor changed. Requirements compare by full
    requirement_sha256. Old conclusions are never copied; inputs are never
    modified; no filesystem or network is consulted.
    """
    cur = _report_index(current, "current")
    if previous is None:
        delta = {"previous_run_id": None,
                 "files_added": sorted(cur["files"]), "files_removed": [], "files_changed": [], "files_renamed": [],
                 "sources_added": sorted(cur["sources"]), "sources_removed": [], "sources_changed": [],
                 "requirements_added": sorted(cur["checks"]), "requirements_removed": [], "requirements_changed": [],
                 "check_status_changes": [
                     {"check_id": check["check_id"], "requirement_id": rid, "previous_status": None,
                      "current_status": check["status"]} for rid, check in sorted(cur["checks"].items())]}
    else:
        prev = _report_index(previous, "previous")
        if previous["run_id"] == current["run_id"]:
            raise ValidationError("previous and current reports share a run_id")
        files_changed, renamed = [], []
        for file_id in sorted(set(prev["files"]) & set(cur["files"])):
            old, new = prev["files"][file_id], cur["files"][file_id]
            if _differs(old["sha256"], new["sha256"]):
                files_changed.append(file_id)
            if old["path"] != new["path"]:
                renamed.append({"file_id": file_id, "previous_path": old["path"], "current_path": new["path"],
                                "same_bytes": _real_sha(old["sha256"]) and old["sha256"] == new["sha256"]})
        sources_changed = sorted(
            sid for sid in set(prev["sources"]) & set(cur["sources"])
            if any(_differs(prev["sources"][sid][key], cur["sources"][sid][key])
                   for key in ("artifact_sha256", "normalized_text_sha256", "original_bytes_sha256")))
        common = set(prev["checks"]) & set(cur["checks"])
        status_changes = []
        for rid, check in sorted(cur["checks"].items()):
            before = prev["checks"].get(rid)
            if before is None or before["status"] != check["status"]:
                status_changes.append({"check_id": check["check_id"], "requirement_id": rid,
                                       "previous_status": None if before is None else before["status"],
                                       "current_status": check["status"]})
        delta = {"previous_run_id": previous["run_id"],
                 "files_added": sorted(set(cur["files"]) - set(prev["files"])),
                 "files_removed": sorted(set(prev["files"]) - set(cur["files"])),
                 "files_changed": files_changed, "files_renamed": renamed,
                 "sources_added": sorted(set(cur["sources"]) - set(prev["sources"])),
                 "sources_removed": sorted(set(prev["sources"]) - set(cur["sources"])),
                 "sources_changed": sources_changed,
                 "requirements_added": sorted(set(cur["checks"]) - set(prev["checks"])),
                 "requirements_removed": sorted(set(prev["checks"]) - set(cur["checks"])),
                 "requirements_changed": sorted(rid for rid in common if prev["checks"][rid]["requirement_sha256"]
                                                != cur["checks"][rid]["requirement_sha256"]),
                 "check_status_changes": status_changes}
    try:
        error = next(iter(_fragment_validator("checks.json", "delta").iter_errors(delta)), None)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(f"delta validation could not complete: {type(exc).__name__}") from None
    if error is not None:
        keyword = error.validator if type(error.validator) is str else "?"
        raise ValidationError(f"delta failed schema at {_error_location(error)}: {keyword[:40]}")
    return delta
