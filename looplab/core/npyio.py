"""A bounded, pickle-free reader of NumPy ``.npy`` / ``.npz`` bytes, in pure Python (doc 81).

An external agent uploads per-query evaluation arrays (hits, denominators, user ids) so LoopLab can
compute paired comparisons itself. The natural container is the ``.npz`` the agent's own analysis
already writes, but ``numpy`` is not a LoopLab runtime dependency and ``numpy.load`` on untrusted
bytes is a pickle surface unless every caller remembers ``allow_pickle=False``. So this module reads
the format directly, and only the safe subset of it: little/big-endian integers, unsigned integers,
floats, booleans, fixed-width unicode (``<U``) and byte strings (``|S``), C order, at most two
dimensions. An object array, a structured dtype, Fortran order or more than ``max_elements`` values
is REFUSED with ``NpyFormatError`` — never partially read.

The header is a Python literal, parsed with ``ast.literal_eval`` (no evaluation of names or calls).
"""
from __future__ import annotations

import ast
import io
import struct
import sys
import zipfile
from array import array

MAGIC = b"\x93NUMPY"
DEFAULT_MAX_ELEMENTS = 20_000_000
DEFAULT_MAX_MEMBERS = 64

_INT_CODES = {1: "b", 2: "h", 4: "i", 8: "q"}
_UINT_CODES = {1: "B", 2: "H", 4: "I", 8: "Q"}
_FLOAT_CODES = {4: "f", 8: "d"}


class NpyFormatError(ValueError):
    """The bytes are not a supported, pickle-free .npy/.npz array."""


def _array_code(kind: str, width: int) -> str:
    table = {"i": _INT_CODES, "u": _UINT_CODES, "f": _FLOAT_CODES}.get(kind)
    code = table.get(width) if table else None
    if code is None or array(code).itemsize != width:
        raise NpyFormatError(f"unsupported numeric dtype {kind}{width}")
    return code


def read_npy(data: bytes, *, max_elements: int = DEFAULT_MAX_ELEMENTS) -> tuple[tuple, list]:
    """Return ``(shape, values)`` for one ``.npy`` payload: ``values`` is a flat row-major list
    (``int``/``float``/``bool``/``str``) — a 2-D array of shape (n, k) is ``n * k`` values."""
    if not data.startswith(MAGIC) or len(data) < 10:
        raise NpyFormatError("not a .npy payload")
    major = data[6]
    if major == 1:
        (hlen,), start = struct.unpack("<H", data[8:10]), 10
    elif major in (2, 3):
        if len(data) < 12:
            raise NpyFormatError("truncated .npy header")
        (hlen,), start = struct.unpack("<I", data[8:12]), 12
    else:
        raise NpyFormatError(f"unsupported .npy version {major}")
    if hlen > 65536:
        raise NpyFormatError(".npy header is too large")
    try:
        header = ast.literal_eval(data[start:start + hlen].decode("latin1" if major < 3 else "utf8"))
    except (ValueError, SyntaxError, UnicodeDecodeError) as exc:
        raise NpyFormatError(f"unreadable .npy header: {exc}") from None
    if not isinstance(header, dict):
        raise NpyFormatError(".npy header is not a dictionary")
    descr, fortran, shape = header.get("descr"), header.get("fortran_order"), header.get("shape")
    if not isinstance(descr, str):
        raise NpyFormatError("structured or object dtypes are not supported")
    if fortran is not False:
        raise NpyFormatError("Fortran-order arrays are not supported")
    if (not isinstance(shape, tuple) or len(shape) > 2
            or not all(isinstance(d, int) and not isinstance(d, bool) and d >= 0 for d in shape)):
        raise NpyFormatError("only 0-, 1- and 2-dimensional arrays are supported")
    count = 1
    for d in shape:
        count *= d
    if count > max_elements:
        raise NpyFormatError(f"array holds {count} values, more than the {max_elements} bound")
    body = data[start + hlen:]
    if len(descr) < 3 or descr[0] not in "<>|=":
        raise NpyFormatError(f"unsupported dtype {descr!r}")
    order, kind = descr[0], descr[1]
    try:
        width = int(descr[2:])
    except ValueError:
        raise NpyFormatError(f"unsupported dtype {descr!r}") from None
    swap = (order == ">" and sys.byteorder == "little") or (order == "<" and sys.byteorder == "big")
    if kind == "U":
        size = 4 * width
        _need(body, size * count)
        text = body[:size * count].decode("utf-32-be" if order == ">" else "utf-32-le")
        values = [text[i * width:(i + 1) * width].rstrip("\x00") for i in range(count)]
    elif kind == "S":
        _need(body, width * count)
        values = [body[i * width:(i + 1) * width].rstrip(b"\x00").decode("utf-8", "replace")
                  for i in range(count)]
    elif kind == "b" and width == 1:
        _need(body, count)
        values = [b != 0 for b in body[:count]]
    elif kind in "iuf":
        arr = array(_array_code(kind, width))
        _need(body, width * count)
        arr.frombytes(body[:width * count])
        if swap and width > 1:
            arr.byteswap()
        values = arr.tolist()
    else:
        raise NpyFormatError(f"unsupported dtype {descr!r} (object/complex/datetime are refused)")
    return tuple(shape), values


