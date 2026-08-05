import React, { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { deadlineGet, get, post, fmt, fmtInt, fmtBytes, fmtElapsedSeconds, CONTROL, saveRunConfig, operatorMeta, commandFeedback, runApiPath, runNodeApiPath, createIdempotencyKey, getRunCommand, isTransientCommandReadError, retryRunCommand, runCommand, getAuthoringOperation, putAuthoringOperation, validAuthoringName, validAuthoringTargetRootId, getRunArtifactContent, getRunArtifactInventory, submitCommand } from "./util.mjs";
import { usePoll } from "./hooks.mjs";
import { Bars, ParallelCoords, Scatter } from "./charts.mjs";
import { hyperImportance } from "./report.mjs";
import Markdown, { stripMd } from "./markdown.mjs";
import { OpIcon } from "./icons.mjs";
import CodeViewer from "./CodeViewer.mjs";
import { diffLines } from "./lineDiff.mjs";
import SettingsForm from "./SettingsForm.mjs";
import { toForm, fromForm, settingsValidationErrors, loadSettingsSchema } from "./settingsSchema.mjs";
import { reconcileAcceptedRecord, reconcileUnknownRecord, runConfigWriteDisposition, splitRunConfigPayload, validateRunConfigSaveAck } from "./settingsModel.mjs";
import { driftStatus, leakageStatus, rewardHackStatus } from "./trustSemantics.mjs";
import { metricComparable, sortRuns } from "./runIndex.mjs";
import VirtualTimeline from "./VirtualTimeline.mjs";
import { timelineEventKey } from "./timelineModel.mjs";
import { queuedGenerationControls } from "./queue.mjs";
import Panel from "./PanelShell.mjs";
import { DataTable } from "./accessibility.mjs";
import { safeExternalHref } from "./urlSafety.mjs";
import { normalizeResearchMemos } from "./researchMemoModel.mjs";
import { deadlineRequest } from "./requestDeadline.mjs";
import { installNavigationLossGuard } from "./navigationLossGuard.mjs";
import { cardControlSubmission, cardEditReflected } from "./cardControlModel.mjs";
import { createInspectorDraftStore, useInspectorDraftField } from "./inspectorDraftStore.mjs";
import { AUTHORING_OPERATION_STORAGE_PREFIX, authoringRecoveryStorageKey } from "./authoringRecoveryStorage.mjs";
import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
export { default as Panel } from "./PanelShell.mjs";
const Stat = ({ n, l }) => /* @__PURE__ */ _jsxs("div", {
	className: "stat",
	children: [/* @__PURE__ */ _jsx("div", {
		className: "n",
		children: n
	}), /* @__PURE__ */ _jsx("div", {
		className: "l",
		children: l
	})]
});
const MetricGauge = ({ value, max = 100, hot = false, label, valueText }) => {
	const numericValue = typeof value === "number" && Number.isFinite(value) ? value : null;
	const numericMax = typeof max === "number" && Number.isFinite(max) && max > 0 ? max : null;
	if (numericValue == null || numericMax == null) {
		return /* @__PURE__ */ _jsx("span", {
			className: "muted",
			"aria-label": `${label} unavailable`,
			children: "unavailable"
		});
	}
	const safeValue = Math.max(0, Math.min(numericMax, numericValue));
	return /* @__PURE__ */ _jsx("div", {
		className: "gauge",
		children: /* @__PURE__ */ _jsx("div", {
			className: "bar",
			role: "progressbar",
			"aria-label": label,
			"aria-valuemin": 0,
			"aria-valuemax": numericMax,
			"aria-valuenow": safeValue,
			"aria-valuetext": valueText,
			children: /* @__PURE__ */ _jsx("div", {
				className: "fill" + (hot ? " hot" : ""),
				style: { width: `${safeValue / numericMax * 100}%` }
			})
		})
	});
};
const invalidPanelPayload = () => {
	throw new Error("Invalid panel payload");
};
const isRecord = (value) => !!value && typeof value === "object" && !Array.isArray(value);
const nullableText = (value) => value === null || typeof value === "string";
const nullableNumber = (value) => value === null || typeof value === "number" && Number.isFinite(value);
// HTTP 200 proves transport success, not resource truth. Each panel validates its exact envelope
// before replacing last-good data or presenting an authoritative empty state.
const PANEL_REQUEST_TIMEOUT_MS = 15e3;
const AUTHORING_SAVE_TIMEOUT_MS = 12e3;
const AUTHORING_MAX_BYTES = 256 * 1024;
const AUTHORING_OPERATION_SCHEMA = "looplab.authoring-operation-intent/v1";
const AUTHORING_PANEL_DRAFT_SCOPE = "panel:authoring";
const AUTHORING_KINDS = new Set([
	"prompts",
	"skills",
	"knowledge"
]);
const AUTHORING_REVISION_RE = /^(?:missing|sha256:[0-9a-f]{64})$/;
const AUTHORING_OPERATION_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const AUTHORING_OPERATION_KEYS = new Set([
	"schema",
	"operationId",
	"kind",
	"name",
	"submittedText",
	"expectedRevision",
	"expectedTargetRootId",
	"desiredRevision",
	"updatedAt"
]);
const authoringDigestQuarantine = new Map();
const publicConfigForm = (form, settingsSchema = null) => {
	const sanitized = {
		...form || {},
		llm_api_key: ""
	};
	for (const [key, field] of Object.entries(settingsSchema?.fieldByKey || {})) {
		if (field?.type === "secret") sanitized[key] = "";
	}
	return sanitized;
};
// Exported for test: the guarantee is "no secret field leaves the browser in a config draft", and
// that is a property of the SCHEMA WALK, not of any one field name — a source regex can only see
// that llm_api_key is mentioned.
export const __testPublicConfigForm = publicConfigForm;
const publicConfigMeta = (meta) => ({
	configRevision: typeof meta?.configRevision === "string" ? meta.configRevision : "",
	pinnedFields: new Set(meta?.pinnedFields instanceof Set ? meta.pinnedFields : []),
	readOnlyFields: new Set(meta?.readOnlyFields instanceof Set ? meta.readOnlyFields : []),
	mismatchFields: Array.isArray(meta?.mismatchFields) ? [...meta.mismatchFields] : []
});
const RUN_GENERATION_RE = /^[0-9a-f]{64}$/;
const CONFIG_DRAFT_SCHEMA = "looplab.config-draft/v1";
const configDraftScope = (runId) => `panel:config:${String(runId)}`;
function validConfigDraftEnvelope(value, runId) {
	if (!isRecord(value) || value.schema !== CONFIG_DRAFT_SCHEMA || value.unsafe !== true || value.runId !== String(runId) || !RUN_GENERATION_RE.test(value.expectedGeneration) || !isRecord(value.settingsSchema) || !isRecord(value.form) || !isRecord(value.saved) || !isRecord(value.agentControl) || !isRecord(value.savedAC) || !isRecord(value.configMeta) || typeof value.saveInFlight !== "boolean" || !Array.isArray(value.dirtyKeys) || !Array.isArray(value.dirtyControlKeys) || value.reconcileGeneration != null && !RUN_GENERATION_RE.test(value.reconcileGeneration) || value.dirtyKeys.some((key) => typeof key !== "string") || value.dirtyControlKeys.some((key) => typeof key !== "string") || value.configMutationUnknown != null && !isRecord(value.configMutationUnknown)) return null;
	return value;
}
function authoringPayload(value) {
	if (!isRecord(value) || !nullableText(value.dir) || !Array.isArray(value.files)) invalidPanelPayload();
	const targetRootId = value.target_root_id == null ? null : value.target_root_id;
	const truncatedFiles = value.truncated_files == null ? 0 : value.truncated_files;
	if (!Number.isSafeInteger(truncatedFiles) || truncatedFiles < 0) invalidPanelPayload();
	if (value.dir == null && (targetRootId !== null || value.files.length > 0 || truncatedFiles > 0) || value.dir != null && !validAuthoringTargetRootId(targetRootId)) invalidPanelPayload();
	const files = value.files.map((file) => {
		if (!isRecord(file) || !validAuthoringName(file.name) || typeof file.text !== "string" || typeof file.truncated !== "boolean") invalidPanelPayload();
		const truncated = file.truncated;
		const revision = typeof file.revision === "string" && AUTHORING_REVISION_RE.test(file.revision) ? file.revision : null;
		if (truncated && file.revision !== null || !truncated && revision == null) invalidPanelPayload();
		return {
			name: file.name,
			text: file.text,
			revision,
			truncated
		};
	});
	return {
		dir: value.dir,
		targetRootId,
		files,
		truncatedFiles
	};
}
const authoringScope = (kind, name) => `${String(kind || "")}\u0000${String(name || "")}`;
const authoringStorageKey = authoringRecoveryStorageKey;
const authoringStorage = () => {
	try {
		return typeof sessionStorage === "undefined" ? null : sessionStorage;
	} catch {
		return null;
	}
};
const authoringUtf8Bytes = (text) => {
	try {
		return new TextEncoder().encode(String(text)).byteLength;
	} catch {
		return Infinity;
	}
};
const authoringTextWellFormed = (text) => {
	if (typeof text !== "string") return false;
	if (typeof text.isWellFormed === "function") return text.isWellFormed();
	for (let index = 0; index < text.length; index++) {
		const unit = text.charCodeAt(index);
		if (unit >= 55296 && unit <= 56319) {
			const next = text.charCodeAt(index + 1);
			if (!(next >= 56320 && next <= 57343)) return false;
			index++;
		} else if (unit >= 56320 && unit <= 57343) return false;
	}
	return true;
};
const validAuthoringOperation = (value) => isRecord(value) && Object.keys(value).every((key) => AUTHORING_OPERATION_KEYS.has(key)) && Object.keys(value).length === AUTHORING_OPERATION_KEYS.size && value.schema === AUTHORING_OPERATION_SCHEMA && AUTHORING_OPERATION_RE.test(value.operationId) && AUTHORING_KINDS.has(value.kind) && validAuthoringName(value.name) && authoringTextWellFormed(value.submittedText) && authoringUtf8Bytes(value.submittedText) <= AUTHORING_MAX_BYTES && AUTHORING_REVISION_RE.test(value.expectedRevision) && validAuthoringTargetRootId(value.expectedTargetRootId) && /^sha256:[0-9a-f]{64}$/.test(value.desiredRevision) && Number.isSafeInteger(value.updatedAt) && value.updatedAt >= 0;
function parseAuthoringStorageIdentity(key) {
	if (typeof key !== "string" || !key.startsWith(AUTHORING_OPERATION_STORAGE_PREFIX)) return null;
	try {
		const decoded = decodeURIComponent(key.slice(AUTHORING_OPERATION_STORAGE_PREFIX.length));
		const split = decoded.indexOf("\0");
		if (split <= 0 || decoded.indexOf("\0", split + 1) !== -1) return null;
		const kind = decoded.slice(0, split), name = decoded.slice(split + 1);
		return AUTHORING_KINDS.has(kind) && validAuthoringName(name) ? {
			kind,
			name,
			scope: authoringScope(kind, name)
		} : null;
	} catch {
		return null;
	}
}
function inspectAuthoringOperations() {
	const storage = authoringStorage();
	if (!storage) return {
		available: false,
		valid: {},
		damaged: {}
	};
	const valid = {}, damaged = {};
	try {
		const keys = [];
		for (let index = 0; index < storage.length; index++) {
			const key = storage.key(index);
			if (key?.startsWith(AUTHORING_OPERATION_STORAGE_PREFIX)) keys.push(key);
		}
		for (const key of keys) {
			const raw = storage.getItem(key);
			const identity = parseAuthoringStorageIdentity(key);
			const quarantined = authoringDigestQuarantine.get(key);
			if (quarantined && quarantined.raw !== raw) authoringDigestQuarantine.delete(key);
			const exactQuarantine = quarantined?.raw === raw ? quarantined : null;
			let parsed = null;
			try {
				parsed = JSON.parse(raw || "null");
			} catch {}
			if (identity && key === authoringStorageKey(identity.kind, identity.name) && !exactQuarantine && !Object.hasOwn(valid, identity.scope) && validAuthoringOperation(parsed) && parsed.kind === identity.kind && parsed.name === identity.name) {
				valid[identity.scope] = {
					...parsed,
					scope: identity.scope,
					storageKey: key,
					storageRaw: raw
				};
			} else {
				const scope = `damaged:${key}`;
				damaged[scope] = {
					scope,
					key,
					raw: raw ?? "",
					identity,
					inspected: false,
					...exactQuarantine ? { reason: exactQuarantine.reason } : {}
				};
			}
		}
		return {
			available: true,
			valid,
			damaged
		};
	} catch {
		return {
			available: false,
			valid: {},
			damaged: {}
		};
	}
}
function saveAuthoringOperationIntent(intent) {
	const storage = authoringStorage();
	if (!storage || !validAuthoringOperation(intent)) return null;
	const key = authoringStorageKey(intent.kind, intent.name);
	try {
		const raw = storage.getItem(key);
		if (raw != null) {
			let existing = null;
			try {
				existing = JSON.parse(raw);
			} catch {
				return null;
			}
			if (!validAuthoringOperation(existing) || existing.kind !== intent.kind || existing.name !== intent.name || existing.operationId !== intent.operationId || existing.submittedText !== intent.submittedText || existing.expectedRevision !== intent.expectedRevision || existing.expectedTargetRootId !== intent.expectedTargetRootId || existing.desiredRevision !== intent.desiredRevision) return null;
		}
		const serialized = JSON.stringify(intent);
		storage.setItem(key, serialized);
		if (storage.getItem(key) !== serialized) return null;
		return {
			...intent,
			scope: authoringScope(intent.kind, intent.name),
			storageKey: key,
			storageRaw: serialized
		};
	} catch {
		return null;
	}
}
function clearAuthoringOperationIntent(intent) {
	const storage = authoringStorage();
	if (!storage || !intent?.storageKey || typeof intent.storageRaw !== "string") return false;
	try {
		if (storage.getItem(intent.storageKey) !== intent.storageRaw) return false;
		storage.removeItem(intent.storageKey);
		return storage.getItem(intent.storageKey) == null;
	} catch {
		return false;
	}
}
function clearDamagedAuthoringOperation(recovery) {
	const storage = authoringStorage();
	if (!storage || !recovery?.key || typeof recovery.raw !== "string") return false;
	try {
		if (storage.getItem(recovery.key) !== recovery.raw) return false;
		storage.removeItem(recovery.key);
		return storage.getItem(recovery.key) == null;
	} catch {
		return false;
	}
}
async function authoringTextRevision(text) {
	if (!authoringTextWellFormed(text)) {
		const error = new Error("File text contains an unpaired Unicode surrogate.");
		error.code = "AUTHORING_TEXT_NOT_WELL_FORMED";
		throw error;
	}
	const bytes = new TextEncoder().encode(text);
	if (bytes.byteLength > AUTHORING_MAX_BYTES) {
		const error = new Error(`File is larger than ${AUTHORING_MAX_BYTES} UTF-8 bytes.`);
		error.code = "AUTHORING_TEXT_TOO_LARGE";
		throw error;
	}
	if (!globalThis.crypto?.subtle) {
		const error = new Error("Secure browser hashing is unavailable.");
		error.code = "AUTHORING_HASH_UNAVAILABLE";
		throw error;
	}
	const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
	return "sha256:" + [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}
function memoryPayload(value) {
	if (!isRecord(value) || !nullableText(value.dir) || ![
		"cases",
		"lessons",
		"notes"
	].every((key) => Array.isArray(value[key]))) invalidPanelPayload();
	const cases = value.cases.map((row) => {
		if (!isRecord(row) || !row.task_id || typeof row.task_id !== "string" || typeof row.goal !== "string" || !nullableNumber(row.metric) || !Object.hasOwn(row, "params") || row.params_truncated != null && typeof row.params_truncated !== "boolean") invalidPanelPayload();
		return {
			...row,
			params_truncated: row.params_truncated === true
		};
	});
	const lessonText = [
		"role",
		"kind",
		"outcome",
		"task_id"
	];
	for (const row of value.lessons) {
		if (!isRecord(row) || !row.statement || typeof row.statement !== "string" || lessonText.some((key) => row[key] != null && typeof row[key] !== "string") || ["delta", "confidence"].some((key) => row[key] != null && !nullableNumber(row[key])) || row.evidence_count != null && (!Number.isSafeInteger(row.evidence_count) || row.evidence_count < 0)) invalidPanelPayload();
	}
	const notes = value.notes.map((row) => {
		const note = isRecord(row) && (row.note || row.statement);
		if (typeof note !== "string" || !note || row.task_id != null && typeof row.task_id !== "string") invalidPanelPayload();
		return {
			...row,
			note
		};
	});
	if (value.dir == null) {
		if (cases.length || value.lessons.length || notes.length || value.projection != null || value.page != null) {
			invalidPanelPayload();
		}
		return {
			dir: null,
			cases,
			lessons: value.lessons,
			notes,
			projection: null,
			page: null
		};
	}
	if (value.projection !== "bounded_recent_tail" || !isRecord(value.page) || !isRecord(value.page.tiers)) invalidPanelPayload();
	const tiers = {};
	for (const key of [
		"cases",
		"lessons",
		"notes"
	]) {
		const receipt = value.page.tiers[key];
		if (!isRecord(receipt) || !Number.isSafeInteger(receipt.limit) || receipt.limit <= 0 || !Number.isSafeInteger(receipt.returned) || receipt.returned < 0 || receipt.returned > receipt.limit || receipt.returned !== value[key].length || !Number.isSafeInteger(receipt.skipped) || receipt.skipped < 0 || typeof receipt.source_window_truncated !== "boolean" || typeof receipt.unavailable !== "boolean" || receipt.unavailable && receipt.returned !== 0) invalidPanelPayload();
		tiers[key] = {
			limit: receipt.limit,
			returned: receipt.returned,
			skipped: receipt.skipped,
			sourceWindowTruncated: receipt.source_window_truncated,
			unavailable: receipt.unavailable
		};
	}
	const truncated = Object.values(tiers).some((receipt) => receipt.sourceWindowTruncated);
	const unavailable = Object.values(tiers).some((receipt) => receipt.unavailable);
	const partial = Object.values(tiers).some((receipt) => receipt.sourceWindowTruncated || receipt.skipped > 0 || receipt.unavailable);
	if (value.page.truncated !== truncated || value.page.unavailable !== unavailable || value.page.partial !== partial) invalidPanelPayload();
	return {
		dir: value.dir,
		cases,
		lessons: value.lessons,
		notes,
		projection: value.projection,
		page: {
			tiers,
			truncated,
			unavailable,
			partial
		}
	};
}
const memoryTierIncomplete = (receipt) => !!receipt && (receipt.sourceWindowTruncated || receipt.skipped > 0 || receipt.unavailable);
function runsPayload(value) {
	if (!Array.isArray(value)) invalidPanelPayload();
	for (const row of value) {
		if (!isRecord(row) || !row.run_id || typeof row.run_id !== "string" || typeof row.task_id !== "string" || !["min", "max"].includes(row.direction) || typeof row.finished !== "boolean" || typeof row.phase !== "string" || !Number.isSafeInteger(row.nodes) || row.nodes < 0 || !nullableNumber(row.best_metric) || !nullableNumber(row.best_confirmed) || !nullableText(row.label)) invalidPanelPayload();
	}
	return value;
}
function gpuPayload(value) {
	if (!isRecord(value) || typeof value.available !== "boolean" || value.gpus != null && !Array.isArray(value.gpus)) invalidPanelPayload();
	const gpus = value.gpus || [];
	for (const gpu of gpus) {
		if (!isRecord(gpu) || !gpu.name || typeof gpu.name !== "string" || [
			"util",
			"mem_used",
			"mem_total",
			"temp",
			"power"
		].some((key) => !nullableNumber(gpu[key]))) invalidPanelPayload();
	}
	if (value.available && !Array.isArray(value.gpus)) invalidPanelPayload();
	return {
		available: value.available,
		gpus
	};
}
// A poll and a manual retry share one synchronous lock: interval ticks skip an active request, while
// Retry starts immediately when idle. A failed refresh retains last-good data and marks it stale.
function usePanelResource(loader, normalize = (value) => value, key = "", pollMs = null) {
	const [value, setValue] = useState({
		key,
		state: "loading",
		data: null,
		pending: null
	});
	const flight = useRef(null);
	const startRef = useRef(null);
	useEffect(() => {
		let alive = true;
		const owner = {};
		const start = (intent = "refresh") => {
			if (flight.current) return false;
			const timed = deadlineRequest((signal) => Promise.resolve().then(() => loader(signal)).then(normalize), PANEL_REQUEST_TIMEOUT_MS);
			const request = {
				owner,
				controller: timed.controller
			};
			flight.current = request;
			setValue((previous) => previous.key !== key ? {
				key,
				state: "loading",
				data: null,
				pending: null
			} : intent === "retry" || ["error", "stale"].includes(previous.state) ? {
				...previous,
				pending: intent
			} : previous);
			const finish = (ok, data = null) => {
				if (flight.current !== request) return;
				flight.current = null;
				if (!alive) return;
				setValue((previous) => {
					const lastGood = previous.key === key ? previous.data : null;
					return ok ? {
						key,
						state: "ready",
						data,
						pending: null
					} : {
						key,
						state: lastGood == null ? "error" : "stale",
						data: lastGood,
						pending: null
					};
				});
			};
			timed.promise.then((data) => finish(true, data), () => finish(false));
			return true;
		};
		startRef.current = start;
		start("load");
		const timer = pollMs == null ? null : setInterval(start, pollMs);
		return () => {
			alive = false;
			if (timer != null) clearInterval(timer);
			if (startRef.current === start) startRef.current = null;
			if (flight.current?.owner === owner) {
				flight.current.controller.abort();
				flight.current = null;
			}
		};
	}, [key, pollMs]);
	const resource = value.key === key ? value : {
		key,
		state: "loading",
		data: null,
		pending: null
	};
	return [resource, () => startRef.current?.("retry") || false];
}
function PanelResourceNotice({ resource, label, onRetry }) {
	if (resource.state === "ready") return null;
	if (resource.state === "loading") return /* @__PURE__ */ _jsxs("div", {
		className: "muted",
		role: "status",
		children: [
			"Loading ",
			label,
			"…"
		]
	});
	const stale = resource.state === "stale";
	return /* @__PURE__ */ _jsxs("div", {
		className: "report-inline-state" + (stale ? "" : " error"),
		role: stale ? "status" : "alert",
		children: [
			/* @__PURE__ */ _jsx(OpIcon, {
				name: "alert",
				size: 14
			}),
			/* @__PURE__ */ _jsxs("span", { children: [
				label,
				": ",
				resource.pending ? `${resource.pending === "retry" ? "Retrying" : "Refreshing"}…${stale ? " Last loaded data remains visible." : ""}` : stale ? "Last loaded data; refresh failed." : "Unavailable."
			] }),
			/* @__PURE__ */ _jsx("button", {
				className: "btn sm",
				disabled: !!resource.pending,
				onClick: onRetry,
				children: resource.pending === "retry" ? "Retrying…" : resource.pending ? "Refreshing…" : "Retry"
			})
		]
	});
}
// Overall-info tab (round-8): the run's at-a-glance metrics, lifted out of the cramped top bar so the
// header stays a single line. Everything derives from the folded state (+ maxEval from config).
export function OverviewPanel({ state, maxEval, onClose, onOpenPanel }) {
	const nodes = Object.values(state.nodes || {});
	const evaluated = nodes.filter((n) => n.metric != null).length;
	const failed = nodes.filter((n) => n.status === "failed").length;
	const best = state.best_node_id != null ? (state.nodes || {})[state.best_node_id] : null;
	const evalSec = state.total_eval_seconds || 0;
	const cost = state.llm_cost;
	const strat = state.active_strategy;
	const hints = state.pending_hints || [];
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Overview",
		sub: state.task_id || "",
		onClose,
		children: [
			state.goal && /* @__PURE__ */ _jsx("div", {
				className: "ov-goal",
				children: state.goal
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "stat-grid",
				children: [
					/* @__PURE__ */ _jsx(Stat, {
						n: best ? fmt(best.confirmed_mean ?? best.metric) : "—",
						l: "best metric"
					}),
					/* @__PURE__ */ _jsx(Stat, {
						n: state.direction || "—",
						l: "direction"
					}),
					/* @__PURE__ */ _jsx(Stat, {
						n: nodes.length,
						l: "nodes"
					}),
					/* @__PURE__ */ _jsx(Stat, {
						n: evaluated,
						l: "evaluated"
					}),
					/* @__PURE__ */ _jsx(Stat, {
						n: failed,
						l: "failed"
					}),
					/* @__PURE__ */ _jsx(Stat, {
						n: fmtElapsedSeconds(evalSec) + (maxEval != null ? " / " + fmtElapsedSeconds(maxEval) : ""),
						l: "eval time"
					}),
					cost && /* @__PURE__ */ _jsx(Stat, {
						n: fmtInt(cost.total_tokens),
						l: "tokens"
					}),
					state.paused ? /* @__PURE__ */ _jsx(Stat, {
						n: "paused",
						l: "status"
					}) : null
				]
			}),
			strat && /* @__PURE__ */ _jsxs("div", {
				className: "ov-row",
				children: [
					/* @__PURE__ */ _jsxs("span", {
						className: "k",
						children: [/* @__PURE__ */ _jsx(OpIcon, {
							name: "compass",
							className: "t-ic"
						}), " strategy"]
					}),
					" ",
					(strat.policy || "greedy") + (strat.fidelity ? "/" + strat.fidelity : ""),
					strat.rationale && /* @__PURE__ */ _jsx("div", {
						className: "muted ov-why",
						children: strat.rationale
					})
				]
			}),
			hints.length > 0 && /* @__PURE__ */ _jsxs("div", {
				className: "ov-row",
				children: [/* @__PURE__ */ _jsxs("span", {
					className: "k",
					children: [
						/* @__PURE__ */ _jsx(OpIcon, {
							name: "bulb",
							className: "t-ic"
						}),
						" hints (",
						hints.length,
						")"
					]
				}), /* @__PURE__ */ _jsx("ul", {
					className: "ov-hints",
					children: hints.map((h, i) => /* @__PURE__ */ _jsx("li", { children: h.text || JSON.stringify(h) }, (h.text || "") + i))
				})]
			}),
			(state.novelty_events?.length > 0 || state.reward_hacks?.length > 0) && /* @__PURE__ */ _jsxs("div", {
				className: "ov-row ov-alerts",
				children: [state.novelty_events?.length > 0 && /* @__PURE__ */ _jsxs("span", {
					className: "chip",
					title: "near-duplicate proposals nudged to diversify (E1)",
					children: [
						/* @__PURE__ */ _jsx(OpIcon, {
							name: "replay",
							className: "t-ic"
						}),
						" dedup ",
						state.novelty_events.length
					]
				}), state.reward_hacks?.length > 0 && /* @__PURE__ */ _jsxs("button", {
					type: "button",
					className: "chip alarm run-metric-chip",
					title: "suspicious wins flagged (B5)",
					onClick: () => onOpenPanel?.("trust"),
					children: [
						/* @__PURE__ */ _jsx(OpIcon, {
							name: "alert",
							size: 11
						}),
						" hack? ",
						state.reward_hacks.length
					]
				})]
			})
		]
	});
}
// Deep-research drawer: every memo in one place (instead of scrolling the timeline feed), with
// ACTIONABLE directions — "steer →" posts a hint the Researcher folds into the next proposal. Deep
// research is no longer a DAG node; this drawer + the Dock timeline marker are its home.
export function ResearchPanel({ state, runId, onToast, onClose }) {
	const memoProjection = useMemo(() => normalizeResearchMemos(state.research), [state.research]);
	const memos = [...memoProjection.memos].reverse();
	const steer = async (text) => {
		await submitCommand(CONTROL.hint(runId, "try this research direction: " + text), {
			success: "Steered the next proposal",
			noop: "That direction was already queued",
			executing: "Steer request accepted — waiting for the run",
			failure: "Could not steer"
		}, onToast);
	};
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Deep research",
		sub: memos.length ? `${memos.length} memo${memos.length === 1 ? "" : "s"}` : "none yet",
		onClose,
		wide: true,
		children: [
			!memos.length && /* @__PURE__ */ _jsxs("div", {
				className: "muted",
				children: [
					"No deep-research memos yet. Trigger one with ",
					/* @__PURE__ */ _jsx("code", { children: "/deep-research" }),
					" in the chat, or set a cadence in Config."
				]
			}),
			memoProjection.omitted > 0 && /* @__PURE__ */ _jsxs("div", {
				className: "muted",
				children: [
					"Showing ",
					memos.length,
					" of ",
					memoProjection.total,
					" newest valid memos; older, malformed, or over-budget entries are omitted."
				]
			}),
			memos.map((m) => /* @__PURE__ */ _jsxs(
				"div",
				// Key by the STABLE original index (research is append-only), not the reversed position:
				// keyed by `i`, a new memo landing at index 0 reuses the prior memo's DOM node and its open
				// <details> state bleeds onto the new one.
				{
					className: "rsch-memo",
					children: [
						/* @__PURE__ */ _jsxs("div", {
							className: "rsch-h",
							children: [
								/* @__PURE__ */ _jsx("span", {
									className: "rsch-ic",
									children: /* @__PURE__ */ _jsx(OpIcon, { name: "search" })
								}),
								/* @__PURE__ */ _jsx("b", { children: m.summary || "(no summary)" }),
								/* @__PURE__ */ _jsx("span", { className: "right" }),
								m.trigger && /* @__PURE__ */ _jsx("span", {
									className: "pill",
									children: m.trigger
								}),
								m.at_node != null && /* @__PURE__ */ _jsxs("span", {
									className: "pill",
									children: ["@#", m.at_node]
								})
							]
						}),
						(m.findings || []).length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
							className: "section-h",
							children: "Findings"
						}), /* @__PURE__ */ _jsx("ul", {
							className: "bul",
							children: m.findings.map((f, j) => /* @__PURE__ */ _jsx("li", { children: f }, j))
						})] }),
						m.verification && ((m.verification.verdicts || []).length > 0 || m.verification.omittedVerdicts > 0) && /* @__PURE__ */ _jsxs(_Fragment, { children: [
							/* @__PURE__ */ _jsxs("div", {
								className: "section-h",
								children: [
									"Verification",
									m.verification.unsupported > 0 && /* @__PURE__ */ _jsxs("span", {
										className: "chip warn",
										title: "claims whose cited evidence does not support them",
										children: [m.verification.unsupported, " unsupported"]
									}),
									m.verification.omittedVerdicts > 0 && /* @__PURE__ */ _jsx("span", {
										className: "chip warn",
										children: "verification incomplete"
									}),
									/* @__PURE__ */ _jsxs("span", {
										className: "muted",
										children: [
											" (",
											m.verification.method,
											")"
										]
									})
								]
							}),
							m.verification.omittedVerdicts > 0 && /* @__PURE__ */ _jsxs("p", {
								className: "memo-verification-incomplete",
								role: "note",
								children: [
									"Showing ",
									m.verification.verdicts.length,
									" of ",
									m.verification.totalVerdicts,
									" verifier verdicts; omitted verdicts make this check incomplete."
								]
							}),
							/* @__PURE__ */ _jsx("ul", {
								className: "bul",
								children: m.verification.verdicts.map((v, j) => /* @__PURE__ */ _jsxs("li", {
									className: v.verdict === "supported" ? "ok" : v.verdict === "unclear" || v.verdict === "cited" ? "" : "bad",
									children: [
										/* @__PURE__ */ _jsx("span", {
											className: "pill",
											children: v.verdict
										}),
										" ",
										v.statement,
										v.note && /* @__PURE__ */ _jsxs("span", {
											className: "muted",
											children: [" — ", v.note]
										})
									]
								}, j))
							})
						] }),
						(m.recommended_directions || []).length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
							className: "section-h",
							children: "Recommended directions"
						}), /* @__PURE__ */ _jsx("ul", {
							className: "rsch-dirs",
							children: m.recommended_directions.map((d, j) => /* @__PURE__ */ _jsxs("li", { children: [/* @__PURE__ */ _jsx("span", { children: d }), /* @__PURE__ */ _jsx("button", {
								className: "btn sm ghost",
								title: "steer the next proposal toward this direction (posts a hint)",
								onClick: () => steer(d),
								children: "steer →"
							})] }, j))
						})] }),
						(m.sources || []).length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
							className: "section-h",
							children: "Sources"
						}), /* @__PURE__ */ _jsx("ul", {
							className: "bul",
							children: m.sources.map((source, index) => {
								// Deep-research sources are untrusted provider output. Only credential-free HTTP(S)
								// URLs become links; unsafe, oversized or malformed values remain bounded inert text.
								const href = safeExternalHref(source?.url);
								const label = String(source?.title ?? source?.url ?? "source").slice(0, 300);
								const snippet = source?.snippet == null ? "" : String(source.snippet).slice(0, 160);
								return /* @__PURE__ */ _jsxs("li", { children: [href ? /* @__PURE__ */ _jsx("a", {
									href,
									target: "_blank",
									rel: "noreferrer noopener",
									children: label
								}) : label, snippet && /* @__PURE__ */ _jsx("div", {
									className: "muted",
									children: snippet
								})] }, index);
							})
						})] }),
						m.reasoning && /* @__PURE__ */ _jsxs("details", {
							className: "rsch-reasoning",
							children: [/* @__PURE__ */ _jsx("summary", { children: "reasoning (debug)" }), /* @__PURE__ */ _jsx(Markdown, {
								className: "think-body",
								text: m.reasoning
							})]
						})
					]
				},
				m.sourceIndex
			))
		]
	});
}
function TrustState({ value, action = null }) {
	const icon = value.tone === "alarm" ? "alert" : value.tone === "ok" ? "check" : "dot";
	return /* @__PURE__ */ _jsxs("div", {
		className: `trust-state ${value.tone}`,
		role: value.tone === "alarm" ? "alert" : "status",
		children: [
			/* @__PURE__ */ _jsx(OpIcon, {
				name: icon,
				size: 14
			}),
			/* @__PURE__ */ _jsx("strong", { children: value.label }),
			/* @__PURE__ */ _jsx("span", { children: value.detail }),
			action && /* @__PURE__ */ _jsx("div", {
				className: "trust-state-actions",
				children: action
			})
		]
	});
}
export function TrustPanel({ state, runId, onClose, onSelect, onToast, readOnly = false }) {
	const [configResource, setConfigResource] = useState({
		status: "loading",
		data: null,
		error: null
	});
	const [configNonce, setConfigNonce] = useState(0);
	useEffect(() => {
		let alive = true;
		setConfigResource({
			status: "loading",
			data: null,
			error: null
		});
		get(runApiPath(runId, "/config")).then((data) => {
			if (alive) setConfigResource({
				status: "ready",
				data,
				error: null
			});
		}).catch((error) => {
			if (alive) setConfigResource({
				status: "error",
				data: null,
				error: error.message || "Request failed"
			});
		});
		return () => {
			alive = false;
		};
	}, [runId, configNonce]);
	const cfg = configResource.data;
	const quarantine = async (id) => {
		await submitCommand(CONTROL.nodeAbort(runId, id, state.nodes?.[id]?.attempt), {
			success: `Quarantined #${id}`,
			noop: `#${id} was already settled`,
			executing: `Quarantine of #${id} requested — waiting for the run`,
			failure: `Could not quarantine #${id}`
		}, onToast);
	};
	const nodes = Object.values(state.nodes);
	const evald = nodes.filter((n) => n.metric != null && n.feasible !== false);
	const chooser = state.direction === "min" ? (a, b) => a < b : (a, b) => a > b;
	const naive = evald.slice().sort((a, b) => chooser(a.metric, b.metric) ? -1 : 1)[0];
	const robust = state.best_node_id != null ? state.nodes[state.best_node_id] : null;
	const leak = state.leakage;
	const leakState = leakageStatus(leak);
	const driftState = driftStatus(state.drifts, cfg, evald.length);
	const hackState = rewardHackStatus(state.reward_hacks, cfg, evald.length);
	return /* @__PURE__ */ _jsx(Panel, {
		title: "Trust & rigor",
		sub: "evidence and coverage",
		onClose,
		wide: true,
		children: /* @__PURE__ */ _jsxs("div", {
			className: "trust-panel-body",
			children: [
				configResource.status === "loading" && /* @__PURE__ */ _jsx(TrustState, { value: {
					tone: "loading",
					label: "Loading detector configuration",
					detail: "Checking which trust controls were actually enabled for this run."
				} }),
				configResource.status === "error" && /* @__PURE__ */ _jsx(TrustState, {
					value: {
						tone: "unknown",
						label: "Detector configuration unavailable",
						detail: `Coverage cannot be verified: ${configResource.error}`
					},
					action: /* @__PURE__ */ _jsx("button", {
						className: "btn sm",
						onClick: () => setConfigNonce((n) => n + 1),
						children: "Retry"
					})
				}),
				/* @__PURE__ */ _jsxs("div", {
					className: "cardgrid",
					children: [
						/* @__PURE__ */ _jsx(Stat, {
							n: cfg?.trust_mode || (configResource.status === "loading" ? "Loading…" : "Unknown"),
							l: "sandbox tier"
						}),
						/* @__PURE__ */ _jsx(Stat, {
							n: cfg?.eval_trust_mode || (configResource.status === "loading" ? "Loading…" : "Unknown"),
							l: "eval trust mode"
						}),
						/* @__PURE__ */ _jsx(Stat, {
							n: state.host_grading ? "host-side" : "self-reported",
							l: "metric scoring"
						}),
						/* @__PURE__ */ _jsx(Stat, {
							n: state.workspace_changed ? "changed" : "no change flag",
							l: "workspace drift"
						})
					]
				}),
				state.host_grading ? /* @__PURE__ */ _jsx(TrustState, { value: {
					tone: "ok",
					label: "Host-side grading recorded",
					detail: `The candidate writes predictions only; ${state.host_grading.scorer || "the host scorer"} evaluates ${state.host_grading.n_labels ?? "held-out"} labels outside the candidate process.`
				} }) : /* @__PURE__ */ _jsx(TrustState, { value: {
					tone: "warn",
					label: "Metric is not host-graded",
					detail: "This run does not record an out-of-process grader, so the displayed metric may be self-reported by the candidate process."
				} }),
				/* @__PURE__ */ _jsx("div", {
					className: "section-h",
					children: "Seed-luck and robustness"
				}),
				robust && naive ? /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx(TrustState, { value: robust.confirmed_mean != null ? {
					tone: "ok",
					label: "Winner is multi-seed confirmed",
					detail: `${robust.confirmed_seeds || "Multiple"} successful seeds produced ${fmt(robust.confirmed_mean)} ±${fmt(robust.confirmed_std)}.`
				} : {
					tone: "warn",
					label: "Winner is single-evaluation",
					detail: "Seed luck has not been ruled out; the selected winner is not a robust result yet."
				} }), /* @__PURE__ */ _jsxs("div", {
					className: "kv",
					children: [
						/* @__PURE__ */ _jsx("div", {
							className: "k",
							children: "single-eval leader"
						}),
						/* @__PURE__ */ _jsxs("div", {
							className: "v",
							children: [
								"#",
								naive.id,
								" · ",
								fmt(naive.metric)
							]
						}),
						/* @__PURE__ */ _jsx("div", {
							className: "k",
							children: "selected winner"
						}),
						/* @__PURE__ */ _jsxs("div", {
							className: "v",
							children: [
								"#",
								robust.id,
								" · ",
								fmt(robust.confirmed_mean ?? robust.metric),
								robust.confirmed_mean != null ? ` ±${fmt(robust.confirmed_std)}` : " (unconfirmed)"
							]
						}),
						robust.confirmed_mean != null && naive.id !== robust.id && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
							className: "k flag",
							children: "demotion"
						}), /* @__PURE__ */ _jsxs("div", {
							className: "v",
							children: [
								"Single-eval leader #",
								naive.id,
								" was not selected — multi-seed confirmation corrected a seed-lucky result."
							]
						})] })
					]
				})] }) : /* @__PURE__ */ _jsx(TrustState, { value: {
					tone: "unknown",
					label: "No result to confirm",
					detail: "There are no feasible evaluated nodes yet."
				} }),
				/* @__PURE__ */ _jsxs("div", {
					className: "section-h",
					children: ["Leakage scan ", leak && leak.leak && /* @__PURE__ */ _jsx("span", {
						className: "chip alarm",
						children: "LEAK — run refused"
					})]
				}),
				/* @__PURE__ */ _jsx(TrustState, { value: leakState }),
				(leak?.verdicts || []).length > 0 && /* @__PURE__ */ _jsx(DataTable, {
					caption: "Leakage detector verdicts",
					card: false,
					children: /* @__PURE__ */ _jsxs("table", {
						className: "tbl",
						children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
							/* @__PURE__ */ _jsx("th", { children: "detector" }),
							/* @__PURE__ */ _jsx("th", { children: "result" }),
							/* @__PURE__ */ _jsx("th", { children: "detail" })
						] }) }), /* @__PURE__ */ _jsx("tbody", { children: (leak.verdicts || []).map((v, i) => /* @__PURE__ */ _jsxs("tr", { children: [
							/* @__PURE__ */ _jsx("td", { children: v.detector || "unnamed detector" }),
							/* @__PURE__ */ _jsx("td", {
								className: `trust-result ${v.leak ? "fail" : "pass"}`,
								children: v.leak ? "Flagged" : "Passed"
							}),
							/* @__PURE__ */ _jsx("td", {
								className: "muted",
								children: Object.entries(v).filter(([k]) => !["detector", "leak"].includes(k)).map(([k, val]) => `${k}=${typeof val === "object" ? JSON.stringify(val) : val}`).join("  ") || "—"
							})
						] }, i)) })]
					})
				}),
				/* @__PURE__ */ _jsx("div", {
					className: "section-h",
					children: "Drift cross-check"
				}),
				/* @__PURE__ */ _jsx(TrustState, { value: driftState }),
				(state.drifts || []).length ? /* @__PURE__ */ _jsx(DataTable, {
					caption: "Run metric drift comparisons",
					card: false,
					children: /* @__PURE__ */ _jsxs("table", {
						className: "tbl",
						children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
							/* @__PURE__ */ _jsx("th", { children: "node" }),
							/* @__PURE__ */ _jsx("th", { children: "primary" }),
							/* @__PURE__ */ _jsx("th", { children: "cross" }),
							/* @__PURE__ */ _jsx("th", { children: "tolerance" })
						] }) }), /* @__PURE__ */ _jsx("tbody", { children: state.drifts.map((d, i) => /* @__PURE__ */ _jsxs("tr", { children: [
							/* @__PURE__ */ _jsxs("td", {
								className: "flag",
								children: ["#", d.node_id]
							}),
							/* @__PURE__ */ _jsx("td", { children: fmt(d.primary) }),
							/* @__PURE__ */ _jsx("td", { children: fmt(d.cross) }),
							/* @__PURE__ */ _jsx("td", { children: fmt(d.tolerance) })
						] }, i)) })]
					})
				}) : null,
				/* @__PURE__ */ _jsxs("div", {
					className: "section-h",
					children: ["Reward-hacking monitor (B5) ", (state.reward_hacks || []).length > 0 && /* @__PURE__ */ _jsxs("span", {
						className: "chip alarm",
						children: [state.reward_hacks.length, " flagged"]
					})]
				}),
				/* @__PURE__ */ _jsx(TrustState, { value: hackState }),
				(state.trust_gate || cfg) && /* @__PURE__ */ _jsxs("div", {
					className: "muted",
					style: { marginBottom: 6 },
					children: [
						"enforcement: ",
						/* @__PURE__ */ _jsx("b", { children: state.trust_gate || cfg?.trust_gate || "audit" }),
						(state.trust_gate || cfg?.trust_gate || "audit") === "audit" ? " — signals are logged only; set trust_gate=gate/block (or the thorough profile) to keep a high-precision flag from winning." : " — a high-precision flag is excluded from best-selection and breeding/confirmation.",
						" ",
						"Only high-precision signals gate; broad critic/perfect-score warnings stay advisory, except",
						/* @__PURE__ */ _jsx("code", { children: " critic:hardcoded_metric" }),
						", which is classified as high precision."
					]
				}),
				(state.reward_hacks || []).length ? /* @__PURE__ */ _jsx(DataTable, {
					caption: "Reward-hacking signals",
					card: false,
					children: /* @__PURE__ */ _jsxs("table", {
						className: "tbl",
						children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
							/* @__PURE__ */ _jsx("th", { children: "node" }),
							/* @__PURE__ */ _jsx("th", { children: "signal" }),
							/* @__PURE__ */ _jsx("th", { children: "detail" }),
							/* @__PURE__ */ _jsx("th", { children: "action" })
						] }) }), /* @__PURE__ */ _jsx("tbody", { children: state.reward_hacks.map((h, i) => /* @__PURE__ */ _jsxs("tr", { children: [
							/* @__PURE__ */ _jsx("td", {
								className: "flag",
								children: /* @__PURE__ */ _jsxs("button", {
									className: "btn xs ghost",
									onClick: () => {
										onSelect && onSelect(h.node_id);
										onClose();
									},
									children: ["#", h.node_id]
								})
							}),
							/* @__PURE__ */ _jsx("td", { children: (h.signals || []).map((s) => s.signal).join(", ") }),
							/* @__PURE__ */ _jsx("td", {
								className: "muted",
								children: (h.signals || []).map((s) => s.detail).filter(Boolean).join(" · ")
							}),
							/* @__PURE__ */ _jsx("td", { children: !readOnly && /* @__PURE__ */ _jsx("button", {
								className: "btn xs ghost",
								title: "quarantine: abort this node so it can't be selected",
								onClick: () => quarantine(h.node_id),
								children: "quarantine"
							}) })
						] }, i)) })]
					})
				}) : null
			]
		})
	});
}
export function SensitivityPanel({ state, onClose, onSelect }) {
	// Aggregate ablation impacts across all ablate events (latest wins per param).
	const impacts = {};
	(state.ablations || []).forEach((a) => Object.entries(a.impacts || {}).forEach(([k, v]) => {
		impacts[k] = Math.abs(v);
	}));
	const bars = Object.entries(impacts).map(([label, value]) => ({
		label,
		value
	})).sort((a, b) => b.value - a.value);
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Parameter sensitivity",
		onClose,
		wide: true,
		children: [
			/* @__PURE__ */ _jsx("div", {
				className: "section-h",
				children: "Ablation impact (|Δmetric| when param zeroed)"
			}),
			bars.length ? /* @__PURE__ */ _jsx(Bars, {
				data: bars,
				color: "#9a6bff"
			}) : /* @__PURE__ */ _jsx("div", {
				className: "muted",
				children: "No ablation events yet (enable ablate_every or use Force-ablate on a node)."
			}),
			/* @__PURE__ */ _jsx("div", {
				className: "section-h",
				children: "Parallel coordinates — params → metric"
			}),
			/* @__PURE__ */ _jsx(ParallelCoords, {
				nodes: Object.values(state.nodes),
				direction: state.direction,
				onPick: onSelect ? (id) => {
					onSelect(id);
					onClose && onClose();
				} : undefined
			})
		]
	});
}
export function FailuresPanel({ state, onClose, onSelect }) {
	const failed = Object.values(state.nodes).filter((n) => n.status === "failed");
	const byReason = {};
	failed.forEach((n) => {
		(byReason[n.error_reason || "unknown"] ||= []).push(n);
	});
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Failures",
		sub: `${failed.length} failed`,
		onClose,
		children: [/* @__PURE__ */ _jsxs("div", {
			className: "cardgrid",
			style: { marginBottom: 12 },
			children: [Object.entries(byReason).map(([r, ns]) => /* @__PURE__ */ _jsx(Stat, {
				n: ns.length,
				l: r
			}, r)), !failed.length && /* @__PURE__ */ _jsx(Stat, {
				n: 0,
				l: "no failures"
			})]
		}), /* @__PURE__ */ _jsx(DataTable, {
			caption: "Failed nodes and errors",
			card: false,
			children: /* @__PURE__ */ _jsxs("table", {
				className: "tbl",
				children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
					/* @__PURE__ */ _jsx("th", { children: "node" }),
					/* @__PURE__ */ _jsx("th", { children: "reason" }),
					/* @__PURE__ */ _jsx("th", { children: "error" })
				] }) }), /* @__PURE__ */ _jsx("tbody", { children: failed.map((n) => /* @__PURE__ */ _jsxs("tr", { children: [
					/* @__PURE__ */ _jsx("td", { children: /* @__PURE__ */ _jsxs("button", {
						type: "button",
						className: "btn xs ghost",
						onClick: () => {
							onSelect?.(n.id);
							onClose?.();
						},
						children: ["#", n.id]
					}) }),
					/* @__PURE__ */ _jsx("td", {
						className: "flag",
						children: n.error_reason
					}),
					/* @__PURE__ */ _jsx("td", {
						className: "muted",
						children: (n.error || "").slice(0, 80)
					})
				] }, n.id)) })]
			})
		})]
	});
}
// U1 · experiment queue: the search's planned/in-flight work, made VISIBLE and cancelable. Pending
// nodes (created, not yet evaluated) are the concrete queue — each cancelable via node_abort; queued
// control requests (injects/forks/confirm/ablate not yet materialized) show as read-only chips so
// the operator can see what's coming. Order is policy-driven (the engine picks next), so this is
// "see + cancel + add", not manual reordering (which no engine event supports).
export function QueuePanel({ state, runId, onSelect, onClose, onToast }) {
	const nodes = Object.values(state.nodes || {});
	const pending = nodes.filter((n) => n.status === "pending").sort((a, b) => a.id - b.id);
	const working = nodes.filter((n) => n.status === "running");
	const injects = (state.inject_requests || []).slice(state.injects_done || 0);
	const forks = (state.fork_requests || []).slice(state.forks_done || 0);
	const { confirms: confirmReq, ablates: ablateReq } = queuedGenerationControls(state);
	const cancel = async (id) => {
		await submitCommand(CONTROL.nodeAbort(runId, id, state.nodes?.[id]?.attempt), {
			success: `Cancelled #${id}`,
			noop: `#${id} was already settled`,
			executing: `Cancellation of #${id} requested — waiting for the run`,
			failure: `Could not cancel #${id}`
		}, onToast);
	};
	const queuedCount = pending.length + injects.length + forks.length + confirmReq.length + ablateReq.length;
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Queue",
		sub: `${queuedCount} planned / in-flight`,
		onClose,
		children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "muted",
				style: { marginBottom: 10 },
				children: [
					"The next experiment is chosen by the search policy; this is the live work-list — cancel a pending experiment, or add one from the chat (",
					/* @__PURE__ */ _jsx("code", { children: "/experiment" }),
					") or a node’s “explore”."
				]
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "section-h",
				children: ["Pending experiments ", pending.length > 0 && /* @__PURE__ */ _jsx("span", {
					className: "pill",
					children: pending.length
				})]
			}),
			pending.length ? /* @__PURE__ */ _jsx(DataTable, {
				caption: "Pending experiment queue",
				card: false,
				children: /* @__PURE__ */ _jsxs("table", {
					className: "tbl",
					children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("th", { children: "node" }),
						/* @__PURE__ */ _jsx("th", { children: "op" }),
						/* @__PURE__ */ _jsx("th", { children: "parents" }),
						/* @__PURE__ */ _jsx("th", { children: "hypothesis / rationale" }),
						/* @__PURE__ */ _jsx("th", { children: "action" })
					] }) }), /* @__PURE__ */ _jsx("tbody", { children: pending.map((n) => /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("td", { children: /* @__PURE__ */ _jsxs("button", {
							className: "btn xs ghost",
							onClick: () => {
								onSelect && onSelect(n.id);
								onClose();
							},
							children: ["#", n.id]
						}) }),
						/* @__PURE__ */ _jsxs("td", { children: [
							/* @__PURE__ */ _jsx("span", {
								className: "op-icon",
								children: /* @__PURE__ */ _jsx(OpIcon, {
									name: operatorMeta(n.operator).icon,
									size: 12
								})
							}),
							" ",
							n.operator
						] }),
						/* @__PURE__ */ _jsx("td", {
							className: "muted",
							children: (n.parent_ids || []).map((p) => "#" + p).join(", ") || "—"
						}),
						/* @__PURE__ */ _jsx("td", {
							className: "muted",
							children: stripMd(n.idea?.hypothesis || n.idea?.rationale || "").slice(0, 70)
						}),
						/* @__PURE__ */ _jsx("td", { children: /* @__PURE__ */ _jsx("button", {
							className: "btn xs ghost",
							"aria-label": `Cancel experiment ${n.id}`,
							title: "cancel this experiment (node_abort)",
							onClick: () => cancel(n.id),
							children: /* @__PURE__ */ _jsx(OpIcon, {
								name: "cross",
								size: 11
							})
						}) })
					] }, n.id)) })]
				})
			}) : /* @__PURE__ */ _jsx("div", {
				className: "muted",
				children: "No experiment is queued right now — the loop is idle or between picks."
			}),
			injects.length + forks.length + confirmReq.length + ablateReq.length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
				className: "section-h",
				children: "Queued control requests"
			}), /* @__PURE__ */ _jsxs("div", {
				className: "chips",
				children: [
					injects.map((q, i) => /* @__PURE__ */ _jsxs("span", {
						className: "chip sm",
						title: "operator-injected experiment awaiting materialization",
						children: ["inject: ", q.idea?.operator || "experiment"]
					}, "i" + i)),
					forks.map((q, i) => /* @__PURE__ */ _jsxs("span", {
						className: "chip sm",
						title: "fork awaiting materialization",
						children: ["fork #", q.from_node_id ?? q.parent_id ?? "?"]
					}, "f" + i)),
					confirmReq.map((r) => /* @__PURE__ */ _jsxs("span", {
						className: "chip sm",
						title: r.generation == null ? undefined : `node generation ${r.generation}`,
						children: ["confirm #", r.node_id]
					}, `c:${r.node_id}:${r.generation ?? "legacy"}`)),
					ablateReq.map((r) => /* @__PURE__ */ _jsxs("span", {
						className: "chip sm",
						title: r.generation == null ? undefined : `node generation ${r.generation}`,
						children: ["ablate #", r.node_id]
					}, `a:${r.node_id}:${r.generation ?? "legacy"}`))
				]
			})] })
		]
	});
}
// I5 · non-dominated (Pareto-optimal) set over the primary metric (direction-aware) + every
// extra_metric (treated as cost-like / minimize). A node is Pareto-optimal if no other node is
// at-least-as-good on all objectives and strictly better on one.
const paretoMetric = (node) => node.confirmed_mean ?? node.metric;
function paretoFront(nodes, direction) {
	const keys = [...new Set(nodes.flatMap((n) => Object.keys(n.extra_metrics || {})))];
	const vec = (n) => [direction === "min" ? paretoMetric(n) : -paretoMetric(n), ...keys.map((k) => {
		const v = n.extra_metrics?.[k];
		return v == null ? Infinity : v;
	})];
	const dominates = (a, b) => {
		let strict = false;
		for (let i = 0; i < a.length; i++) {
			if (a[i] > b[i]) return false;
			if (a[i] < b[i]) strict = true;
		}
		return strict;
	};
	const pts = nodes.map((n) => ({
		n,
		v: vec(n)
	}));
	return {
		keys,
		front: pts.filter((p) => !pts.some((q) => q !== p && dominates(q.v, p.v))).map((p) => p.n)
	};
}
export function ParetoPanel({ state, onClose, onSelect }) {
	const nodes = Object.values(state.nodes).filter((n) => paretoMetric(n) != null && n.feasible !== false);
	// first constraint dimension, if any
	const withV = nodes.filter((n) => (n.violations || []).length || Object.keys(n.extra_metrics || {}).length);
	let scatter = null;
	const cName = withV.length ? withV[0].violations?.[0]?.name || Object.keys(withV[0].extra_metrics || {})[0] : null;
	if (cName) {
		const data = nodes.map((n) => {
			const cv = (n.violations || []).find((v) => v.name === cName)?.value ?? n.extra_metrics?.[cName];
			return cv == null ? null : {
				x: cv,
				y: n.confirmed_mean ?? n.metric,
				feasible: n.feasible !== false,
				id: n.id
			};
		}).filter(Boolean);
		scatter = /* @__PURE__ */ _jsx(Scatter, {
			data,
			xlab: cName,
			ylab: "metric",
			onPick: onSelect ? (id) => {
				onSelect(id);
				onClose && onClose();
			} : undefined
		});
	}
	const archive = state.archive;
	const ops = {};
	Object.values(state.nodes).forEach((n) => {
		const o = ops[n.operator] ||= {
			n: 0,
			ev: 0
		};
		o.n++;
		if (n.status === "evaluated") o.ev++;
	});
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Pareto · Diversity · Operators",
		onClose,
		wide: true,
		children: [
			(() => {
				const { keys, front } = paretoFront(nodes, state.direction);
				const sortedFront = [...front].sort((a, b) => state.direction === "min" ? paretoMetric(a) - paretoMetric(b) : paretoMetric(b) - paretoMetric(a));
				return /* @__PURE__ */ _jsxs(_Fragment, { children: [
					/* @__PURE__ */ _jsxs("div", {
						className: "section-h",
						children: ["Pareto-optimal set (I5) ", keys.length ? /* @__PURE__ */ _jsxs("span", {
							className: "pill",
							children: [keys.length + 1, " objectives"]
						}) : /* @__PURE__ */ _jsx("span", {
							className: "pill",
							children: "metric only"
						})]
					}),
					sortedFront.length ? /* @__PURE__ */ _jsx(DataTable, {
						caption: "Pareto-optimal node metrics",
						card: false,
						children: /* @__PURE__ */ _jsxs("table", {
							className: "tbl",
							children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
								/* @__PURE__ */ _jsx("th", { children: "node" }),
								/* @__PURE__ */ _jsx("th", { children: "metric" }),
								keys.map((k) => /* @__PURE__ */ _jsx("th", { children: k }, k))
							] }) }), /* @__PURE__ */ _jsx("tbody", { children: sortedFront.map((n) => /* @__PURE__ */ _jsxs("tr", { children: [
								/* @__PURE__ */ _jsxs("td", { children: [
									"#",
									n.id,
									n.id === state.best_node_id ? /* @__PURE__ */ _jsx(OpIcon, {
										name: "crown",
										size: 10
									}) : ""
								] }),
								/* @__PURE__ */ _jsx("td", { children: fmt(n.confirmed_mean ?? n.metric) }),
								keys.map((k) => /* @__PURE__ */ _jsx("td", {
									className: "muted",
									children: fmt(n.extra_metrics?.[k])
								}, k))
							] }, n.id)) })]
						})
					}) : /* @__PURE__ */ _jsx("div", {
						className: "muted",
						children: "No feasible evaluated nodes yet."
					}),
					sortedFront.length > 0 && /* @__PURE__ */ _jsxs("div", {
						className: "muted",
						style: { marginTop: 8 },
						children: ["Confirmed mean is used when available; otherwise the recorded metric is used.", !keys.length && /* @__PURE__ */ _jsx(_Fragment, { children: " With one objective, every feasible node tied at the best displayed metric is Pareto-optimal. Add extra_metrics (e.g. latency, size) to show trade-offs." })]
					})
				] });
			})(),
			/* @__PURE__ */ _jsx("div", {
				className: "section-h",
				children: "Pareto (metric vs constraint)"
			}),
			scatter || /* @__PURE__ */ _jsx("div", {
				className: "muted",
				children: "No constraints/aux metrics in this task."
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "section-h",
				children: ["Diversity archive ", archive && /* @__PURE__ */ _jsxs("span", {
					className: "pill",
					children: [archive.niches, " niches"]
				})]
			}),
			archive?.elites?.length ? /* @__PURE__ */ _jsx(DataTable, {
				caption: "Diversity archive elite nodes",
				card: false,
				children: /* @__PURE__ */ _jsxs("table", {
					className: "tbl",
					children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("th", { children: "node" }),
						/* @__PURE__ */ _jsx("th", { children: "metric" }),
						/* @__PURE__ */ _jsx("th", { children: "params" })
					] }) }), /* @__PURE__ */ _jsx("tbody", { children: archive.elites.map((e, i) => /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsxs("td", { children: ["#", e.node_id] }),
						/* @__PURE__ */ _jsx("td", { children: fmt(e.metric) }),
						/* @__PURE__ */ _jsx("td", {
							className: "muted",
							children: JSON.stringify(e.params)
						})
					] }, i)) })]
				})
			}) : /* @__PURE__ */ _jsx("div", {
				className: "muted",
				children: "No archive (run not finished)."
			}),
			/* @__PURE__ */ _jsx("div", {
				className: "section-h",
				children: "Operator productivity"
			}),
			/* @__PURE__ */ _jsx(DataTable, {
				caption: "Operator productivity summary",
				card: false,
				children: /* @__PURE__ */ _jsxs("table", {
					className: "tbl",
					children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("th", { children: "operator" }),
						/* @__PURE__ */ _jsx("th", { children: "nodes" }),
						/* @__PURE__ */ _jsx("th", { children: "evaluated" })
					] }) }), /* @__PURE__ */ _jsx("tbody", { children: Object.entries(ops).map(([o, s]) => /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("td", { children: o }),
						/* @__PURE__ */ _jsx("td", { children: s.n }),
						/* @__PURE__ */ _jsx("td", { children: s.ev })
					] }, o)) })]
				})
			})
		]
	});
}
export function DataQualityPanel({ state, onClose }) {
	const prof = state.data_profile;
	if (!prof) return /* @__PURE__ */ _jsx(Panel, {
		title: "Data quality",
		onClose,
		children: /* @__PURE__ */ _jsx("div", {
			className: "muted",
			children: "No data profile (task exposes no dataset)."
		})
	});
	const cols = Object.entries(prof);
	return /* @__PURE__ */ _jsx(Panel, {
		title: "Data quality",
		sub: `${cols.length} columns`,
		onClose,
		wide: true,
		children: /* @__PURE__ */ _jsx(DataTable, {
			caption: "Dataset column quality profile",
			card: false,
			children: /* @__PURE__ */ _jsxs("table", {
				className: "tbl",
				children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
					/* @__PURE__ */ _jsx("th", { children: "column" }),
					/* @__PURE__ */ _jsx("th", { children: "dtype" }),
					/* @__PURE__ */ _jsx("th", { children: "missing%" }),
					/* @__PURE__ */ _jsx("th", { children: "unique" }),
					/* @__PURE__ */ _jsx("th", { children: "min" }),
					/* @__PURE__ */ _jsx("th", { children: "max" }),
					/* @__PURE__ */ _jsx("th", { children: "mean" }),
					/* @__PURE__ */ _jsx("th", { children: "flags" })
				] }) }), /* @__PURE__ */ _jsx("tbody", { children: cols.map(([c, s]) => /* @__PURE__ */ _jsxs("tr", { children: [
					/* @__PURE__ */ _jsx("td", { children: c }),
					/* @__PURE__ */ _jsx("td", { children: s.dtype }),
					/* @__PURE__ */ _jsx("td", { children: fmt((s.missing_frac || 0) * 100, 3) }),
					/* @__PURE__ */ _jsx("td", { children: fmtInt(s.n_unique) }),
					/* @__PURE__ */ _jsx("td", { children: fmt(s.min) }),
					/* @__PURE__ */ _jsx("td", { children: fmt(s.max) }),
					/* @__PURE__ */ _jsx("td", { children: fmt(s.mean) }),
					/* @__PURE__ */ _jsxs("td", { children: [s.constant && /* @__PURE__ */ _jsx("span", {
						className: "flag",
						children: "constant "
					}), s.high_missing && /* @__PURE__ */ _jsx("span", {
						className: "flag",
						children: "high-missing"
					})] })
				] }, c)) })]
			})
		})
	});
}
// Per-run config: shows the run's config.snapshot.json and lets you EDIT it. Edits are saved back to
// the snapshot, which a later RESUME re-reads (resume does NOT pick up the UI's global new-run
// defaults), so this is how you change a specific run's settings (e.g. raise `timeout`, enable timeout
// repair). Works for live runs too: saving the snapshot is safe mid-run (the engine never re-reads it),
// and a "Pause & resume" applies it now by restarting the engine (pause → wait for it to stop → resume).
export function ConfigPanel({ runId, expectedGeneration, state, live, onClose: closePanel, onToast, draftStore = null, navigationGuardOwner = "panel", publishNavigationGuard = null }) {
	const [cfg, setCfg] = useState(null);
	const [settingsSchema, setSettingsSchema] = useState(null);
	const [form, setForm] = useState(null);
	const [loadError, setLoadError] = useState("");
	const [loadNonce, setLoadNonce] = useState(0);
	const [saved, setSaved] = useState(null);
	const [agentControl, setAgentControl] = useState({});
	const [savedAC, setSavedAC] = useState({});
	const [configMeta, setConfigMeta] = useState({
		configRevision: "",
		pinnedFields: new Set(),
		readOnlyFields: new Set(),
		mismatchFields: []
	});
	const [sec, setSec] = useState("");
	const [busy, setBusy] = useState(false);
	const [raw, setRaw] = useState(false);
	const [configMutationUnknown, setConfigMutationUnknown] = useState(null);
	const [invalidFocus, setInvalidFocus] = useState({
		key: "",
		request: 0
	});
	const budgetHelpId = useId();
	const budgetInputId = `${budgetHelpId}-input`;
	const loadGenerationRef = useRef(0);
	const loadedIdentityRef = useRef({
		runId: "",
		expectedGeneration: ""
	});
	const mutationRef = useRef(null);
	const configSaveInFlightRef = useRef(false);
	const allowConfigNavigationRef = useRef(false);
	useLayoutEffect(() => {
		allowConfigNavigationRef.current = false;
		return () => {
			allowConfigNavigationRef.current = true;
		};
	}, []);
	const draftScope = configDraftScope(runId);
	const retainedDraftRef = useRef({
		scope: "",
		value: null
	});
	if (retainedDraftRef.current.scope !== draftScope) {
		retainedDraftRef.current = {
			scope: draftScope,
			value: validConfigDraftEnvelope(draftStore?.readField(draftScope, "draft", null), runId)
		};
	}
	useEffect(() => setSec(""), [runId, expectedGeneration]);
	useEffect(() => {
		const requestedGeneration = typeof expectedGeneration === "string" ? expectedGeneration : "";
		const previousIdentity = loadedIdentityRef.current;
		const generation = ++loadGenerationRef.current;
		const retainedDraft = retainedDraftRef.current.scope === draftScope ? retainedDraftRef.current.value : null;
		if (retainedDraft) {
			retainedDraftRef.current = {
				scope: draftScope,
				value: null
			};
			mutationRef.current = null;
			configSaveInFlightRef.current = false;
			loadedIdentityRef.current = {
				runId,
				expectedGeneration: retainedDraft.expectedGeneration
			};
			setBusy(false);
			setCfg(null);
			setLoadError("");
			setSettingsSchema(retainedDraft.settingsSchema);
			setForm(publicConfigForm(retainedDraft.form, retainedDraft.settingsSchema));
			setSaved(publicConfigForm(retainedDraft.saved, retainedDraft.settingsSchema));
			setAgentControl(retainedDraft.agentControl);
			setSavedAC(retainedDraft.savedAC);
			setConfigMeta(publicConfigMeta(retainedDraft.configMeta));
			setSec("");
			const generationChanged = retainedDraft.expectedGeneration !== requestedGeneration;
			const requiresReconcile = generationChanged || retainedDraft.reconcileGeneration === requestedGeneration;
			const retainedKeys = [...new Set([...retainedDraft.dirtyKeys, ...retainedDraft.configMutationUnknown?.uncertainKeys || []])];
			const retainedControlKeys = [...new Set([...retainedDraft.dirtyControlKeys, ...retainedDraft.configMutationUnknown?.uncertainControlKeys || []])];
			const retainedRecovery = requiresReconcile || retainedDraft.saveInFlight ? {
				stage: requiresReconcile ? "conflict" : "unknown",
				runId,
				generation,
				expectedGeneration: requestedGeneration,
				submittedForm: publicConfigForm(retainedDraft.form, retainedDraft.settingsSchema),
				submittedControl: retainedDraft.agentControl,
				uncertainKeys: retainedKeys,
				uncertainControlKeys: retainedControlKeys
			} : retainedDraft.configMutationUnknown;
			setConfigMutationUnknown(retainedRecovery);
			if (requiresReconcile) {
				onToast("The run changed. Your settings draft is retained in this tab; load the current version to review it.");
			} else if (retainedDraft.saveInFlight && !retainedDraft.configMutationUnknown) {
				onToast("A settings save was interrupted. Refresh server state before making another change.");
			}
			return undefined;
		}
		const generationChanged = previousIdentity.runId === runId && previousIdentity.expectedGeneration && previousIdentity.expectedGeneration !== requestedGeneration;
		const uncertainKeys = settingsSchema && form && saved ? Object.keys(settingsSchema.fieldByKey).filter((key) => JSON.stringify(form[key]) !== JSON.stringify(saved[key])) : [];
		const uncertainControlKeys = JSON.stringify(agentControl) !== JSON.stringify(savedAC) ? [...new Set([...Object.keys(agentControl), ...Object.keys(savedAC)])] : [];
		const retainedKeys = [...new Set([...uncertainKeys, ...configMutationUnknown?.uncertainKeys || []])];
		const retainedControlKeys = [...new Set([...uncertainControlKeys, ...configMutationUnknown?.uncertainControlKeys || []])];
		// A reset may replace the run while this panel has edits or an uncertain write. Keep that form
		// visibly fenced to its old identity until an explicit authoritative reload rebases the draft.
		if (generationChanged && form && saved && (retainedKeys.length || retainedControlKeys.length || busy || configMutationUnknown)) {
			mutationRef.current = null;
			setBusy(false);
			setLoadError("");
			setConfigMutationUnknown({
				stage: "conflict",
				runId,
				generation,
				expectedGeneration: requestedGeneration,
				submittedForm: publicConfigForm(form, settingsSchema),
				submittedControl: agentControl,
				uncertainKeys: retainedKeys,
				uncertainControlKeys: retainedControlKeys
			});
			onToast("The run changed. Load its current settings and review your retained draft.");
			return undefined;
		}
		loadedIdentityRef.current = {
			runId: "",
			expectedGeneration: ""
		};
		// A reused panel must never display or reconcile the previous run while the next config loads.
		mutationRef.current = null;
		configSaveInFlightRef.current = false;
		setBusy(false);
		setCfg(null);
		setSettingsSchema(null);
		setForm(null);
		setSaved(null);
		setLoadError("");
		setConfigMutationUnknown(null);
		setAgentControl({});
		setSavedAC({});
		setConfigMeta({
			configRevision: "",
			pinnedFields: new Set(),
			readOnlyFields: new Set(),
			mismatchFields: []
		});
		if (!RUN_GENERATION_RE.test(requestedGeneration)) {
			setLoadError("Run identity is not available yet. Wait for the current run state and retry.");
			return undefined;
		}
		const configRequest = deadlineGet(runApiPath(runId, "/config"), PANEL_REQUEST_TIMEOUT_MS);
		Promise.all([configRequest.promise, loadSettingsSchema({ reload: loadNonce > 0 })]).then(([c, nextSchema]) => {
			if (configRequest.controller.signal.aborted || generation !== loadGenerationRef.current) return;
			const parsed = splitRunConfigPayload(c, nextSchema);
			loadedIdentityRef.current = {
				runId,
				expectedGeneration: requestedGeneration
			};
			setCfg(parsed.config);
			setConfigMeta(parsed);
			setSettingsSchema(nextSchema);
			const f = toForm(parsed.config, nextSchema);
			setForm(f);
			setSaved(f);
			const ac = parsed.config.agent_control || {};
			setAgentControl(ac);
			setSavedAC(ac);
		}).catch((error) => {
			if (error?.name !== "AbortError" && generation === loadGenerationRef.current) {
				setCfg(null);
				setLoadError("Run settings could not be loaded. Check the connection and retry.");
			}
		});
		return () => configRequest.controller.abort();
	}, [
		runId,
		expectedGeneration,
		loadNonce,
		draftScope
	]);
	// A live engine keeps its in-memory settings until it restarts; gate on `live` (not the possibly
	// historical `state`) so time-travel doesn't misreport liveness.
	const engineLive = live?.engine_running === true;
	const engineStopped = live?.engine_running === false;
	const loadedIdentity = loadedIdentityRef.current;
	const configIdentityReady = loadedIdentity.runId === runId && loadedIdentity.expectedGeneration === expectedGeneration && RUN_GENERATION_RE.test(loadedIdentity.expectedGeneration);
	const controlBusy = busy || !!configMutationUnknown || !configIdentityReady;
	const liveEvalSeconds = Number(live?.total_eval_seconds ?? state?.total_eval_seconds);
	const runtimeEvalCeiling = Number(live?.budget_overrides?.max_eval_seconds ?? state?.budget_overrides?.max_eval_seconds);
	const configuredEvalCeiling = Number(cfg?.max_eval_seconds);
	const hasRuntimeEvalCeiling = Number.isFinite(runtimeEvalCeiling) && runtimeEvalCeiling > 0;
	const snapshotEvalCeilingKnown = engineStopped && cfg !== null;
	// The snapshot can be edited while an engine is live, but that engine keeps the settings it
	// launched with until restart. Without a folded runtime override the active ceiling is therefore
	// unknown here; presenting the mutable snapshot as current would make lowering warnings dishonest.
	const currentEvalCeiling = hasRuntimeEvalCeiling ? runtimeEvalCeiling : snapshotEvalCeilingKnown && Number.isFinite(configuredEvalCeiling) && configuredEvalCeiling > 0 ? configuredEvalCeiling : null;
	const currentEvalCeilingUnknown = !snapshotEvalCeilingKnown && !hasRuntimeEvalCeiling;
	const requestedEvalCeiling = Number(sec);
	const knownEvalSeconds = Number.isFinite(liveEvalSeconds) && liveEvalSeconds >= 0 ? liveEvalSeconds : null;
	const hasCeilingInput = sec.trim() !== "";
	const validEvalCeiling = hasCeilingInput && Number.isFinite(requestedEvalCeiling) && requestedEvalCeiling > 0 && requestedEvalCeiling <= 0xe8d4a51000;
	const unchangedEvalCeiling = validEvalCeiling && currentEvalCeiling != null && requestedEvalCeiling === currentEvalCeiling;
	const exhaustedEvalCeiling = validEvalCeiling && knownEvalSeconds != null && requestedEvalCeiling <= knownEvalSeconds;
	const loweringEvalCeiling = validEvalCeiling && currentEvalCeiling != null && requestedEvalCeiling < currentEvalCeiling;
	const replacingUnknownEvalCeiling = validEvalCeiling && currentEvalCeilingUnknown;
	let budgetHelp = currentEvalCeilingUnknown ? "The applied engine ceiling is not available in the latest state. " + "Setting a cumulative total replaces it immediately." : currentEvalCeiling == null ? "Current ceiling is unbounded. Enter a cumulative total to create a finite limit." : `Current ceiling ${fmtElapsedSeconds(currentEvalCeiling)}. Setting a value replaces this limit.`;
	if (knownEvalSeconds != null) {
		budgetHelp += ` ${fmtElapsedSeconds(knownEvalSeconds)} spent in the latest state.`;
	}
	if (hasCeilingInput && !validEvalCeiling) {
		budgetHelp = "Enter a finite positive ceiling no greater than 1,000,000,000,000 seconds.";
	} else if (unchangedEvalCeiling) {
		budgetHelp = `The eval ceiling is already ${fmtElapsedSeconds(requestedEvalCeiling)}.`;
	} else if (exhaustedEvalCeiling) {
		budgetHelp = `The latest state has already spent ${fmtElapsedSeconds(knownEvalSeconds)}; ` + "this ceiling will stop new evaluations at the next budget check.";
	} else if (loweringEvalCeiling) {
		budgetHelp = `Based on the latest loaded state, this lowers the ceiling by ` + `${fmtElapsedSeconds(currentEvalCeiling - requestedEvalCeiling)}.`;
	}
	const budgetHelpTone = hasCeilingInput && !validEvalCeiling ? " error" : loweringEvalCeiling || exhaustedEvalCeiling || replacingUnknownEvalCeiling ? " warning" : "";
	const resumeLabels = {
		success: "Resumed with the saved settings",
		noop: "Run was already running",
		executing: "Resume requested — waiting for the engine to load the saved settings",
		failure: "Resume failed"
	};
	const restartLabels = {
		success: "Restarted with the saved settings",
		noop: "Restart was already satisfied",
		executing: "Restart requested — the current experiment will stop before a replacement engine loads the saved settings",
		failure: "Restart failed"
	};
	const acceptResume = async (expectedGeneration = loadGenerationRef.current, requestedRunId = runId) => {
		const record = await CONTROL.resume(requestedRunId);
		if (expectedGeneration !== loadGenerationRef.current) return null;
		const feedback = commandFeedback(record, resumeLabels);
		onToast(feedback.message);
		return feedback;
	};
	const dirty = useMemo(() => {
		if (!form || !saved || !settingsSchema) return new Set();
		const cur = fromForm(form, settingsSchema, { allowClear: false });
		const base = fromForm(saved, settingsSchema, { allowClear: false }), s = new Set();
		for (const k of Object.keys(settingsSchema.fieldByKey)) {
			if (JSON.stringify(cur[k]) !== JSON.stringify(base[k])) s.add(k);
		}
		return s;
	}, [
		form,
		saved,
		settingsSchema
	]);
	const acDirty = useMemo(() => JSON.stringify(agentControl) !== JSON.stringify(savedAC), [agentControl, savedAC]);
	const validationErrors = useMemo(() => form && settingsSchema ? settingsValidationErrors(form, settingsSchema, { allowClear: false }) : {}, [form, settingsSchema]);
	const invalidCount = Object.keys(validationErrors).length;
	const hasChanges = dirty.size > 0 || acDirty;
	const canSave = hasChanges && invalidCount === 0;
	const configNavigationUnsafe = hasChanges || busy || !!configMutationUnknown;
	const configNavigationSummary = [
		hasChanges ? "This Run settings panel has unsaved changes." : "",
		configMutationUnknown?.stage === "conflict" ? "The server version changed while this draft was open." : configMutationUnknown ? "The last Run settings save may or may not have reached the server." : "",
		busy ? "A Run settings operation is still in progress; its server-side outcome may arrive after this view closes." : ""
	].filter(Boolean).join(" ");
	const configCloseMessage = `${configNavigationSummary} Closing it discards this panel's client-only state. Close the Run settings panel anyway?`;
	const configLeaveSummary = `${configNavigationSummary} Leaving this run discards this panel's client-only state.`;
	const writeConfigDraft = (overrides = {}) => {
		if (!draftStore || allowConfigNavigationRef.current) return;
		const nextForm = Object.hasOwn(overrides, "form") ? overrides.form : form;
		const nextSaved = Object.hasOwn(overrides, "saved") ? overrides.saved : saved;
		const nextAgentControl = Object.hasOwn(overrides, "agentControl") ? overrides.agentControl : agentControl;
		const nextSavedAC = Object.hasOwn(overrides, "savedAC") ? overrides.savedAC : savedAC;
		const nextRecovery = Object.hasOwn(overrides, "configMutationUnknown") ? overrides.configMutationUnknown : configMutationUnknown;
		const saveInFlight = Object.hasOwn(overrides, "saveInFlight") ? overrides.saveInFlight : configSaveInFlightRef.current;
		if (!nextForm || !nextSaved || !settingsSchema) return;
		const currentRecord = fromForm(nextForm, settingsSchema, { allowClear: false });
		const savedRecord = fromForm(nextSaved, settingsSchema, { allowClear: false });
		const dirtyKeys = Object.keys(settingsSchema.fieldByKey).filter((key) => JSON.stringify(currentRecord[key]) !== JSON.stringify(savedRecord[key]));
		const nextAcDirty = JSON.stringify(nextAgentControl) !== JSON.stringify(nextSavedAC);
		const dirtyControlKeys = nextAcDirty ? [...new Set([...Object.keys(nextAgentControl), ...Object.keys(nextSavedAC)])] : [];
		if (!dirtyKeys.length && !nextAcDirty && !nextRecovery && !saveInFlight) {
			if (configIdentityReady) draftStore.clear(draftScope);
			return;
		}
		const identityGeneration = loadedIdentityRef.current.expectedGeneration || expectedGeneration;
		if (!RUN_GENERATION_RE.test(identityGeneration || "")) return;
		const storedRecovery = nextRecovery ? {
			...nextRecovery,
			submittedForm: publicConfigForm(nextRecovery.submittedForm, settingsSchema)
		} : null;
		draftStore.updateField(draftScope, "draft", {
			schema: CONFIG_DRAFT_SCHEMA,
			unsafe: true,
			runId: String(runId),
			expectedGeneration: identityGeneration,
			settingsSchema,
			form: publicConfigForm(nextForm, settingsSchema),
			saved: publicConfigForm(nextSaved, settingsSchema),
			agentControl: nextAgentControl,
			savedAC: nextSavedAC,
			configMeta: publicConfigMeta(configMeta),
			saveInFlight,
			dirtyKeys,
			dirtyControlKeys,
			configMutationUnknown: storedRecovery
		}, null);
	};
	useEffect(() => {
		writeConfigDraft();
	}, [
		agentControl,
		busy,
		configIdentityReady,
		configMeta,
		configMutationUnknown,
		form,
		saved,
		savedAC,
		settingsSchema
	]);
	useLayoutEffect(() => {
		if (navigationGuardOwner !== "run" || typeof publishNavigationGuard !== "function") {
			return undefined;
		}
		return publishNavigationGuard({
			route: "config",
			unsafe: configNavigationUnsafe,
			closeMessage: configCloseMessage,
			leaveSummary: configLeaveSummary,
			dispose: () => {
				allowConfigNavigationRef.current = true;
				draftStore?.clear(draftScope);
			}
		});
	}, [
		navigationGuardOwner,
		publishNavigationGuard,
		configNavigationUnsafe,
		configCloseMessage,
		configLeaveSummary,
		draftStore,
		draftScope
	]);
	useEffect(() => {
		if (navigationGuardOwner === "run" || !configNavigationUnsafe) {
			return undefined;
		}
		const guardedHash = location.hash;
		return installNavigationLossGuard({
			allowRef: allowConfigNavigationRef,
			guardedHash,
			message: () => {
				const warning = configMutationUnknown?.stage === "conflict" ? "The server version changed while this draft was open." : configMutationUnknown ? "The last run-settings save may or may not have reached the server." : busy ? "A run-settings operation is still in progress." : "This run-settings panel has unsaved changes.";
				return `${warning} Leave this run anyway?`;
			},
			onAllow: () => draftStore?.clear(draftScope)
		});
	}, [
		navigationGuardOwner,
		configNavigationUnsafe,
		busy,
		configMutationUnknown,
		draftScope,
		draftStore
	]);
	const onChange = (k, v) => {
		const next = {
			...form,
			[k]: v
		};
		writeConfigDraft({ form: next });
		setForm(next);
	};
	const onToggleAgent = (key, role) => {
		const cur = new Set(agentControl[key] || []);
		cur.has(role) ? cur.delete(role) : cur.add(role);
		const next = {
			...agentControl,
			[key]: [...cur]
		};
		writeConfigDraft({ agentControl: next });
		setAgentControl(next);
	};
	const beginMutation = (kind = "control", reconcileGeneration = "") => {
		if (mutationRef.current || configMutationUnknown && kind !== "reconciling") return null;
		const identity = loadedIdentityRef.current;
		const mutationGeneration = kind === "reconciling" ? reconcileGeneration : identity.expectedGeneration;
		if (identity.runId !== runId || !RUN_GENERATION_RE.test(mutationGeneration) || mutationGeneration !== expectedGeneration || kind !== "reconciling" && identity.expectedGeneration !== expectedGeneration) return null;
		const token = {
			generation: loadGenerationRef.current,
			runId,
			expectedGeneration: mutationGeneration,
			kind
		};
		mutationRef.current = token;
		setBusy(true);
		return token;
	};
	const finishMutation = (token) => {
		if (mutationRef.current !== token) return;
		mutationRef.current = null;
		if (token.generation === loadGenerationRef.current) setBusy(false);
	};
	const focusFirstInvalid = () => {
		const key = Object.keys(validationErrors)[0];
		if (!key) return;
		setRaw(false);
		setInvalidFocus((previous) => ({
			key,
			request: previous.request + 1
		}));
	};
	const rememberUnknownSave = (stage, submittedForm, submittedControl, submittedRunId, mutation, uncertainKeys, uncertainControlKeys) => {
		if (allowConfigNavigationRef.current || mutation.generation !== loadGenerationRef.current || submittedRunId !== runId || mutation.expectedGeneration !== expectedGeneration) return;
		const recovery = {
			stage,
			runId: submittedRunId,
			generation: mutation.generation,
			expectedGeneration: mutation.expectedGeneration,
			submittedForm: publicConfigForm(toForm(fromForm(submittedForm, settingsSchema, { allowClear: false }), settingsSchema), settingsSchema),
			submittedControl,
			uncertainKeys,
			uncertainControlKeys
		};
		setConfigMutationUnknown(recovery);
		writeConfigDraft({
			configMutationUnknown: recovery,
			saveInFlight: true
		});
		onToast(stage === "conflict" ? "Run settings changed elsewhere. Load the current server version and review your retained draft." : "Save outcome unknown. Refresh the server state before making another change.");
	};
	const reconcileUnknownSave = async () => {
		const recovery = configMutationUnknown;
		if (!recovery) return;
		const mutation = beginMutation("reconciling", recovery.expectedGeneration);
		if (!mutation) return;
		const request = deadlineGet(runApiPath(recovery.runId, "/config"), PANEL_REQUEST_TIMEOUT_MS);
		try {
			const response = await request.promise;
			if (mutation.generation !== loadGenerationRef.current || recovery.runId !== runId || mutation.expectedGeneration !== expectedGeneration) return;
			const parsed = splitRunConfigPayload(response, settingsSchema);
			const acceptedForm = toForm(parsed.config, settingsSchema);
			const acceptedControl = parsed.config.agent_control || {};
			loadedIdentityRef.current = {
				runId: recovery.runId,
				expectedGeneration: mutation.expectedGeneration
			};
			setCfg(parsed.config);
			setConfigMeta(parsed);
			setSaved(acceptedForm);
			setSavedAC(acceptedControl);
			setForm((current) => reconcileUnknownRecord(current, recovery.submittedForm, acceptedForm, recovery.uncertainKeys));
			setAgentControl((current) => reconcileUnknownRecord(current, recovery.submittedControl, acceptedControl, recovery.uncertainControlKeys));
			setConfigMutationUnknown(null);
			onToast(recovery.stage === "conflict" ? "Current server settings loaded. Review the retained draft before saving again." : "Server state refreshed. The uncertain save was not replayed.");
		} catch (error) {
			if (mutation.generation === loadGenerationRef.current && recovery.runId === runId) {
				onToast("Server state is still unavailable: " + (error.message || error));
			}
		} finally {
			finishMutation(mutation);
		}
	};
	const onSave = async () => {
		if (configMutationUnknown || !configIdentityReady) {
			onToast("Load the current server settings before saving this retained draft.");
			return;
		}
		if (invalidCount) {
			focusFirstInvalid();
			onToast("Fix invalid settings before saving");
			return;
		}
		const submittedForm = form;
		const submittedControl = agentControl;
		const submittedRevision = configMeta.configRevision;
		const cur = fromForm(submittedForm, settingsSchema, { allowClear: false }), changed = {};
		for (const k of dirty) changed[k] = cur[k];
		if (acDirty) changed.agent_control = submittedControl;
		if (!Object.keys(changed).length) return;
		const mutation = beginMutation("save");
		if (!mutation) return;
		configSaveInFlightRef.current = true;
		writeConfigDraft({
			form: submittedForm,
			agentControl: submittedControl,
			saveInFlight: true
		});
		const submittedRunId = mutation.runId;
		try {
			const write = deadlineRequest((signal) => saveRunConfig(submittedRunId, changed, {
				signal,
				expectedRevision: submittedRevision,
				expectedGeneration: mutation.expectedGeneration
			}), PANEL_REQUEST_TIMEOUT_MS);
			const r = validateRunConfigSaveAck(await write.promise, settingsSchema);
			if (mutation.generation !== loadGenerationRef.current || loadedIdentityRef.current.expectedGeneration !== mutation.expectedGeneration) return;
			const parsed = splitRunConfigPayload(r.config, settingsSchema);
			const acceptedForm = toForm(parsed.config, settingsSchema);
			const acceptedControl = parsed.config.agent_control || {};
			setCfg(parsed.config);
			setConfigMeta(parsed);
			setSaved(acceptedForm);
			setSavedAC(acceptedControl);
			setForm((current) => reconcileAcceptedRecord(current, submittedForm, acceptedForm));
			setAgentControl((current) => reconcileAcceptedRecord(current, submittedControl, acceptedControl));
			const repaired = r.normalized_pinned?.length ? `; repaired legacy snapshot drift in ${r.normalized_pinned.join(", ")}` : "";
			const what = (r.changed?.length ? `saved ${r.changed.join(", ")}` : "saved") + repaired;
			onToast(what + (r.engine_running ? " — applies when the live run restarts" : " — applies on next resume"));
		} catch (e) {
			const disposition = e?.status === 409 && e?.code === "run_generation_changed" ? "conflict" : runConfigWriteDisposition(e);
			if (disposition === "conflict") {
				rememberUnknownSave("conflict", submittedForm, submittedControl, submittedRunId, mutation, Object.keys(changed).filter((key) => key !== "agent_control"), acDirty ? [...new Set([...Object.keys(submittedControl), ...Object.keys(savedAC)])] : []);
			} else if (disposition === "unknown") {
				rememberUnknownSave("unknown", submittedForm, submittedControl, submittedRunId, mutation, Object.keys(changed).filter((key) => key !== "agent_control"), acDirty ? [...new Set([...Object.keys(submittedControl), ...Object.keys(savedAC)])] : []);
			} else if (mutation.generation === loadGenerationRef.current) {
				onToast("save failed: " + e.message);
			}
		} finally {
			configSaveInFlightRef.current = false;
			finishMutation(mutation);
		}
	};
	const onResume = async () => {
		const mutation = beginMutation();
		if (!mutation) return;
		try {
			await acceptResume(mutation.generation, runId);
		} catch (e) {
			if (mutation.generation === loadGenerationRef.current) onToast("Resume failed: " + e.message);
		} finally {
			finishMutation(mutation);
		}
	};
	const onPauseResume = async () => {
		const mutation = beginMutation();
		if (!mutation) return;
		const submittedRunId = runId;
		try {
			// This is one durable command/postcondition. Never restore a client-side
			// pause-then-resume saga here: unmounting between commands would strand the accepted intent.
			const record = await CONTROL.restart(submittedRunId);
			if (mutation.generation !== loadGenerationRef.current) return;
			const feedback = commandFeedback(record, restartLabels);
			onToast(feedback.message);
		} catch (e) {
			if (mutation.generation === loadGenerationRef.current) onToast("Pause/resume failed: " + e.message);
		} finally {
			finishMutation(mutation);
		}
	};
	const setEvalCeiling = async () => {
		if (!validEvalCeiling || unchangedEvalCeiling || controlBusy) return;
		const mutation = beginMutation();
		if (!mutation) return;
		const submittedRunId = runId;
		const submittedInput = sec;
		const submittedCeiling = requestedEvalCeiling;
		try {
			const record = await CONTROL.setEvalCeiling(submittedRunId, submittedCeiling);
			if (mutation.generation !== loadGenerationRef.current) return;
			const feedback = commandFeedback(record, {
				success: `Eval ceiling set to ${submittedCeiling}s`,
				noop: `Eval ceiling is already ${submittedCeiling}s`,
				executing: `Eval ceiling change to ${submittedCeiling}s requested`,
				failure: "Eval ceiling change failed"
			});
			if (feedback.kind === "success") {
				setSec((current) => current === submittedInput ? "" : current);
			}
			onToast(feedback.message);
		} catch (error) {
			if (mutation.generation === loadGenerationRef.current) onToast(`Eval ceiling change failed: ${error.message || error}`);
		} finally {
			finishMutation(mutation);
		}
	};
	const revertConfigDraft = () => {
		setForm(saved);
		setAgentControl(savedAC);
		writeConfigDraft({
			form: saved,
			agentControl: savedAC,
			configMutationUnknown: null,
			saveInFlight: false
		});
	};
	const requestClose = () => {
		if (navigationGuardOwner === "run") {
			if (closePanel?.() !== false) allowConfigNavigationRef.current = true;
			return;
		}
		if (!hasChanges && !busy && !configMutationUnknown) {
			draftStore?.clear(draftScope);
			closePanel();
			return;
		}
		const warning = configMutationUnknown?.stage === "conflict" ? "The server version changed while this draft was open." : configMutationUnknown ? "The last save may or may not have reached the server." : busy ? "A settings operation is still in progress." : "This panel has unsaved changes.";
		if (window.confirm(`${warning} Close the run settings panel anyway?`)) {
			allowConfigNavigationRef.current = true;
			draftStore?.clear(draftScope);
			closePanel();
		}
	};
	// PanelShell routes Escape, backdrop clicks, and its close button through this single guard.
	const onClose = requestClose;
	const rawTable = /* @__PURE__ */ _jsx(DataTable, {
		caption: "Raw run configuration",
		card: false,
		children: /* @__PURE__ */ _jsx("table", {
			className: "tbl",
			children: /* @__PURE__ */ _jsx("tbody", { children: cfg && Object.entries(cfg).map(([k, v]) => /* @__PURE__ */ _jsxs("tr", { children: [/* @__PURE__ */ _jsx("th", {
				scope: "row",
				className: "muted",
				children: k
			}), /* @__PURE__ */ _jsx("td", { children: typeof v === "object" ? JSON.stringify(v) : String(v) })] }, k)) })
		})
	});
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Run settings",
		sub: engineLive ? "live · applies on restart" : engineStopped ? "edit · applies on resume" : "engine status unknown",
		onClose,
		wide: true,
		children: [
			/* @__PURE__ */ _jsxs("form", {
				className: "toolbar",
				style: { marginBottom: 12 },
				onSubmit: (event) => {
					event.preventDefault();
					setEvalCeiling();
				},
				children: [
					/* @__PURE__ */ _jsx("label", {
						className: "muted",
						htmlFor: budgetInputId,
						children: "set eval ceiling:"
					}),
					/* @__PURE__ */ _jsx("input", {
						id: budgetInputId,
						className: "text",
						style: { width: 140 },
						type: "number",
						max: "1000000000000",
						step: "any",
						inputMode: "decimal",
						"aria-label": "Cumulative evaluation budget ceiling in seconds",
						"aria-describedby": budgetHelpId,
						"aria-invalid": hasCeilingInput && !validEvalCeiling ? "true" : undefined,
						placeholder: "total seconds",
						value: sec,
						disabled: controlBusy,
						onChange: (e) => setSec(e.target.value)
					}),
					/* @__PURE__ */ _jsx("button", {
						className: "btn sm primary" + (loweringEvalCeiling || exhaustedEvalCeiling || replacingUnknownEvalCeiling ? " warn" : ""),
						type: "submit",
						disabled: !validEvalCeiling || unchangedEvalCeiling || controlBusy,
						children: "set ceiling"
					}),
					/* @__PURE__ */ _jsx("span", {
						id: budgetHelpId,
						className: "budget-ceiling-help" + budgetHelpTone,
						role: hasCeilingInput ? "status" : undefined,
						children: budgetHelp
					})
				]
			}),
			configMutationUnknown && /* @__PURE__ */ _jsxs("div", {
				className: "report-inline-state error",
				role: "alert",
				style: { marginBottom: 12 },
				children: [
					/* @__PURE__ */ _jsx(OpIcon, {
						name: "alert",
						size: 14
					}),
					configMutationUnknown.stage === "conflict" ? /* @__PURE__ */ _jsxs("span", { children: [/* @__PURE__ */ _jsx("b", { children: "Run settings changed elsewhere." }), " Load the current server version, then review your retained draft before saving it against the new version."] }) : /* @__PURE__ */ _jsxs("span", { children: [/* @__PURE__ */ _jsx("b", { children: "Save outcome unknown." }), " The request timed out or lost its response. Refresh the authoritative server state; this client will not replay the save automatically."] }),
					/* @__PURE__ */ _jsx("button", {
						className: "btn sm",
						disabled: busy,
						onClick: reconcileUnknownSave,
						children: configMutationUnknown.stage === "conflict" ? "Load current version" : "Refresh server state"
					})
				]
			}),
			!form || !settingsSchema ? loadError ? /* @__PURE__ */ _jsxs("div", {
				className: "report-inline-state error",
				role: "alert",
				children: [
					/* @__PURE__ */ _jsx(OpIcon, {
						name: "alert",
						size: 14
					}),
					/* @__PURE__ */ _jsx("span", { children: loadError }),
					/* @__PURE__ */ _jsx("button", {
						className: "btn sm",
						onClick: () => setLoadNonce((value) => value + 1),
						children: "Retry"
					})
				]
			}) : /* @__PURE__ */ _jsx("div", {
				className: "muted",
				role: "status",
				children: "Loading run settings…"
			}) : /* @__PURE__ */ _jsxs(_Fragment, { children: [
				/* @__PURE__ */ _jsxs("div", {
					className: "notice",
					style: { marginBottom: 10 },
					children: [
						engineLive ? /* @__PURE__ */ _jsxs(_Fragment, { children: [
							"This run is ",
							/* @__PURE__ */ _jsx("b", { children: "live" }),
							". Saving updates its ",
							/* @__PURE__ */ _jsx("code", { children: "config.snapshot.json" }),
							", but the running engine keeps its current settings until it restarts — use ",
							/* @__PURE__ */ _jsx("b", { children: "Pause & resume" }),
							" to stop it (the current experiment finishes first) and continue with the new settings."
						] }) : /* @__PURE__ */ _jsxs(_Fragment, { children: [
							"Edits are saved to this run's ",
							/* @__PURE__ */ _jsx("code", { children: "config.snapshot.json" }),
							" and applied on the next ",
							/* @__PURE__ */ _jsx("b", { children: "resume" }),
							"."
						] }),
						" ",
						/* @__PURE__ */ _jsx("span", {
							className: "sf-dot unsaved",
							children: "●"
						}),
						" = changed."
					]
				}),
				configMeta.pinnedFields.size > 0 && /* @__PURE__ */ _jsxs("div", {
					className: "notice",
					role: "note",
					style: { marginBottom: 10 },
					children: [
						"Fields marked ",
						/* @__PURE__ */ _jsx("b", { children: "launch-pinned" }),
						" show the values recorded in this run's event log and cannot be changed on resume. Start a new run to change holdout or verifier semantics.",
						configMeta.mismatchFields.length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [
							" ",
							"A legacy snapshot disagrees for ",
							configMeta.mismatchFields.join(", "),
							"; the effective launch values are shown and will be repaired when another editable setting is saved."
						] })
					]
				}),
				/* @__PURE__ */ _jsxs("div", {
					className: "toolbar",
					style: { marginBottom: 10 },
					children: [
						/* @__PURE__ */ _jsx("span", {
							className: "spacer",
							style: { flex: 1 }
						}),
						/* @__PURE__ */ _jsx("button", {
							className: "btn sm ghost",
							disabled: !cfg,
							title: !cfg ? "Load the current server version before viewing raw settings" : undefined,
							onClick: () => setRaw((r) => !r),
							children: raw ? "form" : "raw"
						}),
						invalidCount > 0 && /* @__PURE__ */ _jsxs("button", {
							type: "button",
							className: "settings-summary-link settings-save-state is-invalid",
							onClick: focusFirstInvalid,
							children: [
								invalidCount,
								" invalid setting",
								invalidCount === 1 ? "" : "s",
								" — review"
							]
						}),
						/* @__PURE__ */ _jsx("button", {
							className: "btn sm ghost",
							disabled: controlBusy || !hasChanges,
							onClick: revertConfigDraft,
							children: "↺ revert"
						}),
						/* @__PURE__ */ _jsx("button", {
							className: "btn sm primary",
							disabled: controlBusy || !canSave,
							onClick: onSave,
							children: "Save"
						}),
						engineLive ? /* @__PURE__ */ _jsx("button", {
							className: "btn sm",
							disabled: controlBusy || hasChanges,
							onClick: onPauseResume,
							title: "pause the run, then resume it with the saved settings",
							children: "Pause & resume ▸"
						}) : /* @__PURE__ */ _jsx("button", {
							className: "btn sm",
							disabled: controlBusy || hasChanges,
							onClick: onResume,
							title: "continue this run with the saved settings",
							children: "Resume ▸"
						})
					]
				}),
				raw ? rawTable : /* @__PURE__ */ _jsx(SettingsForm, {
					form,
					onChange,
					unsaved: dirty,
					errors: validationErrors,
					agentControl,
					onToggleAgent,
					readOnlyKeys: configMeta.readOnlyFields,
					hideSecret: true,
					schema: settingsSchema,
					focusKey: invalidFocus.key,
					focusRequest: invalidFocus.request
				})
			] })
		]
	});
}
export function AuthoringPanel({ onClose, onToast, draftStore: sharedDraftStore = null, navigationGuardOwner = "panel", publishNavigationGuard = null }) {
	const [kind, setKind] = useState("prompts");
	const [selectedScope, setSelectedScope] = useState(null);
	const fallbackDraftStoreRef = useRef(null);
	if (!fallbackDraftStoreRef.current) fallbackDraftStoreRef.current = createInspectorDraftStore();
	const draftStore = sharedDraftStore || fallbackDraftStoreRef.current;
	const allowNavigationRef = useRef(false);
	const [documents, setStoredDocuments] = useInspectorDraftField(draftStore, AUTHORING_PANEL_DRAFT_SCOPE, "documents", {});
	const documentsRef = useRef(documents);
	documentsRef.current = documents;
	const setDocuments = (update) => {
		if (allowNavigationRef.current) return documentsRef.current;
		return setStoredDocuments(update);
	};
	const [saveState, setSaveState] = useState(null);
	const [reconciledSource, setReconciledSource] = useState(null);
	const initialRecoveryRef = useRef(null);
	if (initialRecoveryRef.current == null) initialRecoveryRef.current = inspectAuthoringOperations();
	const initialUncertainSavesRef = useRef(null);
	if (initialUncertainSavesRef.current == null) {
		initialUncertainSavesRef.current = Object.fromEntries(Object.entries(initialRecoveryRef.current.valid).map(([scope, recovery]) => [scope, {
			...recovery,
			phase: "unknown",
			inspectedMissing: false,
			releaseAllowed: false,
			releaseInspected: false,
			message: `A saved operation for ${recovery.name} may still be pending. Check its exact durable receipt.`
		}]));
	}
	const [uncertainSaves, setStoredUncertainSaves] = useInspectorDraftField(draftStore, AUTHORING_PANEL_DRAFT_SCOPE, "uncertainSaves", initialUncertainSavesRef.current);
	const [damagedRecoveries, setStoredDamagedRecoveries] = useInspectorDraftField(draftStore, AUTHORING_PANEL_DRAFT_SCOPE, "damagedRecoveries", initialRecoveryRef.current.damaged);
	const [storageAvailable, setStorageAvailable] = useState(initialRecoveryRef.current.available);
	const uncertainSavesRef = useRef(uncertainSaves);
	uncertainSavesRef.current = uncertainSaves;
	const damagedRecoveriesRef = useRef(damagedRecoveries);
	damagedRecoveriesRef.current = damagedRecoveries;
	const setUncertainSaves = (update) => {
		if (allowNavigationRef.current) return uncertainSavesRef.current;
		return setStoredUncertainSaves(update);
	};
	const setDamagedRecoveries = (update) => {
		if (allowNavigationRef.current) return damagedRecoveriesRef.current;
		return setStoredDamagedRecoveries(update);
	};
	const saveRef = useRef(null);
	const activeRef = useRef(true);
	const [source, retry] = usePanelResource((signal) => get(`/api/${kind}`, { signal }), authoringPayload, kind);
	const data = source.data || {
		dir: null,
		targetRootId: null,
		files: [],
		truncatedFiles: 0
	};
	const scopeFor = authoringScope;
	useEffect(() => {
		// Materialize the initial lazy-panel fallback in RunView's shared store. A direct storage scan
		// covers an even earlier generation replacement; subsequent recovery changes are synchronous.
		setStoredUncertainSaves((current) => current);
		setStoredDamagedRecoveries((current) => current);
	}, [setStoredDamagedRecoveries, setStoredUncertainSaves]);
	useLayoutEffect(() => {
		activeRef.current = true;
		allowNavigationRef.current = false;
		return () => {
			activeRef.current = false;
			allowNavigationRef.current = true;
		};
	}, []);
	useEffect(() => {
		if (source.state !== "ready") {
			setReconciledSource(null);
			return;
		}
		setDocuments((current) => {
			let changed = false;
			const next = { ...current };
			for (const file of data.files || []) {
				const scope = scopeFor(kind, file.name);
				const previous = current[scope];
				if (!previous) {
					next[scope] = {
						kind,
						name: file.name,
						savedText: file.text,
						draftText: file.text,
						savedRevision: file.revision,
						observedText: file.text,
						observedRevision: file.revision,
						savedTargetRootId: data.targetRootId,
						observedTargetRootId: data.targetRootId,
						truncated: file.truncated,
						conflict: false,
						rootConflict: false,
						observationIncomplete: false,
						error: "",
						recoveryOperationId: null,
						recoveryStorageRaw: null
					};
					changed = true;
				} else if (previous.draftText === previous.savedText && !uncertainSaves[scope]) {
					if (previous.savedText !== file.text || previous.observedText !== file.text || previous.savedRevision !== file.revision || previous.truncated !== file.truncated || previous.savedTargetRootId !== data.targetRootId || previous.observedTargetRootId !== data.targetRootId || previous.conflict || previous.rootConflict || previous.observationIncomplete || previous.error) {
						next[scope] = {
							...previous,
							savedText: file.text,
							draftText: file.text,
							savedRevision: file.revision,
							observedText: file.text,
							observedRevision: file.revision,
							savedTargetRootId: data.targetRootId,
							observedTargetRootId: data.targetRootId,
							truncated: file.truncated,
							conflict: false,
							rootConflict: false,
							observationIncomplete: false,
							error: ""
						};
						changed = true;
					}
				} else if (previous.observedText !== file.text || previous.observedRevision !== file.revision || previous.observedTargetRootId !== data.targetRootId || previous.observationIncomplete || previous.conflict !== (previous.savedRevision !== file.revision || previous.savedTargetRootId !== data.targetRootId)) {
					// A refresh must never replace local text. Remember the newly observed server version and
					// make the conflict explicit while preserving both the draft and its original baseline.
					const rootConflict = previous.savedTargetRootId !== data.targetRootId;
					next[scope] = {
						...previous,
						observedText: file.text,
						observedRevision: file.revision,
						observedTargetRootId: data.targetRootId,
						truncated: file.truncated,
						rootConflict,
						observationIncomplete: false,
						conflict: rootConflict || previous.savedRevision !== file.revision
					};
					changed = true;
				}
			}
			const visibleNames = new Set((data.files || []).map((file) => file.name));
			const listComplete = data.truncatedFiles === 0;
			for (const [scope, previous] of Object.entries(current)) {
				if (previous.kind !== kind || visibleNames.has(previous.name) || uncertainSaves[scope]) continue;
				// A retained document that disappeared from the response must be rebound even while clean.
				// Otherwise an edit made after this refresh would still submit the old root/revision. Only a
				// complete list proves absence; a capped list leaves the observation unknown and Save disabled.
				const observedRevision = validAuthoringTargetRootId(data.targetRootId) && listComplete ? "missing" : null;
				const rootConflict = previous.savedTargetRootId !== data.targetRootId;
				const observationIncomplete = validAuthoringTargetRootId(data.targetRootId) && !listComplete;
				const conflict = rootConflict || observationIncomplete || previous.savedRevision !== observedRevision;
				if (previous.observedText !== null || previous.observedRevision !== observedRevision || previous.observedTargetRootId !== data.targetRootId || previous.rootConflict !== rootConflict || previous.conflict !== conflict || previous.observationIncomplete !== observationIncomplete) {
					next[scope] = {
						...previous,
						observedText: null,
						observedRevision,
						observedTargetRootId: data.targetRootId,
						rootConflict,
						conflict,
						observationIncomplete
					};
					changed = true;
				}
			}
			for (const recovery of Object.values(uncertainSaves)) {
				if (recovery.kind !== kind) continue;
				const scope = recovery.scope;
				const previous = next[scope];
				const sameRoot = data.targetRootId === recovery.expectedTargetRootId;
				const file = sameRoot ? (data.files || []).find((candidate) => candidate.name === recovery.name) : null;
				const observationIncomplete = sameRoot && !file && !listComplete;
				const observedRevision = sameRoot ? file ? file.revision : listComplete ? "missing" : null : null;
				const sameRecovery = previous?.recoveryOperationId === recovery.operationId && previous?.recoveryStorageRaw === recovery.storageRaw;
				const hydrated = {
					...sameRecovery ? previous : {},
					kind: recovery.kind,
					name: recovery.name,
					savedText: sameRecovery ? previous.savedText : file?.text ?? "",
					draftText: sameRecovery ? previous.draftText : recovery.submittedText,
					savedRevision: recovery.expectedRevision,
					observedText: file?.text ?? null,
					observedRevision,
					savedTargetRootId: recovery.expectedTargetRootId,
					observedTargetRootId: data.targetRootId,
					truncated: file?.truncated === true,
					rootConflict: !sameRoot,
					observationIncomplete,
					conflict: !sameRoot || observationIncomplete || observedRevision !== recovery.expectedRevision && observedRevision !== recovery.desiredRevision,
					error: recovery.message,
					recoveryOperationId: recovery.operationId,
					recoveryStorageRaw: recovery.storageRaw
				};
				if (!previous || Object.keys(hydrated).some((key) => hydrated[key] !== previous[key])) {
					next[scope] = hydrated;
					changed = true;
				}
			}
			return changed ? next : current;
		});
		if (!allowNavigationRef.current) {
			setReconciledSource((current) => current?.kind === kind && current?.data === source.data ? current : {
				kind,
				data: source.data
			});
		}
	}, [
		kind,
		source.state,
		source.data,
		uncertainSaves
	]);
	const selected = selectedScope ? documents[selectedScope] || null : null;
	const sourceReconciled = source.state === "ready" && reconciledSource?.kind === kind && reconciledSource?.data === source.data;
	const selectedSourceReconciled = sourceReconciled && selected?.kind === kind;
	const selectedUncertainSave = selectedScope ? uncertainSaves[selectedScope] || null : null;
	const damagedRows = Object.values(damagedRecoveries);
	const selectedDamagedRecovery = damagedRows.find((recovery) => !recovery.identity || recovery.identity.scope === selectedScope) || null;
	const uncertainSaveCount = Object.keys(uncertainSaves).length;
	const damagedRecoveryCount = damagedRows.length;
	const dirtyCount = Object.values(documents).filter((document) => document.draftText !== document.savedText).length;
	const mutationBusy = !!saveState;
	const navigationUnsafe = dirtyCount > 0 || mutationBusy || uncertainSaveCount > 0 || damagedRecoveryCount > 0;
	const authoringNavigationSummary = [
		dirtyCount > 0 ? `${dirtyCount} unsaved Authoring draft${dirtyCount === 1 ? "" : "s"} will be discarded.` : "",
		mutationBusy ? "A file save is still in progress; its immediate outcome may no longer be visible here." : "",
		uncertainSaveCount > 0 ? `${uncertainSaveCount} file save outcome${uncertainSaveCount === 1 ? "" : "s"} may still be unknown; retained recovery must be reviewed before any retry.` : "",
		damagedRecoveryCount > 0 ? `${damagedRecoveryCount} damaged Authoring recovery record${damagedRecoveryCount === 1 ? " remains" : "s remain"} quarantined in this browser tab.` : ""
	].filter(Boolean).join(" ");
	const authoringCloseMessage = `${authoringNavigationSummary} Close Authoring anyway?`;
	const navigationUnsafeRef = useRef(navigationUnsafe);
	navigationUnsafeRef.current = navigationUnsafe;
	useLayoutEffect(() => {
		if (navigationGuardOwner !== "run" || typeof publishNavigationGuard !== "function") {
			return undefined;
		}
		return publishNavigationGuard({
			route: "authoring",
			unsafe: navigationUnsafe,
			closeMessage: authoringCloseMessage,
			leaveSummary: authoringNavigationSummary,
			dispose: () => {
				allowNavigationRef.current = true;
				activeRef.current = false;
				draftStore.clear(AUTHORING_PANEL_DRAFT_SCOPE);
			}
		});
	}, [
		navigationGuardOwner,
		publishNavigationGuard,
		navigationUnsafe,
		authoringCloseMessage,
		authoringNavigationSummary,
		draftStore
	]);
	useEffect(() => {
		if (navigationGuardOwner === "run" || !navigationUnsafe) {
			return undefined;
		}
		return installNavigationLossGuard({
			allowRef: allowNavigationRef,
			guardedHash: location.hash,
			message: () => uncertainSaveCount > 0 ? `${uncertainSaveCount} file save outcome${uncertainSaveCount === 1 ? "" : "s"} may still be unknown. Leave Authoring anyway?` : damagedRecoveryCount > 0 ? `${damagedRecoveryCount} damaged Authoring recovery record${damagedRecoveryCount === 1 ? "" : "s"} remain quarantined. Leave Authoring anyway?` : mutationBusy ? "A file save is still in progress. Leave Authoring anyway?" : `${dirtyCount} unsaved Authoring draft${dirtyCount === 1 ? "" : "s"} will be lost. Leave anyway?`,
			onAllow: () => draftStore.clear(AUTHORING_PANEL_DRAFT_SCOPE)
		});
	}, [
		navigationGuardOwner,
		draftStore,
		navigationUnsafe,
		mutationBusy,
		uncertainSaveCount,
		damagedRecoveryCount,
		dirtyCount
	]);
	useEffect(() => () => {
		const retained = draftStore.readField(AUTHORING_PANEL_DRAFT_SCOPE, "documents", {});
		const hasUnsafeStoredDocument = retained && typeof retained === "object" && !Array.isArray(retained) && Object.values(retained).some((document) => document && typeof document === "object" && !Array.isArray(document) && (document.draftText !== document.savedText || document.recoveryOperationId || document.recoveryStorageRaw));
		const hasUnsafeStoredRecovery = ["uncertainSaves", "damagedRecoveries"].some((field) => {
			const records = draftStore.readField(AUTHORING_PANEL_DRAFT_SCOPE, field, {});
			return records && typeof records === "object" && !Array.isArray(records) && Object.keys(records).length > 0;
		});
		if (allowNavigationRef.current || !navigationUnsafeRef.current && !hasUnsafeStoredDocument && !hasUnsafeStoredRecovery) {
			draftStore.clear(AUTHORING_PANEL_DRAFT_SCOPE);
		}
	}, [draftStore]);
	const retainNotice = (destination) => {
		if (!selected || selected.draftText === selected.savedText) return;
		onToast?.(`Unsaved draft for ${selected.name} is preserved while you ${destination}.`);
	};
	const chooseKind = (nextKind) => {
		if (nextKind === kind) return;
		retainNotice("switch sections");
		setKind(nextKind);
		setSelectedScope(null);
	};
	const chooseFile = (file) => {
		const scope = scopeFor(kind, file.name);
		if (scope === selectedScope) return;
		retainNotice("switch files");
		setDocuments((current) => current[scope] ? current : {
			...current,
			[scope]: {
				kind,
				name: file.name,
				savedText: file.text,
				draftText: file.text,
				savedRevision: file.revision,
				observedText: file.text,
				observedRevision: file.revision,
				savedTargetRootId: data.targetRootId,
				observedTargetRootId: data.targetRootId,
				truncated: file.truncated,
				conflict: false,
				rootConflict: false,
				observationIncomplete: false,
				error: "",
				recoveryOperationId: null,
				recoveryStorageRaw: null
			}
		});
		setSelectedScope(scope);
	};
	const editSelected = (value) => {
		if (!selectedScope) return;
		setDocuments((current) => current[selectedScope] ? {
			...current,
			[selectedScope]: {
				...current[selectedScope],
				draftText: value,
				error: ""
			}
		} : current);
	};
	const updateRecovery = (token, patch) => setUncertainSaves((current) => {
		const exact = current[token.scope];
		if (!exact || exact.operationId !== token.operationId || exact.storageRaw !== token.storageRaw) return current;
		return {
			...current,
			[token.scope]: {
				...exact,
				...patch
			}
		};
	});
	const removeRecovery = (token) => setUncertainSaves((current) => {
		const exact = current[token.scope];
		if (!exact || exact.operationId !== token.operationId || exact.storageRaw !== token.storageRaw) return current;
		const next = { ...current };
		delete next[token.scope];
		return next;
	});
	const quarantineAuthoringRecovery = (recovery, message) => {
		const exact = uncertainSavesRef.current[recovery?.scope];
		if (!exact || exact.operationId !== recovery.operationId || exact.storageRaw !== recovery.storageRaw) return false;
		const next = { ...uncertainSavesRef.current };
		delete next[recovery.scope];
		uncertainSavesRef.current = next;
		setUncertainSaves(next);
		authoringDigestQuarantine.set(recovery.storageKey, {
			raw: recovery.storageRaw,
			reason: message
		});
		setDamagedRecoveries((current) => {
			let damagedScope = `damaged:${recovery.storageKey}`;
			while (Object.hasOwn(current, damagedScope) && (current[damagedScope].key !== recovery.storageKey || current[damagedScope].raw !== recovery.storageRaw)) damagedScope += ":retained";
			return {
				...current,
				[damagedScope]: {
					scope: damagedScope,
					key: recovery.storageKey,
					raw: recovery.storageRaw,
					identity: {
						kind: recovery.kind,
						name: recovery.name,
						scope: recovery.scope
					},
					inspected: false,
					reason: message
				}
			};
		});
		setDocuments((current) => {
			const retained = current[recovery.scope] || {
				kind: recovery.kind,
				name: recovery.name,
				savedText: null,
				draftText: recovery.submittedText,
				savedRevision: recovery.expectedRevision,
				observedText: null,
				observedRevision: recovery.expectedRevision,
				savedTargetRootId: recovery.expectedTargetRootId,
				observedTargetRootId: null,
				truncated: false,
				conflict: false,
				rootConflict: false,
				observationIncomplete: false
			};
			return {
				...current,
				[recovery.scope]: {
					...retained,
					error: message,
					recoveryOperationId: null,
					recoveryStorageRaw: null
				}
			};
		});
		onToast?.(message);
		return true;
	};
	const verifyAuthoringRecovery = async (recovery) => {
		const exact = uncertainSavesRef.current[recovery?.scope];
		if (!exact || exact.operationId !== recovery.operationId || exact.storageRaw !== recovery.storageRaw) return null;
		let actualRevision;
		try {
			actualRevision = await authoringTextRevision(exact.submittedText);
		} catch (error) {
			const message = `Could not verify the retained contents for ${exact.name}: ${error?.message || error}. No request was sent.`;
			updateRecovery(exact, {
				phase: "unknown",
				releaseAllowed: false,
				releaseInspected: false,
				message
			});
			onToast?.(message);
			return null;
		}
		const storage = authoringStorage();
		const latest = uncertainSavesRef.current[exact.scope];
		let storedRaw = null;
		try {
			storedRaw = storage?.getItem(exact.storageKey) ?? null;
		} catch {
			setStorageAvailable(false);
			const message = `Browser recovery storage became unavailable while verifying ${exact.name}. No request was sent.`;
			updateRecovery(exact, {
				phase: "unknown",
				releaseAllowed: false,
				releaseInspected: false,
				message
			});
			onToast?.(message);
			return null;
		}
		if (!storage || storedRaw !== exact.storageRaw || !latest || latest.operationId !== exact.operationId || latest.storageRaw !== exact.storageRaw) {
			refreshRecoveryStore();
			onToast?.("The Authoring recovery record changed while it was being verified. Inspect the refreshed exact record.");
			return null;
		}
		if (actualRevision !== exact.desiredRevision) {
			quarantineAuthoringRecovery(exact, `The retained operation for ${exact.name} has an invalid content digest. It was quarantined without contacting the server.`);
			return null;
		}
		return latest;
	};
	const refreshRecoveryStore = () => {
		const inspected = inspectAuthoringOperations();
		setStorageAvailable(inspected.available);
		if (!inspected.available) return;
		setDamagedRecoveries((current) => {
			const incoming = Object.values(inspected.damaged);
			const previous = Object.values(current);
			const retainedScopes = new Set();
			const next = {};
			for (const record of incoming) {
				const exact = previous.find((candidate) => candidate.key === record.key && candidate.raw === record.raw);
				if (exact) retainedScopes.add(exact.scope);
				next[record.scope] = exact ? {
					...record,
					inspected: exact.inspected,
					reason: record.reason || exact.reason,
					storageMissing: false
				} : record;
			}
			for (const record of previous) {
				if (retainedScopes.has(record.scope)) continue;
				// A disappearing or replaced unreadable envelope is still evidence of an unknown write.
				// Keep that exact snapshot quarantined in this tab without touching any newer stored record.
				let scope = record.scope;
				while (Object.hasOwn(next, scope)) scope += ":retained";
				next[scope] = {
					...record,
					scope,
					storageMissing: true,
					inspected: false
				};
			}
			return next;
		});
		setUncertainSaves((current) => {
			const next = Object.fromEntries(Object.entries(inspected.valid).map(([scope, record]) => {
				const previous = current[scope];
				return [scope, previous?.operationId === record.operationId && previous?.storageRaw === record.storageRaw ? {
					...record,
					phase: previous.phase,
					inspectedMissing: previous.inspectedMissing,
					releaseAllowed: previous.releaseAllowed,
					releaseInspected: previous.releaseInspected,
					message: previous.message
				} : {
					...record,
					phase: "unknown",
					inspectedMissing: false,
					releaseAllowed: false,
					releaseInspected: false,
					message: `A saved operation for ${record.name} may still be pending. Check its exact durable receipt.`
				}];
			}));
			const damagedSnapshots = new Set(Object.values(inspected.damaged).map((record) => `${record.key}\u0000${record.raw}`));
			for (const [scope, previous] of Object.entries(current)) {
				if (next[scope] || damagedSnapshots.has(`${previous.storageKey}\u0000${previous.storageRaw}`)) continue;
				next[scope] = {
					...previous,
					phase: "storage-missing",
					inspectedMissing: false,
					releaseAllowed: false,
					releaseInspected: false,
					message: `The browser recovery record for ${previous.name} disappeared or changed before a terminal receipt was proved. This tab keeps the exact draft quarantined; no new save will be sent.`
				};
			}
			uncertainSavesRef.current = next;
			return next;
		});
	};
	const releaseTerminalRecovery = (token) => {
		if (clearAuthoringOperationIntent(token)) {
			removeRecovery(token);
			return true;
		}
		try {
			const storage = authoringStorage();
			if (storage && storage.getItem(token.storageKey) == null) {
				removeRecovery(token);
				return true;
			}
		} catch {}
		refreshRecoveryStore();
		updateRecovery(token, {
			phase: "unknown",
			releaseAllowed: true,
			releaseInspected: false,
			message: `The server settled ${token.name}, but its browser recovery record changed or could not be released. Inspect the exact record before another save.`
		});
		return false;
	};
	const applyAuthoringReceipt = (token, receipt) => {
		if (receipt.desired_revision !== token.desiredRevision || receipt.target_root_id !== token.expectedTargetRootId) {
			const error = new Error("The durable receipt does not match the submitted file contents.");
			error.code = "AUTHORING_PROTOCOL_ERROR";
			throw error;
		}
		if (receipt.status === "prepared") {
			const message = `The exact operation for ${token.name} is durably prepared but not complete. Resume the same save identity.`;
			updateRecovery(token, {
				phase: "prepared",
				inspectedMissing: false,
				releaseAllowed: false,
				releaseInspected: false,
				message
			});
			setDocuments((current) => current[token.scope] ? {
				...current,
				[token.scope]: {
					...current[token.scope],
					error: message
				}
			} : current);
			return "prepared";
		}
		if (receipt.status === "succeeded") {
			const released = releaseTerminalRecovery(token);
			setDocuments((current) => {
				const exact = current[token.scope];
				if (!exact || exact.kind !== token.kind || exact.name !== token.name) return current;
				return {
					...current,
					[token.scope]: {
						...exact,
						savedText: token.submittedText,
						savedRevision: token.desiredRevision,
						observedText: token.submittedText,
						observedRevision: token.desiredRevision,
						savedTargetRootId: token.expectedTargetRootId,
						observedTargetRootId: token.expectedTargetRootId,
						conflict: false,
						rootConflict: false,
						observationIncomplete: false,
						error: "",
						recoveryOperationId: released ? null : token.operationId,
						recoveryStorageRaw: released ? null : token.storageRaw
					}
				};
			});
			onToast?.(`Saved ${token.name}`);
			return "succeeded";
		}
		const message = receipt.code === "authoring_intervening_write" ? `${token.name} changed after this exact save was prepared. Your draft is retained; inspect the current server copy before saving again.` : `${token.name} changed before this save began. Your retained draft was not written.`;
		const released = releaseTerminalRecovery(token);
		setDocuments((current) => {
			const exact = current[token.scope];
			const retained = exact || {
				kind: token.kind,
				name: token.name,
				savedText: null,
				draftText: token.submittedText,
				savedRevision: token.expectedRevision,
				savedTargetRootId: token.expectedTargetRootId,
				observedTargetRootId: token.expectedTargetRootId,
				truncated: false,
				rootConflict: false
			};
			return {
				...current,
				[token.scope]: {
					...retained,
					observedText: null,
					observedRevision: receipt.result_revision,
					observedTargetRootId: token.expectedTargetRootId,
					conflict: true,
					rootConflict: false,
					observationIncomplete: false,
					error: message,
					recoveryOperationId: released ? null : token.operationId,
					recoveryStorageRaw: released ? null : token.storageRaw
				}
			};
		});
		retry();
		onToast?.(message);
		return "conflict";
	};
	const submitAuthoringSave = async (token) => {
		if (saveRef.current) return;
		saveRef.current = token;
		setSaveState(token);
		updateRecovery(token, {
			phase: "submitting",
			inspectedMissing: false,
			releaseAllowed: false,
			releaseInspected: false,
			message: `Submitting the exact saved operation for ${token.name}…`
		});
		setDocuments((current) => current[token.scope] ? {
			...current,
			[token.scope]: {
				...current[token.scope],
				error: "",
				recoveryOperationId: token.operationId,
				recoveryStorageRaw: token.storageRaw
			}
		} : current);
		const timed = deadlineRequest((signal) => putAuthoringOperation(token.kind, token.name, token.operationId, {
			text: token.submittedText,
			expectedRevision: token.expectedRevision,
			expectedTargetRootId: token.expectedTargetRootId,
			desiredRevision: token.desiredRevision
		}, { signal }), AUTHORING_SAVE_TIMEOUT_MS);
		try {
			const receipt = await timed.promise;
			if (saveRef.current !== token || !activeRef.current) return;
			applyAuthoringReceipt(token, receipt);
		} catch (error) {
			if (saveRef.current !== token || !activeRef.current) return;
			const message = `Save outcome for ${token.name} is not confirmed. Its exact operation and draft remain durable in this tab; check the receipt before retrying.`;
			updateRecovery(token, {
				phase: "unknown",
				inspectedMissing: false,
				// A failed client request cannot prove that the exact PUT is no longer waiting on the
				// server-side lock or fsync. Keep it quarantined until a validated terminal receipt exists.
				releaseAllowed: false,
				releaseInspected: false,
				message
			});
			setDocuments((current) => current[token.scope] ? {
				...current,
				[token.scope]: {
					...current[token.scope],
					error: message
				}
			} : current);
			onToast?.(message);
		} finally {
			if (saveRef.current === token) {
				saveRef.current = null;
				if (activeRef.current) setSaveState(null);
			}
		}
	};
	const saveSelected = async () => {
		const document = selectedScope ? documents[selectedScope] : null;
		if (!document || document.draftText === document.savedText || saveRef.current || selectedUncertainSave || selectedDamagedRecovery) return;
		if (!selectedSourceReconciled) {
			onToast?.(`Load and reconcile the current ${kind} source before saving ${document.name}.`);
			return;
		}
		if (document.rootConflict || !validAuthoringTargetRootId(document.observedTargetRootId)) {
			const message = `${document.name} belongs to a different or unavailable Authoring directory. Its retained draft was not written; refresh the configured directory before saving.`;
			setDocuments((current) => current[selectedScope] ? {
				...current,
				[selectedScope]: {
					...current[selectedScope],
					error: message
				}
			} : current);
			onToast?.(message);
			return;
		}
		if (document.truncated || !AUTHORING_REVISION_RE.test(document.observedRevision || "")) {
			const message = `${document.name} has no complete writable revision. Refresh or edit the file outside this truncated view.`;
			setDocuments((current) => current[selectedScope] ? {
				...current,
				[selectedScope]: {
					...current[selectedScope],
					error: message
				}
			} : current);
			onToast?.(message);
			return;
		}
		if (document.conflict && !window.confirm(`${document.name} changed on the server while this draft was open. Save this retained draft over the newer server copy?`)) return;
		try {
			const desiredRevision = await authoringTextRevision(document.draftText);
			if (!activeRef.current || saveRef.current) return;
			const latestDocument = documentsRef.current[selectedScope];
			if (!latestDocument || latestDocument.draftText !== document.draftText || latestDocument.savedRevision !== document.savedRevision || latestDocument.observedRevision !== document.observedRevision || latestDocument.savedTargetRootId !== document.savedTargetRootId || latestDocument.observedTargetRootId !== document.observedTargetRootId || latestDocument.conflict !== document.conflict || latestDocument.rootConflict) {
				const message = `${document.name} changed while the save identity was being prepared. Review the retained draft and current server copy; no request was sent.`;
				setDocuments((current) => current[selectedScope] ? {
					...current,
					[selectedScope]: {
						...current[selectedScope],
						error: message
					}
				} : current);
				onToast?.(message);
				return;
			}
			const intent = {
				schema: AUTHORING_OPERATION_SCHEMA,
				operationId: createIdempotencyKey(),
				kind: latestDocument.kind,
				name: latestDocument.name,
				submittedText: latestDocument.draftText,
				expectedRevision: latestDocument.conflict ? latestDocument.observedRevision : latestDocument.savedRevision,
				expectedTargetRootId: latestDocument.observedTargetRootId,
				desiredRevision,
				updatedAt: Date.now()
			};
			const stored = saveAuthoringOperationIntent(intent);
			if (!stored) {
				const message = `Save was not sent because the exact operation for ${document.name} could not be retained in browser recovery storage.`;
				setDocuments((current) => current[selectedScope] ? {
					...current,
					[selectedScope]: {
						...current[selectedScope],
						error: message
					}
				} : current);
				refreshRecoveryStore();
				onToast?.(message);
				return;
			}
			const recovery = {
				...stored,
				phase: "submitting",
				inspectedMissing: false,
				releaseAllowed: false,
				releaseInspected: false,
				message: `Submitting the exact saved operation for ${stored.name}…`
			};
			setUncertainSaves((current) => ({
				...current,
				[stored.scope]: recovery
			}));
			await submitAuthoringSave(recovery);
		} catch (error) {
			const message = `Could not prepare ${document.name} for saving: ${error?.message || error}`;
			setDocuments((current) => current[selectedScope] ? {
				...current,
				[selectedScope]: {
					...current[selectedScope],
					error: message
				}
			} : current);
			onToast?.(message);
		}
	};
	const reconcileSave = async (recovery) => {
		const exactRecovery = uncertainSaves[recovery?.scope];
		if (!recovery || saveRef.current || !exactRecovery || exactRecovery.operationId !== recovery.operationId || exactRecovery.storageRaw !== recovery.storageRaw) return;
		const verifiedRecovery = await verifyAuthoringRecovery(exactRecovery);
		if (!verifiedRecovery || saveRef.current || !activeRef.current) return;
		recovery = verifiedRecovery;
		const token = {
			...recovery,
			reconcile: true
		};
		saveRef.current = token;
		setSaveState(token);
		const request = deadlineRequest((signal) => getAuthoringOperation(recovery.kind, recovery.name, recovery.operationId, {
			signal,
			expectedRevision: recovery.expectedRevision,
			expectedTargetRootId: recovery.expectedTargetRootId,
			desiredRevision: recovery.desiredRevision
		}), PANEL_REQUEST_TIMEOUT_MS);
		try {
			const receipt = await request.promise;
			if (saveRef.current !== token || !activeRef.current) return;
			applyAuthoringReceipt(recovery, receipt);
		} catch (error) {
			if (saveRef.current === token && activeRef.current) {
				const missing = Number(error?.status) === 404 && error?.code === "authoring_operation_not_found";
				const message = missing ? `No durable receipt exists yet for ${recovery.name}. Resume the same operation identity; it cannot overwrite a newer revision.` : `Could not check ${recovery.name}: ${error?.message || error}. Its exact draft and operation identity remain retained.`;
				updateRecovery(recovery, {
					phase: missing ? "missing" : "unknown",
					inspectedMissing: missing,
					// Even a 404 can race a still-running timed-out PUT before its prepared receipt is
					// published. Reuse/check the same identity; never release it from an unknown response.
					releaseAllowed: false,
					releaseInspected: false,
					message
				});
				setDocuments((current) => current[recovery.scope] ? {
					...current,
					[recovery.scope]: {
						...current[recovery.scope],
						error: message
					}
				} : current);
				onToast?.(message);
			}
		} finally {
			if (saveRef.current === token) {
				saveRef.current = null;
				if (activeRef.current) setSaveState(null);
			}
		}
	};
	const retryExactSave = async (recovery) => {
		const exact = uncertainSaves[recovery?.scope];
		if (!exact || saveRef.current || exact.operationId !== recovery.operationId || exact.storageRaw !== recovery.storageRaw || !["prepared", "missing"].includes(exact.phase)) return;
		const verifiedRecovery = await verifyAuthoringRecovery(exact);
		if (!verifiedRecovery || saveRef.current || !activeRef.current || !["prepared", "missing"].includes(verifiedRecovery.phase)) return;
		if (!window.confirm(`Resume the exact retained save for ${recovery.name}?\n\nThis reuses the same durable operation identity, expected revision, and file contents. It cannot overwrite an intervening newer version.`)) return;
		submitAuthoringSave(verifiedRecovery);
	};
	const adoptObservedServerCopy = (document) => {
		if (!selectedSourceReconciled || !document?.conflict || document.truncated || document.observedText == null || !/^sha256:[0-9a-f]{64}$/.test(document.observedRevision || "") || saveRef.current) return;
		if (!window.confirm(`Use the current server copy of ${document.name}?\n\nThis discards the retained local draft for that file.`)) return;
		const scope = scopeFor(document.kind, document.name);
		setDocuments((current) => current[scope] ? {
			...current,
			[scope]: {
				...current[scope],
				savedText: document.observedText,
				draftText: document.observedText,
				savedRevision: document.observedRevision,
				savedTargetRootId: document.observedTargetRootId,
				observedTargetRootId: document.observedTargetRootId,
				conflict: false,
				rootConflict: false,
				observationIncomplete: false,
				error: ""
			}
		} : current);
		onToast?.(`Using the current server copy of ${document.name}.`);
	};
	const releaseAuthoringRecovery = (recovery) => {
		const exact = uncertainSaves[recovery?.scope];
		if (!exact || !exact.releaseAllowed || !exact.releaseInspected || exact.operationId !== recovery.operationId) return;
		if (!window.confirm(`Release the exact saved operation for ${recovery.name}?\n\nThis sends no write. The retained draft stays open here, but closing the panel will discard it.`)) return;
		if (!clearAuthoringOperationIntent(exact)) {
			updateRecovery(exact, {
				releaseAllowed: false,
				releaseInspected: false,
				message: "The browser recovery record changed or could not be released. It remains protected."
			});
			refreshRecoveryStore();
			onToast?.("The exact Authoring recovery record changed or could not be released.");
			return;
		}
		removeRecovery(exact);
		setDocuments((current) => current[exact.scope] ? {
			...current,
			[exact.scope]: {
				...current[exact.scope],
				recoveryOperationId: null,
				recoveryStorageRaw: null,
				error: "The old operation recovery was released. Review the retained draft and current server version before saving again."
			}
		} : current);
		onToast?.("The exact Authoring recovery identity was released. No save was sent.");
	};
	const releaseDamagedRecovery = (recovery) => {
		if (!recovery?.inspected) return;
		if (!window.confirm("Release this exact unreadable Authoring recovery record?\n\nOnly continue after confirming that no retained save operation still needs recovery.")) return;
		const storedRecordReleased = clearDamagedAuthoringOperation(recovery);
		if (!storedRecordReleased) {
			let exactSnapshotIsGone = false;
			if (recovery.storageMissing) {
				const storage = authoringStorage();
				try {
					exactSnapshotIsGone = !!storage && storage.getItem(recovery.key) !== recovery.raw;
				} catch {
					setStorageAvailable(false);
				}
			}
			if (!exactSnapshotIsGone) {
				onToast?.("The recovery record changed or could not be released. It remains protected.");
				refreshRecoveryStore();
				return;
			}
		}
		setDamagedRecoveries((current) => {
			const exact = current[recovery.scope];
			if (!exact || exact.raw !== recovery.raw || exact.key !== recovery.key) return current;
			const next = { ...current };
			delete next[recovery.scope];
			return next;
		});
		const quarantined = authoringDigestQuarantine.get(recovery.key);
		if (quarantined?.raw === recovery.raw) authoringDigestQuarantine.delete(recovery.key);
		if (recovery.identity?.scope) {
			setDocuments((current) => current[recovery.identity.scope] ? {
				...current,
				[recovery.identity.scope]: {
					...current[recovery.identity.scope],
					recoveryOperationId: null,
					recoveryStorageRaw: null,
					error: "The old recovery record was released. Review the retained draft and current server version before saving again."
				}
			} : current);
		}
		if (!storedRecordReleased) refreshRecoveryStore();
		onToast?.(storedRecordReleased ? "The exact damaged Authoring recovery record was released. No save was sent." : "The retained snapshot of the missing recovery record was released. No stored record was changed.");
	};
	const requestClose = () => {
		if (navigationGuardOwner === "run") {
			if (onClose?.() !== false) allowNavigationRef.current = true;
			return;
		}
		if (!navigationUnsafe) {
			draftStore.clear(AUTHORING_PANEL_DRAFT_SCOPE);
			onClose?.();
			return;
		}
		const warning = uncertainSaveCount > 0 ? `${uncertainSaveCount} file save outcome${uncertainSaveCount === 1 ? "" : "s"} may still be unknown, and the exact draft${uncertainSaveCount === 1 ? " is" : "s are"} retained here.` : damagedRecoveryCount > 0 ? `${damagedRecoveryCount} damaged Authoring recovery record${damagedRecoveryCount === 1 ? "" : "s"} remain quarantined in this tab.` : mutationBusy ? "A file save is still in progress." : `${dirtyCount} unsaved draft${dirtyCount === 1 ? "" : "s"} will be lost.`;
		if (!window.confirm(`${warning} Close Authoring anyway?`)) return;
		allowNavigationRef.current = true;
		draftStore.clear(AUTHORING_PANEL_DRAFT_SCOPE);
		onClose?.();
	};
	const dirtyByKind = (documentKind) => Object.values(documents).filter((document) => document.kind === documentKind && document.draftText !== document.savedText).length;
	const fileRows = [...data.files || []];
	for (const recovery of Object.values(uncertainSaves)) {
		if (recovery.kind === kind && !fileRows.some((file) => file.name === recovery.name)) {
			fileRows.push({
				name: recovery.name,
				text: recovery.submittedText,
				revision: null,
				truncated: false,
				recovered: true
			});
		}
	}
	for (const document of Object.values(documents)) {
		if (document.kind === kind && (document.draftText !== document.savedText || document.recoveryOperationId || scopeFor(document.kind, document.name) === selectedScope) && !fileRows.some((file) => file.name === document.name)) {
			fileRows.push({
				name: document.name,
				text: document.draftText,
				revision: document.observedRevision || null,
				truncated: document.truncated === true,
				recovered: true
			});
		}
	}
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Authoring — configure the scientist",
		sub: "hot-reloaded next run",
		onClose: requestClose,
		wide: true,
		children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "toolbar",
				style: { marginBottom: 10 },
				children: [
					[
						"prompts",
						"skills",
						"knowledge"
					].map((k) => /* @__PURE__ */ _jsxs("button", {
						className: "btn sm" + (k === kind ? " primary" : ""),
						onClick: () => chooseKind(k),
						children: [k, dirtyByKind(k) ? ` (${dirtyByKind(k)} unsaved)` : ""]
					}, k)),
					source.state === "ready" && /* @__PURE__ */ _jsx("span", {
						className: "muted",
						children: data.dir || `no ${kind} dir configured (set LOOPLAB_${kind.toUpperCase()}_DIR)`
					}),
					source.state === "ready" && data.truncatedFiles > 0 && /* @__PURE__ */ _jsxs("span", {
						className: "muted",
						children: [
							data.truncatedFiles,
							" more file",
							data.truncatedFiles === 1 ? "" : "s",
							" omitted"
						]
					})
				]
			}),
			/* @__PURE__ */ _jsx(PanelResourceNotice, {
				resource: source,
				label: `${kind} files`,
				onRetry: retry
			}),
			dirtyCount > 0 && /* @__PURE__ */ _jsxs("div", {
				className: "notice",
				role: "status",
				style: { marginBottom: 10 },
				children: [
					dirtyCount,
					" unsaved draft",
					dirtyCount === 1 ? "" : "s",
					" retained in this panel. Switching files or sections is safe; closing is not."
				]
			}),
			selected && !selectedSourceReconciled && /* @__PURE__ */ _jsxs("div", {
				className: "notice",
				role: "status",
				style: { marginBottom: 10 },
				children: [
					"Saving and server-copy actions stay disabled until the current ",
					kind,
					" source is loaded and reconciled with this retained draft."
				]
			}),
			!storageAvailable && /* @__PURE__ */ _jsxs("div", {
				className: "report-inline-state error",
				role: "alert",
				style: { marginBottom: 10 },
				children: [/* @__PURE__ */ _jsx(OpIcon, {
					name: "alert",
					size: 14
				}), /* @__PURE__ */ _jsx("span", { children: "Browser recovery storage is unavailable. Authoring saves stay disabled so an ambiguous write cannot lose its exact identity." })]
			}),
			damagedRows.map((recovery) => /* @__PURE__ */ _jsxs("div", {
				className: "report-inline-state error",
				role: "alert",
				style: { marginBottom: 10 },
				children: [
					/* @__PURE__ */ _jsx(OpIcon, {
						name: "alert",
						size: 14
					}),
					/* @__PURE__ */ _jsx("span", { children: recovery.storageMissing ? `A previously seen unreadable Authoring recovery record${recovery.identity ? ` for ${recovery.identity.name}` : ""} disappeared or was replaced in browser storage. Its exact snapshot remains quarantined in this tab.` : recovery.reason || `An unreadable Authoring recovery record exists${recovery.identity ? ` for ${recovery.identity.name}` : ""}. Saves for that scope remain locked.` }),
					!recovery.inspected ? /* @__PURE__ */ _jsx("button", {
						className: "btn sm",
						onClick: () => setDamagedRecoveries((current) => ({
							...current,
							[recovery.scope]: {
								...current[recovery.scope],
								inspected: true
							}
						})),
						children: "Inspect recovery"
					}) : /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsxs("span", {
						className: "muted",
						children: [
							"Stored record ",
							recovery.raw.length,
							" bytes; ",
							recovery.reason ? "its retained contents fail the integrity check." : "its operation identity cannot be verified."
						]
					}), /* @__PURE__ */ _jsx("button", {
						className: "btn sm danger",
						onClick: () => releaseDamagedRecovery(recovery),
						children: "Release exact record"
					})] })
				]
			}, recovery.scope)),
			Object.values(uncertainSaves).map((recovery) => /* @__PURE__ */ _jsxs("div", {
				className: "report-inline-state error",
				role: "alert",
				style: { marginBottom: 10 },
				children: [
					/* @__PURE__ */ _jsx(OpIcon, {
						name: "alert",
						size: 14
					}),
					/* @__PURE__ */ _jsx("span", { children: recovery.message }),
					/* @__PURE__ */ _jsx("button", {
						className: "btn sm",
						disabled: mutationBusy,
						onClick: () => reconcileSave(recovery),
						children: saveState?.reconcile && saveState.scope === recovery.scope ? "Checking…" : "Check exact operation"
					}),
					["prepared", "missing"].includes(recovery.phase) && /* @__PURE__ */ _jsx("button", {
						className: "btn sm",
						disabled: mutationBusy,
						onClick: () => retryExactSave(recovery),
						children: "Resume exact save"
					}),
					recovery.releaseAllowed && !recovery.releaseInspected && /* @__PURE__ */ _jsx("button", {
						className: "btn sm",
						disabled: mutationBusy,
						onClick: () => updateRecovery(recovery, { releaseInspected: true }),
						children: "Inspect recovery"
					}),
					recovery.releaseAllowed && recovery.releaseInspected && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsxs("span", {
						className: "muted",
						children: [
							"Operation ",
							recovery.operationId,
							"; expected revision ",
							recovery.expectedRevision.slice(0, 18),
							"…."
						]
					}), /* @__PURE__ */ _jsx("button", {
						className: "btn sm danger",
						disabled: mutationBusy,
						onClick: () => releaseAuthoringRecovery(recovery),
						children: "Release exact recovery"
					})] })
				]
			}, recovery.scope)),
			/* @__PURE__ */ _jsxs("div", {
				className: "authoring-layout",
				children: [/* @__PURE__ */ _jsxs("div", {
					className: "authoring-list",
					children: [fileRows.map((f) => /* @__PURE__ */ _jsxs("button", {
						type: "button",
						className: "run-card authoring-file" + (selectedScope === scopeFor(kind, f.name) ? " sel" : ""),
						onClick: () => chooseFile(f),
						children: [
							f.name,
							documents[scopeFor(kind, f.name)]?.draftText !== documents[scopeFor(kind, f.name)]?.savedText ? " • unsaved" : "",
							uncertainSaves[scopeFor(kind, f.name)] ? " • recovery" : ""
						]
					}, f.name)), source.state === "ready" && fileRows.length === 0 && /* @__PURE__ */ _jsx("div", {
						className: "muted",
						children: "no files"
					})]
				}), /* @__PURE__ */ _jsx("div", {
					className: "authoring-editor",
					children: selected ? /* @__PURE__ */ _jsxs(_Fragment, { children: [
						/* @__PURE__ */ _jsx("textarea", {
							className: "text",
							"aria-label": `Edit ${selected.name}`,
							value: selected.draftText,
							disabled: selected.truncated,
							onChange: (e) => editSelected(e.target.value)
						}),
						selected.truncated && /* @__PURE__ */ _jsxs("div", {
							className: "report-inline-state error",
							role: "alert",
							children: [/* @__PURE__ */ _jsx(OpIcon, {
								name: "alert",
								size: 14
							}), /* @__PURE__ */ _jsx("span", { children: "This file is larger than the safe editor limit. Only a prefix is shown, so saving is disabled." })]
						}),
						selected.conflict && /* @__PURE__ */ _jsxs("div", {
							className: "report-inline-state error",
							role: "alert",
							children: [
								/* @__PURE__ */ _jsx(OpIcon, {
									name: "alert",
									size: 14
								}),
								/* @__PURE__ */ _jsx("span", { children: selected.rootConflict ? "The configured Authoring directory changed while this draft was retained. The draft stays bound to its original directory and cannot be written into the new one." : selected.observationIncomplete ? "The server returned an incomplete file list, so this file's current version could not be verified. Saving stays disabled until a complete refresh can prove its revision." : "The server copy changed while this draft was retained. Your text was not replaced." }),
								!selected.truncated && selected.observedText != null && /^sha256:[0-9a-f]{64}$/.test(selected.observedRevision || "") && !selectedUncertainSave && selectedSourceReconciled && /* @__PURE__ */ _jsx("button", {
									className: "btn sm",
									onClick: () => adoptObservedServerCopy(selected),
									children: "Use server copy"
								})
							]
						}),
						selected.error && /* @__PURE__ */ _jsxs("div", {
							className: "report-inline-state error",
							role: "alert",
							children: [/* @__PURE__ */ _jsx(OpIcon, {
								name: "alert",
								size: 14
							}), /* @__PURE__ */ _jsx("span", { children: selected.error })]
						}),
						/* @__PURE__ */ _jsx("button", {
							className: "btn sm primary",
							style: { marginTop: 8 },
							onClick: saveSelected,
							disabled: mutationBusy || !storageAvailable || !!selectedUncertainSave || !!selectedDamagedRecovery || selected.truncated || !selectedSourceReconciled || selected.rootConflict || !validAuthoringTargetRootId(selected.observedTargetRootId) || !AUTHORING_REVISION_RE.test(selected.observedRevision || "") || selected.draftText === selected.savedText,
							children: saveState && !saveState.reconcile ? `Saving ${saveState.name}…` : "Save"
						})
					] }) : source.state === "ready" && /* @__PURE__ */ _jsx("div", {
						className: "muted",
						children: "select a file to edit"
					})
				})]
			})
		]
	});
}
function MemoryCompletenessNotice({ resource, onRetry, error = false, children }) {
	const action = error ? "Retry" : "Refresh";
	return /* @__PURE__ */ _jsxs("div", {
		className: "report-inline-state" + (error ? " error" : ""),
		role: error ? "alert" : "status",
		children: [
			/* @__PURE__ */ _jsx(OpIcon, {
				name: "alert",
				size: 14
			}),
			/* @__PURE__ */ _jsx("span", { children }),
			/* @__PURE__ */ _jsx("button", {
				type: "button",
				className: "btn sm",
				disabled: !!resource.pending,
				onClick: onRetry,
				children: resource.pending ? `${action}ing…` : action
			})
		]
	});
}
function KbNote({ note }) {
	const [open, setOpen] = useState(false);
	return /* @__PURE__ */ _jsxs("div", {
		className: "mem-card",
		children: [/* @__PURE__ */ _jsxs("button", {
			type: "button",
			className: "memory-note-toggle disclosure-button",
			"aria-expanded": open,
			onClick: () => setOpen((o) => !o),
			children: [
				/* @__PURE__ */ _jsx("span", {
					style: {
						opacity: .6,
						fontSize: 10,
						marginRight: 4
					},
					children: open ? "▾" : "▸"
				}),
				note.name,
				note.truncated && /* @__PURE__ */ _jsx("span", {
					className: "muted",
					style: {
						marginLeft: 6,
						fontSize: 10
					},
					children: "· prefix only"
				})
			]
		}), open && /* @__PURE__ */ _jsxs("div", {
			style: { marginTop: 6 },
			children: [note.truncated && /* @__PURE__ */ _jsxs("div", {
				className: "report-inline-state",
				role: "status",
				style: { margin: "0 0 8px" },
				children: [/* @__PURE__ */ _jsx(OpIcon, {
					name: "alert",
					size: 14
				}), /* @__PURE__ */ _jsxs("span", { children: [
					"Only the first ",
					AUTHORING_MAX_BYTES / 1024,
					" KiB of this note was returned; the rest is not shown."
				] })]
			}), /* @__PURE__ */ _jsx(Markdown, { text: note.text || note.content || "" })]
		})]
	});
}
export function MemoryPanel({ onClose }) {
	// Everything the run has LEARNED, in one place: distilled lessons, solved-task cases, meta-notes, and
	// the agentic knowledge-base markdown notes (best configs / recipes the agents save + later retrieve).
	const [memory, retryMemory] = usePanelResource((signal) => get("/api/memory", { signal }), memoryPayload);
	const [knowledge, retryKnowledge] = usePanelResource((signal) => get("/api/knowledge", { signal }), authoringPayload);
	const mem = memory.data || {
		dir: null,
		cases: [],
		lessons: [],
		notes: [],
		projection: null,
		page: null
	};
	const kb = knowledge.data || {
		dir: null,
		files: [],
		truncatedFiles: 0
	};
	const [tab, setTab] = useState("lessons");
	const [lessonRole, setLessonRole] = useState("all");
	const kbFiles = kb.files || [];
	const truncatedKnowledgePreviews = kbFiles.filter((file) => file.truncated).length;
	const knowledgeIncomplete = kb.truncatedFiles > 0 || truncatedKnowledgePreviews > 0;
	const tabs = [
		[
			"lessons",
			"Lessons",
			mem.lessons?.length,
			memoryTierIncomplete(mem.page?.tiers?.lessons)
		],
		[
			"cases",
			"Cases",
			mem.cases?.length,
			memoryTierIncomplete(mem.page?.tiers?.cases)
		],
		[
			"notes",
			"Notes",
			mem.notes?.length,
			memoryTierIncomplete(mem.page?.tiers?.notes)
		],
		[
			"knowledge",
			"Knowledge",
			kbFiles.length,
			knowledgeIncomplete
		]
	];
	const selectedResource = tab === "knowledge" ? knowledge : memory;
	const selectedReceipt = tab === "knowledge" ? null : mem.page?.tiers?.[tab];
	const selectedReceiptIncomplete = memoryTierIncomplete(selectedReceipt);
	const selectedTierLabel = tab === "lessons" ? "Lessons" : tab === "cases" ? "Cases" : "Meta-notes";
	const selectedReceiptIssues = [];
	if (selectedReceipt?.unavailable) selectedReceiptIssues.push("This tier could not be read.");
	if (selectedReceipt?.sourceWindowTruncated) {
		selectedReceiptIssues.push(selectedReceipt.returned > 0 ? `Showing the newest ${selectedReceipt.returned} ${selectedReceipt.returned === 1 ? "item" : "items"} from a bounded window; older entries were omitted.` : "Only a bounded recent source window was checked; older entries were omitted.");
	}
	if (selectedReceipt?.skipped > 0) {
		selectedReceiptIssues.push(`${selectedReceipt.skipped} source ${selectedReceipt.skipped === 1 ? "row was" : "rows were"} not shown.`);
	}
	const memoryEmptyCopy = (receipt, noun, completeCopy) => {
		if (mem.dir == null) return "No cross-run memory directory is configured.";
		if (receipt?.unavailable) return `No ${noun} can be shown because this memory tier is unavailable.`;
		if (memoryTierIncomplete(receipt)) return `No ${noun} are visible in the loaded recent subset.`;
		return completeCopy;
	};
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Memory & knowledge — what the runs have learned",
		sub: memory.data ? mem.dir || "no memory dir" : "",
		onClose,
		wide: true,
		children: [
			/* @__PURE__ */ _jsx("div", {
				className: "conv-toggle memory-tabs",
				style: { marginBottom: 12 },
				children: tabs.map(([k, label, n, incomplete]) => /* @__PURE__ */ _jsxs("button", {
					"aria-pressed": tab === k,
					className: "seg" + (tab === k ? " on" : ""),
					onClick: () => setTab(k),
					children: [
						label,
						" ",
						/* @__PURE__ */ _jsx("span", {
							className: "muted",
							children: (k === "knowledge" ? knowledge : memory).data ? `${n}${incomplete ? " shown" : ""}` : "…"
						})
					]
				}, k))
			}),
			/* @__PURE__ */ _jsx(PanelResourceNotice, {
				resource: selectedResource,
				label: tab === "knowledge" ? "Knowledge notes" : "Cross-run memory",
				onRetry: tab === "knowledge" ? retryKnowledge : retryMemory
			}),
			tab !== "knowledge" && memory.state === "ready" && selectedReceiptIncomplete && /* @__PURE__ */ _jsxs(MemoryCompletenessNotice, {
				resource: memory,
				onRetry: retryMemory,
				error: selectedReceipt.unavailable,
				children: [
					/* @__PURE__ */ _jsxs("b", { children: [selectedTierLabel, " data is incomplete."] }),
					" ",
					selectedReceiptIssues.join(" ")
				]
			}),
			tab === "knowledge" && knowledge.state === "ready" && knowledgeIncomplete && /* @__PURE__ */ _jsxs(MemoryCompletenessNotice, {
				resource: knowledge,
				onRetry: retryKnowledge,
				children: [
					/* @__PURE__ */ _jsx("b", { children: "Knowledge notes are incomplete." }),
					" ",
					kb.truncatedFiles > 0 && `${kb.truncatedFiles} Markdown ${kb.truncatedFiles === 1 ? "entry was" : "entries were"} not returned. `,
					truncatedKnowledgePreviews > 0 && `${truncatedKnowledgePreviews} loaded ${truncatedKnowledgePreviews === 1 ? "preview contains" : "previews contain"} only a prefix.`
				]
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "muted",
				style: {
					fontSize: 11,
					marginBottom: 10,
					lineHeight: 1.5
				},
				children: [
					"Cross-run memory reused to guide future runs. Cases, notes and the knowledge base are shared;",
					" ",
					/* @__PURE__ */ _jsx("b", { children: "lessons are split by role" }),
					"."
				]
			}),
			tab === "lessons" && /* @__PURE__ */ _jsxs("div", {
				className: "muted",
				style: {
					fontSize: 11,
					marginBottom: 10,
					lineHeight: 1.5
				},
				children: [
					"The ",
					/* @__PURE__ */ _jsx("b", { children: "Researcher" }),
					" gets R&D / “what technique to try” lessons; the ",
					/* @__PURE__ */ _jsx("b", { children: "Developer" }),
					" gets only its own “what code change fixed a crash” lessons (untagged/legacy lessons are shared)."
				]
			}),
			tab === "lessons" && /* @__PURE__ */ _jsx("div", {
				className: "conv-toggle memory-role-tabs",
				style: { marginBottom: 8 },
				children: [
					["all", "All"],
					["researcher", "Researcher"],
					["developer", "Developer"]
				].map(([r, label]) => /* @__PURE__ */ _jsx("button", {
					"aria-pressed": lessonRole === r,
					className: "seg" + (lessonRole === r ? " on" : ""),
					onClick: () => setLessonRole(r),
					children: label
				}, r))
			}),
			tab === "lessons" && (() => {
				// Researcher/Developer filters ALSO include untagged (shared) lessons — mirrors the backend
				// routing where an untagged lesson reaches both roles.
				const shown = (mem.lessons || []).filter((l) => lessonRole === "all" || !l.role || l.role === lessonRole);
				return shown.length ? shown.map((l, i) => /* @__PURE__ */ _jsxs("div", {
					className: "mem-card",
					children: [/* @__PURE__ */ _jsx("div", { children: l.statement }), /* @__PURE__ */ _jsxs("div", {
						className: "mem-meta",
						style: {
							marginTop: 4,
							display: "flex",
							gap: 6,
							alignItems: "center",
							flexWrap: "wrap"
						},
						children: [
							/* @__PURE__ */ _jsx("span", {
								className: "chip xs",
								children: l.role || "shared"
							}),
							l.kind && /* @__PURE__ */ _jsx("span", {
								className: "chip xs",
								children: l.kind
							}),
							l.outcome && /* @__PURE__ */ _jsx("span", {
								className: "chip xs",
								children: l.outcome
							}),
							l.delta != null && /* @__PURE__ */ _jsxs("span", {
								className: "chip xs" + (l.delta > 0 ? " ok" : ""),
								children: ["Δ", fmt(l.delta)]
							}),
							l.confidence != null && /* @__PURE__ */ _jsxs("span", {
								className: "muted",
								style: { fontSize: 11 },
								children: [
									"conf ",
									Math.round(l.confidence * 100),
									"%"
								]
							}),
							l.evidence_count ? /* @__PURE__ */ _jsxs("span", {
								className: "muted",
								style: { fontSize: 11 },
								children: [
									"· ",
									l.evidence_count,
									" evidence"
								]
							}) : null,
							l.task_id && /* @__PURE__ */ _jsxs("span", {
								className: "muted",
								style: { fontSize: 11 },
								children: ["· ", l.task_id]
							})
						]
					})]
				}, i)) : memory.state === "ready" && /* @__PURE__ */ _jsx("div", {
					className: "muted",
					children: memoryEmptyCopy(mem.page?.tiers?.lessons, `${lessonRole === "all" ? "" : lessonRole + " "}lessons`, lessonRole !== "all" && mem.lessons.length > 0 ? `No ${lessonRole} lessons match the loaded lessons.` : `No ${lessonRole === "all" ? "" : lessonRole + " "}lessons yet — they accrue as runs finish (reflection distils them into memory).`)
				});
			})(),
			tab === "cases" && ((mem.cases || []).length ? /* @__PURE__ */ _jsx(DataTable, {
				caption: "Stored memory cases",
				card: false,
				children: /* @__PURE__ */ _jsxs("table", {
					className: "tbl",
					children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("th", { children: "task" }),
						/* @__PURE__ */ _jsx("th", { children: "goal" }),
						/* @__PURE__ */ _jsx("th", { children: "metric" }),
						/* @__PURE__ */ _jsx("th", { children: "params" })
					] }) }), /* @__PURE__ */ _jsx("tbody", { children: mem.cases.map((c, i) => /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("td", { children: c.task_id }),
						/* @__PURE__ */ _jsx("td", {
							className: "muted",
							children: c.goal
						}),
						/* @__PURE__ */ _jsx("td", { children: fmt(c.metric) }),
						/* @__PURE__ */ _jsxs("td", {
							className: "muted",
							children: [JSON.stringify(c.params), c.params_truncated && /* @__PURE__ */ _jsx("span", {
								title: "Only a bounded parameter projection was returned",
								"aria-label": "parameters truncated",
								children: " · partial"
							})]
						})
					] }, i)) })]
				})
			}) : memory.state === "ready" && /* @__PURE__ */ _jsx("div", {
				className: "muted",
				children: memoryEmptyCopy(mem.page?.tiers?.cases, "cases", "No cases stored.")
			})),
			tab === "notes" && ((mem.notes || []).length ? mem.notes.map((n, i) => /* @__PURE__ */ _jsxs("div", {
				className: "mem-card",
				children: [n.task_id && /* @__PURE__ */ _jsx("div", {
					className: "muted",
					style: {
						fontSize: 11,
						marginBottom: 2
					},
					children: n.task_id
				}), /* @__PURE__ */ _jsx(Markdown, { text: n.note || n.statement || JSON.stringify(n) })]
			}, i)) : memory.state === "ready" && /* @__PURE__ */ _jsx("div", {
				className: "muted",
				children: memoryEmptyCopy(mem.page?.tiers?.notes, "meta-notes", "No meta-notes yet.")
			})),
			tab === "knowledge" && (kbFiles.length ? /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsxs("div", {
				className: "muted",
				style: {
					fontSize: 11,
					marginBottom: 6
				},
				children: [kb.dir, " — agents save + retrieve these via kb_search"]
			}), kbFiles.map((n, i) => /* @__PURE__ */ _jsx(KbNote, { note: n }, i))] }) : knowledge.state === "ready" && /* @__PURE__ */ _jsx("div", {
				className: "muted",
				children: kb.dir == null ? "No knowledge directory is configured." : knowledgeIncomplete ? "No knowledge notes are visible in the loaded subset." : `No knowledge notes yet (${kb.dir}).`
			}))
		]
	});
}
export function RegistryPanel({ state, onClose }) {
	const [resource, retry] = usePanelResource((signal) => get("/api/runs", { signal }), runsPayload);
	const runs = resource.data || [];
	// Rank through the list view's own comparator instead of a raw descending sort. A raw sort put the
	// BEST run last on a `direction: 'min'` task, and ranked runs of different tasks / objectives /
	// metric units against each other on one unitless axis. `metricComparable` is where that judgement
	// already lives: one task, one direction, or no ranking at all.
	const rankable = metricComparable(runs);
	const rankedRuns = rankable ? sortRuns(runs, "metric", "asc") : runs;
	const champ = state.champion != null ? state.nodes[state.champion] : state.best_node_id != null ? state.nodes[state.best_node_id] : null;
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Solution registry & cross-run",
		onClose,
		wide: true,
		children: [
			/* @__PURE__ */ _jsx("div", {
				className: "section-h",
				children: "Champion (this run)"
			}),
			champ ? /* @__PURE__ */ _jsxs("div", {
				className: "kv",
				children: [
					/* @__PURE__ */ _jsx("div", {
						className: "k",
						children: "node"
					}),
					/* @__PURE__ */ _jsxs("div", {
						className: "v",
						children: [
							"#",
							champ.id,
							" ",
							state.champion != null ? "(promoted)" : "(auto-best)"
						]
					}),
					/* @__PURE__ */ _jsx("div", {
						className: "k",
						children: "metric"
					}),
					/* @__PURE__ */ _jsx("div", {
						className: "v",
						children: fmt(champ.confirmed_mean ?? champ.metric)
					})
				]
			}) : /* @__PURE__ */ _jsx("div", {
				className: "muted",
				children: "no champion yet"
			}),
			/* @__PURE__ */ _jsx("div", {
				className: "toolbar",
				style: { marginTop: 6 },
				children: /* @__PURE__ */ _jsxs("button", {
					className: "btn sm",
					onClick: async () => {
						const p = await get(runApiPath(state.run_id, "/prov"));
						const blob = new Blob([JSON.stringify(p, null, 2)], { type: "application/json" });
						const a = document.createElement("a");
						a.href = URL.createObjectURL(blob);
						a.download = `${state.run_id}_prov.json`;
						a.click();
						URL.revokeObjectURL(a.href);
					},
					children: [/* @__PURE__ */ _jsx(OpIcon, {
						name: "download",
						size: 12
					}), " W3C-PROV graph (JSON)"]
				})
			}),
			/* @__PURE__ */ _jsx("div", {
				className: "section-h",
				children: "Promotions"
			}),
			(state.promotions || []).length ? /* @__PURE__ */ _jsx(DataTable, {
				caption: "Promoted solution nodes",
				card: false,
				children: /* @__PURE__ */ _jsxs("table", {
					className: "tbl",
					children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [/* @__PURE__ */ _jsx("th", { children: "node" }), /* @__PURE__ */ _jsx("th", { children: "alias" })] }) }), /* @__PURE__ */ _jsx("tbody", { children: state.promotions.map((p, i) => /* @__PURE__ */ _jsxs("tr", { children: [/* @__PURE__ */ _jsxs("td", { children: ["#", p.node_id] }), /* @__PURE__ */ _jsx("td", { children: p.alias || "champion" })] }, i)) })]
				})
			}) : /* @__PURE__ */ _jsx("div", {
				className: "muted",
				children: "none — use Promote on a node"
			}),
			/* @__PURE__ */ _jsx("div", {
				className: "section-h",
				children: rankable ? "Cross-run leaderboard" : "Cross-run solutions"
			}),
			/* @__PURE__ */ _jsx(PanelResourceNotice, {
				resource,
				label: "Cross-run leaderboard",
				onRetry: retry
			}),
			runs.length > 0 && !rankable && /* @__PURE__ */ _jsx("div", {
				className: "muted",
				style: {
					fontSize: 11,
					marginBottom: 4
				},
				children: "Mixed tasks or objectives — listed, not ranked."
			}),
			runs.length > 0 && /* @__PURE__ */ _jsx(DataTable, {
				caption: "Cross-run best metric per run",
				card: false,
				children: /* @__PURE__ */ _jsxs("table", {
					className: "tbl",
					children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("th", { children: "run" }),
						/* @__PURE__ */ _jsx("th", { children: "task" }),
						/* @__PURE__ */ _jsx("th", { children: "phase" }),
						/* @__PURE__ */ _jsx("th", { children: "best" }),
						/* @__PURE__ */ _jsx("th", { children: "nodes" })
					] }) }), /* @__PURE__ */ _jsx("tbody", { children: rankedRuns.map((r) => /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("td", { children: r.run_id }),
						/* @__PURE__ */ _jsx("td", {
							className: "muted",
							children: r.task_id
						}),
						/* @__PURE__ */ _jsx("td", { children: r.phase }),
						/* @__PURE__ */ _jsx("td", { children: fmt(r.best_confirmed ?? r.best_metric) }),
						/* @__PURE__ */ _jsx("td", { children: r.nodes })
					] }, r.run_id)) })]
				})
			}),
			resource.state === "ready" && !runs.length && /* @__PURE__ */ _jsx("div", {
				className: "muted",
				children: "No runs in the registry yet."
			})
		]
	});
}
// Live GPU telemetry (nvidia-smi via /api/gpu). Polls while open so an operator can watch
// utilization / VRAM / power during a real training run without leaving the browser.
export function GpuPanel({ onClose }) {
	const [resource, retry] = usePanelResource((signal) => get("/api/gpu", { signal }), gpuPayload, "", 2e3);
	const data = resource.data;
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "GPU monitor",
		sub: "nvidia-smi · live",
		onClose,
		wide: true,
		children: [/* @__PURE__ */ _jsx(PanelResourceNotice, {
			resource,
			label: "GPU telemetry",
			onRetry: retry
		}), data && !data.available ? /* @__PURE__ */ _jsx("div", {
			className: "notice",
			children: "No GPU / nvidia-smi not available on the server host."
		}) : data && !data.gpus.length ? /* @__PURE__ */ _jsx("div", {
			className: "notice",
			children: "No GPU devices reported."
		}) : data?.available && (data.gpus || []).map((g, i, gpus) => {
			const gpuLabel = gpus.length > 1 ? `GPU ${i + 1} of ${gpus.length} · ${g.name}` : g.name;
			const utilizationText = g.util == null ? "—" : `${fmt(g.util)}%`;
			const memoryText = g.mem_used == null || g.mem_total == null || g.mem_total <= 0 ? "—" : `${fmt(g.mem_used)} / ${fmt(g.mem_total)} MiB`;
			const temperatureText = g.temp == null ? "—" : `${fmt(g.temp)}°C`;
			const powerText = g.power == null ? "—" : `${fmt(g.power)} W`;
			return /* @__PURE__ */ _jsxs("div", {
				style: { marginBottom: 16 },
				children: [
					/* @__PURE__ */ _jsx("div", {
						className: "section-h",
						children: gpuLabel
					}),
					/* @__PURE__ */ _jsxs("div", {
						className: "cardgrid",
						style: { marginBottom: 10 },
						children: [
							/* @__PURE__ */ _jsx(Stat, {
								n: utilizationText,
								l: "utilization"
							}),
							/* @__PURE__ */ _jsx(Stat, {
								n: memoryText,
								l: "memory"
							}),
							/* @__PURE__ */ _jsx(Stat, {
								n: temperatureText,
								l: "temperature"
							}),
							/* @__PURE__ */ _jsx(Stat, {
								n: powerText,
								l: "power draw"
							})
						]
					}),
					/* @__PURE__ */ _jsxs("div", {
						className: "kv",
						children: [
							/* @__PURE__ */ _jsx("div", {
								className: "k",
								children: "GPU util"
							}),
							/* @__PURE__ */ _jsx("div", {
								className: "v",
								children: /* @__PURE__ */ _jsx(MetricGauge, {
									value: g.util,
									hot: true,
									label: `${gpuLabel} utilization`,
									valueText: utilizationText
								})
							}),
							/* @__PURE__ */ _jsx("div", {
								className: "k",
								children: "VRAM"
							}),
							/* @__PURE__ */ _jsx("div", {
								className: "v",
								children: /* @__PURE__ */ _jsx(MetricGauge, {
									value: g.mem_used,
									max: g.mem_total,
									label: `${gpuLabel} VRAM usage`,
									valueText: `${fmt(g.mem_used)} of ${fmt(g.mem_total)} MiB`
								})
							})
						]
					})
				]
			}, i);
		})]
	});
}
// F1 · Global hyperparameter importance — across ALL evaluated feasible nodes in the run, how
// strongly does each numeric param predict the metric (|Pearson r|)? The per-node Sensitivity panel
// is local (one ablation); this is the run-wide W&B-style "which knobs matter" view. Pure UI.
// The computation lives in report.js::hyperImportance (shared with the Report's Learnings section).
export function HyperImportancePanel({ state, onClose }) {
	const nodes = Object.values(state.nodes).filter((n) => n.status === "evaluated" && n.metric != null && n.feasible !== false);
	const rows = hyperImportance(state);
	const top = rows[0]?.imp || 1;
	return /* @__PURE__ */ _jsx(Panel, {
		title: "Hyperparameter importance",
		sub: `${nodes.length} evaluated`,
		onClose,
		children: rows.length ? /* @__PURE__ */ _jsxs(_Fragment, { children: [
			/* @__PURE__ */ _jsx("div", {
				className: "section-h",
				children: "|correlation| of each param with the metric (run-wide)"
			}),
			/* @__PURE__ */ _jsx(DataTable, {
				caption: "Run-wide hyperparameter importance",
				card: false,
				children: /* @__PURE__ */ _jsxs("table", {
					className: "tbl",
					children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("th", { children: "param" }),
						/* @__PURE__ */ _jsx("th", { children: "importance" }),
						/* @__PURE__ */ _jsx("th", { children: "r" }),
						/* @__PURE__ */ _jsx("th", { children: "n" }),
						/* @__PURE__ */ _jsx("th", { children: "relative importance" })
					] }) }), /* @__PURE__ */ _jsx("tbody", { children: rows.map((row) => /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("td", { children: row.k }),
						/* @__PURE__ */ _jsx("td", { children: fmt(row.imp, 3) }),
						/* @__PURE__ */ _jsxs("td", {
							className: "muted",
							children: [row.r >= 0 ? "+" : "", fmt(row.r, 3)]
						}),
						/* @__PURE__ */ _jsx("td", {
							className: "muted",
							children: row.n
						}),
						/* @__PURE__ */ _jsx("td", {
							style: { width: 160 },
							children: /* @__PURE__ */ _jsx(MetricGauge, {
								value: row.imp,
								max: top,
								label: `${row.k} relative importance`,
								valueText: `${fmt(row.imp / top * 100, 1)}%`
							})
						})
					] }, row.k)) })]
				})
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "muted",
				style: { marginTop: 8 },
				children: [
					"Sign of r shows direction: with a ",
					state.direction === "min" ? "minimize" : "maximize",
					" objective, a ",
					state.direction === "min" ? "negative" : "positive",
					" r means a larger value tends to help. Needs ≥3 evaluated nodes per param."
				]
			})
		] }) : /* @__PURE__ */ _jsx("div", {
			className: "muted",
			children: "Not enough numeric-param data yet — run more experiments (≥3 evaluated nodes that share a numeric param)."
		})
	});
}
// Same-task IDs are an operational lookup key, not a ComparisonContract. Keep this
// legacy panel useful for navigation without ranking raw objectives whose metric unit, dataset/eval
// identity, and protocol are absent from /api/runs. The Research Atlas owns contract-bound comparison.
const CROSS_RUN_OBSERVATION_LIMIT = 100;
export function CrossRunPanel({ state, onClose }) {
	const [resource, retry] = usePanelResource((signal) => get("/api/runs", { signal }), runsPayload);
	const runs = resource.data || [];
	const task = typeof state.task_id === "string" && state.task_id.trim() ? state.task_id : "";
	// an absent task identity is not a shared identity. Never combine legacy rows merely
	// because they all encode the missing value as an empty string.
	const observations = (task ? runs.filter((r) => r.task_id === task) : []).map((r) => ({
		...r,
		m: r.best_confirmed ?? r.best_metric
	})).filter((r) => r.m != null);
	const rows = observations.slice(0, CROSS_RUN_OBSERVATION_LIMIT);
	const hidden = observations.length - rows.length;
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Same-task run observations",
		sub: resource.data ? `${observations.length} metric observation${observations.length === 1 ? "" : "s"}` : "",
		onClose,
		wide: true,
		children: [
			/* @__PURE__ */ _jsx(PanelResourceNotice, {
				resource,
				label: "Cross-run results",
				onRetry: retry
			}),
			resource.data && /* @__PURE__ */ _jsxs("div", {
				className: "panel-resource-toolbar",
				children: [
					/* @__PURE__ */ _jsx("span", {
						className: "muted",
						children: "task ID:"
					}),
					/* @__PURE__ */ _jsx("code", { children: task || "not recorded" }),
					/* @__PURE__ */ _jsxs("span", {
						className: "muted",
						children: [rows.length, " shown · comparison unavailable"]
					})
				]
			}),
			resource.data && (task ? /* @__PURE__ */ _jsxs("div", {
				className: "notice resource-warning",
				role: "status",
				children: [/* @__PURE__ */ _jsx("b", { children: "Cross-run ranking unavailable." }), /* @__PURE__ */ _jsx("span", { children: " A shared task ID does not bind metric name/unit, dataset and evaluation identity, or a comparison protocol. Values below remain per-run observations." })]
			}) : /* @__PURE__ */ _jsxs("div", {
				className: "notice resource-warning",
				role: "status",
				children: [/* @__PURE__ */ _jsx("b", { children: "Same-task observations unavailable." }), /* @__PURE__ */ _jsx("span", { children: " This run has no recorded task ID, so no portfolio row can be bound to it. Rows with missing identities are never grouped." })]
			})),
			rows.length ? /* @__PURE__ */ _jsx(DataTable, {
				caption: "Same-task per-run metric observations",
				card: false,
				children: /* @__PURE__ */ _jsxs("table", {
					className: "tbl",
					children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("th", { children: "run" }),
						/* @__PURE__ */ _jsx("th", { children: "recorded objective" }),
						/* @__PURE__ */ _jsx("th", { children: "direction" }),
						/* @__PURE__ */ _jsx("th", { children: "nodes" }),
						/* @__PURE__ */ _jsx("th", { children: "status" })
					] }) }), /* @__PURE__ */ _jsx("tbody", { children: rows.map((r) => /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("td", { children: /* @__PURE__ */ _jsx("a", {
							href: `#/run/${encodeURIComponent(r.run_id)}`,
							children: r.label || r.run_id
						}) }),
						/* @__PURE__ */ _jsxs("td", { children: [fmt(r.m), r.best_confirmed != null ? " (confirmed mean)" : ""] }),
						/* @__PURE__ */ _jsx("td", {
							className: "muted",
							children: r.direction
						}),
						/* @__PURE__ */ _jsx("td", {
							className: "muted",
							children: r.nodes
						}),
						/* @__PURE__ */ _jsx("td", {
							className: "muted",
							children: r.phase || (r.finished ? "finished" : "—")
						})
					] }, r.run_id)) })]
				})
			}) : resource.state === "ready" && task && /* @__PURE__ */ _jsx("div", {
				className: "muted",
				children: "No per-run metric observations for this task ID yet."
			}),
			hidden > 0 && /* @__PURE__ */ _jsxs("div", {
				className: "muted",
				style: { marginTop: 8 },
				children: [
					hidden,
					" additional observation",
					hidden === 1 ? "" : "s",
					" omitted by the client render limit."
				]
			})
		]
	});
}
// Legacy direction board retained as a graceful fallback for pre-Card logs. Current runs use the
// bounded public Card DTO and four generation-fenced, server-stamped operator controls below.
const _HYP_COLUMNS = [
	[
		"open",
		"Open",
		"question posed, not yet tested"
	],
	[
		"testing",
		"Testing",
		"experiments running"
	],
	[
		"supported",
		"Supported",
		"an experiment improved"
	],
	[
		"tested",
		"Tested",
		"evaluated, no improvement"
	],
	[
		"abandoned",
		"Abandoned",
		"dropped"
	]
];
// Monochrome source glyphs (no emoji): who posed the hypothesis. Reuses the shared icon set.
const _HYP_ICON = {
	researcher: "search",
	deep_research: "bulb",
	human: "user",
	strategist: "compass"
};
const _CARD_COLUMNS = [
	[
		"proposed",
		"Proposed",
		"work item is open and has not started"
	],
	[
		"speculating",
		"Speculating",
		"speculative build requested"
	],
	[
		"building",
		"Building",
		"code is being produced"
	],
	[
		"built-awaiting-commit",
		"Awaiting commit",
		"build finished; durable node commit is pending"
	],
	[
		"coded",
		"Coded",
		"code exists and is waiting to run"
	],
	[
		"running",
		"Running",
		"evaluation is in flight"
	],
	[
		"evaluated",
		"Evaluated",
		"evidence has reached a verdict"
	],
	[
		"gated",
		"Gated",
		"trust or breeding gates exclude the available evidence"
	],
	[
		"dropped",
		"Dropped",
		"operator or engine removed the work item"
	]
];
const _CARD_FROZEN_STATUSES = new Set([
	"proposed",
	"building",
	"coded",
	"running",
	"evaluated",
	"gated",
	"dropped"
]);
const _CARD_OPTIONAL_STATUSES = new Set(["speculating", "built-awaiting-commit"]);
const _CARD_ICON = {
	researcher: "search",
	deep_research: "bulb",
	human: "user",
	strategist: "compass",
	operator: "user",
	engine: "bot",
	novelty: "bulb"
};
const _CARD_RENDER_LIMIT = 256;
const _cardText = (value) => typeof value === "string" && value.trim() ? value.trim() : null;
const _cardNumber = (value) => typeof value === "number" && Number.isFinite(value) ? value : null;
const _cardInt = (value) => Number.isSafeInteger(value) && value >= 0 ? value : null;
const _cardRefs = (value) => Array.isArray(value) ? value.filter((item) => typeof item === "string" && item).slice(0, 32) : [];
const _cardNodes = (value) => Array.isArray(value) ? value.filter((item) => Number.isSafeInteger(item) && item >= 0).slice(0, 32) : [];
function _cardStatus(card) {
	return _cardText(card.status) || "unknown";
}
function _cardStatusLabel(status) {
	return status.split(/[-_]/).filter(Boolean).map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(" ") || "Other";
}
function _cardRows(state) {
	if (!isRecord(state?.cards)) return [];
	return Object.entries(state.cards).filter(([id, card]) => typeof id === "string" && id && isRecord(card)).slice(0, _CARD_RENDER_LIMIT).map(([id, card]) => ({
		...card,
		id
	}));
}
function _cardLanes(cards) {
	const occupied = new Set(cards.map(_cardStatus));
	const configured = _CARD_COLUMNS.filter(([status]) => !_CARD_OPTIONAL_STATUSES.has(status) || occupied.has(status));
	const known = new Set(_CARD_COLUMNS.map(([status]) => status));
	const extra = [...occupied].filter((status) => !known.has(status)).sort().map((status) => [
		status,
		_cardStatusLabel(status),
		"new derived lifecycle status"
	]);
	const dropped = configured.find(([status]) => status === "dropped");
	return [
		...configured.filter(([status]) => status !== "dropped"),
		...extra,
		dropped
	].filter(Boolean);
}
function _cardOrder(a, b) {
	const ap = _cardNumber(a.priority) ?? _cardNumber(a.foresight_rank) ?? Infinity;
	const bp = _cardNumber(b.priority) ?? _cardNumber(b.foresight_rank) ?? Infinity;
	if (ap !== bp) return ap - bp;
	const an = _cardInt(a.created_at_node) ?? Infinity;
	const bn = _cardInt(b.created_at_node) ?? Infinity;
	return an - bn || a.id.localeCompare(b.id);
}
const _CARD_CONTROL_KINDS = [
	"edit",
	"priority",
	"resources",
	"drop",
	"abandon"
];
function _cardResourceValues(value) {
	if (!isRecord(value)) return null;
	const gpus = _cardInt(value.gpus);
	const gpuMem = _cardInt(value.gpu_mem_mib);
	return gpus == null && gpuMem == null ? null : {
		...gpus == null ? {} : { gpus },
		...gpuMem == null ? {} : { gpu_mem_mib: gpuMem }
	};
}
function _sameCardResourceValues(left, right) {
	const a = _cardResourceValues(left);
	const b = _cardResourceValues(right);
	if (a == null || b == null) return a === b;
	return ["gpus", "gpu_mem_mib"].every((key) => Object.hasOwn(a, key) === Object.hasOwn(b, key) && a[key] === b[key]);
}
function cardControlReflected(card, kind, patch, baseline, expectedEventSeq) {
	if (!card || !isRecord(patch)) return false;
	if (kind === "edit") {
		// Modern folds publish the exact durable event that owns the display overlay. This remains
		// reliable when public-state secret redaction transforms the text into a non-prefix value.
		return cardEditReflected(card, patch, baseline, expectedEventSeq);
	}
	if (kind === "priority") return card.priority === patch.priority && card.pinned === true;
	if (kind === "resources") return _sameCardResourceValues(card.resource_pin, patch.resource_pin);
	if (kind === "drop") return card.status === "dropped" && (!patch.dropped_reason || card.dropped_reason === patch.dropped_reason);
	if (kind === "abandon") return card.verdict === "abandoned";
	return false;
}
function _cardWithOptimisticControls(card, controlState) {
	if (!isRecord(controlState?.updates)) return card;
	const visible = { ...card };
	for (const kind of _CARD_CONTROL_KINDS) {
		if (isRecord(controlState.updates[kind])) Object.assign(visible, controlState.updates[kind]);
	}
	return visible;
}
function _cardResourceSummary(value, { unavailable = "unspecified" } = {}) {
	const footprint = _cardResourceValues(value);
	if (!footprint) return unavailable;
	const gpus = footprint.gpus;
	const memory = footprint.gpu_mem_mib;
	return [gpus == null ? "GPU count unspecified" : gpus === 0 ? "CPU only" : `${gpus} GPU${gpus === 1 ? "" : "s"}`, memory == null ? null : `${fmtInt(memory)} MiB/GPU`].filter(Boolean).join(" · ");
}
function _CardProjectionNotice({ projection, cards }) {
	if (!isRecord(projection)) return /* @__PURE__ */ _jsx("div", {
		className: "card-projection-note",
		role: "status",
		children: "Card coverage receipt unavailable; this older payload may be incomplete."
	});
	if (projection.complete === true) return null;
	const total = _cardInt(projection.total);
	const returned = _cardInt(projection.returned) ?? cards.length;
	const sourceInvalid = projection.source_valid === false;
	return /* @__PURE__ */ _jsxs("div", {
		className: "card-projection-note",
		role: "status",
		children: [/* @__PURE__ */ _jsx(OpIcon, {
			name: "alert",
			size: 12
		}), /* @__PURE__ */ _jsx("span", { children: sourceInvalid ? "Card source was invalid; no complete board can be claimed." : `Showing ${returned}${total == null ? "" : ` of ${total}`} Cards; clipped or redacted public fields are marked partial.` })]
	});
}
function _CardKanbanCard({ card, receipt, onSelect, onClose, onControl, controlState, controlsLocked }) {
	const statement = _cardText(card.statement) || `Card ${card.id}`;
	const source = _cardText(card.source);
	const operator = _cardText(card.operator);
	const evalProfile = _cardText(card.eval_profile);
	const params = isRecord(card.params) ? Object.entries(card.params).filter(([, value]) => _cardNumber(value) != null).slice(0, 6) : [];
	const spaceCount = isRecord(card.space) ? Object.keys(card.space).length : 0;
	const footprintKnown = Object.hasOwn(card, "footprint");
	const baseFootprint = isRecord(card.footprint) ? card.footprint : null;
	const resourcePin = isRecord(card.resource_pin) ? card.resource_pin : null;
	// This is the configured Card footprint after applying the operator override. Runtime scheduling may
	// still allocate less, so never label the client-side projection as an effective allocation.
	const configuredFootprint = baseFootprint || resourcePin ? {
		..._cardResourceValues(baseFootprint) || {},
		..._cardResourceValues(resourcePin) || {}
	} : null;
	if (configuredFootprint?.gpus === 0) delete configuredFootprint.gpu_mem_mib;
	const configuredGpus = configuredFootprint ? _cardInt(configuredFootprint.gpus) : null;
	const pinValues = _cardResourceValues(resourcePin);
	const formGpus = pinValues?.gpus ?? configuredGpus;
	const formGpuMem = pinValues && Object.hasOwn(pinValues, "gpu_mem_mib") ? pinValues.gpu_mem_mib : null;
	const evalTimeout = _cardNumber(card.eval_timeout);
	const identity = isRecord(card.identity) ? card.identity : null;
	const selection = isRecord(card.selection_provenance) ? card.selection_provenance : null;
	const blockersKnown = Object.hasOwn(card, "selection_blockers");
	const blockers = _cardRefs(card.selection_blockers);
	const evidenceKnown = Object.hasOwn(card, "evidence");
	const evidence = _cardNodes(card.evidence).slice(0, 8);
	const concepts = _cardRefs(card.concept_tags).slice(0, 5);
	const parents = _cardNodes(card.parent_ids);
	const parent = _cardInt(card.parent_id);
	if (parent != null && !parents.includes(parent)) parents.unshift(parent);
	const parentGenerations = isRecord(card.parent_generations) ? card.parent_generations : null;
	const scoredAgainst = _cardInt(card.scored_against);
	const scoredAgainstGeneration = _cardInt(card.scored_against_generation);
	const parentLineage = parents.map((id) => {
		const attempt = parentGenerations ? _cardInt(parentGenerations[String(id)]) : null;
		return `#${id} · attempt ${attempt == null ? "unknown" : attempt}`;
	}).join(", ");
	const scoredLineage = scoredAgainst == null ? "" : `#${scoredAgainst} · attempt ${scoredAgainstGeneration == null ? "unknown" : scoredAgainstGeneration}`;
	const bestDelta = _cardNumber(card.best_delta);
	const priority = _cardNumber(card.priority);
	const novelty = isRecord(card.novelty_verdict) ? _cardText(card.novelty_verdict.grade) : null;
	const omissionCount = isRecord(receipt?.omissions) ? Object.keys(receipt.omissions).length : 0;
	const declaredResources = footprintKnown ? _cardResourceSummary(baseFootprint) : "resource projection unavailable";
	const configuredResources = _cardResourceSummary(configuredFootprint);
	const pinResources = _cardResourceSummary(resourcePin);
	const provenanceBits = [
		source && `source ${source}`,
		identity && _cardText(identity.kind) && `identity ${identity.kind}`,
		_cardText(card.provenance_tier) && `tier ${card.provenance_tier}`,
		selection && _cardText(selection.action_source) && `action ${selection.action_source}`,
		baseFootprint && _cardText(baseFootprint.proposed_by) && `proposed ${baseFootprint.proposed_by}`,
		baseFootprint && _cardText(baseFootprint.finalized_by) && `finalized ${baseFootprint.finalized_by}`,
		resourcePin && _cardText(resourcePin.pinned_by) && `resource pin ${resourcePin.pinned_by}`,
		_cardText(card.research_origin) && `research ${card.research_origin}`
	].filter(Boolean);
	const [statementDraft, setStatementDraft] = useState(statement);
	const [priorityDraft, setPriorityDraft] = useState(_cardInt(card.priority) == null ? "" : String(card.priority + 1));
	const [gpuDraft, setGpuDraft] = useState(formGpus == null ? "" : String(formGpus));
	const [memoryDraft, setMemoryDraft] = useState(formGpuMem == null ? "" : String(formGpuMem));
	const [dropReason, setDropReason] = useState("operator dropped");
	const [controlError, setControlError] = useState("");
	const ownPending = isRecord(controlState?.pending) ? controlState.pending : null;
	const busy = !!ownPending || controlsLocked === true;
	// The research VERDICT (open/supported/testing/tested/abandoned — the only values `_evidence_verdict`
	// produces) is distinct from the work-lifecycle STATUS (peer review): replay can publish
	// status=proposed/evaluated with verdict=abandoned, so read the verdict separately — render it as its
	// own chip (a supported/tested outcome was otherwise invisible) and treat an abandoned belief as
	// terminal so the board stops offering edit/priority/drop controls.
	const verdict = _cardText(card.verdict);
	const terminal = _cardStatus(card) === "dropped" || !!_cardText(card.merged_into) || verdict === "abandoned";
	// Re-seed each draft ONLY when its own folded source (or the card identity) changes. A single effect
	// over every dep re-ran on ANY change, so an unrelated live fold (e.g. a card_ranked priority bump
	// arriving while the operator is typing a new statement) reset ALL four drafts and silently discarded
	// the in-progress edits in the other fields. Per-field effects keep each edit until its own source moves.
	useEffect(() => {
		setStatementDraft(statement);
	}, [card.id, statement]);
	useEffect(() => {
		setPriorityDraft(_cardInt(card.priority) == null ? "" : String(card.priority + 1));
	}, [card.id, card.priority]);
	useEffect(() => {
		setGpuDraft(formGpus == null ? "" : String(formGpus));
	}, [card.id, formGpus]);
	useEffect(() => {
		setMemoryDraft(formGpuMem == null ? "" : String(formGpuMem));
	}, [card.id, formGpuMem]);
	const control = async (kind, data, patch) => {
		if (!onControl || busy) return;
		setControlError("");
		try {
			await onControl(card, kind, data, patch);
		} catch (error) {
			setControlError(error?.message || String(error));
		}
	};
	const saveStatement = () => {
		const next = statementDraft.trim();
		if (!next || next.length > 4e3) {
			setControlError("Display statement must be 1–4000 characters.");
			return;
		}
		if (next !== statement) control("edit", { statement: next }, { statement: next });
	};
	const savePriority = () => {
		const visible = Number(priorityDraft);
		if (!Number.isSafeInteger(visible) || visible < 1 || visible > 256) {
			setControlError("Priority must be between 1 and 256.");
			return;
		}
		control("priority", { priority: visible - 1 }, { priority: visible - 1 });
	};
	const saveResources = () => {
		const nextGpus = Number(gpuDraft);
		const nextMemory = memoryDraft.trim() === "" ? null : Number(memoryDraft);
		if (!Number.isSafeInteger(nextGpus) || nextGpus < 0 || nextMemory != null && (!Number.isSafeInteger(nextMemory) || nextMemory < 0)) {
			setControlError("GPU count and memory must be non-negative integers.");
			return;
		}
		if (nextGpus === 0 && nextMemory != null) {
			setControlError("CPU-only Cards cannot request GPU memory.");
			return;
		}
		// This local patch contains quantitative display values only. Authority/provenance is stamped by
		// the server event and is never supplied by the browser, even optimistically.
		const pin = {
			gpus: nextGpus,
			...nextMemory == null ? {} : { gpu_mem_mib: nextMemory }
		};
		control("resources", {
			gpus: nextGpus,
			gpu_mem_mib: nextMemory
		}, { resource_pin: pin });
	};
	const drop = () => {
		const reason = dropReason.trim() || "operator dropped";
		control("drop", { reason }, {
			status: "dropped",
			dropped_reason: reason
		});
	};
	// This is deliberately Card-scoped: the backend receives one Card id, so siblings that happen to
	// share a seed remain unchanged. The control changes the Card's research verdict, not its work lane.
	const abandonCard = () => control("abandon", {}, { verdict: "abandoned" });
	return /* @__PURE__ */ _jsxs("article", {
		className: "card-kanban-card",
		"data-card-id": card.id,
		"aria-label": statement,
		"aria-busy": ownPending ? "true" : undefined,
		children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "card-kanban-stmt",
				children: [/* @__PURE__ */ _jsx("span", {
					className: "hyp-src",
					title: source ? `source: ${source}` : "source unavailable",
					children: /* @__PURE__ */ _jsx(OpIcon, {
						name: _CARD_ICON[source] || "dot",
						size: 12
					})
				}), /* @__PURE__ */ _jsx("span", { children: statement })]
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "card-kanban-meta",
				children: [
					/* @__PURE__ */ _jsx("span", {
						className: "chip xs",
						title: "durable Card identity",
						children: card.id
					}),
					verdict && verdict !== "open" && /* @__PURE__ */ _jsx("span", {
						className: "chip xs " + (verdict === "supported" ? "ok" : verdict === "abandoned" ? "warn" : ""),
						title: `research verdict: ${verdict} (distinct from the work status)`,
						children: verdict
					}),
					priority != null && /* @__PURE__ */ _jsxs("span", {
						className: "chip xs",
						title: "derived priority; 1 is highest",
						children: ["#", priority + 1]
					}),
					card.pinned === true && /* @__PURE__ */ _jsxs("span", {
						className: "chip xs warn",
						children: [/* @__PURE__ */ _jsx(OpIcon, {
							name: "flag",
							size: 10
						}), " pinned"]
					}),
					card.selection_ready === true ? /* @__PURE__ */ _jsx("span", {
						className: "chip xs ok",
						title: "eligible for Card-driven selection",
						children: "selection ready"
					}) : card.selection_ready === false ? /* @__PURE__ */ _jsx("span", {
						className: "chip xs warn",
						title: "not eligible for Card-driven selection",
						children: "not selection ready"
					}) : /* @__PURE__ */ _jsx("span", {
						className: "chip xs",
						title: "selection readiness was not present in the public projection",
						children: "readiness unknown"
					}),
					receipt && receipt.complete !== true && /* @__PURE__ */ _jsxs("span", {
						className: "chip xs warn",
						title: `${omissionCount} public field omission${omissionCount === 1 ? "" : "s"}`,
						children: ["partial details", omissionCount ? ` · ${omissionCount}` : ""]
					})
				]
			}),
			(operator || evalProfile || params.length || spaceCount) && /* @__PURE__ */ _jsxs("div", {
				className: "card-kanban-fact",
				children: [
					/* @__PURE__ */ _jsx("span", {
						className: "card-kanban-k",
						children: "Action"
					}),
					/* @__PURE__ */ _jsx("span", { children: operator || "operator unspecified" }),
					evalProfile && /* @__PURE__ */ _jsxs("span", { children: ["profile ", evalProfile] }),
					params.map(([key, value]) => /* @__PURE__ */ _jsxs("span", {
						className: "card-param",
						children: [
							key,
							"=",
							fmt(value)
						]
					}, key)),
					spaceCount > 0 && /* @__PURE__ */ _jsxs("span", { children: [
						spaceCount,
						" search variable",
						spaceCount === 1 ? "" : "s"
					] })
				]
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "card-kanban-fact",
				children: [/* @__PURE__ */ _jsx("span", {
					className: "card-kanban-k",
					children: "Declared"
				}), /* @__PURE__ */ _jsxs("span", { children: [declaredResources, evalTimeout == null ? "" : ` · ${fmt(evalTimeout)}s timeout`] })]
			}),
			resourcePin && /* @__PURE__ */ _jsxs("div", {
				className: "card-kanban-fact card-resource-pin",
				children: [/* @__PURE__ */ _jsx("span", {
					className: "card-kanban-k",
					children: "Configured pin"
				}), /* @__PURE__ */ _jsxs("span", { children: [
					/* @__PURE__ */ _jsx("span", {
						className: "chip xs warn",
						children: _cardText(resourcePin.pinned_by) === "operator" ? "operator override" : "pending operator override"
					}),
					" ",
					configuredResources,
					/* @__PURE__ */ _jsxs("span", {
						className: "card-resource-request",
						children: ["requested ", pinResources]
					})
				] })]
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "card-kanban-fact",
				children: [/* @__PURE__ */ _jsx("span", {
					className: "card-kanban-k",
					children: "Provenance"
				}), /* @__PURE__ */ _jsx("span", { children: provenanceBits.length ? provenanceBits.join(" · ") : "unavailable" })]
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "card-kanban-fact",
				children: [/* @__PURE__ */ _jsx("span", {
					className: "card-kanban-k",
					children: "Gate"
				}), /* @__PURE__ */ _jsxs("span", { children: [
					selection && _cardText(selection.freshness) ? `freshness ${selection.freshness}` : "freshness unknown",
					selection && _cardText(selection.owner_state) ? ` · owner ${selection.owner_state}` : "",
					selection && typeof selection.action_complete === "boolean" ? ` · action ${selection.action_complete ? "complete" : "incomplete"}` : ""
				] })]
			}),
			blockers.length > 0 && /* @__PURE__ */ _jsxs("div", {
				className: "card-kanban-blockers",
				"aria-label": "Selection blockers",
				children: [blockers.slice(0, 5).map((blocker) => /* @__PURE__ */ _jsx("span", {
					className: "chip xs warn",
					children: blocker.replaceAll("_", " ")
				}, blocker)), blockers.length > 5 && /* @__PURE__ */ _jsxs("span", {
					className: "muted",
					children: ["+", blockers.length - 5]
				})]
			}),
			!blockersKnown && /* @__PURE__ */ _jsx("div", {
				className: "muted card-kanban-unknown",
				children: "Selection blockers unavailable"
			}),
			(parents.length > 0 || scoredAgainst != null) && /* @__PURE__ */ _jsxs("div", {
				className: "card-kanban-fact",
				children: [/* @__PURE__ */ _jsx("span", {
					className: "card-kanban-k",
					children: "Lineage"
				}), /* @__PURE__ */ _jsxs("span", { children: [parents.length ? `parent ${parentLineage}` : "", scoredAgainst != null ? `${parents.length ? " · " : ""}scored vs ${scoredLineage}` : ""] })]
			}),
			(concepts.length > 0 || novelty) && /* @__PURE__ */ _jsxs("div", {
				className: "card-kanban-tags",
				children: [novelty && /* @__PURE__ */ _jsxs("span", {
					className: "chip xs",
					children: ["novelty ", novelty]
				}), concepts.map((concept) => /* @__PURE__ */ _jsx("span", {
					className: "chip xs",
					children: concept
				}, concept))]
			}),
			(_cardText(card.merged_into) || _cardText(card.dropped_reason)) && /* @__PURE__ */ _jsxs("div", {
				className: "card-kanban-terminal",
				children: [_cardText(card.merged_into) ? `Merged into ${card.merged_into}` : card.dropped_reason, _cardText(card.dropped_by) ? ` · by ${card.dropped_by}` : ""]
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "card-kanban-evidence",
				children: [
					evidence.map((nid) => /* @__PURE__ */ _jsxs("button", {
						type: "button",
						className: "btn xs ghost",
						"aria-label": `Open evidence node #${nid}`,
						title: `evidence node #${nid}`,
						onClick: () => {
							onSelect?.(nid);
							onClose?.();
						},
						children: ["#", nid]
					}, nid)),
					bestDelta != null && /* @__PURE__ */ _jsxs("span", {
						className: "chip xs " + (bestDelta > 0 ? "ok" : ""),
						title: "best improvement over parent among the evidence",
						children: ["Δ", fmt(bestDelta)]
					}),
					evidence.length === 0 && /* @__PURE__ */ _jsx("span", {
						className: "muted",
						children: evidenceKnown ? "No evidence nodes" : "Evidence unavailable"
					})
				]
			}),
			onControl && !terminal && /* @__PURE__ */ _jsxs("details", {
				className: "card-kanban-controls",
				children: [
					/* @__PURE__ */ _jsx("summary", {
						"aria-label": `Operator controls for ${card.id}`,
						children: "Operator controls"
					}),
					/* @__PURE__ */ _jsxs("form", {
						className: "card-control-form",
						onSubmit: (event) => {
							event.preventDefault();
							saveStatement();
						},
						children: [/* @__PURE__ */ _jsxs("label", { children: [/* @__PURE__ */ _jsx("span", { children: "Display statement" }), /* @__PURE__ */ _jsx("textarea", {
							className: "text card-control-statement",
							"aria-label": `Display statement for ${card.id}`,
							rows: "3",
							value: statementDraft,
							maxLength: 4e3,
							disabled: busy,
							onChange: (event) => setStatementDraft(event.target.value)
						})] }), /* @__PURE__ */ _jsx("button", {
							type: "submit",
							className: "btn xs",
							disabled: busy || !statementDraft.trim() || statementDraft.trim() === statement,
							children: "Save text"
						})]
					}),
					/* @__PURE__ */ _jsxs("form", {
						className: "card-control-form",
						onSubmit: (event) => {
							event.preventDefault();
							savePriority();
						},
						children: [/* @__PURE__ */ _jsxs("label", { children: [/* @__PURE__ */ _jsx("span", { children: "Priority (1 is highest)" }), /* @__PURE__ */ _jsx("input", {
							className: "text",
							type: "number",
							min: "1",
							max: "256",
							"aria-label": `Priority for ${card.id}`,
							value: priorityDraft,
							disabled: busy,
							onChange: (event) => setPriorityDraft(event.target.value)
						})] }), /* @__PURE__ */ _jsx("button", {
							type: "submit",
							className: "btn xs",
							disabled: busy || !priorityDraft,
							children: "Pin priority"
						})]
					}),
					/* @__PURE__ */ _jsx("form", {
						className: "card-control-resource",
						onSubmit: (event) => {
							event.preventDefault();
							saveResources();
						},
						children: /* @__PURE__ */ _jsxs("fieldset", {
							disabled: busy,
							children: [
								/* @__PURE__ */ _jsx("legend", { children: "Configured resource override" }),
								/* @__PURE__ */ _jsxs("div", {
									className: "card-control-resource-fields",
									children: [/* @__PURE__ */ _jsxs("label", { children: [/* @__PURE__ */ _jsx("span", { children: "GPUs" }), /* @__PURE__ */ _jsx("input", {
										className: "text",
										type: "number",
										min: "0",
										step: "1",
										"aria-label": `GPU count for ${card.id}`,
										value: gpuDraft,
										onChange: (event) => {
											setGpuDraft(event.target.value);
											if (event.target.value === "0") setMemoryDraft("");
										}
									})] }), /* @__PURE__ */ _jsxs("label", { children: [/* @__PURE__ */ _jsx("span", { children: "MiB / GPU" }), /* @__PURE__ */ _jsx("input", {
										className: "text",
										type: "number",
										min: "0",
										step: "1",
										"aria-label": `GPU memory in MiB for ${card.id}`,
										placeholder: "inherit declared",
										value: memoryDraft,
										disabled: busy || gpuDraft === "0",
										onChange: (event) => setMemoryDraft(event.target.value)
									})] })]
								}),
								/* @__PURE__ */ _jsx("div", {
									className: "card-control-help",
									children: "Validated against the current server GPU envelope; blank memory inherits the declared value. Execution may still wait for local GPU admission."
								}),
								/* @__PURE__ */ _jsx("button", {
									type: "submit",
									className: "btn xs",
									disabled: busy || gpuDraft === "",
									children: "Pin resources"
								})
							]
						})
					}),
					/* @__PURE__ */ _jsx("div", {
						className: "card-control-form",
						children: /* @__PURE__ */ _jsx("button", {
							type: "button",
							className: "btn xs",
							disabled: busy,
							onClick: abandonCard,
							title: "Mark only this Card’s research verdict abandoned; sibling Cards stay unchanged and this Card remains visible",
							children: "Abandon this Card"
						})
					}),
					/* @__PURE__ */ _jsxs("details", {
						className: "card-control-danger",
						children: [/* @__PURE__ */ _jsx("summary", { children: "Drop Card…" }), /* @__PURE__ */ _jsxs("form", {
							className: "card-control-form",
							onSubmit: (event) => {
								event.preventDefault();
								drop();
							},
							children: [/* @__PURE__ */ _jsxs("label", { children: [/* @__PURE__ */ _jsx("span", { children: "Reason (optional)" }), /* @__PURE__ */ _jsx("input", {
								className: "text",
								value: dropReason,
								maxLength: 400,
								"aria-label": `Drop reason for ${card.id}`,
								disabled: busy,
								onChange: (event) => setDropReason(event.target.value)
							})] }), /* @__PURE__ */ _jsx("button", {
								type: "submit",
								className: "btn xs danger",
								disabled: busy,
								children: "Confirm drop"
							})]
						})]
					}),
					controlsLocked && !ownPending && /* @__PURE__ */ _jsx("div", {
						className: "card-control-feedback",
						role: "status",
						children: "Another Card command is still being submitted for this run."
					})
				]
			}),
			isRecord(controlState?.notice) && /* @__PURE__ */ _jsx("div", {
				className: "card-control-feedback " + (controlState.notice.tone || ""),
				role: controlState.notice.tone === "error" ? "alert" : "status",
				"aria-live": "polite",
				children: controlState.notice.text
			}),
			controlError && /* @__PURE__ */ _jsx("div", {
				className: "card-control-feedback error",
				role: "alert",
				children: controlError
			})
		]
	});
}
function _CardKanban({ state, cards, runId, onSelect, onClose, onToast }) {
	const [optim, setOptim] = useState({});
	const [addDraft, setAddDraft] = useState("");
	const inFlight = useRef(new Set());
	const activeRef = useRef(true);
	useEffect(() => {
		activeRef.current = true;
		return () => {
			activeRef.current = false;
		};
	}, []);
	// Last edit statement SUBMITTED per card id. It outlives the optimistic override (which clears on a
	// success ack before the SSE fold arrives), so a chained extend edit can baseline against the prior
	// in-flight submission instead of a stale fold — see the editBaseline capture in cardControl.
	const sentEditRef = useRef({});
	const cardsById = new Map(cards.map((card) => [card.id, card]));
	const cardsByIdRef = useRef(cardsById);
	cardsByIdRef.current = cardsById;
	useEffect(() => {
		// Prune sentEditRef for cards no longer on the board: the ref outlives the optimistic override
		// (needed so a chained extend edit can baseline against the prior in-flight submission), so without
		// this it accumulates one entry per distinct id ever edited AND a recreated same id would inherit a
		// vanished card's last submission as a stale edit baseline. Bound it to live cards.
		for (const id of Object.keys(sentEditRef.current)) {
			if (!cardsByIdRef.current.has(id)) delete sentEditRef.current[id];
		}
		setOptim((current) => {
			let changed = false;
			const next = { ...current };
			for (const [id, entry] of Object.entries(current)) {
				const card = cardsByIdRef.current.get(id);
				if (!card) {
					delete next[id];
					changed = true;
					continue;
				}
				const updates = { ...entry.updates || {} };
				for (const kind of _CARD_CONTROL_KINDS) {
					if (updates[kind] && cardControlReflected(card, kind, updates[kind], entry.editBaseline, entry.editEventSeq)) {
						delete updates[kind];
						changed = true;
					}
				}
				const pending = entry.pending && updates[entry.pending.kind] ? entry.pending : null;
				if (pending !== entry.pending) changed = true;
				if (Object.keys(updates).length === 0 && !pending) {
					delete next[id];
					changed = true;
				} else if (changed || pending !== entry.pending) {
					next[id] = {
						...entry,
						updates,
						pending
					};
				}
			}
			return changed ? next : current;
		});
	}, [state.cards]);
	const visibleCards = cards.map((card) => _cardWithOptimisticControls(card, optim[card.id]));
	// A 'confirmation-unknown' pending (a lost/uncertain submission) MAY never self-clear: if the intent
	// never actually landed, the fold never reflects it and the reconcile effect above never drops it (if
	// it DID land, that effect clears it normally). Because it can hang indefinitely, it must not count
	// toward the board-wide lock, or one uncertain command would freeze the controls on EVERY Card until
	// reload. The real concurrency guard is `inFlight` (released in the finally), and the stuck Card still
	// shows its own 'waiting for the live fold' notice via its own pending. Only genuinely-progressing
	// pendings gate the rest of the board.
	const globalPending = Object.values(optim).some((entry) => isRecord(entry?.pending) && entry.pending.phase !== "confirmation-unknown");
	const cardControl = async (card, kind, data, patch) => {
		const labels = {
			edit: {
				saving: "Saving Card display text…",
				success: "Card display text updated",
				failure: "Could not edit Card"
			},
			priority: {
				saving: "Pinning Card priority…",
				success: "Card priority pinned",
				failure: "Could not pin Card priority"
			},
			resources: {
				saving: "Pinning Card resources…",
				success: "Card resources pinned",
				failure: "Could not pin Card resources"
			},
			drop: {
				saving: "Dropping Card…",
				success: "Card dropped",
				failure: "Could not drop Card"
			},
			abandon: {
				saving: "Abandoning this Card…",
				success: "Card abandoned",
				failure: "Could not abandon Card"
			}
		}[kind];
		if (!labels || inFlight.current.size > 0) {
			const message = "Another Card command is still being submitted for this run.";
			onToast?.(message);
			return {
				kind: "pending",
				message
			};
		}
		inFlight.current.add(card.id);
		// Baseline for edit-reflection = the value the card shows JUST BEFORE this edit. Normally that is the
		// current fold, but for a CHAINED edit the prior edit may not have folded yet, so the visible fold is
		// stale (one step behind). Use the prior SUBMITTED statement when the current card is a proper prefix
		// of it (we are still catching up to that earlier edit); otherwise the card has already moved on, so
		// the current statement is right. This self-cleans: once the fold reaches the prior submission the
		// prefix test fails and we fall back to the fold. (See `cardControlReflected`.)
		let editBaseline;
		if (kind === "edit" && typeof card.statement === "string") {
			const prior = sentEditRef.current[card.id];
			editBaseline = typeof prior === "string" && prior !== card.statement && prior.startsWith(card.statement) ? prior : card.statement;
			if (typeof patch.statement === "string") sentEditRef.current[card.id] = patch.statement;
		}
		// Only this submission's receipt may satisfy the edit fence; a chained edit resets the previous seq.
		setOptim((current) => cardControlSubmission(current, card.id, kind, patch, editBaseline, labels.saving));
		try {
			const record = kind === "edit" ? await CONTROL.editCard(runId, card.id, data.statement) : kind === "priority" ? await CONTROL.reprioritizeCard(runId, card.id, data.priority) : kind === "resources" ? await CONTROL.pinCardResources(runId, card.id, data.gpus, data.gpu_mem_mib) : kind === "abandon" ? await CONTROL.abandonHypothesis(runId, card.id) : await CONTROL.dropCard(runId, card.id, data.reason);
			if (!activeRef.current) return {
				kind: "stale",
				message: "Card board scope changed"
			};
			const feedback = commandFeedback(record, {
				success: labels.success,
				noop: `${labels.success} (already current)`,
				executing: `${labels.success} — waiting for the live fold`,
				failure: labels.failure
			});
			const recordEditSeq = kind === "edit" ? _cardInt(record?.event_seq) : null;
			onToast?.(feedback.message);
			setOptim((current) => {
				const entry = current[card.id];
				if (!entry) return current;
				const updates = { ...entry.updates || {} };
				const rawCard = cardsByIdRef.current.get(card.id);
				const editEventSeq = recordEditSeq ?? entry.editEventSeq;
				// Clear the optimistic override once the command DEFINITIVELY settles (success/noop/error) — not
				// only on an exact fold reflection: the server may clip/redact the value, so waiting for a
				// byte-equal fold would leave the card stuck showing operator text with its controls disabled.
				// Only a 'pending' (accepted, engine will apply later) settle keeps the override until the fold.
				const settled = [
					"error",
					"success",
					"noop"
				].includes(feedback.kind);
				if (settled || cardControlReflected(rawCard, kind, patch, entry.editBaseline, editEventSeq)) {
					delete updates[kind];
				}
				const pending = feedback.kind === "pending" && updates[kind] ? {
					kind,
					phase: "waiting-for-fold"
				} : null;
				const notice = feedback.kind === "error" ? {
					tone: "error",
					text: feedback.message
				} : {
					tone: feedback.kind === "pending" ? "pending" : "success",
					text: feedback.message
				};
				return {
					...current,
					[card.id]: {
						...entry,
						updates,
						pending,
						notice,
						...editEventSeq == null ? {} : { editEventSeq }
					}
				};
			});
			return feedback;
		} catch (error) {
			if (!activeRef.current) return {
				kind: "stale",
				message: "Card board scope changed"
			};
			const uncertain = error?.submissionMayHaveSucceeded === true || error?.commandUnknown === true || ["accepted", "executing"].includes(error?.commandRecord?.status);
			const commandEditSeq = kind === "edit" ? _cardInt(error?.commandRecord?.event_seq) : null;
			const message = uncertain ? `${labels.success} may still complete — waiting for the live fold` : `${labels.failure}: ${error?.message || error}`;
			onToast?.(message);
			setOptim((current) => {
				const entry = current[card.id];
				if (!entry) return current;
				const updates = { ...entry.updates || {} };
				if (!uncertain) delete updates[kind];
				return {
					...current,
					[card.id]: {
						...entry,
						updates,
						...commandEditSeq == null ? {} : { editEventSeq: commandEditSeq },
						pending: uncertain ? {
							kind,
							phase: "confirmation-unknown"
						} : null,
						notice: {
							tone: uncertain ? "pending" : "error",
							text: message
						}
					}
				};
			});
			return {
				kind: uncertain ? "pending" : "error",
				message
			};
		} finally {
			inFlight.current.delete(card.id);
		}
	};
	const projection = isRecord(state.cards_projection) ? state.cards_projection : null;
	const receipts = isRecord(projection?.items) ? projection.items : {};
	const lanes = _cardLanes(visibleCards);
	const total = _cardInt(projection?.total);
	const sub = total != null && total !== cards.length ? `${visibleCards.length} of ${total} public work items` : `${visibleCards.length} work item${visibleCards.length === 1 ? "" : "s"}`;
	// A card is born as a hypothesis (peer review): keep the "+ Add" belief affordance on the
	// authoritative Card board, not only the empty-Card fallback — otherwise the operator loses the
	// documented control the moment the first card exists. Wired to the same addHypothesis control.
	const canAdd = typeof runId === "string" && !!runId;
	const addCard = async () => {
		const s = addDraft.trim();
		if (!s) return;
		const feedback = await submitCommand(CONTROL.addHypothesis(runId, s), {
			success: "Card added",
			noop: "That hypothesis was already tracked",
			executing: "Card requested — waiting for the run",
			failure: "Could not add Card"
		}, onToast);
		if (feedback.kind === "success") setAddDraft("");
	};
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Cards",
		sub,
		onClose,
		wide: true,
		children: [
			/* @__PURE__ */ _jsx(_CardProjectionNotice, {
				projection,
				cards: visibleCards
			}),
			canAdd && /* @__PURE__ */ _jsxs("div", {
				className: "toolbar",
				style: {
					marginBottom: 10,
					gap: 6
				},
				children: [/* @__PURE__ */ _jsx("input", {
					className: "text",
					style: { flex: 1 },
					"aria-label": "New hypothesis",
					placeholder: "Pose a hypothesis to test (e.g. “target is right-skewed; a log transform helps”)",
					value: addDraft,
					onChange: (e) => setAddDraft(e.target.value),
					onKeyDown: (e) => {
						if (e.key === "Enter") addCard();
					}
				}), /* @__PURE__ */ _jsx("button", {
					className: "btn sm primary",
					onClick: addCard,
					disabled: !addDraft.trim(),
					children: "+ Add"
				})]
			}),
			/* @__PURE__ */ _jsx("div", {
				className: "card-board",
				role: "region",
				"aria-label": "Card lifecycle kanban",
				children: lanes.map(([key, label, hint]) => {
					const rows = visibleCards.filter((card) => _cardStatus(card) === key).sort(_cardOrder);
					const tone = _CARD_FROZEN_STATUSES.has(key) ? ` card-${key}` : "";
					const laneId = `card-lane-${encodeURIComponent(key)}`;
					return /* @__PURE__ */ _jsxs("section", {
						className: "card-col" + tone,
						"aria-labelledby": laneId,
						children: [
							/* @__PURE__ */ _jsxs("h3", {
								id: laneId,
								className: "card-col-h",
								title: hint,
								children: [
									label,
									" ",
									/* @__PURE__ */ _jsx("span", {
										className: "muted",
										children: rows.length
									})
								]
							}),
							rows.map((card) => /* @__PURE__ */ _jsx(_CardKanbanCard, {
								card,
								receipt: isRecord(receipts[card.id]) ? receipts[card.id] : null,
								controlState: optim[card.id],
								controlsLocked: globalPending && !optim[card.id]?.pending,
								onSelect,
								onClose,
								onControl: typeof runId === "string" && runId ? cardControl : null
							}, card.id)),
							rows.length === 0 && /* @__PURE__ */ _jsx("div", {
								className: "muted card-empty",
								children: "—"
							})
						]
					}, key);
				})
			})
		]
	});
}
const HYPOTHESIS_DELETE_STORAGE_PREFIX = "ll.hypothesis-delete.";
const HYPOTHESIS_DELETE_COMMAND_RE = /^cmd_[0-9a-f]{32}$/;
const HYPOTHESIS_DELETE_PENDING = new Set([
	"submitting",
	"accepted",
	"executing"
]);
const HYPOTHESIS_DELETE_TERMINAL = new Set([
	"succeeded",
	"noop",
	"failed",
	"timed_out",
	"rejected"
]);
const HYPOTHESIS_DELETE_STORED = new Set([
	...HYPOTHESIS_DELETE_PENDING,
	"failed",
	"timed_out",
	"rejected"
]);
const HYPOTHESIS_DELETE_KEYS = new Set([
	"runId",
	"expectedGeneration",
	"hypothesisId",
	"idempotencyKey",
	"commandId",
	"status",
	"updatedAt"
]);
const canonicalHypothesisId = (value) => typeof value === "string" && value === value.trim() && value.length > 0 && [...value].length <= 256 && !/\p{C}/u.test(value);
const ownHypothesisEntry = (collection, id) => collection && Object.hasOwn(collection, String(id)) ? collection[String(id)] : null;
const exactHypothesisDeleteRecord = (intent, record) => !!intent && isRecord(record) && typeof record.id === "string" && HYPOTHESIS_DELETE_COMMAND_RE.test(record.id) && (!intent.commandId || record.id === intent.commandId) && record.event_type === "hypothesis_updated" && record.run_generation === intent.expectedGeneration && isRecord(record.subject) && record.subject.kind === "hypothesis" && record.subject.id === intent.hypothesisId && record.subject.status === "deleted";
const hypothesisDeleteStorage = () => {
	try {
		return typeof sessionStorage === "undefined" ? null : sessionStorage;
	} catch {
		return null;
	}
};
const hypothesisDeleteStorageKey = (runId, generation) => HYPOTHESIS_DELETE_STORAGE_PREFIX + encodeURIComponent(`${String(runId || "")}\u0000${String(generation || "")}`);
const validHypothesisDeleteIntent = (value, runId, generation) => !!value && isRecord(value) && Object.keys(value).every((key) => HYPOTHESIS_DELETE_KEYS.has(key)) && value.runId === String(runId) && value.expectedGeneration === String(generation) && RUN_GENERATION_RE.test(value.expectedGeneration) && canonicalHypothesisId(value.hypothesisId) && typeof value.idempotencyKey === "string" && value.idempotencyKey.length > 0 && value.idempotencyKey.length <= 200 && !/[\u0000-\u001f\u007f]/.test(value.idempotencyKey) && typeof value.commandId === "string" && (!value.commandId || HYPOTHESIS_DELETE_COMMAND_RE.test(value.commandId)) && HYPOTHESIS_DELETE_STORED.has(value.status) && Number.isFinite(value.updatedAt);
function loadHypothesisDeleteIntent(runId, generation) {
	const storage = hypothesisDeleteStorage();
	if (!storage || !runId || !RUN_GENERATION_RE.test(String(generation || ""))) return null;
	try {
		const parsed = JSON.parse(storage.getItem(hypothesisDeleteStorageKey(runId, generation)) || "null");
		return validHypothesisDeleteIntent(parsed, runId, generation) ? parsed : null;
	} catch {
		return null;
	}
}
function inspectHypothesisDeleteRecovery(runId, generation) {
	const storage = hypothesisDeleteStorage();
	if (!storage || !runId || !RUN_GENERATION_RE.test(String(generation || ""))) {
		return {
			state: "unavailable",
			raw: null,
			key: null,
			intent: null
		};
	}
	const key = hypothesisDeleteStorageKey(runId, generation);
	try {
		const raw = storage.getItem(key);
		if (raw == null) return {
			state: "empty",
			raw: null,
			key,
			intent: null
		};
		const intent = loadHypothesisDeleteIntent(runId, generation);
		return intent ? {
			state: "valid",
			raw,
			key,
			intent
		} : {
			state: "damaged",
			raw,
			key,
			intent: null
		};
	} catch {
		return {
			state: "unavailable",
			raw: null,
			key,
			intent: null
		};
	}
}
function clearDamagedHypothesisDeleteRecovery(recovery) {
	const storage = hypothesisDeleteStorage();
	if (!storage || recovery?.state !== "damaged" || !recovery.key || typeof recovery.raw !== "string") return false;
	try {
		// Compare-and-clear only the unreadable envelope the operator inspected. A different tab or a
		// late command receipt wins the race and remains protected.
		if (storage.getItem(recovery.key) !== recovery.raw) return false;
		storage.removeItem(recovery.key);
		return storage.getItem(recovery.key) == null;
	} catch {
		return false;
	}
}
function saveHypothesisDeleteIntent(intent, expectedRaw) {
	const storage = hypothesisDeleteStorage();
	if (!storage || !validHypothesisDeleteIntent(intent, intent?.runId, intent?.expectedGeneration)) return null;
	try {
		const key = hypothesisDeleteStorageKey(intent.runId, intent.expectedGeneration);
		const raw = storage.getItem(key);
		if (expectedRaw !== undefined && raw !== expectedRaw) return null;
		const existing = loadHypothesisDeleteIntent(intent.runId, intent.expectedGeneration);
		// A corrupt/unknown recovery envelope may still describe an accepted destructive command. Never
		// overwrite it with a fresh identity; keep the surface fail-closed until storage is repaired.
		if (raw != null && !existing) return null;
		if (existing && (existing.idempotencyKey !== intent.idempotencyKey || existing.hypothesisId !== intent.hypothesisId || existing.commandId && existing.commandId !== intent.commandId)) return null;
		const serialized = JSON.stringify(intent);
		storage.setItem(key, serialized);
		const stored = loadHypothesisDeleteIntent(intent.runId, intent.expectedGeneration);
		return stored && stored.idempotencyKey === intent.idempotencyKey && stored.hypothesisId === intent.hypothesisId && stored.commandId === intent.commandId && storage.getItem(key) === serialized ? {
			storageKey: key,
			storageRaw: serialized
		} : null;
	} catch {
		return null;
	}
}
function clearHypothesisDeleteIntent(intent) {
	const storage = hypothesisDeleteStorage();
	if (!storage || !intent?.storageKey || typeof intent.storageRaw !== "string") return false;
	try {
		const key = hypothesisDeleteStorageKey(intent.runId, intent.expectedGeneration);
		if (intent.storageKey !== key || storage.getItem(key) !== intent.storageRaw) return false;
		storage.removeItem(key);
		return storage.getItem(key) == null;
	} catch {
		return false;
	}
}
function _HypothesisFallback({ state, runId, runGeneration, onSelect, onClose, onToast, onRecoveryReleased }) {
	const [draft, setDraft] = useState("");
	// Optimistic status overrides {id: 'abandoned'|'deleted'}: the run-state round-trip that reflects a
	// control event can lag (its SSE is buffered by a proxy), so apply the click to the board AT ONCE
	// instead of leaving it looking dead for up to a minute. The real fold catches up idempotently.
	const [optim, setOptim] = useState({});
	const [deleteIntents, setDeleteIntents] = useState(() => {
		const recovery = inspectHypothesisDeleteRecovery(runId, runGeneration);
		const restored = recovery.state === "valid" ? recovery.intent : null;
		return restored ? { [restored.hypothesisId]: {
			...restored,
			storageKey: recovery.key,
			storageRaw: recovery.raw,
			phase: "unknown",
			releaseAllowed: false,
			releaseInspected: false,
			message: restored.commandId ? "A saved permanent deletion needs recovery. Check this exact command before another action." : "A prior permanent deletion has an unknown outcome. Resume the exact saved request to recover it safely."
		} } : {};
	});
	const [deleteNotices, setDeleteNotices] = useState({});
	const [damagedRecovery, setDamagedRecovery] = useState(() => {
		const inspected = inspectHypothesisDeleteRecovery(runId, runGeneration);
		return inspected.state === "damaged" ? inspected : null;
	});
	const [damagedInspected, setDamagedInspected] = useState(false);
	const deleteIntentsRef = useRef(deleteIntents);
	deleteIntentsRef.current = deleteIntents;
	const deleteFlights = useRef(new Set());
	const activeRef = useRef(true);
	useEffect(() => {
		activeRef.current = true;
		return () => {
			activeRef.current = false;
		};
	}, []);
	const refreshDeleteRecovery = (fallback) => {
		const inspected = inspectHypothesisDeleteRecovery(runId, runGeneration);
		if (inspected.state === "valid") {
			const restored = {
				...inspected.intent,
				storageKey: inspected.key,
				storageRaw: inspected.raw,
				phase: "unknown",
				releaseAllowed: false,
				releaseInspected: false,
				message: inspected.intent.commandId ? "The saved recovery changed. Check its exact command before another action." : "The saved recovery changed. Resume its exact retained request before another action."
			};
			const collection = { [restored.hypothesisId]: restored };
			deleteIntentsRef.current = collection;
			if (activeRef.current) setDeleteIntents(collection);
			setDamagedRecovery(null);
			setDamagedInspected(false);
			return;
		}
		if (inspected.state === "damaged") {
			deleteIntentsRef.current = {};
			if (activeRef.current) setDeleteIntents({});
			setDamagedRecovery(inspected);
			setDamagedInspected(false);
			return;
		}
		const current = ownHypothesisEntry(deleteIntentsRef.current, fallback.hypothesisId);
		if (!current || current.idempotencyKey !== fallback.idempotencyKey) return;
		const retained = {
			...current,
			phase: "unknown",
			releaseAllowed: false,
			releaseInspected: false,
			message: "Recovery storage changed or became unavailable. No command was sent; keep this tab open and inspect recovery again."
		};
		const collection = {
			...deleteIntentsRef.current,
			[fallback.hypothesisId]: retained
		};
		deleteIntentsRef.current = collection;
		if (activeRef.current) setDeleteIntents(collection);
	};
	const updateDeleteIntent = (intent, patch, persist = true) => {
		const current = ownHypothesisEntry(deleteIntentsRef.current, intent.hypothesisId);
		if (!current || current.idempotencyKey !== intent.idempotencyKey || current.expectedGeneration !== intent.expectedGeneration) return null;
		const next = {
			...current,
			...patch,
			updatedAt: Date.now()
		};
		const storedSnapshot = persist ? saveHypothesisDeleteIntent({
			runId: next.runId,
			expectedGeneration: next.expectedGeneration,
			hypothesisId: next.hypothesisId,
			idempotencyKey: next.idempotencyKey,
			commandId: next.commandId || "",
			status: HYPOTHESIS_DELETE_STORED.has(next.status) ? next.status : "submitting",
			updatedAt: next.updatedAt
		}, current.storageRaw) : null;
		const durable = !persist || !!storedSnapshot;
		const presented = durable ? {
			...next,
			...storedSnapshot || {
				storageKey: current.storageKey,
				storageRaw: current.storageRaw
			}
		} : {
			...next,
			phase: "unknown",
			message: "The command was observed, but its updated recovery receipt could not be saved. Keep this tab open; recovery will reuse the original exact request identity."
		};
		if (!durable) {
			refreshDeleteRecovery(current);
			return null;
		}
		const collection = {
			...deleteIntentsRef.current,
			[intent.hypothesisId]: presented
		};
		deleteIntentsRef.current = collection;
		if (activeRef.current) setDeleteIntents(collection);
		return presented;
	};
	const dropDeleteIntent = (intent) => {
		const current = ownHypothesisEntry(deleteIntentsRef.current, intent.hypothesisId);
		if (!current || current.idempotencyKey !== intent.idempotencyKey) return false;
		if (!clearHypothesisDeleteIntent({
			...current,
			commandId: current.commandId || ""
		})) {
			updateDeleteIntent(current, {
				phase: "unknown",
				message: "The command settled, but its saved recovery identity could not be released. No new deletion will be sent."
			}, false);
			return false;
		}
		const collection = { ...deleteIntentsRef.current };
		delete collection[intent.hypothesisId];
		deleteIntentsRef.current = collection;
		if (activeRef.current) setDeleteIntents(collection);
		onRecoveryReleased?.();
		return true;
	};
	// Drop an optimistic override once the real fold REFLECTS it (deleted card gone from state; abandoned
	// card now status='abandoned'), so a stale override can't keep masking a LATER server-side reopen of
	// the same hypothesis while the board stays mounted.
	useEffect(() => {
		setOptim((o) => {
			const next = Object.create(null);
			for (const [id, v] of Object.entries(o)) {
				const h = ownHypothesisEntry(state.hypotheses, id);
				if (v === "deleted" && h) next[id] = v;
				else if (v === "abandoned" && h && h.status !== "abandoned") next[id] = v;
			}
			return next;
		});
	}, [state.hypotheses]);
	const hyps = Object.values(state.hypotheses || {}).filter((h) => ownHypothesisEntry(optim, h.id) !== "deleted").map((h) => {
		const status = ownHypothesisEntry(optim, h.id);
		return status ? {
			...h,
			status
		} : h;
	});
	// FOREAGENT board prioritization: order cards by predicted payoff (`priority`, 0 = best;
	// unranked cards last), so the kanban shows the sort the world model chose. `ranking` carries the
	// analysis trace (reason + confidence) surfaced as a header note and per-card tooltip.
	const ranking = state.hypothesis_ranking || null;
	const rankConf = ranking && typeof ranking.confidence === "number" ? Math.round(ranking.confidence * 100) : null;
	const byStatus = (s) => hyps.filter((h) => (h.status || "open") === s).sort((a, b) => (a.priority ?? Infinity) - (b.priority ?? Infinity));
	const pendingDelete = Object.values(deleteIntents)[0] || null;
	const deleteLocked = !!pendingDelete || !!damagedRecovery;
	const add = async () => {
		const s = draft.trim();
		if (!s || deleteLocked) return;
		const feedback = await submitCommand(CONTROL.addHypothesis(runId, s), {
			success: "Hypothesis added",
			noop: "That hypothesis was already tracked",
			executing: "Hypothesis requested — waiting for the run",
			failure: "Could not add hypothesis"
		}, onToast);
		if (feedback.kind === "success") setDraft("");
	};
	const _revert = (id) => setOptim((o) => {
		const n = { ...o };
		delete n[id];
		return n;
	});
	const abandon = async (h) => {
		if (deleteLocked) {
			onToast?.("Finish checking the pending permanent deletion before another hypothesis command.");
			return;
		}
		setOptim((o) => ({
			...o,
			[h.id]: "abandoned"
		}));
		const feedback = await submitCommand(CONTROL.abandonHypothesis(runId, h.id), {
			success: "Hypothesis abandoned",
			noop: "Hypothesis was already abandoned",
			executing: "Abandon requested — waiting for the run",
			failure: "Could not update hypothesis"
		}, onToast);
		// NOT `kind === 'error'`: a still-`executing` command has not abandoned anything yet, so the
		// optimistic strike-through would be showing an outcome the run may still refuse.
		if (feedback.kind !== "success") _revert(h.id);
	};
	const deleteFailure = (intent, message) => {
		dropDeleteIntent(intent);
		if (!activeRef.current) return;
		setDeleteNotices((current) => ({
			...current,
			[intent.hypothesisId]: message
		}));
		onToast?.(message);
	};
	const retainDeleteFailure = (intent, record, message) => {
		const retryable = ["failed", "timed_out"].includes(record?.status) && record?.error?.retryable === true;
		updateDeleteIntent(intent, {
			commandId: record.id,
			status: record.status,
			phase: retryable ? "retryable" : "terminal",
			message,
			releaseAllowed: !retryable,
			releaseInspected: false
		});
		if (activeRef.current) onToast?.(message);
	};
	const deleteSuccess = (intent, message) => {
		dropDeleteIntent(intent);
		if (!activeRef.current) return;
		setOptim((current) => ({
			...current,
			[intent.hypothesisId]: "deleted"
		}));
		setDeleteNotices((current) => {
			const next = { ...current };
			delete next[intent.hypothesisId];
			return next;
		});
		onToast?.(message);
	};
	const observeDeleteRecord = (intent, record) => {
		if (!record || typeof record.id !== "string" || !HYPOTHESIS_DELETE_COMMAND_RE.test(record.id) || !exactHypothesisDeleteRecord(intent, record)) return null;
		if (!HYPOTHESIS_DELETE_PENDING.has(record.status)) return record;
		const message = record.status === "executing" ? "Permanent deletion is executing — waiting for the run." : "Permanent deletion was accepted — waiting for the run.";
		return updateDeleteIntent(intent, {
			commandId: record.id,
			status: record.status,
			phase: "pending",
			message,
			releaseAllowed: false,
			releaseInspected: false
		}) ? record : null;
	};
	const submitDelete = async (intent) => {
		const hypothesisId = intent.hypothesisId;
		const current = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId);
		if (deleteFlights.current.has(hypothesisId) || !current || current.idempotencyKey !== intent.idempotencyKey || current.expectedGeneration !== intent.expectedGeneration) return;
		const durableSubmission = updateDeleteIntent(current, {
			commandId: current.commandId || "",
			status: current.status || "submitting",
			phase: "submitting",
			message: "Submitting the exact saved permanent deletion…",
			releaseAllowed: false,
			releaseInspected: false
		});
		if (!durableSubmission) return;
		deleteFlights.current.add(hypothesisId);
		try {
			const record = await runCommand(intent.runId, "hypothesis_updated", {
				id: intent.hypothesisId,
				status: "deleted"
			}, {
				expectedGeneration: intent.expectedGeneration,
				idempotencyKey: intent.idempotencyKey,
				onRecord: (next) => {
					const latest = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId);
					if (!latest || latest.idempotencyKey !== intent.idempotencyKey) return;
					observeDeleteRecord(intent, next);
				}
			});
			const currentIntent = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId);
			if (!currentIntent || currentIntent.idempotencyKey !== intent.idempotencyKey) return;
			if (!record || !exactHypothesisDeleteRecord(currentIntent, record)) {
				const current = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId);
				const observedCommandId = typeof record?.id === "string" && HYPOTHESIS_DELETE_COMMAND_RE.test(record.id) ? record.id : "";
				const terminalMismatch = !!record && HYPOTHESIS_DELETE_TERMINAL.has(record.status) && !!observedCommandId && (!current?.commandId || current.commandId === observedCommandId);
				updateDeleteIntent(intent, {
					commandId: current?.commandId || observedCommandId,
					phase: "unknown",
					releaseAllowed: terminalMismatch,
					releaseInspected: false,
					message: "The delete command receipt did not prove this exact run generation and hypothesis. It remains quarantined."
				});
				if (activeRef.current) onToast?.("Permanent deletion outcome is unknown. The receipt did not prove the exact target.");
				return;
			}
			const feedback = commandFeedback(record, {
				success: "Hypothesis deleted",
				noop: "Hypothesis was already deleted",
				executing: "Delete requested — waiting for the run",
				failure: "Could not delete hypothesis"
			});
			if (feedback.kind === "success") deleteSuccess(intent, feedback.message);
			else if (feedback.kind === "pending") {
				observeDeleteRecord(intent, record);
				if (activeRef.current) onToast?.(feedback.message);
			} else if (record.status === "rejected") deleteFailure(intent, feedback.message);
			else retainDeleteFailure(intent, record, feedback.message);
		} catch (error) {
			const latestIntent = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId);
			if (!latestIntent || latestIntent.idempotencyKey !== intent.idempotencyKey) return;
			const record = error?.commandRecord;
			const recoveryIntent = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId);
			if (record && !exactHypothesisDeleteRecord(recoveryIntent, record)) {
				const savedCommandId = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)?.commandId || "";
				const commandId = savedCommandId || (typeof record.id === "string" && HYPOTHESIS_DELETE_COMMAND_RE.test(record.id) ? record.id : "");
				const transportUnknown = error?.submissionMayHaveSucceeded === true || error?.commandUnknown === true || isTransientCommandReadError(error);
				const terminalMismatch = HYPOTHESIS_DELETE_TERMINAL.has(record.status) && (!savedCommandId || savedCommandId === record.id);
				const message = transportUnknown ? "The exact command is temporarily unavailable. Its identity remains quarantined; check it again." : "A command receipt was returned, but it did not prove this exact run generation and hypothesis. The deletion remains quarantined.";
				updateDeleteIntent(intent, {
					commandId,
					phase: "unknown",
					message,
					releaseAllowed: !transportUnknown && terminalMismatch,
					releaseInspected: false
				});
				if (activeRef.current) onToast?.(message);
				return;
			}
			if (record && ["failed", "timed_out"].includes(record.status)) {
				const feedback = commandFeedback(record, { failure: "Could not delete hypothesis" });
				retainDeleteFailure(intent, record, feedback.message);
				return;
			}
			if (record?.status === "rejected") {
				const feedback = commandFeedback(record, { failure: "Could not delete hypothesis" });
				deleteFailure(intent, feedback.message);
				return;
			}
			const pendingRecord = HYPOTHESIS_DELETE_PENDING.has(record?.status) && typeof record?.id === "string" && HYPOTHESIS_DELETE_COMMAND_RE.test(record.id) && exactHypothesisDeleteRecord(recoveryIntent, record);
			const errorCommandId = [error?.commandId].map((value) => String(value || "")).find((value) => HYPOTHESIS_DELETE_COMMAND_RE.test(value)) || "";
			const ambiguous = pendingRecord || error?.submissionMayHaveSucceeded === true || error?.commandUnknown === true || isTransientCommandReadError(error);
			if (ambiguous) {
				const commandId = pendingRecord ? record.id : ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)?.commandId || errorCommandId;
				const status = pendingRecord ? record.status : ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)?.status || "submitting";
				const message = commandId ? "Permanent deletion may still complete. Check this exact saved command; it was not replayed." : "Permanent deletion outcome is unknown. Resume this exact saved request; it reuses the same identity and cannot create a second logical deletion.";
				updateDeleteIntent(intent, {
					commandId,
					status,
					phase: "unknown",
					message,
					releaseAllowed: false,
					releaseInspected: false
				});
				if (activeRef.current) onToast?.(message);
			} else if (errorCommandId) {
				const message = "The exact command identity was returned, but its target outcome could not be proved. Inspect it before releasing recovery.";
				updateDeleteIntent(intent, {
					commandId: ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)?.commandId || errorCommandId,
					phase: "unknown",
					message,
					releaseAllowed: false,
					releaseInspected: false
				});
				if (activeRef.current) onToast?.(message);
			} else {
				const feedback = record ? commandFeedback(record, { failure: "Could not delete hypothesis" }) : null;
				deleteFailure(intent, feedback?.message || `Could not delete hypothesis: ${error?.message || error}`);
			}
		} finally {
			deleteFlights.current.delete(hypothesisId);
		}
	};
	const del = async (h) => {
		const hypothesisId = String(h.id);
		if (!canonicalHypothesisId(hypothesisId)) {
			const message = "This hypothesis has a non-canonical id and cannot be targeted safely. Refresh or repair the run record before deleting it.";
			setDeleteNotices((current) => ({
				...current,
				[hypothesisId]: message
			}));
			onToast?.(message);
			return;
		}
		if (deleteFlights.current.has(hypothesisId) || ownHypothesisEntry(deleteIntentsRef.current, hypothesisId) || Object.keys(deleteIntentsRef.current).length > 0) {
			onToast?.("A permanent hypothesis deletion is already pending. Recover that exact command first.");
			return;
		}
		if (!runId || !RUN_GENERATION_RE.test(String(runGeneration || ""))) {
			const message = "The displayed run generation is unavailable. Refresh the run before deleting a hypothesis.";
			setDeleteNotices((current) => ({
				...current,
				[hypothesisId]: message
			}));
			onToast?.(message);
			return;
		}
		const statement = String(h.statement || "").trim();
		if (!window.confirm(`Delete this hypothesis permanently?\n\n${statement.slice(0, 500)}\n\nThis removes it from the board and cannot be undone.`)) return;
		const intent = {
			runId: String(runId),
			expectedGeneration: String(runGeneration),
			hypothesisId,
			idempotencyKey: createIdempotencyKey(),
			commandId: "",
			status: "submitting",
			updatedAt: Date.now(),
			phase: "submitting",
			message: "Submitting one permanent deletion…"
		};
		const stored = {
			runId: intent.runId,
			expectedGeneration: intent.expectedGeneration,
			hypothesisId: intent.hypothesisId,
			idempotencyKey: intent.idempotencyKey,
			commandId: "",
			status: "submitting",
			updatedAt: intent.updatedAt
		};
		// Commit the exact operation identity before POST. If tab-scoped storage is unavailable, fail
		// closed: an unremembered destructive request could be replayed after close/reload.
		const storedSnapshot = saveHypothesisDeleteIntent(stored, null);
		if (!storedSnapshot) {
			const message = "Permanent deletion was not sent because this browser could not retain its recovery identity.";
			setDeleteNotices((current) => ({
				...current,
				[hypothesisId]: message
			}));
			onToast?.(message);
			return;
		}
		const retainedIntent = {
			...intent,
			...storedSnapshot
		};
		deleteIntentsRef.current = { [hypothesisId]: retainedIntent };
		setDeleteIntents(deleteIntentsRef.current);
		setDeleteNotices((current) => {
			const next = { ...current };
			delete next[hypothesisId];
			return next;
		});
		await submitDelete(retainedIntent);
	};
	const resumeDelete = async (intent) => {
		const current = ownHypothesisEntry(deleteIntentsRef.current, intent?.hypothesisId);
		if (!current || current.commandId || deleteFlights.current.has(current.hypothesisId) || current.idempotencyKey !== intent.idempotencyKey) return;
		if (!window.confirm("Resume the exact saved permanent deletion?\n\nThis reuses the original idempotency identity and payload. It cannot create a second logical deletion.")) return;
		await submitDelete(current);
	};
	const checkDelete = async (intent) => {
		if (!intent?.commandId || deleteFlights.current.has(intent.hypothesisId)) return;
		const durableCheck = updateDeleteIntent(intent, {
			phase: "checking",
			message: "Checking the exact delete command…",
			releaseAllowed: false,
			releaseInspected: false
		});
		if (!durableCheck) return;
		deleteFlights.current.add(intent.hypothesisId);
		try {
			const record = await getRunCommand(intent.runId, intent.commandId, { requestTimeoutMs: PANEL_REQUEST_TIMEOUT_MS });
			const current = ownHypothesisEntry(deleteIntentsRef.current, intent.hypothesisId);
			if (!activeRef.current || !current || current.idempotencyKey !== intent.idempotencyKey || current.commandId !== intent.commandId) return;
			if (!exactHypothesisDeleteRecord(intent, record)) {
				const terminalMismatch = HYPOTHESIS_DELETE_TERMINAL.has(record?.status) && record?.id === intent.commandId;
				updateDeleteIntent(intent, {
					phase: "unknown",
					releaseAllowed: terminalMismatch,
					releaseInspected: false,
					message: "The saved command did not prove this exact run generation and hypothesis. It remains quarantined."
				});
				return;
			}
			const feedback = commandFeedback(record, {
				success: "Hypothesis deleted",
				noop: "Hypothesis was already deleted",
				executing: "Delete requested — waiting for the run",
				failure: "Could not delete hypothesis"
			});
			if (feedback.kind === "success") deleteSuccess(intent, feedback.message);
			else if (feedback.kind === "pending") {
				observeDeleteRecord(intent, record);
				onToast?.(feedback.message);
			} else if (record.status === "rejected") deleteFailure(intent, feedback.message);
			else retainDeleteFailure(intent, record, feedback.message);
		} catch (error) {
			const current = ownHypothesisEntry(deleteIntentsRef.current, intent.hypothesisId);
			if (!activeRef.current || !current || current.idempotencyKey !== intent.idempotencyKey || current.commandId !== intent.commandId) return;
			const message = isTransientCommandReadError(error) ? "The exact delete command is temporarily unavailable. Its identity is retained; try checking again." : "The exact delete command could not be verified. Its identity remains quarantined; no delete was replayed.";
			updateDeleteIntent(intent, {
				phase: "unknown",
				message,
				releaseAllowed: !isTransientCommandReadError(error) && ([403, 404].includes(Number(error?.status)) || error?.code === "COMMAND_PROTOCOL_ERROR"),
				releaseInspected: false
			});
			onToast?.(message);
		} finally {
			deleteFlights.current.delete(intent.hypothesisId);
		}
	};
	const retryDelete = async (intent) => {
		const current = ownHypothesisEntry(deleteIntentsRef.current, intent?.hypothesisId);
		if (!current || current.phase !== "retryable" || !current.commandId || deleteFlights.current.has(current.hypothesisId) || current.idempotencyKey !== intent.idempotencyKey) return;
		if (!window.confirm("Retry this exact failed permanent-deletion command?\n\nThis reuses the same durable command id; it does not submit a new delete intent.")) return;
		const durableRetry = updateDeleteIntent(current, {
			phase: "retrying",
			releaseAllowed: false,
			releaseInspected: false,
			message: "Retrying the exact durable delete command…"
		});
		if (!durableRetry) return;
		deleteFlights.current.add(current.hypothesisId);
		try {
			const record = await retryRunCommand(current.runId, current.commandId, {
				requestTimeoutMs: PANEL_REQUEST_TIMEOUT_MS,
				onRecord: (next) => {
					const latest = ownHypothesisEntry(deleteIntentsRef.current, current.hypothesisId);
					if (!latest || latest.idempotencyKey !== current.idempotencyKey) return;
					observeDeleteRecord(latest, next);
				}
			});
			const latest = ownHypothesisEntry(deleteIntentsRef.current, current.hypothesisId);
			if (!activeRef.current || !latest || latest.idempotencyKey !== current.idempotencyKey) return;
			if (!exactHypothesisDeleteRecord(latest, record)) {
				const message = "The retry receipt did not prove this exact run generation and hypothesis. Recovery remains quarantined.";
				updateDeleteIntent(latest, {
					phase: "unknown",
					message,
					releaseAllowed: HYPOTHESIS_DELETE_TERMINAL.has(record?.status) && record?.id === latest.commandId,
					releaseInspected: false
				});
				onToast?.(message);
				return;
			}
			const feedback = commandFeedback(record, {
				success: "Hypothesis deleted",
				noop: "Hypothesis was already deleted",
				executing: "Delete retry is still executing",
				failure: "Could not delete hypothesis"
			});
			if (feedback.kind === "success") deleteSuccess(latest, feedback.message);
			else if (feedback.kind === "pending") {
				observeDeleteRecord(latest, record);
				onToast?.(feedback.message);
			} else if (record.status === "rejected") deleteFailure(latest, feedback.message);
			else retainDeleteFailure(latest, record, feedback.message);
		} catch (error) {
			const latest = ownHypothesisEntry(deleteIntentsRef.current, current.hypothesisId);
			if (!activeRef.current || !latest || latest.idempotencyKey !== current.idempotencyKey) return;
			const message = isTransientCommandReadError(error) || error?.submissionMayHaveSucceeded === true ? "The exact retry outcome is temporarily unavailable. Its command identity remains retained." : "The exact retry could not be verified. Inspect the saved command before releasing recovery.";
			updateDeleteIntent(latest, {
				phase: "unknown",
				message,
				releaseAllowed: false,
				releaseInspected: false
			});
			onToast?.(message);
		} finally {
			deleteFlights.current.delete(current.hypothesisId);
		}
	};
	const releaseValidRecovery = (intent) => {
		const current = ownHypothesisEntry(deleteIntentsRef.current, intent?.hypothesisId);
		if (!current || !current.releaseAllowed || !current.releaseInspected || current.idempotencyKey !== intent.idempotencyKey) return;
		if (!window.confirm("Release this exact permanent-deletion recovery identity?\n\nThis sends no command. Only continue after inspecting the run and accepting that this old outcome cannot be proved.")) return;
		if (!clearHypothesisDeleteIntent({
			...current,
			commandId: current.commandId || ""
		})) {
			updateDeleteIntent(current, {
				phase: "unknown",
				releaseAllowed: false,
				releaseInspected: false,
				message: "The saved recovery identity changed or could not be released. It remains protected."
			}, false);
			onToast?.("The exact recovery identity changed or could not be released.");
			return;
		}
		const collection = { ...deleteIntentsRef.current };
		delete collection[current.hypothesisId];
		deleteIntentsRef.current = collection;
		setDeleteIntents(collection);
		onRecoveryReleased?.();
		onToast?.("The exact recovery identity was released. No deletion was sent.");
	};
	const releaseDamagedRecovery = () => {
		if (!damagedRecovery || !damagedInspected) return;
		if (!window.confirm("Release this exact unreadable recovery record?\n\nOnly continue after inspecting the current run state and confirming that no permanent deletion still needs recovery.")) return;
		if (!clearDamagedHypothesisDeleteRecovery(damagedRecovery)) {
			onToast?.("The recovery record changed or could not be released. It remains protected; inspect it again.");
			const refreshed = inspectHypothesisDeleteRecovery(runId, runGeneration);
			if (refreshed.state === "valid") {
				const restored = {
					...refreshed.intent,
					phase: "unknown",
					storageKey: refreshed.key,
					storageRaw: refreshed.raw,
					releaseAllowed: false,
					releaseInspected: false,
					message: refreshed.intent.commandId ? "The recovery record changed to a valid permanent deletion. Check its exact command." : "The recovery record changed to a valid id-less deletion. Resume its exact saved request."
				};
				deleteIntentsRef.current = { [restored.hypothesisId]: restored };
				setDeleteIntents(deleteIntentsRef.current);
				setDamagedRecovery(null);
			} else if (refreshed.state === "damaged") setDamagedRecovery(refreshed);
			setDamagedInspected(false);
			return;
		}
		setDamagedRecovery(null);
		setDamagedInspected(false);
		onToast?.("The exact damaged recovery record was released. No deletion was sent.");
		onRecoveryReleased?.();
	};
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Hypotheses",
		sub: `${hyps.length} tracked — what the run is trying to learn`,
		onClose,
		wide: true,
		children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "toolbar",
				style: {
					marginBottom: 10,
					gap: 6
				},
				children: [/* @__PURE__ */ _jsx("input", {
					className: "text",
					style: { flex: 1 },
					"aria-label": "New hypothesis",
					placeholder: "Pose a hypothesis to test (e.g. “target is right-skewed; a log transform helps”)",
					value: draft,
					disabled: deleteLocked,
					onChange: (e) => setDraft(e.target.value),
					onKeyDown: (e) => {
						if (e.key === "Enter") add();
					}
				}), /* @__PURE__ */ _jsx("button", {
					className: "btn sm primary",
					onClick: add,
					disabled: !draft.trim() || deleteLocked,
					children: "+ Add"
				})]
			}),
			pendingDelete && /* @__PURE__ */ _jsxs("div", {
				className: "report-inline-state",
				role: "status",
				style: { marginBottom: 10 },
				children: [
					/* @__PURE__ */ _jsx(OpIcon, {
						name: "alert",
						size: 14
					}),
					/* @__PURE__ */ _jsx("span", { children: pendingDelete.message }),
					pendingDelete.commandId && /* @__PURE__ */ _jsx("button", {
						className: "btn sm",
						onClick: () => checkDelete(pendingDelete),
						disabled: [
							"checking",
							"retrying",
							"submitting"
						].includes(pendingDelete.phase),
						children: pendingDelete.phase === "checking" ? "Checking…" : "Check exact command"
					}),
					pendingDelete.commandId && pendingDelete.phase === "retryable" && /* @__PURE__ */ _jsx("button", {
						className: "btn sm",
						onClick: () => retryDelete(pendingDelete),
						children: "Retry exact command"
					}),
					!pendingDelete.commandId && /* @__PURE__ */ _jsx("button", {
						className: "btn sm",
						onClick: () => resumeDelete(pendingDelete),
						disabled: pendingDelete.phase === "submitting",
						children: pendingDelete.phase === "submitting" ? "Submitting…" : "Resume exact request"
					}),
					pendingDelete.releaseAllowed && !pendingDelete.releaseInspected && /* @__PURE__ */ _jsx("button", {
						className: "btn sm",
						onClick: () => updateDeleteIntent(pendingDelete, { releaseInspected: true }, false),
						children: "Inspect recovery"
					}),
					pendingDelete.releaseAllowed && pendingDelete.releaseInspected && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsxs("span", {
						className: "muted",
						children: [
							"Run ",
							pendingDelete.runId,
							"; generation ",
							pendingDelete.expectedGeneration.slice(0, 12),
							"…; hypothesis ",
							pendingDelete.hypothesisId,
							"; command ",
							pendingDelete.commandId || "not recorded",
							"."
						]
					}), /* @__PURE__ */ _jsx("button", {
						className: "btn sm danger",
						onClick: () => releaseValidRecovery(pendingDelete),
						children: "Release exact recovery"
					})] })
				]
			}),
			damagedRecovery && /* @__PURE__ */ _jsxs("div", {
				className: "report-inline-state error",
				role: "alert",
				style: { marginBottom: 10 },
				children: [
					/* @__PURE__ */ _jsx(OpIcon, {
						name: "alert",
						size: 14
					}),
					/* @__PURE__ */ _jsx("span", { children: "An unreadable permanent-deletion recovery record exists for this exact run generation. Destructive controls stay locked until it is inspected and explicitly released." }),
					!damagedInspected ? /* @__PURE__ */ _jsx("button", {
						className: "btn sm",
						onClick: () => setDamagedInspected(true),
						children: "Inspect recovery"
					}) : /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsxs("span", {
						className: "muted",
						children: [
							"Run ",
							runId,
							"; generation ",
							String(runGeneration).slice(0, 12),
							"…; stored record ",
							damagedRecovery.raw.length,
							" bytes. Its command identity cannot be verified."
						]
					}), /* @__PURE__ */ _jsx("button", {
						className: "btn sm danger",
						onClick: releaseDamagedRecovery,
						children: "Release exact record"
					})] })
				]
			}),
			ranking && /* @__PURE__ */ _jsxs("div", {
				className: "muted",
				style: {
					marginBottom: 8,
					fontSize: 12,
					display: "flex",
					gap: 6,
					alignItems: "baseline"
				},
				title: ranking.reason || "predicted before execution",
				children: [/* @__PURE__ */ _jsx(OpIcon, {
					name: "bulb",
					size: 11
				}), /* @__PURE__ */ _jsxs("span", { children: [
					"Predicted priority order (FOREAGENT",
					rankConf != null ? `, ${rankConf}% confidence` : "",
					")",
					ranking.reason ? `: ${ranking.reason}` : ""
				] })]
			}),
			hyps.length === 0 ? /* @__PURE__ */ _jsxs("div", {
				className: "muted",
				children: [
					"No hypotheses yet. The Researcher states one per experiment (its",
					/* @__PURE__ */ _jsx("code", { children: " hypothesis" }),
					" field); deep-research directions and your “+ Add” questions land here too, then get tracked to a verdict as experiments run."
				]
			}) : /* @__PURE__ */ _jsx("div", {
				className: "hyp-board",
				children: _HYP_COLUMNS.map(([key, label, hint]) => {
					const col = byStatus(key);
					return /* @__PURE__ */ _jsxs("div", {
						className: "hyp-col hyp-" + key,
						children: [
							/* @__PURE__ */ _jsxs("div", {
								className: "hyp-col-h",
								title: hint,
								children: [
									label,
									" ",
									/* @__PURE__ */ _jsx("span", {
										className: "muted",
										children: col.length
									})
								]
							}),
							col.map((h) => {
								const deletion = ownHypothesisEntry(deleteIntents, h.id);
								const deleteNotice = ownHypothesisEntry(deleteNotices, h.id) || "";
								return /* @__PURE__ */ _jsxs("div", {
									className: "hyp-card",
									children: [
										/* @__PURE__ */ _jsxs("div", {
											className: "hyp-stmt",
											children: [
												/* @__PURE__ */ _jsx("span", {
													className: "hyp-src",
													title: `source: ${h.source}`,
													children: /* @__PURE__ */ _jsx(OpIcon, {
														name: _HYP_ICON[h.source] || "dot",
														size: 12
													})
												}),
												" ",
												h.statement
											]
										}),
										/* @__PURE__ */ _jsxs("div", {
											className: "hyp-meta",
											children: [
												h.priority != null && /* @__PURE__ */ _jsxs("span", {
													className: "chip xs",
													title: "predicted priority " + (h.priority + 1) + (rankConf != null ? ` · ${rankConf}% confidence` : "") + (ranking && ranking.reason ? ` · ${ranking.reason}` : ""),
													children: ["#", h.priority + 1]
												}),
												(h.evidence || []).slice(0, 8).map((nid) => /* @__PURE__ */ _jsxs("button", {
													className: "btn xs ghost",
													title: `experiment #${nid}`,
													onClick: () => {
														onSelect && onSelect(nid);
														onClose();
													},
													children: ["#", nid]
												}, nid)),
												h.best_delta != null && /* @__PURE__ */ _jsxs("span", {
													className: "chip xs " + (h.best_delta > 0 ? "ok" : ""),
													title: "best improvement over parent among the evidence",
													children: ["Δ", fmt(h.best_delta)]
												}),
												key !== "abandoned" && /* @__PURE__ */ _jsx("button", {
													className: "btn xs ghost",
													title: "abandon — move to the Abandoned column (keeps the record)",
													disabled: deleteLocked,
													onClick: () => abandon(h),
													children: /* @__PURE__ */ _jsx(OpIcon, {
														name: "cross",
														size: 11
													})
												}),
												/* @__PURE__ */ _jsx("button", {
													className: "btn xs ghost danger",
													title: "delete this hypothesis permanently (remove from the board)",
													disabled: deleteLocked,
													"aria-label": `Delete hypothesis ${h.id} permanently`,
													onClick: () => del(h),
													children: deletion ? "Deleting…" : "Delete"
												})
											]
										}),
										deleteNotice && /* @__PURE__ */ _jsxs("div", {
											className: "report-inline-state error",
											role: "alert",
											children: [/* @__PURE__ */ _jsx(OpIcon, {
												name: "alert",
												size: 14
											}), /* @__PURE__ */ _jsx("span", { children: deleteNotice })]
										})
									]
								}, h.id);
							}),
							col.length === 0 && /* @__PURE__ */ _jsx("div", {
								className: "muted hyp-empty",
								children: "—"
							})
						]
					}, key);
				})
			})
		]
	});
}
export function HypothesisBoard({ state, runId, runGeneration, onSelect, onClose, onToast }) {
	const [, setRecoveryEpoch] = useState(0);
	const cards = _cardRows(state);
	const projection = isRecord(state?.cards_projection) ? state.cards_projection : null;
	// A non-empty/omitted/invalid Card projection is authoritative. With no Cards at all, preserve the
	// hypothesis add/abandon workflow for older logs and for a run before its first Card is minted.
	// Both operator affordances now live on the authoritative Card board too: `+ Add` (below) and
	// `Abandon this Card` (the per-card `abandon` control emitting hypothesis_updated(status=abandoned)),
	// so an ordinary cards-only run — where this fallback is unmounted after the first Card — still
	// exposes them; this fallback remains only for the pre-first-Card / legacy-hypotheses shape.
	const hasAuthoritativeCards = cards.length > 0 || (_cardInt(projection?.total) ?? 0) > 0 || projection?.source_valid === false;
	// Card ids can repeat across runs and after an in-place reset. Remount every optimistic/ref tracker
	// at that exact scope boundary; the child also ignores completions after unmount.
	const scopeKey = `${runId || ""}:${runGeneration || ""}`;
	const recovery = inspectHypothesisDeleteRecovery(runId, runGeneration);
	const recoveryVisible = recovery.state === "valid" || recovery.state === "damaged";
	return hasAuthoritativeCards && !recoveryVisible ? /* @__PURE__ */ _jsx(_CardKanban, {
		state,
		cards,
		runId,
		onSelect,
		onClose,
		onToast
	}, `cards:${scopeKey}`) : /* @__PURE__ */ _jsx(_HypothesisFallback, {
		state,
		runId,
		runGeneration,
		onSelect,
		onClose,
		onToast,
		onRecoveryReleased: () => setRecoveryEpoch((value) => value + 1)
	}, `hypotheses:${scopeKey}`);
}
// Module scope so their identity is stable across SSE frames (ComparePanel re-renders on every live
// fold); defined inline they remounted the <select> each frame, closing an open dropdown mid-pick.
function CmpSel({ label, v, set, ids, nodes, currentAttempt = null, selectRef = null }) {
	return /* @__PURE__ */ _jsxs("label", {
		className: "cmp-select",
		children: [/* @__PURE__ */ _jsx("span", { children: label }), /* @__PURE__ */ _jsx("select", {
			ref: selectRef,
			className: "text",
			value: v ?? "",
			"aria-label": label,
			onChange: (e) => set(Number(e.target.value)),
			children: ids.map((i) => {
				const attempt = i === v && Number.isSafeInteger(currentAttempt) ? currentAttempt : nodes?.[i]?.attempt;
				return /* @__PURE__ */ _jsxs("option", {
					value: i,
					children: [
						"#",
						i,
						" · attempt ",
						Number.isSafeInteger(attempt) ? attempt : "unknown"
					]
				}, i);
			})
		})]
	});
}
function useNodeResource({ runId, stateRunId, nodeId, expectedGeneration, expectedAttempt, reviewMode }) {
	const accessMode = reviewMode ? "review" : "owner";
	const identityReady = String(stateRunId || "") === String(runId || "") && RUN_GENERATION_RE.test(expectedGeneration || "") && Number.isSafeInteger(nodeId) && nodeId >= 0 && Number.isSafeInteger(expectedAttempt) && expectedAttempt >= 0;
	const scope = JSON.stringify([
		String(runId || ""),
		String(stateRunId || ""),
		expectedGeneration || "",
		nodeId,
		expectedAttempt,
		accessMode
	]);
	const [resource, setResource] = useState({
		scope: null,
		status: "idle",
		data: null,
		error: null,
		retryable: false
	});
	const [nonce, setNonce] = useState(0);
	useEffect(() => {
		if (nodeId == null) {
			setResource({
				scope,
				status: "idle",
				data: null,
				error: null,
				retryable: false
			});
			return undefined;
		}
		if (!identityReady) {
			setResource({
				scope,
				status: "waiting",
				data: null,
				error: null,
				retryable: false
			});
			return undefined;
		}
		let alive = true;
		const requested = {
			runId: String(runId),
			nodeId,
			generation: expectedGeneration,
			attempt: expectedAttempt,
			accessMode
		};
		setResource({
			scope,
			status: "loading",
			data: null,
			error: null,
			retryable: false
		});
		const suffix = `?expected_generation=${encodeURIComponent(requested.generation)}`;
		const request = deadlineGet(runNodeApiPath(requested.runId, requested.nodeId, suffix), PANEL_REQUEST_TIMEOUT_MS);
		request.promise.then((data) => {
			if (!alive) return;
			const responseAttemptValid = Number.isSafeInteger(data?.attempt) && data.attempt >= 0;
			const attemptMatches = responseAttemptValid && (requested.accessMode === "review" ? data.attempt === requested.attempt : data.attempt >= requested.attempt);
			const valid = isRecord(data) && String(data.id) === String(requested.nodeId) && typeof data.status === "string" && data.run_generation === requested.generation && attemptMatches;
			setResource(valid ? {
				scope,
				status: "ready",
				data,
				error: null,
				retryable: false
			} : {
				scope,
				status: "error",
				data: null,
				error: "The run or experiment attempt changed while details were loading.",
				retryable: false
			});
		}, (error) => {
			if (!alive || error?.name === "AbortError") return;
			const identityChanged = [
				"invalid_run_generation",
				"run_generation_changed",
				"run_generation_unavailable"
			].includes(error?.code);
			const httpStatus = Number(error?.status);
			const retryable = !identityChanged && (error?.name === "TimeoutError" || error?.status == null || httpStatus === 0 || httpStatus === 408 || httpStatus === 429 || httpStatus >= 500);
			setResource({
				scope,
				status: "error",
				data: null,
				error: identityChanged ? "The run changed while details were loading." : error?.name === "TimeoutError" ? "Detail loading timed out." : "Full details could not be loaded.",
				retryable
			});
		});
		return () => {
			alive = false;
			request.controller.abort();
		};
	}, [
		runId,
		stateRunId,
		nodeId,
		expectedGeneration,
		expectedAttempt,
		accessMode,
		identityReady,
		scope,
		nonce
	]);
	const current = resource.scope === scope ? resource : {
		scope,
		status: nodeId == null ? "idle" : identityReady ? "loading" : "waiting",
		data: null,
		error: null,
		retryable: false
	};
	const retry = () => {
		setResource((previous) => previous.scope === scope ? {
			...previous,
			status: "loading",
			data: null,
			error: null,
			retryable: false
		} : previous);
		setNonce((n) => n + 1);
	};
	return {
		...current,
		retry
	};
}
function CmpCol({ resource, label, surfaceRef = null, onFocusCapture = null, onRetry = null }) {
	const d = resource.data;
	const failed = resource.status === "error";
	return /* @__PURE__ */ _jsx("div", {
		ref: surfaceRef,
		tabIndex: -1,
		onFocusCapture,
		className: `cmp-col${failed ? " notice resource-error" : d ? "" : " muted"}`,
		role: failed ? "alert" : d ? "group" : "status",
		"aria-label": failed ? undefined : d ? `${label} details` : undefined,
		"aria-live": failed || d ? undefined : "polite",
		children: failed ? /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsxs("span", { children: [
			label,
			": ",
			resource.error || "full details could not be loaded."
		] }), resource.retryable && /* @__PURE__ */ _jsx("button", {
			className: "btn sm",
			onClick: onRetry || resource.retry,
			"aria-label": `Retry ${label} details`,
			children: "Retry"
		})] }) : d ? /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsxs("div", {
			className: "kv",
			children: [
				/* @__PURE__ */ _jsx("div", {
					className: "k",
					children: "operator"
				}),
				/* @__PURE__ */ _jsx("div", {
					className: "v",
					children: d.operator
				}),
				/* @__PURE__ */ _jsx("div", {
					className: "k",
					children: "metric"
				}),
				/* @__PURE__ */ _jsx("div", {
					className: "v",
					children: fmt(d.confirmed_mean ?? d.metric)
				}),
				/* @__PURE__ */ _jsx("div", {
					className: "k",
					children: "status"
				}),
				/* @__PURE__ */ _jsx("div", {
					className: "v",
					children: d.status
				}),
				/* @__PURE__ */ _jsx("div", {
					className: "k",
					children: "params"
				}),
				/* @__PURE__ */ _jsx("div", {
					className: "v",
					children: JSON.stringify(d.idea?.params)
				})
			]
		}), /* @__PURE__ */ _jsx(CodeViewer, {
			code: d.code || "(no code)",
			label: `${label} code`,
			maxHeight: 280
		})] }) : /* @__PURE__ */ _jsxs(_Fragment, { children: [
			"Loading ",
			label,
			" details…"
		] })
	});
}
export function ComparePanel({ state, runId, expectedGeneration, reviewMode = false, onClose, initialPair = null }) {
	const ids = Object.keys(state.nodes).map(Number).sort((a, b) => a - b);
	const [a, setA] = useState(null), [b, setB] = useState(null);
	const [diff, setDiff] = useState(false);
	const leftSelectRef = useRef(null), rightSelectRef = useRef(null);
	const leftSurfaceRef = useRef(null), rightSurfaceRef = useRef(null), diffSurfaceRef = useRef(null);
	const emptyStateRef = useRef(null);
	const detailFocusOwnerRef = useRef(null);
	const previousNodeCountRef = useRef(ids.length);
	const comparisonScope = JSON.stringify([
		String(runId || ""),
		String(state?.run_id || ""),
		expectedGeneration || "",
		reviewMode ? "review" : "owner"
	]);
	const previousComparisonScopeRef = useRef(comparisonScope);
	useLayoutEffect(() => {
		if (previousComparisonScopeRef.current === comparisonScope) return;
		previousComparisonScopeRef.current = comparisonScope;
		const active = typeof document === "undefined" ? null : document.activeElement;
		const diffFocusWouldBeLost = detailFocusOwnerRef.current === "diff" && (!active || active === document.body || !active.isConnected || diffSurfaceRef.current?.contains(active));
		if (diffFocusWouldBeLost) {
			detailFocusOwnerRef.current = null;
			const resetFocusTarget = leftSelectRef.current || emptyStateRef.current;
			resetFocusTarget?.focus({ preventScroll: true });
		}
		setA(null);
		setB(null);
		setDiff(false);
	}, [comparisonScope]);
	// Seed from an explicit pair (e.g. canvas "diff vs champion"), else best vs latest.
	useEffect(() => {
		if (initialPair && initialPair.length === 2) {
			if (initialPair[0] != null) setA(initialPair[0]);
			if (initialPair[1] != null) setB(initialPair[1]);
			setDiff(true);
		}
	}, [initialPair && initialPair.join(",")]);
	// Seed/repair the selectors once nodes exist (the panel may open before any node arrives).
	// Functional updates: on mount this runs in the same commit as the initialPair seeding above
	// and would otherwise read a=null from the stale closure and overwrite the explicit pair.
	useEffect(() => {
		if (!ids.length) return;
		setA((cur) => cur == null || !ids.includes(cur) ? state.best_node_id ?? ids[0] : cur);
		setB((cur) => cur == null || !ids.includes(cur) ? ids[ids.length - 1] : cur);
	}, [
		comparisonScope,
		ids.join(","),
		state.best_node_id
	]);
	const attemptA = a == null ? null : state.nodes?.[a]?.attempt;
	const attemptB = b == null ? null : state.nodes?.[b]?.attempt;
	const resourceA = useNodeResource({
		runId,
		stateRunId: state?.run_id,
		nodeId: a,
		expectedGeneration,
		expectedAttempt: attemptA,
		reviewMode
	});
	const resourceB = useNodeResource({
		runId,
		stateRunId: state?.run_id,
		nodeId: b,
		expectedGeneration,
		expectedAttempt: attemptB,
		reviewMode
	});
	const da = resourceA.data, db = resourceB.data;
	const displayedAttemptA = Number.isSafeInteger(da?.attempt) ? da.attempt : attemptA;
	const displayedAttemptB = Number.isSafeInteger(db?.attempt) ? db.attempt : attemptB;
	const selectionReady = Number.isSafeInteger(a) && ids.includes(a) && Number.isSafeInteger(b) && ids.includes(b);
	const codeDiff = useMemo(() => diff && da?.code != null && db?.code != null ? diffLines(da.code, db.code) : null, [
		diff,
		da?.code,
		db?.code
	]);
	const diffError = resourceA.status === "error" || resourceB.status === "error";
	const diffErrorText = [resourceA.status === "error" ? `Left node #${a}: ${resourceA.error}` : "", resourceB.status === "error" ? `Right node #${b}: ${resourceB.error}` : ""].filter(Boolean).join(" ");
	const previousDetailScopesRef = useRef([resourceA.scope, resourceB.scope]);
	useLayoutEffect(() => {
		const previous = previousDetailScopesRef.current;
		const leftChanged = previous[0] !== resourceA.scope;
		const rightChanged = previous[1] !== resourceB.scope;
		previousDetailScopesRef.current = [resourceA.scope, resourceB.scope];
		if (!leftChanged && !rightChanged || typeof document === "undefined") return;
		const active = document.activeElement;
		if (active && active !== document.body && active.isConnected) return;
		const owner = detailFocusOwnerRef.current;
		const target = owner === "left" && leftChanged ? leftSurfaceRef.current || leftSelectRef.current || emptyStateRef.current : owner === "right" && rightChanged ? rightSurfaceRef.current || rightSelectRef.current || emptyStateRef.current : owner === "diff" && (leftChanged || rightChanged) ? diffSurfaceRef.current || leftSelectRef.current || emptyStateRef.current : null;
		target?.focus({ preventScroll: true });
	}, [resourceA.scope, resourceB.scope]);
	useLayoutEffect(() => {
		const previousCount = previousNodeCountRef.current;
		previousNodeCountRef.current = ids.length;
		if (previousCount !== 0 || ids.length === 0 || detailFocusOwnerRef.current !== "empty") return;
		const active = typeof document === "undefined" ? null : document.activeElement;
		detailFocusOwnerRef.current = null;
		if (!active || active === document.body || !active.isConnected) {
			leftSelectRef.current?.focus({ preventScroll: true });
		}
	}, [ids.length]);
	const retrySide = (owner, resource, surfaceRef) => {
		if (!resource.retryable) return;
		detailFocusOwnerRef.current = owner;
		resource.retry();
		requestAnimationFrame(() => surfaceRef.current?.focus({ preventScroll: true }));
	};
	const retryDiff = () => {
		detailFocusOwnerRef.current = "diff";
		if (resourceA.retryable) resourceA.retry();
		if (resourceB.retryable) resourceB.retry();
		requestAnimationFrame(() => diffSurfaceRef.current?.focus({ preventScroll: true }));
	};
	if (!ids.length) return /* @__PURE__ */ _jsx(Panel, {
		title: "Compare nodes",
		onClose,
		children: /* @__PURE__ */ _jsx("div", {
			ref: emptyStateRef,
			tabIndex: -1,
			className: "muted",
			role: "status",
			onFocus: () => {
				detailFocusOwnerRef.current = "empty";
			},
			children: "No nodes yet."
		})
	});
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Compare nodes",
		onClose,
		wide: true,
		children: [/* @__PURE__ */ _jsxs("div", {
			className: "toolbar",
			style: { marginBottom: 10 },
			children: [
				/* @__PURE__ */ _jsx(CmpSel, {
					label: "Left node",
					v: a,
					set: setA,
					ids,
					nodes: state.nodes,
					currentAttempt: displayedAttemptA,
					selectRef: leftSelectRef
				}),
				/* @__PURE__ */ _jsx("span", {
					className: "muted",
					children: "vs"
				}),
				/* @__PURE__ */ _jsx(CmpSel, {
					label: "Right node",
					v: b,
					set: setB,
					ids,
					nodes: state.nodes,
					currentAttempt: displayedAttemptB,
					selectRef: rightSelectRef
				}),
				/* @__PURE__ */ _jsx("span", {
					className: "spacer",
					style: { flex: 1 }
				}),
				/* @__PURE__ */ _jsx("button", {
					className: "btn sm" + (diff ? " primary" : ""),
					"aria-pressed": diff,
					disabled: !selectionReady,
					onClick: () => setDiff((d) => !d),
					title: "ordered line diff of the two nodes' code",
					children: "Code diff"
				})
			]
		}), !selectionReady ? /* @__PURE__ */ _jsx("div", {
			className: "muted",
			role: "status",
			"aria-live": "polite",
			children: "Preparing node comparison…"
		}) : diff ? /* @__PURE__ */ _jsx("div", {
			ref: diffSurfaceRef,
			tabIndex: -1,
			role: "group",
			"aria-label": "Code comparison details",
			onFocusCapture: () => {
				detailFocusOwnerRef.current = "diff";
			},
			children: diffError ? /* @__PURE__ */ _jsxs("div", {
				className: "notice resource-error",
				role: "alert",
				children: [/* @__PURE__ */ _jsx("span", { children: diffErrorText || "Could not load both node details for the diff." }), (resourceA.retryable || resourceB.retryable) && /* @__PURE__ */ _jsx("button", {
					className: "btn sm",
					onClick: retryDiff,
					children: "Retry failed details"
				})]
			}) : codeDiff ? /* @__PURE__ */ _jsx(CodeViewer, {
				diff: codeDiff,
				copyText: db.code || "",
				label: `Code diff #${a} attempt ${displayedAttemptA} to #${b} attempt ${displayedAttemptB}`,
				maxHeight: 460
			}) : /* @__PURE__ */ _jsxs("div", {
				className: "muted",
				role: "status",
				"aria-live": "polite",
				children: [
					"Loading code for #",
					a,
					" attempt ",
					displayedAttemptA ?? "unknown",
					" and #",
					b,
					" attempt ",
					displayedAttemptB ?? "unknown",
					"…"
				]
			})
		}) : /* @__PURE__ */ _jsxs("div", {
			className: "cmp-cols",
			children: [/* @__PURE__ */ _jsx(CmpCol, {
				resource: resourceA,
				label: `Node #${a} · attempt ${displayedAttemptA ?? "unknown"}`,
				surfaceRef: leftSurfaceRef,
				onFocusCapture: () => {
					detailFocusOwnerRef.current = "left";
				},
				onRetry: () => retrySide("left", resourceA, leftSurfaceRef)
			}), /* @__PURE__ */ _jsx(CmpCol, {
				resource: resourceB,
				label: `Node #${b} · attempt ${displayedAttemptB ?? "unknown"}`,
				surfaceRef: rightSurfaceRef,
				onFocusCapture: () => {
					detailFocusOwnerRef.current = "right";
				},
				onRetry: () => retrySide("right", resourceB, rightSurfaceRef)
			})]
		})]
	});
}
const explorerEvent = (event) => {
	const omitted = event?._log_page?.truncated === true;
	const bytes = omitted ? Number(event._log_page.raw_bytes || 0) : 0;
	if (omitted) {
		const preview = `details omitted · ${bytes.toLocaleString()} source bytes exceed page limit`;
		return {
			event,
			preview,
			searchType: String(event.type || "").toLowerCase(),
			searchData: preview.toLowerCase(),
			omitted: true,
			serialized: null
		};
	}
	let serialized = "{}";
	try {
		serialized = JSON.stringify(event.data || {});
	} catch {
		serialized = "[unserializable event data]";
	}
	const preview = serialized.length > 500 ? serialized.slice(0, 500) + "…" : serialized;
	return {
		event,
		preview,
		serialized,
		searchType: String(event.type || "").toLowerCase(),
		searchData: serialized.slice(0, 4e3).toLowerCase(),
		omitted: false
	};
};
const explorerEventKey = (item) => timelineEventKey(item.event);
const explorerPreviewForQuery = (item, query) => {
	if (!query || item.omitted || !item.serialized) return item.preview;
	const match = item.searchData.indexOf(query);
	if (match < 0) return item.preview;
	// Put the hit near the beginning so it remains visible in the single-line desktop preview. The
	// rest of the 500-character window supplies useful context after the match.
	const start = Math.max(0, match - 24);
	const end = Math.min(item.serialized.length, start + Math.max(500, query.length + 24));
	return `${start ? "…" : ""}${item.serialized.slice(start, end)}${end < item.serialized.length ? "…" : ""}`;
};
const highlightedExplorerText = (value, query) => {
	if (!query) return value;
	const lower = value.toLowerCase();
	const parts = [];
	let from = 0;
	let match = lower.indexOf(query);
	while (match >= 0) {
		if (match > from) parts.push(value.slice(from, match));
		parts.push(/* @__PURE__ */ _jsx("mark", {
			className: "event-explorer-hit",
			children: value.slice(match, match + query.length)
		}, `${match}:${parts.length}`));
		from = match + query.length;
		match = lower.indexOf(query, from);
	}
	if (from < value.length) parts.push(value.slice(from));
	return parts.length ? parts : value;
};
export function EventExplorer({ runId, timeline, historyActive = false, onReturnToLive = null, onClose }) {
	const [f, setF] = useState("");
	const [expandedPayload, setExpandedPayload] = useState(null);
	const [copyStatus, setCopyStatus] = useState(null);
	const copyRequestRef = useRef(0);
	const payloadRef = useRef(null);
	const detailIdPrefix = useId();
	const query = f.trim().toLowerCase();
	const indexed = useMemo(() => timeline.rows.map(explorerEvent), [timeline.rows]);
	const rows = useMemo(() => indexed.filter((item) => !query || item.searchType.includes(query) || item.searchData.includes(query)), [indexed, query]);
	const explorerIdentity = `${runId}:${timeline.generation || "pending"}`;
	const expandedKey = expandedPayload?.identity === explorerIdentity ? expandedPayload.key : null;
	useEffect(() => {
		copyRequestRef.current += 1;
		setExpandedPayload(null);
		setCopyStatus(null);
	}, [explorerIdentity]);
	useEffect(() => {
		if (expandedKey == null || rows.some((item) => explorerEventKey(item) === expandedKey && !item.omitted && item.serialized !== "{}")) return;
		copyRequestRef.current += 1;
		setExpandedPayload(null);
		setCopyStatus(null);
	}, [expandedKey, rows]);
	const togglePayload = (item) => {
		const key = explorerEventKey(item);
		const opening = expandedKey !== key;
		if (opening && !historyActive && timeline.followingTail) timeline.setFollowingTail(false);
		copyRequestRef.current += 1;
		setCopyStatus(null);
		setExpandedPayload(opening ? {
			identity: explorerIdentity,
			key
		} : null);
	};
	const copyPayload = async (item) => {
		const key = explorerEventKey(item);
		const request = copyRequestRef.current + 1;
		copyRequestRef.current = request;
		setCopyStatus({
			identity: explorerIdentity,
			key,
			state: "copying"
		});
		try {
			if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
			await navigator.clipboard.writeText(item.serialized);
			if (copyRequestRef.current === request) {
				setCopyStatus({
					identity: explorerIdentity,
					key,
					state: "copied"
				});
			}
		} catch {
			if (copyRequestRef.current === request) {
				setCopyStatus({
					identity: explorerIdentity,
					key,
					state: "error"
				});
			}
		}
	};
	const selectPayload = (item) => {
		const key = explorerEventKey(item);
		const element = payloadRef.current;
		const selection = window.getSelection?.();
		if (!element || expandedKey !== key || !selection) {
			setCopyStatus({
				identity: explorerIdentity,
				key,
				state: "selection-error"
			});
			return;
		}
		copyRequestRef.current += 1;
		try {
			element.focus({ preventScroll: true });
			const range = document.createRange();
			range.selectNodeContents(element);
			selection.removeAllRanges();
			selection.addRange(range);
			setCopyStatus({
				identity: explorerIdentity,
				key,
				state: "selected"
			});
		} catch {
			setCopyStatus({
				identity: explorerIdentity,
				key,
				state: "selection-error"
			});
		}
	};
	const totalLabel = timeline.totalEvents == null ? `${timeline.rows.length} loaded events` : `${timeline.rows.length} loaded of ${timeline.totalEvents} events`;
	return /* @__PURE__ */ _jsxs(Panel, {
		title: "Raw event explorer",
		sub: totalLabel,
		onClose,
		wide: true,
		children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "event-explorer-tools",
				children: [
					/* @__PURE__ */ _jsx("input", {
						className: "text",
						"aria-label": "Filter loaded events by type or first 4,000 data characters",
						placeholder: "filter loaded type or first 4k data chars…",
						value: f,
						onChange: (event) => setF(event.target.value)
					}),
					/* @__PURE__ */ _jsx("button", {
						type: "button",
						className: "btn sm",
						disabled: !timeline.hasMore.older || timeline.loading.older,
						onClick: timeline.loadOlder,
						children: timeline.loading.older ? "Loading…" : "Load older"
					}),
					timeline.hasMore.newer && /* @__PURE__ */ _jsx("button", {
						type: "button",
						className: "btn sm",
						disabled: timeline.loading.newer,
						onClick: timeline.loadNewer,
						children: timeline.loading.newer ? "Loading…" : "Load newer"
					}),
					/* @__PURE__ */ _jsx("button", {
						type: "button",
						className: "btn sm ghost",
						disabled: timeline.loading.tail || !historyActive && timeline.followingTail && timeline.windowAtTail,
						onClick: onReturnToLive || timeline.jumpToLive,
						children: timeline.loading.tail ? "Refreshing…" : "Latest"
					})
				]
			}),
			timeline.totalEvents != null && timeline.totalEvents > timeline.rows.length && /* @__PURE__ */ _jsx("div", {
				className: "timeline-window-note",
				role: "note",
				children: "Search covers the loaded window only. Page through the log to inspect other source events."
			}),
			timeline.errors.tail && /* @__PURE__ */ _jsxs("div", {
				className: "notice resource-error compact",
				role: "alert",
				children: ["Newest events could not be refreshed. ", /* @__PURE__ */ _jsx("button", {
					type: "button",
					className: "btn sm",
					onClick: () => timeline.retry("tail"),
					children: "Retry"
				})]
			}),
			timeline.errors.older && /* @__PURE__ */ _jsxs("div", {
				className: "notice resource-error compact",
				role: "alert",
				children: ["Could not load older events; current rows are unchanged. ", /* @__PURE__ */ _jsx("button", {
					type: "button",
					className: "btn sm",
					onClick: timeline.loadOlder,
					children: "Retry"
				})]
			}),
			timeline.errors.newer && /* @__PURE__ */ _jsxs("div", {
				className: "notice resource-error compact",
				role: "alert",
				children: ["Newer-page refresh failed; loaded rows may be behind. ", /* @__PURE__ */ _jsx("button", {
					type: "button",
					className: "btn sm",
					onClick: timeline.loadNewer,
					children: "Retry"
				})]
			}),
			timeline.errors.around && /* @__PURE__ */ _jsxs("div", {
				className: "notice resource-error compact",
				role: "alert",
				children: ["Replay window could not be loaded. ", /* @__PURE__ */ _jsx("button", {
					type: "button",
					className: "btn sm",
					onClick: () => timeline.retry("around"),
					children: "Retry"
				})]
			}),
			timeline.tornTail && /* @__PURE__ */ _jsx("div", {
				className: "timeline-window-note warning",
				role: "status",
				children: timeline.sourceTailLimited ? "Raw source tail exceeded the safety limit." : "Only the verified canonical event prefix is shown."
			}),
			timeline.status === "loading" && !timeline.rows.length ? /* @__PURE__ */ _jsx("div", {
				className: "timeline-resource muted",
				role: "status",
				children: "Loading events…"
			}) : timeline.status !== "error" && rows.length === 0 ? /* @__PURE__ */ _jsx("div", {
				className: "timeline-resource muted",
				children: query ? "No matches in the loaded window." : "No verified events."
			}) : /* @__PURE__ */ _jsx(VirtualTimeline, {
				rows,
				getKey: explorerEventKey,
				identity: `${runId}:${timeline.generation || "pending"}:explorer`,
				className: "event-explorer-timeline",
				ariaLabel: "Loaded raw events",
				followingTail: !historyActive && timeline.followingTail,
				windowAtTail: !historyActive && timeline.windowAtTail,
				unread: timeline.unread,
				unreadUnknown: timeline.unreadUnknown,
				busy: Object.values(timeline.loading).some(Boolean),
				onFollowingTailChange: (value) => {
					if (!historyActive) timeline.setFollowingTail(value);
				},
				onJumpToLive: onReturnToLive || timeline.jumpToLive,
				estimateSize: 42,
				renderRow: (item) => {
					const key = explorerEventKey(item);
					const open = expandedKey === key;
					const expandable = !item.omitted && item.serialized !== "{}";
					const detailsId = `${detailIdPrefix}-${item.event.seq}`;
					const status = copyStatus?.identity === explorerIdentity && copyStatus.key === key ? copyStatus.state : null;
					const preview = explorerPreviewForQuery(item, query);
					return /* @__PURE__ */ _jsxs("div", {
						className: "event-explorer-row" + (item.omitted ? " omitted" : ""),
						children: [
							expandable ? /* @__PURE__ */ _jsx("button", {
								type: "button",
								className: "event-explorer-toggle",
								"aria-expanded": open,
								"aria-controls": detailsId,
								"aria-label": `${open ? "Hide" : "Show"} full payload for event ${item.event.seq}`,
								onClick: () => togglePayload(item),
								children: /* @__PURE__ */ _jsx("span", {
									"aria-hidden": "true",
									children: open ? "▾" : "▸"
								})
							}) : /* @__PURE__ */ _jsx("span", {
								className: "event-explorer-toggle-placeholder",
								"aria-hidden": "true"
							}),
							/* @__PURE__ */ _jsx("span", {
								className: "event-explorer-seq",
								children: item.event.seq
							}),
							/* @__PURE__ */ _jsx("span", {
								className: "event-explorer-type",
								children: highlightedExplorerText(String(item.event.type || ""), query)
							}),
							/* @__PURE__ */ _jsx("span", {
								className: "event-explorer-data",
								children: highlightedExplorerText(preview, query)
							}),
							open && /* @__PURE__ */ _jsxs("div", {
								id: detailsId,
								className: "event-explorer-detail",
								children: [
									/* @__PURE__ */ _jsxs("div", {
										className: "event-explorer-detail-tools",
										children: [/* @__PURE__ */ _jsxs("span", { children: [
											"Full payload · ",
											item.serialized.length.toLocaleString(),
											" characters"
										] }), /* @__PURE__ */ _jsxs("div", {
											className: "event-explorer-detail-actions",
											children: [/* @__PURE__ */ _jsx("button", {
												type: "button",
												className: "btn sm ghost",
												onClick: () => selectPayload(item),
												children: "Select payload"
											}), /* @__PURE__ */ _jsx("button", {
												type: "button",
												className: "btn sm ghost",
												disabled: status === "copying",
												onClick: () => copyPayload(item),
												children: status === "copying" ? "Copying…" : status === "copied" ? "Copied" : "Copy payload"
											})]
										})]
									}),
									status === "copied" && /* @__PURE__ */ _jsx("div", {
										className: "event-explorer-copy-status",
										role: "status",
										children: "Payload copied."
									}),
									status === "selected" && /* @__PURE__ */ _jsx("div", {
										className: "event-explorer-copy-status",
										role: "status",
										children: "Payload selected. Press Ctrl+C to copy it."
									}),
									status === "error" && /* @__PURE__ */ _jsx("div", {
										className: "event-explorer-copy-status error",
										role: "status",
										children: "Copy failed. Use Select payload, then press Ctrl+C."
									}),
									status === "selection-error" && /* @__PURE__ */ _jsx("div", {
										className: "event-explorer-copy-status error",
										role: "status",
										children: "Payload selection is unavailable in this browser."
									}),
									/* @__PURE__ */ _jsx("pre", {
										ref: payloadRef,
										className: "event-explorer-json",
										role: "region",
										tabIndex: 0,
										"aria-label": `Full payload for event ${item.event.seq}`,
										children: item.serialized
									})
								]
							})
						]
					});
				}
			})
		]
	});
}
const _MAX_VIEW = 2e6;
const ARTIFACT_SCOPE_ERRORS = new Set([
	"artifact_attempt_protocol_error",
	"artifact_generation_protocol_error",
	"artifact_path_protocol_error",
	"artifact_run_protocol_error",
	"invalid_run_generation",
	"node_attempt_changed",
	"node_attempt_required",
	"run_generation_changed",
	"run_generation_unavailable"
]);
const artifactScopeChanged = (error) => error?.status === 409 || ARTIFACT_SCOPE_ERRORS.has(error?.code);
// Workspace file browser: lists the run directory and live host repo / reference / data paths declared
// by a RepoTask. Those task paths can contain inputs, later edits and outputs; without a start-time
// manifest the UI deliberately makes no file-level production claim. Text files open inline; binary /
// oversize ones are flagged. Backed by GET /api/runs/{id}/artifacts + /artifact.
export function ArtifactsPanel({ runId, expectedGeneration, onToast, onClose }) {
	const [roots, setRoots] = useState(null);
	const [err, setErr] = useState(null);
	const [open, setOpen] = useState({});
	const [sel, setSel] = useState(null);
	const [content, setContent] = useState(null);
	const [busy, setBusy] = useState(false);
	const [filter, setFilter] = useState("");
	const [reload, setReload] = useState(0);
	const inventoryReqRef = useRef(0);
	const contentAbortRef = useRef(null);
	const scope = `${String(runId)}@${String(expectedGeneration || "")}`;
	const scopeRef = useRef(scope);
	scopeRef.current = scope;
	const generationReady = RUN_GENERATION_RE.test(expectedGeneration || "");
	const reqRef = useRef(0);
	useEffect(() => {
		const requestScope = scope;
		const token = ++inventoryReqRef.current;
		++reqRef.current;
		contentAbortRef.current?.abort();
		contentAbortRef.current = null;
		setRoots(null);
		setErr(null);
		setOpen({});
		setSel(null);
		setContent(null);
		setBusy(false);
		setFilter("");
		if (!generationReady) return () => {
			inventoryReqRef.current += 1;
		};
		const controller = new AbortController();
		getRunArtifactInventory(runId, expectedGeneration, { signal: controller.signal }).then((d) => {
			if (token !== inventoryReqRef.current || requestScope !== scopeRef.current) return;
			if (!Array.isArray(d.roots)) throw new Error("Invalid file inventory");
			const rs = d.roots;
			setRoots(rs);
			const o = {};
			rs.forEach((r) => {
				o[r.id] = !!r.is_run_dir;
			});
			setOpen(o);
		}).catch((e) => {
			if (e?.name === "AbortError" || token !== inventoryReqRef.current || requestScope !== scopeRef.current) return;
			setErr(artifactScopeChanged(e) ? "The run or experiment attempt changed while files were loading. Reopen Files from the current run." : e.message);
		});
		return () => {
			controller.abort();
			++reqRef.current;
			contentAbortRef.current?.abort();
			contentAbortRef.current = null;
			if (token === inventoryReqRef.current) inventoryReqRef.current += 1;
		};
	}, [
		expectedGeneration,
		generationReady,
		reload,
		runId,
		scope
	]);
	const view = (rootId, f) => {
		if (!generationReady) return;
		const token = ++reqRef.current;
		const requestScope = scope;
		contentAbortRef.current?.abort();
		const controller = new AbortController();
		contentAbortRef.current = controller;
		const hasNodeIdentity = f?.node_id != null || f?.attempt != null;
		setSel({
			root: rootId,
			path: f.path,
			...hasNodeIdentity ? {
				nodeId: f.node_id,
				attempt: f.attempt
			} : {}
		});
		setContent(null);
		setBusy(true);
		// Always fetch — don't trust the extension-based is_text GUESS (a text .bin/.pb would otherwise be
		// unviewable); the SERVER does the authoritative binary sniff. The token ignores a stale response
		// (fast-clicking A then B must not let A's slower reply render under B).
		getRunArtifactContent(runId, {
			root: rootId,
			path: f.path,
			expectedGeneration,
			...hasNodeIdentity ? {
				nodeId: f.node_id,
				attempt: f.attempt
			} : {},
			signal: controller.signal
		}).then((c) => {
			if (token === reqRef.current && requestScope === scopeRef.current) setContent(c);
		}).catch((e) => {
			if (e?.name === "AbortError" || token !== reqRef.current || requestScope !== scopeRef.current) return;
			setContent(null);
			if (artifactScopeChanged(e)) {
				setRoots(null);
				setOpen({});
				setSel(null);
				setErr(e?.code === "node_attempt_changed" ? "This experiment attempt changed. Reopen Files to load its current inventory." : "The run changed while this file was loading. Reopen Files from the current run.");
			} else onToast?.("view failed: " + e.message);
		}).finally(() => {
			if (token !== reqRef.current || requestScope !== scopeRef.current) return;
			if (contentAbortRef.current === controller) contentAbortRef.current = null;
			setBusy(false);
		});
	};
	const ql = filter.trim().toLowerCase();
	// Search every server-bounded root, including collapsed ones. Keeping disclosure state separate
	// avoids rendering thousands of hidden-root rows while still making each root's match count honest.
	const matchesByRoot = ql && roots ? new Map(roots.map((r) => [r.id, r.files.filter((f) => f.path.toLowerCase().includes(ql))])) : null;
	const totalMatches = matchesByRoot ? [...matchesByRoot.values()].reduce((total, files) => total + files.length, 0) : 0;
	const cappedSearch = !!(ql && roots?.some((r) => r.truncated));
	const binary = content && content.is_text === false;
	return /* @__PURE__ */ _jsx(Panel, {
		title: "Files",
		sub: runId,
		wide: true,
		onClose,
		children: !generationReady ? /* @__PURE__ */ _jsx("div", {
			className: "muted",
			children: "Waiting for the current run identity…"
		}) : err ? /* @__PURE__ */ _jsxs("div", {
			className: "notice",
			children: [
				"Could not load files: ",
				err,
				" ",
				/* @__PURE__ */ _jsx("button", {
					type: "button",
					className: "btn sm",
					onClick: () => {
						setErr(null);
						setReload((n) => n + 1);
					},
					children: "Retry"
				})
			]
		}) : !roots ? /* @__PURE__ */ _jsx("div", {
			className: "muted",
			children: "Loading…"
		}) : /* @__PURE__ */ _jsxs("div", {
			className: "art-wrap",
			children: [/* @__PURE__ */ _jsxs("div", {
				className: "art-list",
				children: [
					/* @__PURE__ */ _jsx("input", {
						className: "text art-filter",
						"aria-label": "Filter loaded files",
						placeholder: "filter loaded files…",
						value: filter,
						onChange: (e) => setFilter(e.target.value)
					}),
					ql && /* @__PURE__ */ _jsxs("div", {
						className: "muted art-filter-status",
						role: "status",
						"aria-live": "polite",
						"aria-atomic": "true",
						children: [totalMatches === 0 ? "No matches in the loaded file inventory." : `${totalMatches} ${totalMatches === 1 ? "match" : "matches"} in the loaded file inventory.`, cappedSearch && " Some roots reached the listing limit, so other matches may exist."]
					}),
					roots.length === 0 && /* @__PURE__ */ _jsx("div", {
						className: "muted",
						children: "No files found."
					}),
					roots.map((r) => {
						const isOpen = !!open[r.id];
						const matches = matchesByRoot?.get(r.id) || r.files;
						const files = isOpen ? matches : null;
						return /* @__PURE__ */ _jsxs("div", {
							className: "art-root",
							children: [/* @__PURE__ */ _jsxs("button", {
								type: "button",
								className: "art-root-h disclosure-button",
								title: r.path,
								"aria-expanded": isOpen,
								onClick: () => setOpen((o) => ({
									...o,
									[r.id]: !isOpen
								})),
								children: [
									/* @__PURE__ */ _jsx("span", {
										className: "art-chev",
										children: isOpen ? "▾" : "▸"
									}),
									/* @__PURE__ */ _jsx("b", { children: r.label }),
									/* @__PURE__ */ _jsx("span", {
										className: "muted art-root-n",
										children: ql ? `${matches.length} ${matches.length === 1 ? "match" : "matches"} · ${r.n_files} loaded` : `${r.n_files}${r.truncated ? " loaded · cap reached" : ""}`
									})
								]
							}), isOpen && /* @__PURE__ */ _jsxs("div", {
								className: "art-files",
								children: [files.length === 0 ? /* @__PURE__ */ _jsx("div", {
									className: "muted art-empty",
									children: ql ? r.truncated ? "no match in loaded subset" : "no match in this root" : "empty"
								}) : files.map((f) => /* @__PURE__ */ _jsxs("button", {
									type: "button",
									title: f.path + (f.is_text ? "" : " · looks binary"),
									"aria-pressed": !!(sel && sel.root === r.id && sel.path === f.path),
									className: "art-file disclosure-button" + (sel && sel.root === r.id && sel.path === f.path ? " sel" : "") + (f.is_text ? "" : " bin"),
									onClick: () => view(r.id, f),
									children: [/* @__PURE__ */ _jsx("span", {
										className: "art-name",
										children: f.path
									}), /* @__PURE__ */ _jsx("span", {
										className: "art-size",
										children: fmtBytes(f.size)
									})]
								}, f.path)), r.truncated && /* @__PURE__ */ _jsx("div", {
									className: "muted art-empty",
									children: ql ? `Filter checked ${r.n_files} loaded files; this root may contain more matches.` : `Listing stopped at ${r.n_files} files; this root may contain more.`
								})]
							})]
						}, r.id);
					})
				]
			}), /* @__PURE__ */ _jsx("div", {
				className: "art-view",
				children: !sel ? /* @__PURE__ */ _jsx("div", {
					className: "muted art-hint",
					children: "Select a file to view its contents."
				}) : /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsxs("div", {
					className: "art-view-h",
					children: [/* @__PURE__ */ _jsx("span", {
						className: "art-view-path",
						title: sel.path,
						children: sel.path
					}), content && /* @__PURE__ */ _jsx("span", {
						className: "muted",
						children: fmtBytes(content.size)
					})]
				}), busy ? /* @__PURE__ */ _jsx("div", {
					className: "muted",
					children: "Loading…"
				}) : binary ? /* @__PURE__ */ _jsxs("div", {
					className: "notice",
					children: [
						"Binary file — not shown inline (",
						fmtBytes(content.size),
						")."
					]
				}) : content ? /* @__PURE__ */ _jsxs(_Fragment, { children: [content.truncated && /* @__PURE__ */ _jsxs("div", {
					className: "notice art-trunc",
					children: [
						"Showing the first ",
						fmtBytes(_MAX_VIEW),
						" — the file is larger."
					]
				}), /* @__PURE__ */ _jsx("pre", {
					className: "art-pre",
					role: "region",
					"aria-label": `File ${sel.path} contents`,
					tabIndex: 0,
					children: content.content
				})] }) : /* @__PURE__ */ _jsx("div", {
					className: "muted art-hint",
					children: "Could not load this file."
				})] })
			})]
		})
	});
}
