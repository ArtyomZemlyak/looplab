const seq = (value) => Number.isSafeInteger(value) && value >= 0 ? value : null;
export function cardEditReflected(card, patch, baseline, expectedEventSeq) {
	const foldedEventSeq = seq(card?.statement_edit_seq);
	const expected = seq(expectedEventSeq);
	if (expected != null && foldedEventSeq != null && foldedEventSeq >= expected) return true;
	if (typeof card?.statement !== "string" || typeof patch?.statement !== "string") return false;
	if (card.statement === patch.statement) return true;
	return typeof baseline === "string" && card.statement.length > 0 && patch.statement.startsWith(card.statement) && !baseline.startsWith(card.statement);
}
export function cardControlSubmission(current, id, kind, patch, editBaseline, saving) {
	const entry = current[id] || {};
	return {
		...current,
		[id]: {
			...entry,
			editEventSeq: kind === "edit" ? null : entry.editEventSeq,
			updates: {
				...entry.updates || {},
				[kind]: patch
			},
			editBaseline: editBaseline ?? entry.editBaseline,
			pending: {
				kind,
				phase: "submitting"
			},
			notice: {
				tone: "pending",
				text: saving
			}
		}
	};
}