def _need(body: bytes, size: int) -> None:
    if len(body) < size:
        raise NpyFormatError(".npy payload is shorter than its header declares")


def read_npz(data: bytes, *, max_elements: int = DEFAULT_MAX_ELEMENTS,
             max_members: int = DEFAULT_MAX_MEMBERS) -> dict[str, tuple[tuple, list]]:
    """``{name: (shape, values)}`` for every member of an ``.npz`` (zip of ``.npy``) payload.

    The element bound is over the WHOLE archive, and each member's declared uncompressed size is
    checked before it is inflated, so a zip bomb is refused rather than expanded."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise NpyFormatError(f"not an .npz archive: {exc}") from None
    with archive:
        members = archive.infolist()
        if len(members) > max_members:
            raise NpyFormatError(f"archive holds {len(members)} members (bound {max_members})")
        out: dict[str, tuple[tuple, list]] = {}
        budget = max_elements
        for info in members:
            name = info.filename
            if not name.endswith(".npy") or "/" in name or "\\" in name:
                raise NpyFormatError(f"unexpected archive member {name!r}")
            if info.file_size > 16 * max_elements + 65536:
                raise NpyFormatError(f"member {name!r} is too large")
            shape, values = read_npy(archive.read(info), max_elements=budget)
            budget -= len(values)
            out[name[:-4]] = (shape, values)
        return out


def write_npy(values, *, shape=None, dtype: str = "<f8") -> bytes:
    """A minimal writer of the same subset (tests and fixtures; LoopLab never needs numpy)."""
    flat = list(values)
    shape = tuple(shape) if shape is not None else (len(flat),)
    if dtype.startswith("<U"):
        width = int(dtype[2:])
        body = "".join(str(v)[:width].ljust(width, "\x00") for v in flat).encode("utf-32-le")
    elif dtype == "|b1":
        body = bytes(1 if v else 0 for v in flat)
    else:
        arr = array(_array_code(dtype[1], int(dtype[2:])), flat)
        if sys.byteorder == "big":
            arr.byteswap()
        body = arr.tobytes()
    header = repr({"descr": dtype, "fortran_order": False, "shape": shape}).encode("latin1")
    pad = 64 - (10 + len(header) + 1) % 64
    header += b" " * (pad % 64) + b"\n"
    return MAGIC + b"\x01\x00" + struct.pack("<H", len(header)) + header + body


def write_npz(arrays: dict[str, bytes]) -> bytes:
    """Zip already-encoded ``.npy`` payloads into an ``.npz`` (tests and fixtures)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in arrays.items():
            archive.writestr(name + ".npy", payload)
    return buffer.getvalue()


__all__ = ["NpyFormatError", "read_npy", "read_npz", "write_npy", "write_npz"]
