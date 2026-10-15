"""`trust/paired.py` and `core/npyio.py`: the arithmetic and the parser a campaign comparison stands
on, checked against independent formulas (numpy where present) and against hostile inputs."""
from __future__ import annotations

import io
import math
import random
import zipfile

import pytest

from looplab.core.npyio import NpyFormatError, read_npy, read_npz, write_npy, write_npz
from looplab.trust import paired


def _data(seed=1, users=200, per_user=3):
    rng = random.Random(seed)
    t, c, clusters = [], [], []
    for u in range(users):
        shift = rng.gauss(0, 0.3)
        for _ in range(per_user):
            base = rng.random() + shift
            c.append(base)
            t.append(base + 0.05 + rng.gauss(0, 0.1))
            clusters.append(f"u{u}")
    return t, c, clusters


def test_paired_mean_matches_the_cluster_robust_formula():
    t, c, clusters = _data()
    out = paired.paired_mean_delta(t, c, clusters)
    d = [a - b for a, b in zip(t, c)]
    delta = sum(d) / len(d)
    sums = {}
    for v, g in zip(d, clusters):
        sums[g] = sums.get(g, 0.0) + (v - delta)
    g = len(sums)
    se = math.sqrt(g / (g - 1) * sum(s * s for s in sums.values())) / len(d)
    assert math.isclose(out["estimate"], delta) and math.isclose(out["se"], se)
    assert out["ci95_low"] < delta < out["ci95_high"]
    assert out["clusters"] == 200 and out["n"] == 600
    assert math.isclose(out["p_two_sided"], math.erfc(abs(delta / se) / math.sqrt(2)))


def test_clustering_widens_the_interval_when_queries_of_a_user_are_correlated():
    t, c, clusters = _data(per_user=5)
    clustered = paired.paired_mean_delta(t, c, clusters)
    naive = paired.paired_mean_delta(t, c, list(range(len(t))))
    assert clustered["se"] > naive["se"]


