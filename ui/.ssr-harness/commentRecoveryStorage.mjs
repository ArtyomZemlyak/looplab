import { COMMENT_MAX_BYTES } from "./commentsModel.mjs";
export const COMMENT_OPERATION_STORAGE_PREFIX = "ll.comment-operation.";
export const COMMENT_OPERATION_SCHEMA = "looplab.comment-operation-intent/v1";
const RUN_GENERATION_RE = /^[0-9a-f]{64}$/;
const COMMENT_ID_RE = /^cmt_[0-9a-f]{32}$/;
const OPERATION_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const COMMENT_OPERATION_KINDS = new Set([
	"create",
	"edit",
	"resolution"
]);
const COMMENT_OPERATION_KEYS = new Set([
	"schema",
	"operationId",
	"kind",
	"runId",
	"expectedGeneration",
	"nodeId",
	"nodeGeneration",
	"commentId",
	"expectedVersion",
	"text",
	"resolved",
	"updatedAt"
]);
let recoveryRevision = 0;
const recoveryListeners = new Set();
const publishRecoveryChange = () => {
	recoveryRevision += 1;
	for (const listener of recoveryListeners) listener();
};
export const subscribeCommentOperationRecoveries = (listener) => {
	recoveryListeners.add(listener);
	return () => recoveryListeners.delete(listener);
};
export const readCommentOperationRecoveryRevision = () => recoveryRevision;
export const refreshCommentOperationRecoveries = () => publishRecoveryChange();
const isRecord = (value) => !!value && typeof value === "object" && !Array.isArray(value);
const safeText = (value, max) => typeof value === "string" && value.length > 0 && value.length <= max && !/[\u0000-\u001f\u007f]/.test(value);
const safeIndex = (value) => Number.isSafeInteger(value) && value >= 0;
const utf8Bytes = (text) => {
	try {
		return new TextEncoder().encode(String(text)).byteLength;
	} catch {
		return Infinity;
	}
};
const wellFormedText = (text) => {
	if (typeof text !== "string") return false;
	if (typeof text.isWellFormed === "function") return text.isWellFormed();
	for (let index = 0; index < text.length; index += 1) {
		const unit = text.charCodeAt(index);
		if (unit >= 55296 && unit <= 56319) {
			const next = text.charCodeAt(index + 1);
			if (!(next >= 56320 && next <= 57343)) return false;
			index += 1;
		} else if (unit >= 56320 && unit <= 57343) return false;
	}
	return true;
};
const validCommentText = (text) => wellFormedText(text) && text.trim().length > 0 && utf8Bytes(text) <= COMMENT_MAX_BYTES;
const storageTarget = (storage) => {
	if (storage !== undefined) return storage;
	try {
		return typeof sessionStorage === "undefined" ? null : sessionStorage;
	} catch {
		return null;
	}
};
const validIdentity = (identity) => isRecord(identity) && COMMENT_OPERATION_KINDS.has(identity.kind) && safeText(identity.runId, 255) && wellFormedText(identity.runId) && RUN_GENERATION_RE.test(identity.expectedGeneration) && safeIndex(identity.nodeId) && safeIndex(identity.nodeGeneration) && (identity.kind === "create" ? identity.commentId == null : COMMENT_ID_RE.test(identity.commentId || ""));
const identityParts = (identity) => [
	identity.runId,
	identity.expectedGeneration,
	identity.kind,
	String(identity.nodeId),
	String(identity.nodeGeneration),
	identity.commentId || ""
];
export const commentOperationStorageKey = (identity) => validIdentity(identity) ? COMMENT_OPERATION_STORAGE_PREFIX + encodeURIComponent(identityParts(identity).join("\0")) : null;
const parseStorageIdentity = (key) => {
	if (typeof key !== "string" || !key.startsWith(COMMENT_OPERATION_STORAGE_PREFIX)) return null;
	try {
		const parts = decodeURIComponent(key.slice(COMMENT_OPERATION_STORAGE_PREFIX.length)).split("\0");
		if (parts.length !== 6 || !/^(?:0|[1-9]\d*)$/.test(parts[3]) || !/^(?:0|[1-9]\d*)$/.test(parts[4])) return null;
		const identity = {
			runId: parts[0],
			expectedGeneration: parts[1],
			kind: parts[2],
			nodeId: Number(parts[3]),
			nodeGeneration: Number(parts[4]),
			commentId: parts[5] || null
		};
		return validIdentity(identity) && commentOperationStorageKey(identity) === key ? identity : null;
	} catch {
		return null;
	}
};
const sameIdentity = (left, right) => validIdentity(left) && validIdentity(right) && identityParts(left).every((value, index) => value === identityParts(right)[index]);
export const validCommentOperationIntent = (value) => isRecord(value) && Object.keys(value).length === COMMENT_OPERATION_KEYS.size && Object.keys(value).every((key) => COMMENT_OPERATION_KEYS.has(key)) && value.schema === COMMENT_OPERATION_SCHEMA && OPERATION_ID_RE.test(value.operationId || "") && validIdentity(value) && Number.isSafeInteger(value.updatedAt) && value.updatedAt >= 0 && (value.kind === "create" ? value.commentId === null && value.expectedVersion === null && validCommentText(value.text) && value.resolved === null : value.kind === "edit" ? COMMENT_ID_RE.test(value.commentId) && Number.isSafeInteger(value.expectedVersion) && value.expectedVersion >= 1 && validCommentText(value.text) && value.resolved === null : COMMENT_ID_RE.test(value.commentId) && Number.isSafeInteger(value.expectedVersion) && value.expectedVersion >= 1 && value.text === null && typeof value.resolved === "boolean");
export function createCommentOperationIntent({ operationId, kind, runId, expectedGeneration, nodeId, nodeGeneration, commentId = null, expectedVersion = null, text = null, resolved = null }) {
	const intent = {
		schema: COMMENT_OPERATION_SCHEMA,
		operationId: String(operationId || "").toLowerCase(),
		kind,
		runId: String(runId || ""),
		expectedGeneration: String(expectedGeneration || ""),
		nodeId,
		nodeGeneration,
		commentId,
		expectedVersion,
		text,
		resolved,
		updatedAt: Date.now()
	};
	return validCommentOperationIntent(intent) ? intent : null;
}
const parsedIntent = (raw, identity) => {
	let parsed = null;
	try {
		parsed = JSON.parse(raw || "null");
	} catch {
		return null;
	}
	return validCommentOperationIntent(parsed) && sameIdentity(parsed, identity) ? parsed : null;
};
export function inspectCommentOperation(identity, storage = undefined) {
	const target = storageTarget(storage);
	const key = commentOperationStorageKey(identity);
	if (!target || !key) return {
		kind: "unavailable",
		key
	};
	try {
		const raw = target.getItem(key);
		if (raw == null) return {
			kind: "none",
			key
		};
		const intent = parsedIntent(raw, identity);
		return intent ? {
			kind: "valid",
			key,
			raw,
			intent: {
				...intent,
				storageKey: key,
				storageRaw: raw
			}
		} : {
			kind: "damaged",
			key,
			raw,
			identity: { ...identity }
		};
	} catch {
		return {
			kind: "unavailable",
			key
		};
	}
}
export function saveCommentOperationIntent(intent, storage = undefined) {
	const target = storageTarget(storage);
	const key = commentOperationStorageKey(intent);
	if (!target || !key || !validCommentOperationIntent(intent)) return null;
	try {
		const existingRaw = target.getItem(key);
		if (existingRaw != null) {
			const existing = parsedIntent(existingRaw, intent);
			if (!existing || existing.operationId !== intent.operationId || existing.expectedVersion !== intent.expectedVersion || existing.text !== intent.text || existing.resolved !== intent.resolved) return null;
			publishRecoveryChange();
			return {
				...existing,
				storageKey: key,
				storageRaw: existingRaw
			};
		}
		const raw = JSON.stringify(intent);
		target.setItem(key, raw);
		if (target.getItem(key) !== raw) return null;
		publishRecoveryChange();
		return {
			...intent,
			storageKey: key,
			storageRaw: raw
		};
	} catch {
		return null;
	}
}
export function commentOperationIntentStored(intent, storage = undefined) {
	const target = storageTarget(storage);
	if (!target || !intent?.storageKey || typeof intent.storageRaw !== "string") return false;
	try {
		return target.getItem(intent.storageKey) === intent.storageRaw;
	} catch {
		return false;
	}
}
export function clearCommentOperationIntent(intent, storage = undefined) {
	const target = storageTarget(storage);
	if (!target || !intent?.storageKey || typeof intent.storageRaw !== "string") return false;
	try {
		if (target.getItem(intent.storageKey) !== intent.storageRaw) return false;
		target.removeItem(intent.storageKey);
		const cleared = target.getItem(intent.storageKey) == null;
		if (cleared) publishRecoveryChange();
		return cleared;
	} catch {
		return false;
	}
}
export function clearDamagedCommentOperation(recovery, storage = undefined) {
	const target = storageTarget(storage);
	if (!target || !recovery?.key || typeof recovery.raw !== "string") return false;
	try {
		if (target.getItem(recovery.key) !== recovery.raw) return false;
		target.removeItem(recovery.key);
		const cleared = target.getItem(recovery.key) == null;
		if (cleared) publishRecoveryChange();
		return cleared;
	} catch {
		return false;
	}
}
export function listCommentOperationRecoveries(runId, storage = undefined) {
	const target = storageTarget(storage);
	if (!target || !safeText(runId, 255)) return {
		available: false,
		valid: [],
		damaged: []
	};
	const valid = [], damaged = [];
	try {
		const keys = [];
		for (let index = 0; index < target.length; index += 1) {
			const key = target.key(index);
			if (key?.startsWith(COMMENT_OPERATION_STORAGE_PREFIX)) keys.push(key);
		}
		for (const key of keys) {
			const identity = parseStorageIdentity(key);
			if (!identity || identity.runId !== String(runId)) continue;
			const raw = target.getItem(key);
			const intent = parsedIntent(raw, identity);
			if (intent) valid.push({
				...intent,
				storageKey: key,
				storageRaw: raw
			});
			else damaged.push({
				key,
				raw: raw ?? "",
				identity
			});
		}
		return {
			available: true,
			valid,
			damaged
		};
	} catch {
		return {
			available: false,
			valid: [],
			damaged: []
		};
	}
}
