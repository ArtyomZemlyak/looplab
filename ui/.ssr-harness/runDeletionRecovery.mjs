const STORAGE_PREFIX = "ll.run-deletion.";
const SCHEMA = 1;
const LOCAL_PHASES = new Set([
	"submitting",
	"pending",
	"unknown"
]);
const GENERATION_RE = /^[0-9a-f]{64}$/;
const UUID_V4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SERVER_PHASES = new Set([
	"prepared",
	"fenced",
	"quarantining",
	"quarantine_ambiguous",
	"quarantined",
	"metadata_committed",
	"purging",
	"retiring_fence",
	"succeeded"
]);
const INTENT_KEYS = new Set([
	"schema",
	"runId",
	"expectedGeneration",
	"expectedSeq",
	"operationId",
	"phase",
	"serverPhase",
	"updatedAt"
]);
const storageKey = (runId) => STORAGE_PREFIX + encodeURIComponent(String(runId || ""));
const safeRunId = (value) => typeof value === "string" && value.length > 0 && value.length <= 512 && !/[\u0000-\u001f\u007f]/.test(value);
const exactKeys = (value) => Object.keys(value).length === INTENT_KEYS.size && Object.keys(value).every((key) => INTENT_KEYS.has(key));
const browserStorage = (storage) => {
	if (storage !== undefined) return storage;
	try {
		return typeof sessionStorage === "undefined" ? null : sessionStorage;
	} catch {
		return null;
	}
};
export function validRunDeletionIntent(value, runId = value?.runId) {
	return !!value && typeof value === "object" && !Array.isArray(value) && exactKeys(value) && value.schema === SCHEMA && safeRunId(value.runId) && value.runId === String(runId || "") && GENERATION_RE.test(value.expectedGeneration || "") && Number.isSafeInteger(value.expectedSeq) && value.expectedSeq >= -1 && UUID_V4_RE.test(value.operationId || "") && LOCAL_PHASES.has(value.phase) && (value.serverPhase === null || SERVER_PHASES.has(value.serverPhase)) && Number.isSafeInteger(value.updatedAt) && value.updatedAt > 0;
}
export function createRunDeletionIntent(runId, expectedGeneration, expectedSeq, operationId, phase = "submitting", serverPhase = null, now = Date.now()) {
	const intent = {
		schema: SCHEMA,
		runId: String(runId || ""),
		expectedGeneration: String(expectedGeneration || "").toLowerCase(),
		expectedSeq,
		operationId: String(operationId || "").toLowerCase(),
		phase,
		serverPhase: serverPhase == null ? null : String(serverPhase).toLowerCase(),
		updatedAt: Math.trunc(now)
	};
	return validRunDeletionIntent(intent, runId) ? intent : null;
}
export function loadRunDeletionIntent(runId, storage) {
	const target = browserStorage(storage);
	if (!target) return {
		kind: "unavailable",
		intent: null
	};
	let raw;
	try {
		raw = target.getItem(storageKey(runId));
	} catch {
		return {
			kind: "unavailable",
			intent: null
		};
	}
	if (raw == null) return {
		kind: "none",
		intent: null
	};
	if (raw === "") return {
		kind: "corrupt",
		intent: null
	};
	try {
		const intent = JSON.parse(raw);
		return validRunDeletionIntent(intent, runId) ? {
			kind: "active",
			intent
		} : {
			kind: "corrupt",
			intent: null
		};
	} catch {
		return {
			kind: "corrupt",
			intent: null
		};
	}
}
export function saveRunDeletionIntent(intent, storage, expectedOperationId = null) {
	if (!validRunDeletionIntent(intent)) return false;
	const target = browserStorage(storage);
	if (!target) return false;
	try {
		const current = loadRunDeletionIntent(intent.runId, target);
		if (expectedOperationId == null) {
			// A fresh destructive identity must never overwrite unresolved or malformed evidence.
			if (current.kind !== "none") return false;
		} else if (current.kind !== "active" || current.intent.operationId !== String(expectedOperationId).toLowerCase()) return false;
		const encoded = JSON.stringify(intent);
		target.setItem(storageKey(intent.runId), encoded);
		return target.getItem(storageKey(intent.runId)) === encoded;
	} catch {
		return false;
	}
}
export function clearRunDeletionIntent(runId, storage, expectedOperationId = null) {
	const target = browserStorage(storage);
	if (!target) return false;
	try {
		if (expectedOperationId != null) {
			const current = loadRunDeletionIntent(runId, target);
			if (current.kind !== "active" || current.intent.operationId !== String(expectedOperationId).toLowerCase()) return false;
		}
		target.removeItem(storageKey(runId));
		return target.getItem(storageKey(runId)) == null;
	} catch {
		return false;
	}
}
export function listRunDeletionRecoveries(storage) {
	const target = browserStorage(storage);
	if (!target) return [];
	const found = [];
	try {
		const length = Math.min(Number(target.length) || 0, 256);
		for (let index = 0; index < length && found.length < 64; index += 1) {
			const key = target.key(index);
			if (typeof key !== "string" || !key.startsWith(STORAGE_PREFIX)) continue;
			let runId;
			try {
				runId = decodeURIComponent(key.slice(STORAGE_PREFIX.length));
			} catch {
				continue;
			}
			if (!safeRunId(runId)) continue;
			const recovery = loadRunDeletionIntent(runId, target);
			if (recovery.kind === "active" || recovery.kind === "corrupt") {
				found.push({
					runId,
					...recovery
				});
			}
		}
	} catch {
		return found;
	}
	return found.sort((left, right) => left.runId.localeCompare(right.runId));
}
