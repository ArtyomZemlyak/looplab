import React, { useEffect, useRef, useState } from "react";
import { abandonScopeReportAction, createIdempotencyKey, getScopeReport, genScopeReport, reconcileScopeReportGeneration, fmt } from "./util.mjs";
import { scopeObservationRows, scopeReportAuthority, scopeReportGenerationError, scopeReportKey, scopeReportPublicationUnconfirmed } from "./scopeReportModel.mjs";
import { useDialogFocus } from "./useDialogFocus.mjs";
import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
const list = (value) => Array.isArray(value) ? value : [];
const text = (value) => typeof value === "string" ? value : "";
const count = (value) => Number.isInteger(value) && value >= 0 ? value : null;
const status = (value) => text(value).slice(0, 64).replaceAll("_", " ");
const valid = (value) => typeof value?.exists === "boolean" && (!value.exists || value.content && typeof value.content === "object" && !Array.isArray(value.content));
const ACTION_ID_RE = /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/i;
const GENERATION_STORAGE_PREFIX = "ll.scope-report-generation.";
const generationFlights = new Map();
const generationAmbiguous = (error) => error?.ambiguous === true || error?.submissionMayHaveSucceeded === true;
const safeJobId = (value) => typeof value === "string" && value.length > 0 && value.length <= 200 && !/[\u0000-\u001f\u007f]/.test(value);
const errorActionId = (error) => ACTION_ID_RE.test(error?.action_id || error?.actionId || "") ? (error.action_id || error.actionId).toLowerCase() : null;
const errorJobId = (error) => safeJobId(error?.job_id || error?.jobId) ? error.job_id || error.jobId : null;
const flightStorageKey = (key) => GENERATION_STORAGE_PREFIX + encodeURIComponent(key);
const flightStorage = () => {
	try {
		return typeof sessionStorage === "undefined" ? null : sessionStorage;
	} catch {
		return null;
	}
};
function persistGeneration(key, flight) {
	const storage = flightStorage();
	if (!storage || !ACTION_ID_RE.test(flight?.actionId || "")) return false;
	flight.actionId = flight.actionId.toLowerCase();
	const raw = JSON.stringify({
		v: 1,
		action_id: flight.actionId,
		job_id: flight.jobId || null
	});
	try {
		storage.setItem(flightStorageKey(key), raw);
		return storage.getItem(flightStorageKey(key)) === raw;
	} catch {
		return false;
	}
}
function clearPersistedGeneration(key, actionId = null) {
	const storage = flightStorage();
	if (!storage) return false;
	const storageKey = flightStorageKey(key);
	try {
		if (actionId) {
			const current = storage.getItem(storageKey);
			if (current != null) {
				try {
					const value = JSON.parse(current);
					const storedActionId = ACTION_ID_RE.test(value?.action_id || "") ? value.action_id.toLowerCase() : value?.action_id;
					if (storedActionId && storedActionId !== actionId.toLowerCase()) return false;
				} catch {
					return false;
				}
			}
		}
		storage.removeItem(storageKey);
		if (storage.getItem(storageKey) == null) return true;
		// A blocked remove must not resurrect a settled paid lock on reload. A bounded tombstone has no
		// report payload or credential and is ignored by the strict reader below.
		storage.setItem(storageKey, JSON.stringify({
			v: 1,
			terminal: true
		}));
		return storage.getItem(storageKey)?.includes("\"terminal\":true") === true;
	} catch {
		return false;
	}
}
function readPersistedGeneration(key) {
	const storage = flightStorage();
	if (!storage) return null;
	let raw, value;
	try {
		raw = storage.getItem(flightStorageKey(key));
	} catch {
		return { invalid: true };
	}
	if (raw == null) return null;
	try {
		value = JSON.parse(raw);
	} catch {
		return { invalid: true };
	}
	if (value?.v === 1 && value?.terminal === true) {
		try {
			storage.removeItem(flightStorageKey(key));
		} catch {}
		return null;
	}
	if (!value || typeof value !== "object" || Array.isArray(value) || value.v !== 1 || !ACTION_ID_RE.test(value.action_id || "") || value.job_id != null && !safeJobId(value.job_id) || Object.keys(value).some((field) => ![
		"v",
		"action_id",
		"job_id"
	].includes(field))) {
		return { invalid: true };
	}
	return {
		actionId: value.action_id.toLowerCase(),
		jobId: value.job_id || null
	};
}
function restoredFlight(key, { reprobeStorageError = false } = {}) {
	const current = generationFlights.get(key);
	if (current && !(reprobeStorageError && !current.actionId && current.error?.code === "SCOPE_REPORT_ACTION_STORAGE_UNAVAILABLE")) return current;
	if (current) generationFlights.delete(key);
	const stored = readPersistedGeneration(key);
	if (!stored) return null;
	const error = new Error("scope report action has not reached an exact terminal");
	error.code = stored.invalid ? "SCOPE_REPORT_ACTION_STORAGE_UNAVAILABLE" : "scope_report_action_unresolved";
	error.ambiguous = true;
	error.submissionMayHaveSucceeded = true;
	if (stored.actionId) {
		error.actionId = stored.actionId;
		error.action_id = stored.actionId;
	}
	if (stored.jobId) {
		error.jobId = stored.jobId;
		error.job_id = stored.jobId;
	}
	const flight = {
		...stored,
		actionId: stored.actionId || null,
		jobId: stored.jobId || null,
		active: false,
		uncertain: true,
		error,
		promise: null
	};
	generationFlights.set(key, flight);
	return flight;
}
function rememberFlightIdentity(key, flight, errorOrJobId) {
	const jobId = typeof errorOrJobId === "string" ? errorOrJobId : errorJobId(errorOrJobId);
	const actionId = typeof errorOrJobId === "object" ? errorActionId(errorOrJobId) : null;
	if (actionId && actionId !== flight.actionId) {
		// A server scope fence may reject this tab's fresh UUID while naming the exact paid action
		// already owned by another tab. Only that typed conflict may replace the local identity.
		if (errorOrJobId?.code !== "scope_report_action_in_progress") {
			flight.error = errorOrJobId;
			return false;
		}
		flight.actionId = actionId;
		flight.jobId = null;
	}
	if (jobId) flight.jobId = jobId;
	if (persistGeneration(key, flight)) return true;
	const error = new Error("durable scope generation state is unavailable");
	error.code = "SCOPE_REPORT_ACTION_STORAGE_UNAVAILABLE";
	error.ambiguous = true;
	error.submissionMayHaveSucceeded = true;
	if (flight.actionId) {
		error.actionId = flight.actionId;
		error.action_id = flight.actionId;
	}
	if (flight.jobId) {
		error.jobId = flight.jobId;
		error.job_id = flight.jobId;
	}
	flight.error = error;
	return false;
}
function driveGeneration(key, flight, start, { uncertain = false } = {}) {
	if (flight.active) return flight;
	flight.active = true;
	flight.uncertain = uncertain;
	flight.promise = Promise.resolve().then(start);
	flight.promise.then(() => {
		flight.active = false;
		if (generationFlights.get(key) === flight) generationFlights.delete(key);
		clearPersistedGeneration(key, flight.actionId);
	}, (error) => {
		if (generationFlights.get(key) !== flight) return;
		flight.active = false;
		const remembered = rememberFlightIdentity(key, flight, error);
		if (generationAmbiguous(error)) {
			flight.uncertain = true;
			if (remembered) flight.error = error;
		} else {
			generationFlights.delete(key);
			clearPersistedGeneration(key, flight.actionId);
		}
	});
	return flight;
}
function beginGeneration(key, actionId, start) {
	const existing = restoredFlight(key);
	if (existing) return existing;
	const flight = {
		actionId,
		jobId: null,
		active: false,
		uncertain: false,
		error: null,
		promise: null
	};
	generationFlights.set(key, flight);
	if (!persistGeneration(key, flight)) {
		generationFlights.delete(key);
		const error = new Error("durable scope generation state is unavailable");
		error.code = "SCOPE_REPORT_ACTION_STORAGE_UNAVAILABLE";
		throw error;
	}
	return driveGeneration(key, flight, start);
}
async function completedGeneration(value, actionId, type, id) {
	const terminal = value && typeof value === "object" && !Array.isArray(value) ? value : null;
	if (terminal?.action_id === actionId && terminal?.ok === true) {
		// The action receipt proves settlement, not that its historical payload is still the current
		// publication. Re-read the canonical scope report so an older recovered action cannot replace a
		// newer regeneration in the UI. The GET also proves the strict publication survived.
		let current;
		try {
			current = await getScopeReport(type, id);
		} catch (cause) {
			// The paid action is already an exact durable terminal. A bounded canonical read failure is a
			// normal read problem, not permission to keep the paid-action lock ambiguous forever.
			const error = new Error("scope report publication could not be read", { cause });
			error.code = "scope_report_publication_read_failed";
			throw error;
		}
		if (valid(current) && current.exists) return current;
		const error = new Error("scope report publication is missing or invalid");
		error.code = "scope_report_publication_read_failed";
		throw error;
	}
	const error = new Error("scope report terminal response is invalid");
	error.code = "scope_report_invalid_response";
	error.ambiguous = true;
	error.submissionMayHaveSucceeded = true;
	error.actionId = actionId;
	error.action_id = actionId;
	throw error;
}
function Section({ title, items }) {
	items = list(items).filter((x) => typeof x === "string");
	if (!items.length) return null;
	return /* @__PURE__ */ _jsxs("div", {
		className: "sr-sec",
		children: [/* @__PURE__ */ _jsx("div", {
			className: "sr-h",
			children: title
		}), /* @__PURE__ */ _jsx("ul", {
			className: "sr-list",
			children: items.map((x, i) => /* @__PURE__ */ _jsx("li", { children: x }, i))
		})]
	});
}
function MetricRun({ item, note, onOpen }) {
	const id = text(item?.run_id);
	return /* @__PURE__ */ _jsxs("button", {
		type: "button",
		className: "sr-best",
		disabled: !id,
		onClick: () => onOpen?.(id),
		children: [
			/* @__PURE__ */ _jsx("b", {
				className: "sr-m",
				children: Number.isFinite(item?.metric) ? fmt(item.metric) : "—"
			}),
			/* @__PURE__ */ _jsx("span", {
				className: "sr-rid",
				children: id
			}),
			note && /* @__PURE__ */ _jsxs("span", {
				className: "muted",
				children: [" · ", note]
			})
		]
	});
}
// Cross-run aggregate report for a scope (project folder | task | super-task). On open it fetches the
// stored report (if any); you can Generate when there's none, or Regenerate when it's gone stale. The
// report is authored from a bounded/redacted projection. Numeric ranking exists only inside an exact
// server-validated comparison contract; legacy best_runs are counted but their unverified rows stay hidden.
export default function ScopeReport({ scope, onOpen, onClose }) {
	const dialogRef = useRef(null);
	const requestEpoch = useRef(0);
	const readAbort = useRef(null);
	const key = scopeReportKey(scope);
	const keyRef = useRef(key);
	keyRef.current = key;
	const [readRevision, setReadRevision] = useState(0);
	const [view, setView] = useState({
		key,
		data: null,
		busy: false,
		uncertain: false,
		err: null
	});
	// Never render the previous scope's response during the render before its replacement effect runs.
	const shown = view.key === key ? view : {
		data: null,
		busy: false,
		uncertain: false,
		err: null
	};
	const { data, busy, uncertain, err } = shown;
	const publicationUnconfirmed = scopeReportPublicationUnconfirmed(data);
	const observeGeneration = (flight, epoch) => {
		flight.promise.then((value) => {
			if (requestEpoch.current !== epoch || keyRef.current !== key) return;
			setView({
				key,
				data: value,
				busy: false,
				uncertain: false,
				err: null
			});
		}).catch((error) => {
			if (requestEpoch.current !== epoch || keyRef.current !== key) return;
			const effectiveError = generationFlights.get(key)?.error || error;
			const ambiguous = generationAmbiguous(effectiveError);
			const inputsChanged = effectiveError?.code === "scope_report_inputs_changed";
			const publicationReadFailed = effectiveError?.code === "scope_report_publication_read_failed";
			setView((previous) => {
				const previousData = previous.key === key ? previous.data : null;
				const guardedData = !previousData ? previousData : inputsChanged ? {
					...previousData,
					stale: true
				} : ambiguous ? {
					...previousData,
					stale: null
				} : previousData;
				return {
					key,
					data: guardedData,
					busy: false,
					uncertain: ambiguous,
					err: scopeReportGenerationError(effectiveError)
				};
			});
			if (inputsChanged || publicationReadFailed) setReadRevision((value) => value + 1);
		});
	};
	useEffect(() => {
		const epoch = ++requestEpoch.current;
		let flight = restoredFlight(key, { reprobeStorageError: true });
		if (flight?.uncertain && !flight.active && flight.actionId) {
			// remount/reload never invents a new action. It observes the known job/receipt;
			// only a strict server `unknown` may safely replay this same UUID through POST.
			flight = driveGeneration(key, flight, async () => completedGeneration(await reconcileScopeReportGeneration(scope.type, scope.id, {
				actionId: flight.actionId,
				jobId: flight.jobId,
				onJob: (jobId) => rememberFlightIdentity(key, flight, jobId)
			}), flight.actionId, scope.type, scope.id), { uncertain: true });
		}
		if (flight?.active) {
			readAbort.current?.abort();
			setView((previous) => ({
				key,
				data: previous.key === key && previous.data ? flight.uncertain ? {
					...previous.data,
					stale: null
				} : previous.data : null,
				busy: true,
				uncertain: flight.uncertain,
				err: flight.uncertain ? scopeReportGenerationError(flight.error) : null
			}));
			observeGeneration(flight, epoch);
			return () => {
				requestEpoch.current += 1;
			};
		}
		if (flight?.uncertain) {
			// Corrupt durable metadata cannot be overwritten with a new paid action: its missing exact
			// identity is itself an unresolved outcome. Keep the surface locked until storage is repaired.
			setView((previous) => ({
				key,
				data: previous.key === key && previous.data ? {
					...previous.data,
					stale: null
				} : null,
				busy: false,
				uncertain: true,
				err: scopeReportGenerationError(flight.error)
			}));
			return () => {
				requestEpoch.current += 1;
			};
		}
		const controller = new AbortController();
		readAbort.current = controller;
		setView((previous) => ({
			key,
			data: previous.key === key ? previous.data : null,
			busy: true,
			uncertain: false,
			err: previous.key === key ? previous.err : null
		}));
		getScopeReport(scope.type, scope.id, { signal: controller.signal }).then((value) => {
			// A late GET must not overwrite a regeneration or a newly selected scope.
			if (requestEpoch.current !== epoch || keyRef.current !== key) return;
			if (!valid(value)) {
				setView({
					key,
					data: null,
					busy: false,
					uncertain: false,
					err: "Invalid report response."
				});
				return;
			}
			setView({
				key,
				data: value,
				busy: false,
				uncertain: false,
				err: null
			});
		}).catch((error) => {
			if (requestEpoch.current !== epoch || keyRef.current !== key || error?.name === "AbortError") return;
			setView((previous) => ({
				key,
				data: previous.key === key ? previous.data : null,
				busy: false,
				uncertain: false,
				err: "Report unavailable."
			}));
		});
		return () => {
			controller.abort();
			if (readAbort.current === controller) readAbort.current = null;
			requestEpoch.current += 1;
		};
	}, [
		key,
		scope.type,
		scope.id,
		readRevision
	]);
	const generate = () => {
		const existing = restoredFlight(key);
		if (existing) return;
		const actionId = createIdempotencyKey();
		let flight;
		try {
			flight = beginGeneration(key, actionId, async () => {
				return completedGeneration(await genScopeReport(scope.type, scope.id, {
					actionId,
					onJob: (jobId) => rememberFlightIdentity(key, flight, jobId)
				}), actionId, scope.type, scope.id);
			});
		} catch (error) {
			setView((previous) => ({
				key,
				data: previous.key === key ? previous.data : null,
				busy: false,
				uncertain: false,
				err: scopeReportGenerationError(error)
			}));
			return;
		}
		readAbort.current?.abort();
		const epoch = ++requestEpoch.current;
		setView((previous) => ({
			key,
			data: previous.key === key ? previous.data : null,
			busy: true,
			uncertain: false,
			err: null
		}));
		observeGeneration(flight, epoch);
	};
	const generateWithConfirmation = () => {
		const action = data?.exists ? "Regenerate" : "Generate";
		const warning = `${action} this cross-run report? This starts a paid generation and may incur provider cost. Continue?`;
		if (typeof window === "undefined" || typeof window.confirm !== "function" || !window.confirm(warning)) return;
		generate();
	};
	const generateReplacement = () => {
		if (!publicationUnconfirmed) return;
		// This escape hatch starts a new paid UUID after an unconfirmed publication.
		// Require an explicit static, client-owned cost warning; never interpolate raw server/provider copy.
		const warning = "The previous paid generation may have completed, but its report publication was not durably confirmed. A replacement starts a new paid action and may incur additional provider cost. Continue?";
		if (typeof window === "undefined" || typeof window.confirm !== "function" || !window.confirm(warning)) return;
		generate();
	};
	const abandon = async () => {
		const flight = generationFlights.get(key);
		if (!flight?.actionId || flight.active || !["scope_report_action_indeterminate", "scope_report_action_unknown"].includes(flight.error?.code)) return;
		const warning = "The previous paid request may have completed. Abandoning its recovery lock can allow a second paid generation. Continue?";
		if (typeof window !== "undefined" && typeof window.confirm === "function" && !window.confirm(warning)) return;
		flight.active = true;
		setView((previous) => ({
			key,
			data: previous.key === key ? previous.data : null,
			busy: true,
			uncertain: true,
			err: scopeReportGenerationError(flight.error)
		}));
		try {
			await abandonScopeReportAction(scope.type, scope.id, flight.actionId);
			if (generationFlights.get(key) === flight) generationFlights.delete(key);
			clearPersistedGeneration(key, flight.actionId);
			setView((previous) => ({
				key,
				data: previous.key === key ? previous.data : null,
				busy: false,
				uncertain: false,
				err: null
			}));
			setReadRevision((value) => value + 1);
		} catch (error) {
			flight.active = false;
			flight.error = error;
			rememberFlightIdentity(key, flight, error);
			setView((previous) => ({
				key,
				data: previous.key === key ? previous.data : null,
				busy: false,
				uncertain: true,
				err: scopeReportGenerationError(error)
			}));
		}
	};
	const retrySameAction = () => {
		let flight = generationFlights.get(key);
		if (!flight?.actionId || flight.active || flight.error?.code !== "scope_report_action_unknown") return;
		const warning = "No durable claim exists for this UUID. Retrying may start the paid generation now. Retry the same action?";
		if (typeof window !== "undefined" && typeof window.confirm === "function" && !window.confirm(warning)) return;
		const actionId = flight.actionId;
		flight.error = null;
		flight = driveGeneration(key, flight, async () => completedGeneration(await genScopeReport(scope.type, scope.id, {
			actionId,
			onJob: (jobId) => rememberFlightIdentity(key, flight, jobId)
		}), actionId, scope.type, scope.id));
		readAbort.current?.abort();
		const epoch = ++requestEpoch.current;
		setView((previous) => ({
			key,
			data: previous.key === key ? previous.data : null,
			busy: true,
			uncertain: false,
			err: null
		}));
		observeGeneration(flight, epoch);
	};
	const c = data?.content;
	const authority = scopeReportAuthority(data);
	const groups = authority.inspectable ? list(c?.comparison_groups) : [];
	const observations = authority.inspectable ? list(c?.metric_observations) : [];
	const evidenceRuns = count(c?.coverage?.prompt_runs) ?? count(c?.coverage?.model_runs);
	const sourceRuns = count(c?.coverage?.source_runs);
	const label = text(data?.label) || text(data?.scope?.label) || text(scope.label) || `${scope.type} ${scope.id}`;
	const runCount = count(data?.run_count) ?? 0;
	const headline = text(c?.headline);
	const verdict = text(c?.verdict);
	const formatUpgrade = data?.stale === true && data?.stale_reason === "report_format_upgrade";
	useDialogFocus(dialogRef, onClose);
	return /* @__PURE__ */ _jsx("div", {
		className: "overlay",
		onMouseDown: (event) => {
			if (event.target === event.currentTarget) onClose?.();
		},
		children: /* @__PURE__ */ _jsxs("div", {
			ref: dialogRef,
			className: "panel sr-panel",
			role: "dialog",
			"aria-modal": "true",
			"aria-label": `Report for ${label}`,
			tabIndex: -1,
			children: [/* @__PURE__ */ _jsxs("div", {
				className: "panel-h",
				children: [
					/* @__PURE__ */ _jsxs("span", {
						className: "ttl",
						children: ["Cross-run report · ", label]
					}),
					/* @__PURE__ */ _jsx("span", { className: "right" }),
					data?.exists && /* @__PURE__ */ _jsx("button", {
						className: "btn sm",
						disabled: busy || uncertain,
						onClick: generateWithConfirmation,
						children: uncertain ? "… outcome unknown" : busy ? "… generating" : "↻ Regenerate · paid"
					}),
					/* @__PURE__ */ _jsx("button", {
						className: "btn sm ghost",
						onClick: onClose,
						"aria-label": "Close report",
						children: "✕"
					})
				]
			}), /* @__PURE__ */ _jsxs("div", {
				className: "panel-b",
				children: [
					err && /* @__PURE__ */ _jsxs("div", {
						className: "notice resource-error",
						role: "alert",
						children: [
							/* @__PURE__ */ _jsx("span", { children: err }),
							" ",
							/* @__PURE__ */ _jsx("button", {
								type: "button",
								className: "btn sm ghost",
								disabled: busy,
								onClick: () => setReadRevision((value) => value + 1),
								children: uncertain ? "Check paid status" : "Retry read"
							}),
							uncertain && generationFlights.get(key)?.error?.code === "scope_report_action_unknown" && /* @__PURE__ */ _jsx("button", {
								type: "button",
								className: "btn sm ghost",
								disabled: busy,
								onClick: retrySameAction,
								children: "Retry same paid action"
							}),
							uncertain && ["scope_report_action_indeterminate", "scope_report_action_unknown"].includes(generationFlights.get(key)?.error?.code) && /* @__PURE__ */ _jsx("button", {
								type: "button",
								className: "btn sm ghost",
								disabled: busy,
								onClick: abandon,
								children: generationFlights.get(key)?.error?.code === "scope_report_action_unknown" ? "Discard unaccepted action" : "Abandon recovery lock"
							})
						]
					}),
					data == null && !err && /* @__PURE__ */ _jsx("div", {
						className: "notice",
						role: "status",
						children: "Loading…"
					}),
					data && !data.exists && publicationUnconfirmed && /* @__PURE__ */ _jsxs("div", {
						className: "sr-empty",
						children: [
							/* @__PURE__ */ _jsx("div", {
								className: "notice sr-quarantine",
								role: "alert",
								children: "A previous paid generation may have completed, but its report publication was not durably confirmed. Generating a replacement starts a new paid action and may incur additional provider cost."
							}),
							/* @__PURE__ */ _jsxs("div", {
								className: "muted",
								children: [/* @__PURE__ */ _jsx("b", { children: runCount }), " runs are currently in this scope."]
							}),
							/* @__PURE__ */ _jsx("button", {
								className: "btn primary",
								disabled: busy || uncertain || !runCount,
								onClick: generateReplacement,
								children: uncertain ? "… outcome unknown" : busy ? "… generating" : "Generate replacement (may incur cost)"
							})
						]
					}),
					data && !data.exists && !publicationUnconfirmed && /* @__PURE__ */ _jsxs("div", {
						className: "sr-empty",
						children: [/* @__PURE__ */ _jsxs("div", {
							className: "muted",
							children: [
								"No report — ",
								/* @__PURE__ */ _jsx("b", { children: runCount }),
								" runs. Evidence is bounded."
							]
						}), /* @__PURE__ */ _jsx("button", {
							className: "btn primary",
							disabled: busy || uncertain || !runCount,
							onClick: generateWithConfirmation,
							children: uncertain ? "… outcome unknown" : busy ? "… generating" : "✦ Generate report · paid"
						})]
					}),
					data?.exists && c && /* @__PURE__ */ _jsxs("div", {
						className: "sr-body",
						children: [
							/* @__PURE__ */ _jsxs("div", {
								className: "sr-meta",
								children: [
									evidenceRuns != null && sourceRuns != null ? /* @__PURE__ */ _jsxs("span", { children: [
										"· evidence ",
										evidenceRuns,
										"/",
										sourceRuns,
										" runs",
										c.coverage.incomplete === true ? " (incomplete)" : ""
									] }) : /* @__PURE__ */ _jsxs("span", { children: [
										"· snapshot: ",
										Array.isArray(data.run_ids) ? data.run_ids.length : "?",
										" runs"
									] }),
									data.stale === true && /* @__PURE__ */ _jsxs("span", {
										className: "sr-stale",
										children: [" · ", formatUpgrade ? "report format upgraded — regenerate once" : "stale snapshot — regenerate"]
									}),
									authority.freshness === "unknown" && /* @__PURE__ */ _jsx("span", {
										className: "sr-stale",
										children: " · snapshot freshness unknown"
									})
								]
							}),
							!authority.authoritative && /* @__PURE__ */ _jsx("div", {
								className: "notice sr-quarantine",
								role: "status",
								children: "Stored report content is quarantined because its authority is unavailable. Regenerate to inspect it."
							}),
							authority.authoritative && authority.freshness === "unknown" && /* @__PURE__ */ _jsx("div", {
								className: "notice sr-quarantine",
								role: "status",
								children: "Stored report freshness cannot be verified. Narrative, observations, and outcome claims are withheld."
							}),
							authority.freshness === "stale" && /* @__PURE__ */ _jsx("div", {
								className: "notice sr-quarantine",
								role: "status",
								children: formatUpgrade ? "This report predates the current scope receipt. Regenerate once to migrate it; historical advisory content remains inspectable." : "This is a stale historical snapshot. Advisory narrative and observations remain inspectable; snapshot outcome claims are withheld."
							}),
							authority.fresh && !authority.verdict && /* @__PURE__ */ _jsx("div", {
								className: "notice sr-quarantine",
								role: "status",
								children: "The server did not provide a current authoritative verdict. Report observations remain unranked."
							}),
							authority.verdict && verdict && /* @__PURE__ */ _jsx("div", {
								className: "sr-verdict",
								children: verdict
							}),
							groups.length > 0 && /* @__PURE__ */ _jsxs("div", {
								className: "sr-sec",
								children: [/* @__PURE__ */ _jsx("div", {
									className: "sr-h",
									children: "Comparable cohorts"
								}), groups.map((group, i) => {
									const declaredRows = scopeObservationRows(group);
									const trusted = authority.inspectable && declaredRows !== null;
									const rows = declaredRows || [];
									const reason = !trusted ? "unverified" : status(group?.indeterminate) || "unavailable";
									return /* @__PURE__ */ _jsxs("div", {
										className: "sr-group",
										children: [
											/* @__PURE__ */ _jsxs("div", {
												className: "muted",
												children: [
													text(group?.metric_uid) || "metric",
													" · ",
													text(group?.direction) || "?",
													" · ",
													trusted ? "declared observations" : "unverified"
												]
											}),
											/* @__PURE__ */ _jsx("div", {
												className: "muted",
												role: "status",
												children: trusted ? `No winner — ${reason}.` : "Cohort withheld — unverified observation contract."
											}),
											/* @__PURE__ */ _jsx("div", {
												className: "sr-bests",
												children: rows.map((item, j) => {
													const id = text(item?.run_id);
													const note = list(group?.incomplete_runs).includes(id) ? "run incomplete" : "";
													return /* @__PURE__ */ _jsx(MetricRun, {
														item,
														onOpen,
														note
													}, j);
												})
											})
										]
									}, text(group?.contract_id) || i);
								})]
							}),
							observations.length > 0 && /* @__PURE__ */ _jsxs("div", {
								className: "sr-sec",
								children: [/* @__PURE__ */ _jsx("div", {
									className: "sr-h",
									children: "Unranked metrics"
								}), /* @__PURE__ */ _jsx("div", {
									className: "sr-bests",
									children: observations.map((item, i) => /* @__PURE__ */ _jsx(MetricRun, {
										item,
										onOpen,
										note: status(item?.comparison_status) || "unranked"
									}, `${item?.run_id}-${i}`))
								})]
							}),
							authority.inspectable && !authority.narrative && /* @__PURE__ */ _jsx("div", {
								className: "notice sr-quarantine",
								role: "status",
								children: "The narrative is withheld because it has no model-advisory authority marker."
							}),
							authority.narrative && /* @__PURE__ */ _jsxs("div", {
								className: "sr-narrative",
								children: [
									/* @__PURE__ */ _jsx("div", {
										className: "sr-advisory",
										children: "Model-advisory narrative · not a selection decision"
									}),
									headline && /* @__PURE__ */ _jsx("div", {
										className: "sr-headline",
										children: headline
									}),
									/* @__PURE__ */ _jsx(Section, {
										title: "What worked",
										items: c.what_worked
									}),
									/* @__PURE__ */ _jsx(Section, {
										title: "What didn’t",
										items: c.what_didnt
									}),
									/* @__PURE__ */ _jsx(Section, {
										title: "Learnings",
										items: c.learnings
									}),
									/* @__PURE__ */ _jsx(Section, {
										title: "Next directions",
										items: c.next_directions
									}),
									/* @__PURE__ */ _jsx(Section, {
										title: "Caveats",
										items: c.caveats
									})
								]
							})
						]
					})
				]
			})]
		})
	});
}
