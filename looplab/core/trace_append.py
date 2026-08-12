"""Small shared contract for trusted ``spans.jsonl`` append receipts.

The span exporter is the sole supported in-place appender. Readers use its contiguous receipts to
distinguish that operation from an out-of-band same-inode rewrite followed by growth. The journal is
derived diagnostic metadata: absence/corruption never changes trace truth, it only forces a rebuild.
"""

SPAN_APPEND_JOURNAL_NAME = ".spans-append.jsonl"
# Schema 1 proved "no in-place rewrite" with ``st_ctime_ns``, which is the mutation stamp on POSIX and
# the CREATION stamp on Windows — so a Windows prefix rewrite followed by a legitimate append was
# indistinguishable from a plain append, and readers had to rebuild on every growth there. Schema 2
# carries the descriptor-bound mutation token as well (``FILE_BASIC_INFO.ChangeTime`` on Windows,
# ``st_ctime_ns`` on POSIX), which is exactly the missing proof. Both versions are still READ: a
# schema-1 chain keeps its POSIX fast path and keeps failing closed on Windows, so an index written by
# an older build degrades to a rebuild instead of being trusted on evidence it never carried.
SPAN_APPEND_RECEIPT_SCHEMA = 2
SPAN_APPEND_RECEIPT_SCHEMA_V1 = 1
# Bound retained receipt metadata. Rotation changes the journal inode, deliberately forcing any
# older in-memory/persisted index to rebuild once and checkpoint the compact journal.
SPAN_APPEND_JOURNAL_MAX_BYTES = 4 * 1024 * 1024
