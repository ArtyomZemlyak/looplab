import test from 'node:test'
import assert from 'node:assert/strict'

import {
  driftStatus,
  leakageStatus,
  nodeFeasibilityStatus,
  reportStepIdentity,
  rewardHackStatus,
} from '../src/trustSemantics.js'
import { analyze, verdict } from '../src/report.js'

test('missing leakage evidence is unknown, never a clean success', () => {
  assert.equal(leakageStatus(null).tone, 'unknown')
  assert.equal(leakageStatus({ leak: false, verdicts: [] }).tone, 'unknown')
  assert.equal(leakageStatus({ leak: false, verdicts: [{ detector: 'split', leak: false }] }).tone, 'ok')
  assert.equal(leakageStatus({ leak: true, verdicts: [] }).tone, 'alarm')
})

test('detector-off and flag-absence remain semantically distinct', () => {
  assert.deepEqual(rewardHackStatus([], { reward_hack_detect: false }, 4).tone, 'unknown')
  assert.equal(rewardHackStatus([], { reward_hack_detect: true }, 4).tone, 'ok')
  assert.equal(rewardHackStatus([{ node_id: 2 }], { reward_hack_detect: true }, 4).tone, 'alarm')
})

test('no drift event does not overclaim a completed cross-check', () => {
  assert.equal(driftStatus([], null, 2).tone, 'unknown')
  assert.equal(driftStatus([], { eval_trust_mode: 'ratify_freeze' }, 2).label, 'Cross-check not enabled')
  assert.equal(driftStatus([], { eval_trust_mode: 'ratify_freeze_drift' }, 2).tone, 'unknown')
  assert.equal(driftStatus([{ node_id: 3 }], { eval_trust_mode: 'ratify_freeze_drift' }, 2).tone, 'alarm')
})

test('node feasibility is green only after a successful evaluated result', () => {
  assert.equal(nodeFeasibilityStatus({ status: 'pending', feasible: true }).tone, 'unknown')
  assert.equal(nodeFeasibilityStatus({ status: 'evaluated', feasible: true }).tone, 'ok')
  assert.equal(nodeFeasibilityStatus({ status: 'evaluated', feasible: false, violations: [{}] }).tone, 'alarm')
})

test('report step identity never glues operator and theme', () => {
  assert.equal(reportStepIdentity('manual', 'manual-probe'), 'manual · manual-probe')
  assert.equal(reportStepIdentity('draft', 'draft'), 'draft')
})

test('report verdict never calls missing detector coverage trustworthy', () => {
  const state = {
    direction: 'min', best_node_id: 1, reward_hacks: [], drifts: [],
    nodes: {
      0: { id: 0, status: 'evaluated', metric: 2, feasible: true, parent_ids: [], idea: { params: {} } },
      1: { id: 1, status: 'evaluated', metric: 1, feasible: true, parent_ids: [0], idea: { params: {} },
        confirmed_mean: 1, confirmed_std: 0.01, confirmed_seeds: 3 },
    },
  }
  const value = verdict(state, analyze(state))
  assert.equal(value.trust, 'unverified')
  assert.match(value.headline, /not fully verified/i)
  assert.doesNotMatch(value.headline, /passes the trust checks/i)
})
