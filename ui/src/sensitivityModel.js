// The Parameter-sensitivity panel's bars (`panels.jsx::SensitivityPanel`): the latest MEASURED
// |Δmetric| per parameter across every `ablate` event — the pure half beside the React one.
//
// ABSENT IS NOT ZERO (`test/absentIsNotZero.test.js` names the class). An impact of `null` is a probe
// that measured nothing: code-block mode's "the run broke without this block", or a parameter probe
// of a parent with no measured metric (doc 67 67.4). `Math.abs(null)` is 0, so it drew a bar of 0
// and — the latest ablation winning per parameter — erased an earlier REAL measurement of the same
// parameter (critic 2026-09-26, driven: `[{x:0.31,y:0.02},{x:null,y:null}]` read as two zeros).
export function sensitivityBars (ablations) {
  const impacts = {}
  for (const ablation of ablations || []) {
    const rows = (ablation && typeof ablation.impacts === 'object' && ablation.impacts) || {}
    for (const [label, value] of Object.entries(rows)) {
      if (typeof value === 'number' && Number.isFinite(value)) impacts[label] = Math.abs(value)
    }
  }
  return Object.entries(impacts)
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => b.value - a.value)
}
