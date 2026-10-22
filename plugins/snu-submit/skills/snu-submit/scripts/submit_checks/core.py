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
