import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const source = () => readFile(new URL('../src/RunList.jsx', import.meta.url), 'utf8')

test('RunList resources preserve last-good data and distinguish initial from refresh failure', async () => {
  const text = await source()
  const hook = text.slice(text.indexOf('function useResource'), text.indexOf('function ResourceNotice'))

  assert.match(hook, /const \[state, setState\] = useState\('loading'\)/)
  assert.match(hook, /\.then\(value => \{ setData\(value\); setState\('ready'\) \}\)/)
  assert.match(hook, /\.catch\(\(\) => setState\(current => \['ready', 'stale'\]\.includes\(current\) \? 'stale' : 'error'\)\)/)
  assert.doesNotMatch(hook.slice(hook.indexOf('.catch')), /setData/,
    'a failed initial load or refresh must not overwrite the last successful payload')
  assert.match(text, /useResource\(listProjects, \{ projects: \[\], assignments: \{\} \}\)/)
  assert.match(text, /useResource\(listSupertasks, \{ supertasks: \[\], assignments: \{\} \}\)/)
  assert.doesNotMatch(text, /listSupertasks\(\)\.then\(setSuperdata\)\.catch\(\(\) => \{\}\)/,
    'super-task failures must never be swallowed')
})

test('RunList exposes safe loading, error, stale, and retry semantics', async () => {
  const text = await source()
  const notice = text.slice(text.indexOf('function ResourceNotice'), text.indexOf('// Module-scope'))

  assert.match(notice, /state === 'loading'[\s\S]*role="status">\{label\} loading/)
  assert.match(notice, /state === 'stale'/)
  assert.match(notice, /role=\{stale \? 'status' : 'alert'\}/)
  assert.match(notice, /\{label\}: \{stale \? 'Last loaded data; refresh failed\.' : 'Unavailable\.'/)
  assert.match(notice, /<button className="btn sm" onClick=\{retry\}>Retry<\/button>/)
  assert.doesNotMatch(notice, /error\?\.message|String\(error\)/,
    'resource notices use bounded copy instead of reflecting provider text')
})

test('project and super-task empties require a successful current response', async () => {
  const text = await source()

  assert.match(text, /projectsState === 'ready' && !proj\.projects\.length[\s\S]*No projects yet\./)
  assert.match(text, /state === 'ready' && !supertasks\.length[\s\S]*No super-tasks yet\./)
  assert.match(text, /\['ready', 'stale'\]\.includes\(projectsState\)[\s\S]*<MapView/,
    'the map may use current or explicitly stale last-good project metadata')
  assert.doesNotMatch(text, /projectsState === 'error'[\s\S]*<MapView/,
    'the map must stay closed when no successful project payload exists')
  assert.match(text, /!stModal && <ResourceNotice state=\{superState\}/)
  assert.match(text, /<SuperTaskModal supertasks=\{superdata\.supertasks\} state=\{superState\} onRetry=\{loadSupers\}/,
    'the active modal must own the super-task error and retry surface')
})
