"""Pinned external source snapshots and confined CPU smoke; no promotion or scientific utility claim."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.parse import urlsplit

from looplab.core.jsonutil import canonical_json_digest

SOURCE_SCHEMA = 'looplab.external-skill-source/v1'
MAX_SOURCE_FILES = 16
MAX_SOURCE_BYTES = 128 * 1024
_GIT = re.compile('[0-9a-f]{40}|[0-9a-f]{64}')


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 256 or '\\' in value
            or any(ord(c) < 32 for c in value)):
        raise ValueError('invalid_external_source_path')
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or '..' in path.parts or any(part.startswith('.') for part in path.parts):
        raise ValueError('invalid_external_source_path')
    if any(word in path.name.lower() for word in ('credential', 'auth.json', 'secret', 'token')):
        raise ValueError('private_external_source_path')
    return value


def _origin(value):
    if not isinstance(value, dict) or set(value) != {'kind', 'revision', 'label', 'url'}:
        raise ValueError('invalid_external_source_origin')
    if value['kind'] != 'git' or not isinstance(value['revision'], str) or _GIT.fullmatch(value['revision']) is None:
        raise ValueError('external_source_requires_exact_git_commit')
    label, url = value['label'], value['url']
    if not isinstance(label, str) or not label.strip() or len(label) > 160:
        raise ValueError('invalid_external_source_label')
    if url is not None:
        if not isinstance(url, str) or len(url) > 1024:
            raise ValueError('invalid_external_source_url')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('external_source_url_must_be_public_reference')
    return dict(value)


def normalize_source(value):
    if not isinstance(value, dict) or set(value) != {'schema', 'origin', 'files', 'snapshot_sha256'} or value['schema'] != SOURCE_SCHEMA:
        raise ValueError('invalid_external_source_snapshot')
    origin = _origin(value['origin'])
    if not isinstance(value['files'], list) or not 1 <= len(value['files']) <= MAX_SOURCE_FILES:
        raise ValueError('invalid_external_source_files')
    rows, seen, used = [], set(), 0
    for row in value['files']:
        if not isinstance(row, dict) or set(row) != {'path', 'sha256', 'content'}:
            raise ValueError('invalid_external_source_file')
        name = _path(row['path'])
        if name in seen or not isinstance(row['content'], str):
            raise ValueError('invalid_external_source_file')
        seen.add(name)
        data = row['content'].encode('utf-8'); used += len(data)
        if len(data) > 64 * 1024 or used > MAX_SOURCE_BYTES:
            raise ValueError('external_source_exceeds_byte_budget')
        if row['sha256'] != hashlib.sha256(data).hexdigest():
            raise ValueError('external_source_file_digest_mismatch')
        rows.append(dict(row))
    rows.sort(key=lambda row: row['path'])
    digest = canonical_json_digest({'schema': SOURCE_SCHEMA, 'origin': origin, 'files': rows})
    if value['snapshot_sha256'] != digest:
        raise ValueError('external_source_snapshot_digest_mismatch')
    return {'schema': SOURCE_SCHEMA, 'origin': origin, 'files': rows, 'snapshot_sha256': digest}


def collect_git_source(repo, revision, paths, *, label, url=None):
    """Read selected immutable blobs, never a working tree, config dump, symlink or remote fetch."""
    origin = _origin({'kind': 'git', 'revision': revision, 'label': label, 'url': url})
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_SOURCE_FILES:
        raise ValueError('invalid_external_source_files')
    names = [_path(name) for name in paths]
    if len(set(names)) != len(names):
        raise ValueError('duplicate_external_source_file')
    root = Path(repo).resolve(strict=True)
    def git(*args):
        result = subprocess.run(['git', '--no-replace-objects', '-c', 'core.hooksPath=/dev/null',
            '--literal-pathspecs', '-C', str(root), *args], capture_output=True, timeout=10, check=False)
        if result.returncode != 0:
            raise ValueError('external_source_git_read_failed')
        return result.stdout
    if git('rev-parse', '--verify', revision + '^{commit}').decode().strip() != revision:
        raise ValueError('external_source_commit_changed')
    rows, used = [], 0
    for name in sorted(names):
        raw = git('ls-tree', '-z', revision, '--', name)
        parts = raw.rstrip(b'\0').split(b'\t')
        if len(parts) != 2 or parts[1].decode('utf-8') != name:
            raise ValueError('external_source_requires_regular_git_blob')
        metadata = parts[0].decode('ascii').split()
        if len(metadata) != 3 or metadata[0] not in ('100644', '100755') or metadata[1] != 'blob' or _GIT.fullmatch(metadata[2]) is None:
            raise ValueError('external_source_requires_regular_git_blob')
        size = int(git('cat-file', '-s', metadata[2])); used += size
        if size > 64 * 1024 or used > MAX_SOURCE_BYTES:
            raise ValueError('external_source_exceeds_byte_budget')
        data = git('cat-file', 'blob', metadata[2])
        if len(data) != size:
            raise ValueError('external_source_blob_changed')
        content = data.decode('utf-8')
        rows.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'content': content})
    value = {'schema': SOURCE_SCHEMA, 'origin': origin, 'files': rows}
    value['snapshot_sha256'] = canonical_json_digest(value)
    return normalize_source(value)


def exclusion_policy(task_id, forbidden_terms, *, review_reason):
    """Declared exact exclusion checks, not semantic contamination attestation."""
    if (not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 256
            or not isinstance(forbidden_terms, list) or len(forbidden_terms) > 32
            or any(not isinstance(term, str) or not term.strip() or len(term) > 256 for term in forbidden_terms)
            or not isinstance(review_reason, str) or not review_reason.strip() or len(review_reason) > 1000):
        raise ValueError('external_source_requires_bounded_exclusion_review')
    return {'task_id': task_id, 'forbidden_terms': sorted(set([task_id, *forbidden_terms])),
            'review_reason': review_reason, 'authority': 'declared_exact_exclusions_not_semantic_proof'}


def check_exclusions(source, policy, *, extra_text=''):
    text = json.dumps(source, ensure_ascii=False) + '\n' + extra_text
    for term in policy['forbidden_terms']:
        # Exact Unicode word boundaries also cover URL/path/competition slugs. An author must
        # still review semantic near-matches; this never claims that no leakage exists.
        if re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', text, re.IGNORECASE):
            raise ValueError('external_source_matches_declared_task_exclusion')


def validate_external_skill_source(source, validation_code, policy, *, timeout_s=5.0, protected_roots=()):
    """Execute only the confined CPU smoke, not training/scoring; import never starts work."""
    source = normalize_source(source)
    if (not isinstance(policy, dict) or set(policy) != {'task_id', 'forbidden_terms', 'review_reason', 'authority'}
            or policy.get('authority') != 'declared_exact_exclusions_not_semantic_proof'):
        raise ValueError('invalid_external_skill_exclusion_policy')
    original = policy
    terms = policy.get('forbidden_terms')
    if not isinstance(terms, list):
        raise ValueError('invalid_external_skill_exclusion_policy')
    policy = exclusion_policy(policy['task_id'], [term for term in terms if term != policy['task_id']],
                              review_reason=policy['review_reason'])
    if policy != original:
        raise ValueError('invalid_external_skill_exclusion_policy')
    if (not isinstance(validation_code, str) or not validation_code.strip() or len(validation_code.encode('utf-8')) > 32000
            or type(timeout_s) not in (int, float) or not 1 <= timeout_s <= 30):
        raise ValueError('invalid_external_skill_validation')
    check_exclusions(source, policy, extra_text=validation_code)
    from looplab.tools.dev_probe import DevProbeTools
    files = {row['path']: row['content'] for row in source['files']}
    staged = SimpleNamespace(files=files, deleted=[])
    probe = DevProbeTools(staged=staged, timeout_s=timeout_s, max_calls=1,
        confine_reads=True, protect_roots=tuple(protected_roots))
    started = time.monotonic()
    result = probe.execute_result('run_probe', {'code': validation_code, 'timeout': timeout_s})
    details = result.structured if isinstance(result.structured, Mapping) else {}
    ok = (not result.is_error and type(details.get('exit_code')) is int and details['exit_code'] == 0
          and details.get('timed_out') is False and details.get('cancelled') is False)
    return {'schema': 'looplab.external-skill-validation/v1', 'passed': ok,
            'source_sha256': source['snapshot_sha256'], 'policy_sha256': canonical_json_digest(policy),
            'validation_code_sha256': hashlib.sha256(validation_code.encode()).hexdigest(),
            'exit_code': details.get('exit_code'), 'timed_out': details.get('timed_out'),
            'cancelled': details.get('cancelled'), 'elapsed_seconds': time.monotonic() - started,
            'execution': 'existing_confined_CPU_developer_probe',
            'meaning': 'Received snapshot smoke only; original Git origin is client-declared until collected locally. No scientific utility, semantic contamination or production promotion is attested.'}
