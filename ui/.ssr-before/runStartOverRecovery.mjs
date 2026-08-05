const STORAGE_PREFIX = "ll.run-start-over.";
const SCHEMA = 3;
const PREVIOUS_SCHEMA = 2;
const LEGACY_SCHEMA = 1;
const PHASES = new Set([
	"submitting",
	"pending",
	"accepted",
	"unknown"
]);
const RUN_GENERATION_RE = /^[0-9a-f]{64}$/;
const OPERATION_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const INTENT_KEYS = new Set([
	"schema",
	"runId",
	"expectedGeneration",
	"operationId",
	"replacementGeneration",
	"phase",
	"updatedAt"
]);
const PREVIOUS_INTENT_KEYS = new Set([
	"schema",
	"runId",
	"expectedGeneration",
	"operationId",
	"phase",
	"updatedAt"
]);
const LEGACY_INTENT_KEYS = new Set([
	"schema",
	"runId",
	"expectedGeneration",
	"phase",
	"updatedAt"
]);
const storageKey = (runId) => STORAGE_PREFIX + encodeURIComponent(String(runId || ""));
const safeRunId = (value) => typeof value === "string" && value.length > 0 && value.length <= 512 && !/[\u0000-\u001f\u007f]/.test(value);
const hasExactKeys = (value, keys) => Object.keys(value).length === keys.size && Object.keys(value).every((key) => keys.has(key));
const browserStorage = (storage) => {
	if (storage !== undefined) return storage;
	try {
		return typeof sessionStorage === "undefined" ? null : sessionStorage;
	} catch {
		return null;
	}
};
export function validRunStartOverIntent(value, runId = value?.runId) {
	return !!value && typeof value === "object" && !Array.isArray(value) && hasExactKeys(value, INTENT_KEYS) && value.schema === SCHEMA && safeRunId(value.runId) && value.runId === String(runId || "") && typeof value.expectedGeneration === "string" && RUN_GENERATION_RE.test(value.expectedGeneration) && typeof value.operationId === "string" && OPERATION_ID_RE.test(value.operationId) && (value.replacementGeneration === null || typeof value.replacementGeneration === "string" && RUN_GENERATION_RE.test(value.replacementGeneration) && value.replacementGeneration !== value.expectedGeneration) && (value.phase !== "accepted" || value.replacementGeneration !== null) && PHASES.has(value.phase) && Number.isSafeInteger(value.updatedAt) && value.updatedAt > 0;
}
const migrateLegacyIntent = (value, runId) => {
	if (value && typeof value === "object" && !Array.isArray(value) && hasExactKeys(value, PREVIOUS_INTENT_KEYS) && value.schema === PREVIOUS_SCHEMA && safeRunId(value.runId) && value.runId === String(runId || "") && typeof value.expectedGeneration === "string" && RUN_GENERATION_RE.test(value.expectedGeneration) && typeof value.operationId === "string" && OPERATION_ID_RE.test(value.operationId) && PHASES.has(value.phase) && Number.isSafeInteger(value.updatedAt) && value.updatedAt > 0) {
		return {
			...value,
			schema: SCHEMA,
			// Schema 2 never persisted the generation from the matching success receipt. An old
			// `accepted` flag therefore cannot authorize a route handoff; require an exact-op retry.
			phase: value.phase === "accepted" ? "unknown" : value.phase,
			replacementGeneration: null
		};
	}
	if (!value || typeof value !== "object" || Array.isArray(value) || !hasExactKeys(value, LEGACY_INTENT_KEYS) || value.schema !== LEGACY_SCHEMA || !safeRunId(value.runId) || value.runId !== String(runId || "") || typeof value.expectedGeneration !== "string" || !RUN_GENERATION_RE.test(value.expectedGeneration) || !PHASES.has(value.phase) || !Number.isSafeInteger(value.updatedAt) || value.updatedAt <= 0) return null;
	const digest = value.expectedGeneration;
	return {
		...value,
		schema: SCHEMA,
		// Pre-operation-id builds used the generation fence as their identity. Preserve it as a stable
		// UUID-shaped token so a reload can safely retry that exact request instead of inventing a new one.
		operationId: `${digest.slice(0, 8)}-${digest.slice(8, 12)}-${digest.slice(12, 16)}-${digest.slice(16, 20)}-${digest.slice(20, 32)}`,
		replacementGeneration: null,
		phase: value.phase === "accepted" ? "unknown" : value.phase
	};
};
export function createRunStartOverIntent(runId, expectedGeneration, operationId, phase = "submitting", now = Date.now(), replacementGeneration = null) {
	const intent = {
		schema: SCHEMA,
		runId: String(runId || ""),
		expectedGeneration: String(expectedGeneration || ""),
		operationId: String(operationId || "").toLowerCase(),
		replacementGeneration: replacementGeneration == null ? null : String(replacementGeneration || "").toLowerCase(),
		phase,
		updatedAt: Math.trunc(now)
	};
	return validRunStartOverIntent(intent, runId) ? intent : null;
}
export function loadRunStartOverIntent(runId, storage) {
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
		const parsed = JSON.parse(raw);
		const intent = validRunStartOverIntent(parsed, runId) ? parsed : migrateLegacyIntent(parsed, runId);
		if (!intent) return {
			kind: "corrupt",
			intent: null
		};
		if (parsed !== intent) {
			try {
				target.setItem(storageKey(runId), JSON.stringify(intent));
			} catch {
				return {
					kind: "active",
					intent,
					storageUnavailable: true
				};
			}
		}
		return {
			kind: "active",
			intent
		};
	} catch {
		return {
			kind: "corrupt",
			intent: null
		};
	}
}
export function saveRunStartOverIntent(intent, storage, expectedOperationId = null) {
	if (!validRunStartOverIntent(intent)) return false;
	const target = browserStorage(storage);
	if (!target) return false;
	try {
		if (expectedOperationId != null) {
			const current = loadRunStartOverIntent(intent.runId, target);
			if (current.kind !== "active" || current.intent.operationId !== String(expectedOperationId).toLowerCase()) return false;
		}
		target.setItem(storageKey(intent.runId), JSON.stringify(intent));
		const restored = loadRunStartOverIntent(intent.runId, target);
		return restored.kind === "active" && JSON.stringify(restored.intent) === JSON.stringify(intent);
	} catch {
		return false;
	}
}
export function clearRunStartOverIntent(runId, storage, expectedOperationId = null) {
	const target = browserStorage(storage);
	if (!target) return false;
	try {
		if (expectedOperationId != null) {
			const current = loadRunStartOverIntent(runId, target);
			if (current.kind !== "active" || current.intent.operationId !== String(expectedOperationId).toLowerCase()) return false;
		}
		target.removeItem(storageKey(runId));
		return target.getItem(storageKey(runId)) == null;
	} catch {
		return false;
	}
}
export function listRunStartOverRecoveries(storage) {
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
			const recovery = loadRunStartOverIntent(runId, target);
			if (recovery.kind === "active" || recovery.kind === "corrupt") {
				found.push({
					runId,
					kind: recovery.kind
				});
			}
		}
	} catch {
		return found;
	}
	return found.sort((left, right) => left.runId.localeCompare(right.runId));
}
