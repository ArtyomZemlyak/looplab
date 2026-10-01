import test from 'node:test'
import assert from 'node:assert/strict'
import {
  MAX_CAMPAIGN_RUNS, campaignFolders, foldersSkipped, openCommand, rootListingCut,
} from '../src/campaignRunsModel.js'

const row = (runId, extra = {}) => ({ run_id: runId, task_id: 't', phase: 'finished',
  best_metric: 0.5, nodes: 3, engine_running: false, mtime: 1_790_000_000, ...extra })

test('a folder lists its runs, and what cannot be shown is dropped rather than thrown on', () => {
  const folders = campaignFolders({ folders: [
    { folder: 'campA', run_root: '/runs/campA', runs: [row('seed1'), row('', {}), null,
      row('seed2', { best_metric: 'NaN', engine_running: 'yes', nodes: -1, mtime: null })],
    runs_skipped: 2 },
    { folder: 'empty', run_root: '/runs/empty', runs: [] },
    { folder: '', run_root: '/runs/x', runs: [row('a')] },
    { folder: 'noroot', runs: [row('a')] },
    'not a folder',
  ] })
  assert.deepEqual(folders.map(f => f.folder), ['campA'])
  const [campA] = folders
  assert.equal(campA.runRoot, '/runs/campA')
  assert.equal(campA.runsSkipped, 2)
  assert.deepEqual(campA.runs.map(r => r.runId), ['seed1', 'seed2'])
  assert.deepEqual(campA.runs[1], { runId: 'seed2', taskId: 't', phase: 'finished',
    bestMetric: null, nodes: null, running: false, external: false, updated: null })
  assert.equal(campA.runs[0].bestMetric, 0.5)
  assert.deepEqual(campaignFolders(null), [])
  assert.deepEqual(campaignFolders({ folders: 'x' }), [])
})

test('the bounds are the server\'s own, and a count is never negative or fractional', () => {
  const many = Array.from({ length: MAX_CAMPAIGN_RUNS + 5 }, (_, i) => row(`s${i}`))
  const [folder] = campaignFolders({ folders: [{ folder: 'c', run_root: '/r/c', runs: many }] })
  assert.equal(folder.runs.length, MAX_CAMPAIGN_RUNS)
  assert.equal(foldersSkipped({ folders_skipped: 3 }), 3)
  for (const bad of [-1, 1.5, '3', null, undefined]) {
    assert.equal(foldersSkipped({ folders_skipped: bad }), 0, String(bad))
  }
})

test('the open command is quoted only when it must be, and runs as shown', () => {
  assert.equal(openCommand('/data/runs/campA'), 'looplab ui --run-root /data/runs/campA')
  assert.equal(openCommand('C:\\runs\\campA'), 'looplab ui --run-root C:\\runs\\campA')
  assert.equal(openCommand('/data/my runs/camp'), "looplab ui --run-root '/data/my runs/camp'")
  assert.equal(openCommand("/data/o'brien/camp"),
    "looplab ui --run-root '/data/o'\\''brien/camp'")
  assert.equal(openCommand('/r/$(rm -rf ~)'), "looplab ui --run-root '/r/$(rm -rf ~)'")
  assert.equal(openCommand(''), '')
  assert.equal(openCommand(null), '')
})

test('a backslash is quoted on a POSIX path and plain only in a Windows drive path', () => {
  // Critic 2026-09-29: unquoted, `camp\A` ran as `campA` in sh.
  assert.equal(openCommand('/data/runs/camp\\A'), "looplab ui --run-root '/data/runs/camp\\A'")
  assert.equal(openCommand('C:\\runs\\camp_1'), 'looplab ui --run-root C:\\runs\\camp_1')
  assert.equal(openCommand('C:\\my runs\\camp'), "looplab ui --run-root 'C:\\my runs\\camp'")
})

test('a listing the server cut at its entry bound says so, per folder and for the root', () => {
  const [folder] = campaignFolders({ folders: [
    { folder: 'c', run_root: '/r/c', runs: [row('s1')], listing_cut: true }] })
  assert.equal(folder.listingCut, true)
  const [plain] = campaignFolders({ folders: [
    { folder: 'c', run_root: '/r/c', runs: [row('s1')], listing_cut: 'yes' }] })
  assert.equal(plain.listingCut, false, 'only a real true is a cut')
  assert.equal(rootListingCut({ listing_cut: true }), true)
  assert.equal(rootListingCut({ listing_cut: 1 }), false)
  assert.equal(rootListingCut(null), false)
})
