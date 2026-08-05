// One current-state visibility predicate for every experiment projection. Tombstones and the
// run-level aborted id set remain in the fold for replay/audit, but they are no longer live
// experiments and must not affect DAG geometry, concept counts, charts, or report aggregates.
export function nodeIsActive(node, state = null, aborted = null) {
	if (!node || node.tombstoned) return false;
	const excluded = aborted || new Set((state?.aborted_nodes || []).map(Number));
	return !excluded.has(Number(node.id));
}
export function activeNodeMap(nodes = {}, state = null) {
	const out = {};
	const aborted = new Set((state?.aborted_nodes || []).map(Number));
	for (const [key, node] of Object.entries(nodes || {})) {
		if (nodeIsActive(node, state, aborted)) out[key] = node;
	}
	return out;
}
// The rows whose membership is AUTHORITATIVE, plus a count of the active tagged rows withheld because
// theirs is not. A materialization receipt is minted PER NODE (replay.py::_materialize_concept_deltas
// stamps `node_concept_materialization_receipts[nid]`), and it is complete information about that node:
// a root delta node inherits the run base's own problems into its own receipt (`reasons.update(
// base_reasons)`), so a row with no receipt of its own is exact regardless of what the base receipt says.
// That makes withholding a per-ROW operation. Collapsing the receipts to one run-wide verdict and then
// dropping every row -- what this file used to force ConceptChipBar to do -- discards the memberships the
// fold proved exact: `b2-validate` withheld all 21 memberships across 6 clean experiments because ONE
// node (#4, whose delta parent #3 was never tagged) could not be resolved. The server never did this;
// serve/concept_frame.py::bounded_inputs keeps every row and stamps the reason on the frame instead.
// This REPLACES a lifecycle-only `activeNodeConcepts`, deliberately rather than sitting beside it: a
// projection that applies only the tombstone/abort filter is the one every caller reached for and is
// exactly how a degraded row gets counted as exact. There is one concept projection, and it is this one.
export function authoritativeNodeConcepts(state = null) {
	const nodes = state?.nodes || {};
	const aborted = new Set((state?.aborted_nodes || []).map(Number));
	const receipts = state?.node_concept_materialization_receipts;
	const wellFormed = receipts == null || typeof receipts === "object" && !Array.isArray(receipts);
	const degraded = wellFormed ? receipts || {} : {};
	const concepts = {};
	let withheld = 0;
	for (const [key, ids] of Object.entries(state?.node_concepts || {})) {
		if (!nodeIsActive(nodes[key], state, aborted)) continue;
		// A receipt STORE that is not a map is structural corruption, and this projection must fail closed
		// on its own rather than relying on its one caller checking the status first.
		if (!wellFormed) {
			withheld += 1;
			continue;
		}
		// Absence within a degraded row is not truth, so it may not reach chip counts, search, selection
		// or DAG filtering -- but its EXISTENCE is truth, and stays disclosed through `withheld`.
		if (Object.hasOwn(degraded, key)) {
			withheld += 1;
			continue;
		}
		concepts[key] = ids;
	}
	return {
		concepts,
		withheld
	};
}
// A retained concept row is authoritative only when replay emitted no
// materialization receipt for it. The trust boundary is receipt presence (never the reason text);
// malformed envelopes fail unavailable while future partial reasons remain safely display-only.
const UNAVAILABLE_REASON = /^(?:concept_mode_|delta_dependency_|invalid_consolidation)/;
const receiptStoreCache = new WeakMap();
function receiptStatus(receipt) {
	const reasons = receipt?.reasons;
	if (!receipt || typeof receipt !== "object" || Array.isArray(receipt) || Object.keys(receipt).length !== 2 || !Object.hasOwn(receipt, "status") || !Object.hasOwn(receipt, "reasons") || !Array.isArray(reasons) || !reasons.length || !reasons.every((reason) => typeof reason === "string" && reason)) return "invalid";
	const unavailable = reasons.some((reason) => UNAVAILABLE_REASON.test(reason));
	if (receipt.status === "partial") return unavailable ? "invalid" : "partial";
	return receipt.status === "unavailable" && unavailable ? "unavailable" : "invalid";
}
function receiptStoreValid(receipts, nodes) {
	const cached = receiptStoreCache.get(receipts);
	if (cached?.nodes === nodes) return cached.valid;
	const valid = Object.entries(receipts).every(([key, receipt]) => {
		const id = Number(key);
		return Number.isSafeInteger(id) && id >= 0 && String(id) === key && nodes && typeof nodes === "object" && !Array.isArray(nodes) && Object.hasOwn(nodes, key) && receiptStatus(receipt) !== "invalid";
	});
	receiptStoreCache.set(receipts, {
		nodes,
		valid
	});
	return valid;
}
// Omit nodeId for current-view aggregate truth; pass it for one node's theme/tag projection.
export function conceptMaterializationStatus(state = null, nodeId = undefined) {
	let aggregate = "complete";
	if (state?.run_base_concept_receipt != null) {
		const status = receiptStatus(state.run_base_concept_receipt);
		if (status === "invalid" || status === "unavailable") return "unavailable";
		aggregate = status;
	}
	const receipts = state?.node_concept_materialization_receipts;
	if (receipts == null) return aggregate;
	if (typeof receipts !== "object" || Array.isArray(receipts)) return "unavailable";
	const nodes = state?.nodes;
	// ConceptFrame rejects an orphan or malformed receipt globally. Validate the whole
	// immutable snapshot once before serving any node-specific theme, including inactive receipt rows.
	if (!receiptStoreValid(receipts, nodes)) return "unavailable";
	if (nodeId !== undefined) {
		const key = String(nodeId);
		if (!Object.hasOwn(receipts, key)) return aggregate;
		const status = receiptStatus(receipts[key]);
		return status === "unavailable" ? status : "partial";
	}
	const aborted = new Set((state?.aborted_nodes || []).map(Number));
	// A per-node receipt is scoped to ITS node. Returning `unavailable` for the whole run the moment one
	// active node carried an unavailable receipt was the bug behind "Concepts UNAVAILABLE" on runs whose
	// concepts plainly existed: it answered "I could not determine membership" for a run where membership
	// was determined, exactly, for every other experiment. The two run-SCOPED failures above still answer
	// `unavailable` -- a degraded/malformed run-base receipt and a malformed receipt store are statements
	// about the whole projection. Anything keyed by node degrades the run to `partial` and is withheld
	// row-wise by `authoritativeNodeConcepts`.
	// `receiptStoreValid` already rejected every `invalid` envelope, so a surviving receipt is `partial` or
	// `unavailable` -- either way that node's row is not exact and the RUN is, at best, partially known.
	for (const key of Object.keys(receipts)) {
		if (nodeIsActive(nodes[key], state, aborted)) return "partial";
	}
	return aggregate;
}
