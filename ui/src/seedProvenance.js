// Cross-run SEED provenance, READ side (doc 67 67.2): what the DAG may say of a node imported from
// another run — by the server's `import` action or by `Settings.seed_from_run` — beside the source's
// metric. Pure, so `node --test` drives it (`test/seedProvenance.test.js`); `Dag.jsx` renders it.
//
// WHY IT EXISTS (critic 2026-09-26, driven). A seed's receipt carries the evaluation-contract VERDICT
// (`same` / `different` / `unknown`, `engine/seed_from_run.py::seed_verdict`) and the sentence
// naming what differs, and nothing in the browser read it: a web operator saw "source metric 0.91"
// on the chip and never that 0.91 was measured on a different scale. And a seed from OUTSIDE the
// runs root carries no `run_id` — the receipt keeps it only for a sibling, which is what the chip
// LINKS and what the portfolio map draws as a `seeded_from` edge — so such a node showed nothing.

export const SEED_CONTRACT_VERDICTS = Object.freeze(['same', 'different', 'unknown'])

// The source run's display name and whether it can be linked: a sibling's `run_id` links to its run;
// a launch seed from elsewhere names its directory's last component and links nowhere.
export function seedSource(origin) {
  if (!origin || typeof origin !== 'object') return null
  if (typeof origin.run_id === 'string' && origin.run_id) {
    return { name: origin.run_id, linkable: true, nodeId: origin.node_id }
  }
  const dir = typeof origin.run_dir === 'string' ? origin.run_dir : ''
  if (origin.seed_from_run !== true || !dir) return null
  const name = dir.split(/[\\/]/).filter(Boolean).pop() || dir
  return { name, linkable: false, nodeId: origin.node_id, dir }
}

// ` · evaluation contract: <verdict>` plus the receipt's sentence, or '' when the receipt carries no
// verdict (the server's `import` action records none — absent is not `same`).
export function seedContractText(origin) {
  if (!origin || typeof origin !== 'object') return ''
  const verdict = SEED_CONTRACT_VERDICTS.includes(origin.eval_contract) ? origin.eval_contract : null
  if (!verdict) return ''
  const note = typeof origin.eval_contract_note === 'string' ? origin.eval_contract_note.trim() : ''
  return ` · evaluation contract: ${verdict}` + (note ? ` — ${note}` : '')
}
