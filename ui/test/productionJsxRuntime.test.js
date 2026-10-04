import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { resolve, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import config from '../vite.config.js'

const root = fileURLToPath(new URL('..', import.meta.url))

// A transform that parses JSX can still ship an unbound React factory or change keys/children.
// Execute the real minified production SSR pipeline, with automatic JSX as the control. Real
// components catch missing imports; the small probe covers React's special props and falsy children.
test('production JSX preserves real reader markup and React children/key semantics', async () => {
  const directory = await mkdtemp(join(root, 'test/.jsx-runtime-'))
  const entry = join(directory, 'entry.jsx')
  const components = {
    ThemeSwitcher: 'ThemeSwitcher.jsx', EnergyToggle: 'EnergyToggle.jsx', GlobalMenu: 'GlobalMenu.jsx',
    ConceptView: 'ConceptView.jsx', ExperimentResult: 'ExperimentResult.jsx',
    PanelResourceNotice: 'PanelResourceNotice.jsx', OpIcon: 'icons.jsx',
  }
  const imports = Object.entries(components).map(([name, file]) =>
    `import ${['PanelResourceNotice', 'OpIcon'].includes(name) ? `{ ${name} }` : name}
      from ${JSON.stringify(join(root, 'src', file))}`)
  await writeFile(entry, `import React from 'react'
    import { renderToStaticMarkup } from 'react-dom/server'
    ${imports.join('\n')}
    export function readings() {
      const frames = [<ThemeSwitcher/>, <EnergyToggle/>, <GlobalMenu/>, <OpIcon name="bolt"/>,
        <ConceptView runId="demo" state={{nodes: {}}} />]
      for (const metric of [undefined, null, NaN, 0, -1, 2.5, 'unknown']) {
        const node = {id: 1, status: 'evaluated', attempt: 0, parent_ids: [], metric}
        frames.push(<ExperimentResult node={node} state={{nodes: {1: node}, direction: 'max'}} />)
      }
      for (const language of ['en', 'ru']) for (const status of ['ready', 'loading', 'stale', 'error'])
        frames.push(<PanelResourceNotice language={language} label="Evidence" resource={{status}} />)
      frames.push(<div aria-expanded={false}>{false && <b/>}{null}{undefined}{0}{['a', 'b']}</div>)
      return frames.map(frame => renderToStaticMarkup(frame))
    }
    export function keys() {
      const props = {key: 'spread', title: 'recorded'}
      return [<b key="explicit" {...props}/>, <b {...props} key="last"/>]
        .map(element => ({key: element.key, title: element.props.title}))
    }
  `)
  try {
    const results = []
    for (const automatic of [false, true]) {
      const result = await build({ ...config, root, configFile: false, logLevel: 'silent',
        plugins: automatic ? [react()] : config.plugins,
        build: { ...config.build, ssr: entry, write: false, manifest: false,
          rollupOptions: { ...config.build.rollupOptions, input: entry,
            output: { ...config.build.rollupOptions.output, codeSplitting: undefined } } } })
      const chunk = result.output.find(item => item.type === 'chunk' && item.isEntry)
      const output = join(directory, automatic ? 'automatic.mjs' : 'shipped.mjs')
      await writeFile(output, chunk.code)
      const module = await import(pathToFileURL(output).href)
      results.push({ readings: module.readings(), keys: module.keys() })
    }
    assert.deepEqual(results[0], results[1])
    assert.ok(results[0].readings[0].includes('aria-expanded="false"'))
    assert.ok(results[0].readings[3].includes('href="#bolt"'))
    assert.deepEqual(results[0].keys.map(row => row.key), ['spread', 'last'])
    assert.ok(!results[0].readings.slice(0, 3).some(markup => />0</.test(markup)))
  } finally {
    // This test owns only its mkdtemp output beneath ui/test, never a build being served.
    assert.ok(resolve(directory).startsWith(resolve(root, 'test/.jsx-runtime-')))
    await rm(directory, { recursive: true, force: true })
  }
})
