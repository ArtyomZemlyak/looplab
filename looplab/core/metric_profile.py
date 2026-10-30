"""Small project-authored metric presentation profile, never a scoring or admission policy."""
from __future__ import annotations
import hashlib
import json
from looplab.core.redact import redact_persisted_text


def profile_digest(profile: dict | None) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def normalize_profile(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {'name', 'metrics', 'slices', 'notes', 'authoring_language', 'publication'}:
        raise ValueError('profile must contain name, metrics, slices and notes only')
    def text(v, limit):
        if not isinstance(v, str):
            raise ValueError('profile text must be a string')
        return redact_persisted_text(v.strip(), max_chars=limit, entropy=False, single_line=True)
    name = text(value.get('name', ''), 160)
    metrics = value.get('metrics')
    if not name or not isinstance(metrics, list) or not 1 <= len(metrics) <= 32:
        raise ValueError('profile needs a name and 1..32 metrics')
    rows = []
    for m in metrics:
        if not isinstance(m, dict) or set(m) - {'key', 'label', 'role', 'unit', 'denominator', 'stack_group'}:
            raise ValueError('metric must contain key, label, role, unit and denominator only')
        key = text(m.get('key', ''), 200); role = m.get('role', 'secondary')
        if not key or role not in ('primary', 'secondary', 'transport'):
            raise ValueError('metric key or role is invalid')
        row = {'key': key, 'label': text(m.get('label', ''), 160), 'role': role,
               'unit': text(m.get('unit', ''), 100), 'denominator': text(m.get('denominator', ''), 300)}
        if 'stack_group' in m:
            row['stack_group'] = text(m['stack_group'], 160)
        rows.append(row)
    if len({m['key'] for m in rows}) != len(rows) or sum(m['role'] == 'primary' for m in rows) != 1:
        raise ValueError('metric keys must be distinct and exactly one must be primary')
    slices = value.get('slices', [])
    if not isinstance(slices, list) or len(slices) > 32:
        raise ValueError('slices must be a list of at most 32')
    result = {'name': name, 'metrics': rows, 'slices': [text(x, 160) for x in slices],
              'notes': text(value.get('notes', ''), 1000)}
    if 'authoring_language' in value:
        language = value['authoring_language']
        if language not in ('auto', 'en', 'ru'):
            raise ValueError('authoring language must be auto, en or ru')
        result['authoring_language'] = language
    if 'publication' in value:
        publication = value['publication']
        if not isinstance(publication, dict) or set(publication) != {'adapter', 'destination_label'} or publication['adapter'] != 'manual_markdown':
            raise ValueError('publication adapter must be manual_markdown with a destination label')
        label = text(publication['destination_label'], 120)
        if not label or any(x in label for x in ('/', ':', '@', '?', '#')):
            raise ValueError('publication destination must be a descriptive label, not a URL or credential')
        result['publication'] = {'adapter': 'manual_markdown', 'destination_label': label}
    return result


def profile_for_run(root, run_id: str) -> dict:
    """Read only the local project metadata snapshot; never claim it is an atomic run observation."""
    from pathlib import Path
    from looplab.core.node_evidence import read_bounded_regular_file
    path = Path(root) / 'projects.json'
    try:
        if not path.exists():
            return {'state': 'unconfigured'}
        raw = read_bounded_regular_file(path, 1024 * 1024 + 1)
        if raw is None or len(raw) > 1024 * 1024:
            return {'state': 'unavailable'}
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('assignments', {}), dict) or not isinstance(data.get('projects', []), list):
            return {'state': 'unavailable'}
        pid = data.get('assignments', {}).get(run_id)
        if not isinstance(pid, str):
            return {'state': 'unconfigured'}
        project = next((p for p in data.get('projects', []) if isinstance(p, dict) and p.get('id') == pid), None)
        if project is None:
            return {'state': 'unavailable'}
        profile = project.get('metric_profile')
        if profile is None:
            return {'state': 'unconfigured', 'project_id': pid, 'digest': profile_digest(None)}
        normalized = normalize_profile(profile)
        return {'state': 'configured', 'project_id': pid, 'profile': normalized, 'digest': profile_digest(normalized),
                'meaning': 'Project-authored display profile, not proof of a measurement or comparable protocol.'}
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return {'state': 'unavailable'}