def test_paired_ratio_with_a_shared_denominator_is_the_hit_difference_over_targets():
    rng = random.Random(3)
    den = [rng.randint(0, 3) for _ in range(500)]
    ht = [rng.randint(0, d) for d in den]
    hc = [rng.randint(0, d) for d in den]
    clusters = [i // 2 for i in range(500)]
    out = paired.paired_ratio_delta(ht, den, hc, den, clusters)
    total = sum(den)
    delta = (sum(ht) - sum(hc)) / total
    sums = {}
    for a, b, d, g in zip(ht, hc, den, clusters):
        sums[g] = sums.get(g, 0.0) + (a - b - delta * d)
    g = len(sums)
    se = math.sqrt(g / (g - 1) * sum(s * s for s in sums.values())) / total
    assert math.isclose(out["estimate"], delta) and math.isclose(out["se"], se)
    assert out["denominator_total"] == total


def test_holm_matches_the_step_down_definition_and_keeps_order():
    p = [0.01, 0.04, 0.03, None, 0.5]
    adjusted = paired.holm(p)
    assert adjusted[3] is None
    assert math.isclose(adjusted[0], 0.04)          # 4 * 0.01
    assert math.isclose(adjusted[2], 0.09)          # max(0.04, 3 * 0.03)
    assert math.isclose(adjusted[1], 0.09)          # max(0.09, 2 * 0.04)
    assert adjusted[4] == 0.5


def test_bootstrap_brackets_the_estimate_in_both_engines(monkeypatch):
    t, c, clusters = _data(users=60)
    est = paired.paired_mean_delta(t, c, clusters)["estimate"]
    fast = paired.cluster_bootstrap("mean", {"t": t, "c": c}, clusters, replicates=300, seed=1)
    assert fast["ci95_low"] < est < fast["ci95_high"]
    import builtins
    real_import = builtins.__import__

    def no_numpy(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("no numpy")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_numpy)
    slow = paired.cluster_bootstrap("mean", {"t": t, "c": c}, clusters, replicates=300, seed=1)
    assert slow["engine"] == "python" and slow["ci95_low"] < est < slow["ci95_high"]
    huge = paired.cluster_bootstrap("mean", {"t": t * 1000, "c": c * 1000},
                                    [f"{i}" for i in range(len(t) * 1000)], replicates=1000)
    assert "unavailable" in huge


def test_inputs_that_cannot_be_compared_are_refused():
    with pytest.raises(paired.PairedInputError):
        paired.paired_mean_delta([1.0], [1.0, 2.0], ["a"])
    with pytest.raises(paired.PairedInputError):
        paired.paired_mean_delta([float("nan")], [1.0], ["a"])
    with pytest.raises(paired.PairedInputError):
        paired.paired_ratio_delta([0], [0], [0], [0], ["a"])


def test_npy_round_trip_covers_the_campaign_dtypes():
    for values, dtype, shape in (
            ([1, -2, 3, 4], "<i4", (2, 2)), ([1.5, 2.25], "<f8", None), ([0.5], "<f4", None),
            ([True, False], "|b1", None), (["u1", "user-22"], "<U8", None),
            ([7, 8], "<i8", None), ([3], "<u2", None)):
        shape_out, out = read_npy(write_npy(values, shape=shape, dtype=dtype))
        assert out == values and shape_out == (shape or (len(values),))


def test_npz_matches_numpy_when_numpy_wrote_it():
    np = pytest.importorskip("numpy")
    buffer = io.BytesIO()
    np.savez_compressed(buffer, keys=np.array(["a:1", "b:22"]), den=np.arange(6, dtype=np.int32).reshape(2, 3),
                        w=np.array([[0.5, 0.25], [1.0, 0.0]]), big=np.array([1, 2], dtype=">i8"))
    arrays = read_npz(buffer.getvalue())
    assert arrays["keys"] == ((2,), ["a:1", "b:22"])
    assert arrays["den"] == ((2, 3), [0, 1, 2, 3, 4, 5])
    assert arrays["w"] == ((2, 2), [0.5, 0.25, 1.0, 0.0])
    assert arrays["big"] == ((2,), [1, 2])


def test_hostile_npz_payloads_are_refused_not_executed():
    np = pytest.importorskip("numpy")
    buffer = io.BytesIO()
    np.save(buffer, np.array([{"a": 1}], dtype=object), allow_pickle=True)
    with pytest.raises(NpyFormatError):
        read_npy(buffer.getvalue())
    buffer = io.BytesIO()
    np.save(buffer, np.asfortranarray(np.arange(6).reshape(2, 3)))
    with pytest.raises(NpyFormatError):
        read_npy(buffer.getvalue())
    with pytest.raises(NpyFormatError):
        read_npy(write_npy(list(range(10)), dtype="<i4"), max_elements=5)
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr("../evil.npy", write_npy([1], dtype="<i4"))
    with pytest.raises(NpyFormatError):
        read_npz(nested.getvalue())
    with pytest.raises(NpyFormatError):
        read_npz(b"not a zip")
    assert read_npz(write_npz({"x": write_npy([1, 2], dtype="<i4")}))["x"] == ((2,), [1, 2])


def _stored_npz(payload: bytes) -> bytearray:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("x.npy", payload)
    return bytearray(buffer.getvalue())


def test_malformed_bytes_below_the_format_checks_are_refused_as_npy_format_errors():
    """Review 2026-10-15: the standard library's own errors on corrupt bytes (a bad CRC, a corrupt
    deflate stream, an encrypted member, an unknown compression method, a `<U` body that is not
    UTF-32) leaked past the module's one promised exception, and the per-query upload route — which
    catches `NpyFormatError` alone — answered them with a 500."""
    import struct
    good = write_npy([1, 2, 3, 4], dtype="<i4")
    cases = {}
    crc = _stored_npz(good)
    crc[crc.find(good) + len(good) - 1] ^= 0xFF                 # zipfile.BadZipFile (Bad CRC-32)
    cases["bad_crc"] = crc
    encrypted = _stored_npz(good)                               # RuntimeError (password required)
    encrypted[6:8] = struct.pack("<H", 0x1)                     # the local and central flag bits
    central = encrypted.find(b"PK\x01\x02")
    encrypted[central + 8:central + 10] = struct.pack("<H", 0x1)
    cases["encrypted"] = encrypted
    method = _stored_npz(good)                                  # NotImplementedError (method 99)
    method[8:10] = struct.pack("<H", 99)
    central = method.find(b"PK\x01\x02")
    method[central + 10:central + 12] = struct.pack("<H", 99)
    cases["unknown_method"] = method
    deflated = io.BytesIO()
    with zipfile.ZipFile(deflated, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("x.npy", write_npy(list(range(400)), dtype="<i8"))
    corrupt = bytearray(deflated.getvalue())                    # zlib.error (invalid stream)
    start = corrupt.find(b"x.npy") + len(b"x.npy")
    corrupt[start + 2:start + 40] = b"\xff" * 38
    cases["corrupt_deflate"] = corrupt
    for name, data in cases.items():
        # The unwrapped reader shows each case reaches a NON-format exception (the leak) ...
        with pytest.raises(Exception) as raw:
            read_npz.__wrapped__(bytes(data))
        assert not isinstance(raw.value, NpyFormatError), (name, raw.value)
        # ... and the public reader refuses it as the one exception the module promises.
        with pytest.raises(NpyFormatError):
            read_npz(bytes(data))
    utf32 = bytearray(write_npy(["ab"], dtype="<U2"))
    utf32[-4:] = b"\xff\xff\xff\xff"                            # UnicodeDecodeError (not UTF-32)
    with pytest.raises(UnicodeDecodeError):
        read_npy.__wrapped__(bytes(utf32))
    with pytest.raises(NpyFormatError):
        read_npy(bytes(utf32))
    with pytest.raises(NpyFormatError):
        read_npz(write_npz({"x": bytes(utf32)}))
    # The well-formed payload beside them still reads.
    assert read_npz(write_npz({"x": good}))["x"] == ((4,), [1, 2, 3, 4])
