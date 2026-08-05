export const AUTHORING_OPERATION_STORAGE_PREFIX = "ll.authoring-operation.";
export const authoringRecoveryStorageKey = (kind, name) => AUTHORING_OPERATION_STORAGE_PREFIX + encodeURIComponent(`${String(kind || "")}\u0000${String(name || "")}`);
const authoringStorage = () => {
	try {
		return typeof sessionStorage === "undefined" ? null : sessionStorage;
	} catch {
		return null;
	}
};
// RunView owns the generation fence and may have to protect Authoring before the lazy panel has
// committed. Read only app-owned exact snapshots here; panels.jsx performs the full envelope
// validation and treats every invalid matching key as a damaged recovery record.
export function inspectAuthoringRecoveryStorage() {
	const storage = authoringStorage();
	if (!storage) return {
		available: false,
		count: 0,
		records: []
	};
	try {
		const records = [];
		for (let index = 0; index < storage.length; index++) {
			const key = storage.key(index);
			if (!key?.startsWith(AUTHORING_OPERATION_STORAGE_PREFIX)) continue;
			const raw = storage.getItem(key);
			if (raw != null) records.push({
				key,
				raw
			});
		}
		return {
			available: true,
			count: records.length,
			records
		};
	} catch {
		return {
			available: false,
			count: 0,
			records: []
		};
	}
}
