import { computeGroups, nodeGroupMap } from "./grouping.mjs";
import { activeNodeMap } from "./nodeProjection.mjs";
// Build the cheap, current render projection on every folded-state revision, but key expensive
// geometry only on fields layoutWithGroups actually reads. Metric/report/status/liveness churn is
// intentionally absent unless the selected grouping mode turns it into topology (metric groups).
export function dagLayoutProjection(state, groupMode = "none") {
	const nodes = activeNodeMap(state?.nodes || {}, state);
	const groups = groupMode === "none" ? new Map() : computeGroups(nodes, groupMode, state);
	const nodeGroup = nodeGroupMap(groups);
	const rows = Object.values(nodes).sort((a, b) => a.id - b.id).map((node) => {
		const parents = [...new Set((node.parent_ids || []).filter((parent) => parent in nodes))].sort((a, b) => a - b);
		// Niche cell rank reads raw params (not only the rounded group label), so retain that immutable
		// ordering input. Other modes are fully described by node→group membership.
		const rankInput = groupMode === "niche" ? Object.entries(node.idea?.params || {}).sort() : null;
		return [
			node.id,
			parents,
			nodeGroup.get(node.id) ?? null,
			rankInput
		];
	});
	return {
		nodes,
		groups,
		nodeGroup,
		key: JSON.stringify(rows)
	};
}
// Compute this every render rather than memoizing on Set identity: callers are protected even if a
// parent mutates a Set in place.
export function dagCollapsedKey(collapsed = new Set()) {
	return JSON.stringify([...collapsed].sort());
}
