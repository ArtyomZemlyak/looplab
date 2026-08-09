"""Small shared contract for trusted ``spans.jsonl`` append receipts.

The span exporter is the sole supported in-place appender. Readers use its contiguous receipts to
distinguish that operation from an out-of-band same-inode rewrite followed by growth. The journal is
derived diagnostic metadata: absence/corruption never changes trace truth, it only forces a rebuild.
"""

SPAN_APPEND_JOURNAL_NAME = ".spans-append.jsonl"
SPAN_APPEND_RECEIPT_SCHEMA = 1
# Bound retained receipt metadata. Rotation changes the journal inode, deliberately forcing any
# older in-memory/persisted index to rebuild once and checkpoint the compact journal.
SPAN_APPEND_JOURNAL_MAX_BYTES = 4 * 1024 * 1024
