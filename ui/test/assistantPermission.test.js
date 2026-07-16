import test from 'node:test'
import assert from 'node:assert/strict'

import { permissionPresentation } from '../src/assistantPermission.js'

const DIGEST = 'a'.repeat(64)

test('legacy permission payload is visibly unknown and never offers persistent approval', () => {
  const view = permissionPresentation({
    created: 10,
    action: { tool: 'delete_file', tool_kind: 'write', path: 'src/old.js' },
  }, 20_000)
  assert.equal(view.risk, 'UNKNOWN')
  assert.equal(view.canAlways, false)
  assert.equal(view.scope, 'File: src/old.js')
  assert.equal(view.consequence, 'Deletes the named file from disk.')
  assert.match(view.expiryLabel, /Exact expiry unavailable/)
  assert.equal(view.mode, 'unknown')
})

test('canonical reversible permission renders security scope, consequence, expiry, and mode', () => {
  const expires = 1_900_000_000
  const view = permissionPresentation({
    expires_at: expires,
    grant_ttl_seconds: 600,
    mode: 'default',
    action: {
      tool: 'edit_file', tool_kind: 'write', risk: 'REVERSIBLE', rememberable: true,
      action_id: 'edit:src/app.js', scope: { root: 'workspace', path: 'src/app.js', lines: [10, 11] },
      scope_digest: DIGEST, consequence: 'Changes two lines in src/app.js.',
    },
  }, 1_800_000_000_000)
  assert.equal(view.risk, 'REVERSIBLE')
  assert.equal(view.canAlways, true)
  assert.match(view.scope, /root: workspace/)
  assert.match(view.scope, /path: src\/app\.js/)
  assert.match(view.scope, /lines: 10, 11/)
  assert.equal(view.scopeDigest, DIGEST)
  assert.equal(view.consequence, 'Changes two lines in src/app.js.')
  assert.equal(view.expiresMs, expires * 1000)
  assert.equal(view.expired, false)
  assert.equal(view.mode, 'default')
  assert.equal(view.grantTtlSeconds, 600)
  assert.equal(view.grantDurationLabel, '10 min')
})

test('HIGH and UNKNOWN suppress Always even if a payload claims the action is rememberable', () => {
  for (const risk of ['HIGH', 'UNKNOWN', 'not-a-risk']) {
    const view = permissionPresentation({ action: {
      tool: 'shell', tool_kind: 'shell', risk, rememberable: true,
      action_id: 'shell:one', scope_digest: DIGEST,
    }, expires_at: 1_900_000_000, grant_ttl_seconds: 600 }, 1_800_000_000_000)
    assert.equal(view.canAlways, false, risk)
  }
})

test('missing rememberable is fail-closed while consequential can be explicitly rememberable', () => {
  assert.equal(permissionPresentation({ action: {
    tool: 'run', tool_kind: 'shell', risk: 'READ', action_id: 'read:one',
    scope_digest: DIGEST,
  }, expires_at: 1_900_000_000, grant_ttl_seconds: 600 }, 1_800_000_000_000).canAlways, false)
  assert.equal(permissionPresentation({ action: {
    tool: 'run', tool_kind: 'shell', risk: 'CONSEQUENTIAL', rememberable: true,
    action_id: 'run:one', scope_digest: DIGEST,
  }, mode: 'auto', expires_at: 1_900_000_000, grant_ttl_seconds: 600 }, 1_800_000_000_000).canAlways, true)
  assert.equal(permissionPresentation({ action: {
    tool: 'run', tool_kind: 'shell', risk: 'REVERSIBLE', rememberable: true,
    action_id: 'run:missing-digest',
  }, expires_at: 1_900_000_000, grant_ttl_seconds: 600 }, 1_800_000_000_000).canAlways, false)
  assert.equal(permissionPresentation({ action: {
    tool: 'run', tool_kind: 'shell', risk: 'REVERSIBLE', rememberable: true,
    action_id: 'run:missing-ttl', scope_digest: DIGEST,
  }, expires_at: 1_900_000_000 }, 1_800_000_000_000).canAlways, false)
})

test('persistent approval requires an explicitly supported non-plan mode', () => {
  const canonical = {
    expires_at: 1_900_000_000,
    grant_ttl_seconds: 600,
    action: {
      tool: 'edit_file', tool_kind: 'write', risk: 'REVERSIBLE', rememberable: true,
      action_id: 'edit:one', scope_digest: DIGEST,
    },
  }
  for (const mode of ['default', 'acceptEdits', 'auto']) {
    assert.equal(permissionPresentation({ ...canonical, mode }, 1_800_000_000_000).canAlways, true, mode)
  }
  assert.equal(permissionPresentation(canonical, 1_800_000_000_000).canAlways, false, 'missing')
  for (const mode of ['unknown', 'plan', 'DEFAULT', 'accept_edits']) {
    assert.equal(permissionPresentation({ ...canonical, mode }, 1_800_000_000_000).canAlways, false, mode)
  }
})

test('scope rendering excludes secret-like keys and identifies expired requests', () => {
  const view = permissionPresentation({
    expires_at: 100,
    action: {
      tool_kind: 'shell', risk: 'READ', rememberable: true,
      scope: { cwd: 'repo', api_token: 'MUST_NOT_RENDER', password: 'NOPE', command: 'git status' },
    },
  }, 101_000)
  assert.match(view.scope, /cwd: repo/)
  assert.match(view.scope, /command: git status/)
  assert.doesNotMatch(view.scope, /MUST_NOT_RENDER|NOPE|api_token|password/)
  assert.equal(view.expired, true)
})

test('scope rendering omits identity digests and duplicate verb copy', () => {
  const view = permissionPresentation({ action: {
    tool: 'run_command', tool_kind: 'shell', risk: 'HIGH',
    scope: { cwd: 'C:/repo', argv_digest: 'a'.repeat(64), verb: 'run hidden copy', background: false },
  }, mode: 'default' })
  assert.match(view.scope, /cwd: C:\/repo/)
  assert.match(view.scope, /background: false/)
  assert.doesNotMatch(view.scope, /argv digest|hidden copy|a{20}/)
})

import { reconcilePendingPermissions } from '../src/assistantPermission.js'

test('reconcilePendingPermissions hides a locally-resolved card a lagging poll re-adds, then self-heals', () => {
  const resolved = new Set(['req-2'])   // user just approved req-2
  // A stale poll snapshot still lists req-2 (server hasn't reflected the resolution yet).
  const stale = [{ id: 'req-1' }, { id: 'req-2' }]
  assert.deepEqual(reconcilePendingPermissions(stale, resolved).map(r => r.id), ['req-1'],
    'the resolved card must not reappear from a lagging poll')
  assert.ok(resolved.has('req-2'), 'resolved id is retained while the server still reports it')
  // Next poll: the server has dropped req-2 → it is pruned from the set (self-heal, no unbounded growth).
  const fresh = [{ id: 'req-1' }]
  assert.deepEqual(reconcilePendingPermissions(fresh, resolved).map(r => r.id), ['req-1'])
  assert.ok(!resolved.has('req-2'), 'a server-dropped id is pruned so it can never hide a re-issued id')
  // Non-array / malformed inputs degrade to [] without throwing.
  assert.deepEqual(reconcilePendingPermissions(null, new Set()), [])
  assert.deepEqual(reconcilePendingPermissions([null, { id: 'x' }], new Set()).map(r => r.id), ['x'])
})
