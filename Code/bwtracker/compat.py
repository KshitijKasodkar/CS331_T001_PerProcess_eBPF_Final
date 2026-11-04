"""Self-healing compatibility between BCC and new kernel headers.

Why this exists
---------------
BCC compiles our C file against the *kernel's* headers, but it force-includes
its *own* copy of uapi/linux/bpf.h first (same include guard, so the kernel's
copy is skipped). When the kernel is newer than the BCC release - e.g. the
Raspberry Pi's 6.18 kernel with Debian's BCC - the kernel's internal headers
(include/linux/bpf.h) mention structs and constants that BCC's older copy
does not have, and clang stops with errors like:

    invalid application of 'sizeof' to an incomplete type 'struct bpf_task_work'
    use of undeclared identifier 'BPF_LOAD_ACQ'

Our program never uses these things; they only need to *exist*. So when a
compile fails we read clang's error list, look each missing name up in the
kernel's own uapi/linux/bpf.h (the authoritative definition for this exact
kernel), and inject just those definitions in front of the kernel headers.
We repeat until it compiles (a definition can depend on another missing one)
and cache the result per kernel + BCC version.
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
import tempfile
from typing import Dict, List, Optional, Set

MARKER = "/* BWT_COMPAT_SHIM */"

_RE_STRUCT = re.compile(r"incomplete type '(?:const )?struct (\w+)'|"
                        r"(?:variable|field) has incomplete type 'struct (\w+)'")
_RE_IDENT = re.compile(r"use of undeclared identifier '(\w+)'")
_RE_TYPE = re.compile(r"unknown type name '(\w+)'")


def uapi_candidates() -> List[str]:
    rel = os.uname().release
    roots = []
    if os.environ.get("BCC_KERNEL_SOURCE"):
        roots.append(os.environ["BCC_KERNEL_SOURCE"])
    roots += [f"/lib/modules/{rel}/build", f"/lib/modules/{rel}/source"]
    out = []
    for r in roots:
        p = os.path.join(os.path.realpath(r), "include", "uapi", "linux", "bpf.h")
        out.append(p)
        # Debian/Ubuntu split headers: <name>-generic/... and the common <name>/...
        base = os.path.realpath(r)
        for suffix in ("-generic", "-rpi-2712", "-rpi-v8", "-amd64", "-arm64", "-common"):
            if base.endswith(suffix):
                out.append(os.path.join(base[: -len(suffix)], "include", "uapi", "linux", "bpf.h"))
    out.append("/usr/include/linux/bpf.h")   # linux-libc-dev, last resort
    seen, res = set(), []
    for p in out:
        if p not in seen and os.path.exists(p):
            seen.add(p)
            res.append(p)
    return res


def read_uapi() -> str:
    text = ""
    for p in uapi_candidates():
        try:
            with open(p, errors="replace") as f:
                text += f.read() + "\n"
        except OSError:
            pass
    return text


@contextlib.contextmanager
def capture_fd2():
    """Capture everything written to file descriptor 2 (clang prints its
    diagnostics there from C++, so sys.stderr redirection is not enough)."""
    res = {"text": ""}
    tmp = tempfile.TemporaryFile()
    try:
        sys.stderr.flush()
    except Exception:
        pass
    saved = os.dup(2)
    os.dup2(tmp.fileno(), 2)
    try:
        yield res
    finally:
        try:
            sys.stderr.flush()
        except Exception:
            pass
        os.dup2(saved, 2)
        os.close(saved)
        tmp.seek(0)
        res["text"] = tmp.read().decode("utf-8", "replace")
        tmp.close()


def missing_names(diag: str):
    structs = {a or b for a, b in _RE_STRUCT.findall(diag)}
    idents = set(_RE_IDENT.findall(diag)) | set(_RE_TYPE.findall(diag))
    return structs, idents


def _strip_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def _struct_def(uapi: str, name: str) -> Optional[str]:
    m = re.search(r"^struct %s \{.*?^\}[^;]*;" % re.escape(name), uapi, flags=re.M | re.S)
    return _strip_comments(m.group(0)) if m else None


def _eval_int(expr: str, known: Dict[str, int]) -> Optional[int]:
    e = _strip_comments(expr).strip()
    e = re.sub(r"\b(0x[0-9a-fA-F]+|\d+)[uUlL]+\b", r"\1", e)
    for k in sorted(known, key=len, reverse=True):
        e = re.sub(r"\b%s\b" % re.escape(k), str(known[k]), e)
    if not re.fullmatch(r"[0-9a-fA-FxX\s()<>|&+\-~*^]+", e):
        return None
    try:
        return int(eval(e, {"__builtins__": {}}, {}))
    except Exception:
        return None


def _enum_value(uapi: str, name: str) -> Optional[str]:
    clean = _strip_comments(uapi)
    for m in re.finditer(r"\benum\s*\w*\s*\{(.*?)\}", clean, flags=re.S):
        body = m.group(1)
        if not re.search(r"\b%s\b" % re.escape(name), body):
            continue
        known: Dict[str, int] = {}
        nxt = 0
        for item in body.split(","):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                ident, expr = [x.strip() for x in item.split("=", 1)]
                val = _eval_int(expr, known)
                if val is None:
                    if ident == name:
                        return f"enum {{ {name} = {expr} }};"
                    continue
            else:
                ident, val = item, nxt
            known[ident] = val
            nxt = val + 1
            if ident == name:
                return f"enum {{ {name} = {val} }};"
    return None


def _define(uapi: str, name: str) -> Optional[str]:
    m = re.search(r"^#define\s+%s\b(?:[^\n]*\\\n)*[^\n]*" % re.escape(name), uapi, flags=re.M)
    return _strip_comments(m.group(0)).rstrip() if m else None


def definitions_for(diag: str, uapi: str, done: Set[str]) -> str:
    """Definitions (C text) for every missing name in `diag` not yet in `done`."""
    structs, idents = missing_names(diag)
    out = []
    for s in sorted(structs):
        key = "struct " + s
        if key in done:
            continue
        d = _struct_def(uapi, s)
        if d:
            out.append(d)
            done.add(key)
    for i in sorted(idents):
        if i in done:
            continue
        d = _define(uapi, i) or _enum_value(uapi, i)
        if d:
            out.append(("#ifndef %s\n%s\n#endif" % (i, d)) if d.startswith("#define") else d)
            done.add(i)
    return "\n".join(out)


def error_lines(diag: str, limit: int = 12) -> str:
    lines = [ln for ln in diag.splitlines() if " error: " in ln or ln.startswith("error")]
    return "\n".join(lines[:limit])


def cache_path(bcc_version: str) -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bpf", ".compat")
    src = os.environ.get("BCC_KERNEL_SOURCE")
    tag = os.path.basename(os.path.realpath(src)) if src else os.uname().release
    return os.path.join(d, f"{tag}--bcc-{bcc_version}.h")
