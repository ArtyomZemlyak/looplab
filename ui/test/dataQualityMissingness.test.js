import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

// `state.data_profile` is whatever the profiler that ran actually recorded, and they do not all
// record the same things: `core/profile.py::profile_column` writes the full set, but real logs under
// `runs/` carry `data_profiled` events whose columns are only `{count, dtype, constant}`
// (`live-nosignal`, `live-periodic`, and every sim run beside them). The missing% cell used to read
// `(s.missing_frac || 0) * 100`, so those rows rendered a confident "0% missing" next to an `—` in
// every other cell of the SAME row — the one number on the row that nobody measured was also the
// only one stated as fact.
//
// Both directions are driven here on purpose. A row that measured zero missing values must keep
// saying `0`, so a "fix" that blanks the column whenever it is falsy cannot pass either.
test('the data-quality table distinguishes zero missing values from unmeasured missingness', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { DataQualityPanel } = await vite.ssrLoadModule('/src/panels.jsx')
    const markup = renderToStaticMarkup(React.createElement(DataQualityPanel, {
      onClose() {},
      state: {
        data_profile: {
          // A full profile whose measured missing fraction really is zero.
          measured: {
            count: 400, dtype: 'numeric', constant: false, missing_frac: 0.0, n_unique: 7,
            min: -1, max: 1, mean: 0,
          },
          // The shape a real `data_profiled` event carries when the profiler recorded no
          // missingness at all.
          unmeasured: { count: 400, dtype: 'numeric', constant: false },
        },
      },
    }))

    const cells = name => {
      const at = markup.indexOf(`>${name}<`)
      assert.ok(at > 0, `column ${name} must be in the table`)
      const row = markup.slice(at + 1, markup.indexOf('</tr>', at))
      return row.split(/<[^>]*>/g).map(part => part.trim()).filter(Boolean)
    }

    // column | dtype | missing% | unique | min | max | mean
    assert.deepEqual(cells('measured').slice(0, 4), ['measured', 'numeric', '0', '7'])
    assert.deepEqual(cells('unmeasured').slice(0, 4), ['unmeasured', 'numeric', '—', '—'],
      'an unrecorded missing fraction must read as unknown, not as 0% missing')
  } finally {
    await vite.close()
  }
})
