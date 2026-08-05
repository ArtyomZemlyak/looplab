import React, { useCallback, useEffect, useMemo, useState, useRef } from "react";
import { deadlineGet, get, fmt, fmtInt, isSweep, spanDetail, nodeConversation, CONTROL, clearNodeTrace, commandFeedback, commandCanRetry, createIdempotencyKey, getRunCommand, retryRunCommand, runNodeApiPath, submitCommand } from "./util.mjs";
import { usePoll } from "./hooks.mjs";
import { Trajectory, ParallelCoords, Scatter, MetricLines } from "./charts.mjs";
import { themeFilteredGroupAggregate } from "./grouping.mjs";
import { mergeSummary, nodeChip } from "./report.mjs";
import { OpIcon } from "./icons.mjs";
import Markdown from "./markdown.mjs";
import CodeViewer from "./CodeViewer.mjs";
import { diffLines } from "./lineDiff.mjs";
import { nodeFeasibilityStatus } from "./trustSemantics.mjs";
import { reviewInspectorTabs } from "./runRouteState.mjs";
import { DataTable, nextRovingIndex } from "./accessibility.mjs";
import { traceDetailState, tracePartial, traceUnavailable, unavailableTraceDetail } from "./traceProjection.mjs";
import { nodeTheme } from "./conceptId.mjs";
import { nodeCanonicalConcepts, parseConceptTagsInput } from "./conceptChips.mjs";
import { conceptMaterializationStatus } from "./nodeProjection.mjs";
import { buildingMarkers } from "./buildingModel.mjs";
import { deadlineRequest } from "./requestDeadline.mjs";
import { createInspectorDraftStore, useInspectorDraftField } from "./inspectorDraftStore.mjs";
import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
// Comments are an explicit Inspector interaction. Keep their independently secured
// review transport out of the base DAG closure, then load the same component only when this tab opens.
const CommentsThread = React.lazy(() => import("./CommentsThread.jsx"));
const withoutNodeTrace = (value) => value && typeof value === "object" ? {
	...value,
	trace: { nodes: [] }
} : value;
const newTraceClearOperationId = () => {
	const token = globalThis.crypto?.randomUUID?.().replace(/-/g, "").toLowerCase();
	if (!/^[0-9a-f]{32}$/.test(token || "")) {
		const error = new Error("Secure operation identity is unavailable.");
		error.code = "trace_clear_operation_unavailable";
		throw error;
	}
	return `tc_${token}`;
};
// One lifecycle "Trace" tab replaces the old Reasoning / LLM / Agent split: a node is worked on by
// several parts in sequence (Researcher proposes, Developer implements/repairs, then it's evaluated
// and confirmed), so we show that whole story in one place — each stage with its sub-steps, inline
// LLM I/O, and the coding-agent's validation — instead of three disconnected panes. The Inspector is
// READ-ONLY (Workstream C): every node action — confirm/ablate/fork/promote/note — is done from the
// chat (add the node via its ＋#id chip, or use a /command), so there's no per-node button toolbar.
// Tab order keeps durable review context closest to the summary: Overview → Comments →
// Trials (sweeps) → Trace → Code → Metrics → Trust → Cost.
const TABS = [
	"Overview",
	"Comments",
	"Trace",
	"Code",
	"Metrics",
	"Trust",
	"Cost"
];
// The ONE per-node write action (Workstream-C exception): re-run THIS node in place — no new node —
// from a chosen stage. It's a recovery/fix control (natural to trigger from the failed node itself),
// unlike the exploratory confirm/ablate/fork which stay in the chat. Appends a node_reset control
// event; the engine applies it on the next resume.
function ResetBtn({ runId, id, generation, onToast }) {
	const [open, setOpen] = useState(false);
	const [busy, setBusy] = useState(false);
	const rootRef = useRef(null);
	const triggerRef = useRef(null);
	const menuRef = useRef(null);
	const STAGES = [
		[
			"eval",
			"re-score",
			"keep the idea + code, just re-run the evaluation (an infra / API-key blip)"
		],
		[
			"implement",
			"re-run the Developer",
			"keep the Researcher's idea, re-write the code (its code crashed)"
		],
		[
			"propose",
			"full redo",
			"re-propose the idea, re-develop, then re-evaluate"
		]
	];
	const doReset = async (stage) => {
		if (busy) return;
		setOpen(false);
		requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }));
		setBusy(true);
		try {
			// `transport` deliberately WITHHOLDS the thrown message here: a reset menu is a dense control
			// surface and the actionable half is "it never reached the server, press it again".
			await submitCommand(CONTROL.resetNode(runId, id, stage, generation), {
				success: `Reset #${id} from ${stage} applied — the engine is processing it`,
				noop: `#${id} already reflects that reset`,
				executing: `Reset #${id} from ${stage} requested — waiting for the engine`,
				failure: `Reset #${id} failed`,
				transport: `Reset #${id} could not be submitted. Try again.`
			}, onToast);
		} finally {
			setBusy(false);
		}
	};
	useEffect(() => {
		if (!open) return;
		requestAnimationFrame(() => menuRef.current?.querySelector("[role=\"menuitem\"]")?.focus());
	}, [open]);
	useEffect(() => {
		if (!open) return;
		const dismiss = (event) => {
			if (!rootRef.current?.contains(event.target)) setOpen(false);
		};
		document.addEventListener("pointerdown", dismiss, true);
		return () => document.removeEventListener("pointerdown", dismiss, true);
	}, [open]);
	const onMenuKeyDown = (event) => {
		const items = [...menuRef.current?.querySelectorAll("[role=\"menuitem\"]") || []];
		const index = items.indexOf(document.activeElement);
		if (event.key === "Tab") {
			setOpen(false);
			return;
		}
		if (event.key === "Escape") {
			event.preventDefault();
			setOpen(false);
			requestAnimationFrame(() => triggerRef.current?.focus());
			return;
		}
		const next = nextRovingIndex(event.key, Math.max(0, index), items.length);
		if (next == null) return;
		event.preventDefault();
		items[next]?.focus();
	};
	return /* @__PURE__ */ _jsxs("span", {
		ref: rootRef,
		className: "reset-control",
		children: [/* @__PURE__ */ _jsx("button", {
			ref: triggerRef,
			className: "ctx-chip ctx-chip-action",
			title: "re-run THIS node in place (no new node) from a chosen stage",
			"aria-haspopup": "menu",
			"aria-expanded": open,
			"aria-disabled": busy,
			"aria-busy": busy,
			onClick: () => {
				if (!busy) setOpen(!open);
			},
			children: busy ? "↻ Resetting…" : "↻ Reset ▾"
		}), open && /* @__PURE__ */ _jsx("div", {
			ref: menuRef,
			role: "menu",
			className: "reset-stage-menu",
			"aria-label": `Reset experiment ${id} from stage`,
			onKeyDown: onMenuKeyDown,
			onBlur: (event) => {
				if (event.relatedTarget !== triggerRef.current && !event.currentTarget.contains(event.relatedTarget)) setOpen(false);
			},
			children: STAGES.map(([stage, label, desc]) => /* @__PURE__ */ _jsxs("button", {
				type: "button",
				role: "menuitem",
				className: "reset-stage-option",
				tabIndex: -1,
				title: desc,
				onClick: () => doReset(stage),
				children: [/* @__PURE__ */ _jsxs("span", {
					className: "reset-option-title",
					children: [
						/* @__PURE__ */ _jsx("b", { children: label }),
						" ",
						/* @__PURE__ */ _jsxs("span", {
							className: "muted",
							children: ["from ", stage]
						})
					]
				}), /* @__PURE__ */ _jsx("span", {
					className: "muted reset-option-description",
					children: desc
				})]
			}, stage))
		})]
	});
}
export default function Inspector({ runId, nodeId, state, live, tab, setTab, onToast, readOnly = false, historySeq = null, expectedGeneration = null, readOnlyReason = "history", evidenceAvailable = true, commentsRevision = null, focusCommentId = null, traceClearRecoveryStore: sharedClearStore = null, traceClearRecoverySnapshot: sharedClearSnapshot = null, publishTraceClearRecovery: publishSharedClearRecovery = null, draftStore: sharedDraftStore = null }) {
	const fallbackDraftStoreRef = useRef(null);
	if (!fallbackDraftStoreRef.current) fallbackDraftStoreRef.current = createInspectorDraftStore();
	const draftStore = sharedDraftStore || fallbackDraftStoreRef.current;
	const nodeAttempt = state?.nodes?.[nodeId]?.attempt;
	const detailScope = `${runId}@${expectedGeneration || "?"}:${nodeId ?? "-"}:${nodeAttempt ?? "?"}:${readOnly ? historySeq ?? readOnlyReason : "live"}:${evidenceAvailable ? 1 : 0}`;
	const [detailResource, setDetailResource] = useState({
		scope: null,
		status: "idle",
		data: null,
		error: "",
		pending: null
	});
	const detailCurrent = detailResource.scope === detailScope;
	const detail = detailCurrent ? detailResource.data : null;
	// Accept a detail payload whose attempt is >= the summary's: the /nodes endpoint is often FRESHER
	// than the lagging run-state poll (e.g. right after an inline repair bumps `attempt`), and showing
	// the current truth is correct — only a genuinely STALER payload (an old attempt's late response)
	// should be rejected. Exact-only matching here flashed a spurious "attempt changed" error banner
	// during normal live repairs until the next poll reconciled.
	const detailMatchesAttempt = (value) => !Number.isSafeInteger(nodeAttempt) || Number.isSafeInteger(value?.attempt) && (readOnly ? value.attempt === nodeAttempt : value.attempt >= nodeAttempt);
	const detailMatchesNode = (value) => value != null && typeof value === "object" && !Array.isArray(value) && String(value.id) === String(nodeId) && typeof value.status === "string";
	const detailMatchesGeneration = (value) => !expectedGeneration || value?.run_generation === expectedGeneration;
	const detailStatus = detailCurrent ? detailResource.status : readOnlyReason === "review" && !evidenceAvailable ? "restricted" : "loading";
	const detailError = detailCurrent ? detailResource.error : "";
	const detailPending = detailCurrent ? detailResource.pending : null;
	const detailPendingLabel = detailPending === "retry" ? "Retrying…" : ["refresh", "reconcile"].includes(detailPending) ? "Refreshing…" : "Loading…";
	const [traceClearedScopes, setTraceClearedScopes] = useState(() => new Set());
	const detailFlightRef = useRef(null);
	const detailStartRef = useRef(null);
	const detailSurfaceRef = useRef(null);
	const detailFocusScopeRef = useRef(null);
	const fallbackClearStore = useRef(new Map());
	const [fallbackClearSignal, setFallbackClearSignal] = useState({
		scope: null,
		kind: null,
		revision: 0
	});
	const traceClearRecoveryStore = sharedClearStore || fallbackClearStore;
	const publishClearRecovery = publishSharedClearRecovery || ((scope, kind) => {
		setFallbackClearSignal((current) => ({
			scope,
			kind,
			revision: current.revision + 1
		}));
	});
	const requestDetail = (intent = "refresh", options) => {
		const current = detailStartRef.current;
		return current?.scope === detailScope ? current.start(intent, options) : false;
	};
	const retryDetailWith = (options = {}) => {
		detailFocusScopeRef.current = detailScope;
		// An explicit user retry owns freshness over an invisible background refresh. Superseding it
		// guarantees immediate busy feedback and prevents that older response from removing the focused
		// retry control without passing through the focus-restoration state.
		return requestDetail("retry", {
			supersede: true,
			...options
		});
	};
	const retryDetail = () => retryDetailWith();
	useEffect(() => {
		let alive = true;
		const owner = {};
		if (nodeId == null) {
			setDetailResource({
				scope: detailScope,
				status: "idle",
				data: null,
				error: "",
				pending: null
			});
			detailStartRef.current = null;
			return () => {
				alive = false;
			};
		}
		if (readOnlyReason === "review" && !evidenceAvailable) {
			setDetailResource({
				scope: detailScope,
				status: "restricted",
				data: null,
				error: "",
				pending: null
			});
			detailStartRef.current = null;
			return () => {
				alive = false;
			};
		}
		const query = [];
		if (readOnly && historySeq != null) query.push(`seq=${historySeq}`);
		if (expectedGeneration) query.push(`expected_generation=${encodeURIComponent(expectedGeneration)}`);
		const at = query.length ? `?${query.join("&")}` : "";
		const start = (intent = "refresh", { supersede = false, mapLastGood = null, onSettled = null } = {}) => {
			if (supersede && detailFlightRef.current) {
				const obsolete = detailFlightRef.current;
				detailFlightRef.current = null;
				obsolete.controller.abort();
			}
			if (detailFlightRef.current) return false;
			const timed = deadlineGet(runNodeApiPath(runId, nodeId, at));
			const request = {
				owner,
				controller: timed.controller,
				promise: timed.promise
			};
			detailFlightRef.current = request;
			setDetailResource((previous) => {
				const sameScope = previous.scope === detailScope;
				const loaded = sameScope ? previous.data : null;
				const lastGood = typeof mapLastGood === "function" ? mapLastGood(loaded) : loaded;
				if (lastGood != null && previous.status === "ready" && intent === "refresh") return previous;
				return {
					scope: detailScope,
					status: lastGood == null ? sameScope && previous.status === "error" ? "error" : "loading" : previous.status === "stale" ? "stale" : "ready",
					data: lastGood,
					error: sameScope ? previous.error : "",
					pending: intent
				};
			});
			const cancel = () => {
				if (detailFlightRef.current !== request) return;
				detailFlightRef.current = null;
				if (!alive) return;
				setDetailResource((previous) => previous.scope === detailScope ? {
					...previous,
					pending: null
				} : previous);
			};
			const finish = (ok, data = null, error = "") => {
				if (detailFlightRef.current !== request) return;
				detailFlightRef.current = null;
				if (!alive) return;
				if (ok) {
					setTraceClearedScopes((current) => {
						if (!current.has(detailScope)) return current;
						const next = new Set(current);
						next.delete(detailScope);
						return next;
					});
				}
				setDetailResource((previous) => {
					const lastGood = previous.scope === detailScope ? previous.data : null;
					const resourceError = intent === "reconcile" ? error === "transport" ? "Trace was cleared, but the remaining experiment details could not be refreshed." : String(error).startsWith("The experiment attempt") ? "Trace was cleared, but the experiment attempt changed before details could be refreshed." : "Trace was cleared, but the detail refresh returned an invalid response." : error === "transport" ? lastGood == null ? "Full node details could not be loaded." : "Experiment details could not be refreshed." : error;
					return ok ? {
						scope: detailScope,
						status: "ready",
						data,
						error: "",
						pending: null
					} : {
						scope: detailScope,
						status: lastGood == null ? "error" : "stale",
						data: lastGood,
						error: resourceError,
						pending: null
					};
				});
				onSettled?.(ok);
			};
			timed.promise.then((value) => {
				const valid = detailMatchesNode(value);
				if (valid && detailMatchesGeneration(value) && detailMatchesAttempt(value)) {
					finish(true, value);
					return;
				}
				finish(false, null, valid ? "The experiment attempt changed while details were loading." : "Full node details returned an invalid response.");
			}, (error) => {
				if (error?.name === "AbortError") cancel();
				else finish(false, null, "transport");
			});
			return request;
		};
		detailStartRef.current = {
			scope: detailScope,
			start
		};
		start("load");
		return () => {
			alive = false;
			if (detailStartRef.current?.start === start) detailStartRef.current = null;
			if (detailFlightRef.current?.owner === owner) {
				detailFlightRef.current.controller.abort();
				detailFlightRef.current = null;
			}
		};
	}, [
		runId,
		nodeId,
		nodeAttempt,
		state?.nodes?.[nodeId]?.status,
		readOnly,
		historySeq,
		expectedGeneration,
		readOnlyReason,
		evidenceAvailable,
		detailScope
	]);
	useEffect(() => {
		if (detailFocusScopeRef.current == null) return;
		if (detailFocusScopeRef.current !== detailScope) {
			detailFocusScopeRef.current = null;
			return;
		}
		if (detailPending || ![
			"ready",
			"stale",
			"error",
			"restricted"
		].includes(detailStatus)) return;
		const frame = requestAnimationFrame(() => {
			const active = document.activeElement;
			if (active === document.body || !active?.isConnected) {
				detailSurfaceRef.current?.focus({ preventScroll: true });
			}
			detailFocusScopeRef.current = null;
		});
		return () => cancelAnimationFrame(frame);
	}, [
		detailScope,
		detailStatus,
		detailPending
	]);
	// Live-refresh the node detail (it carries n.trace spans + the agent report) while the run is ACTIVELY
	// working this node — so the Trace tab fills in WITHOUT the user toggling tabs. Two windows, both
	// engine-alive & not-finished (stops at terminal / engine death):
	//   • building  — an LLM is authoring the node (propose + implement, or a repair).
	//   • pending   — the sandbox is EVALUATING it (data_prep → train → score). Training used to show
	//     nothing live (no child LLM spans, and the stage op flushes only on close); command_eval now
	//     emits a `stage_started` anchor per stage so the Train/Evaluate band fills in DURING the run.
	//     A pending node's status doesn't change until it's scored, so without polling here the Trace
	//     tab froze after "Developer implement" for the whole training run.
	const nodeStatus = state?.nodes?.[nodeId]?.status;
	const engineActive = !readOnly && !!live && live.engine_running !== false && !live.finished && nodeId != null;
	// Poll ANY pending node the user is inspecting while the engine is active (peer review). "Latest
	// pending" was not an evaluation-ownership test: under eval_parallel>1 several nodes are evaluated
	// concurrently, so inspecting an active OLDER pending node used to disable detail polling and freeze
	// its live Trace/metrics. There is no client-visible eval-ownership marker, so poll the selected
	// pending lifecycle conservatively (the poll is per-inspected-node — it never spins more than the one
	// open node, and a pending node in an active run is genuinely in the eval pipeline).
	const evaluatingThis = nodeStatus === "pending" && !live?.paused;
	// Building = a RAW build marker for this node (buildingMarkers), NOT the spliced `building` flag:
	// withBuilding skips ids already in state.nodes, so a node_reset re-build (which emits node_building
	// for an EXISTING pending node) never sets the spliced flag — the poll then stopped and the Trace tab
	// never showed writing/repairing during the rebuild.
	const buildingThis = buildingMarkers(live).some((m) => Number(m?.node_id) === Number(nodeId));
	const nodeWorking = engineActive && (buildingThis || evaluatingThis);
	// Initial load, polling, and manual retries share one scope-owned request. A rejected or invalid
	// refresh therefore keeps last-good detail visible but explicitly stale instead of silently
	// presenting it as current. Returning the owned request lets usePoll abort it during cleanup.
	usePoll(() => requestDetail("refresh"), 4e3, [
		runId,
		nodeId,
		nodeWorking,
		detailScope,
		detailStatus
	], {
		enabled: !readOnly && nodeWorking && detailStatus === "ready",
		immediate: false
	});
	if (nodeId == null) return /* @__PURE__ */ _jsx("div", {
		className: "insp-empty",
		children: "Select a node to inspect its idea, code, metrics, trust, and agent trace."
	});
	const baseNode = detail || state?.nodes?.[nodeId];
	const n = traceClearedScopes.has(detailScope) ? withoutNodeTrace(baseNode) : baseNode;
	const visibleDetailStatus = detailStatus;
	if (!n) {
		if (visibleDetailStatus === "error") return /* @__PURE__ */ _jsxs("div", {
			ref: detailSurfaceRef,
			className: "notice resource-error detail-resource-notice",
			role: "alert",
			tabIndex: -1,
			children: [/* @__PURE__ */ _jsx("span", { children: detailError || "Full node details could not be loaded." }), /* @__PURE__ */ _jsx("button", {
				type: "button",
				className: "btn sm",
				onClick: retryDetail,
				disabled: !!detailPending,
				children: detailPending ? detailPendingLabel : "Retry"
			})]
		});
		if (visibleDetailStatus === "restricted") return /* @__PURE__ */ _jsxs("div", {
			ref: detailSurfaceRef,
			className: "insp-empty",
			role: "status",
			tabIndex: -1,
			children: [
				"Experiment #",
				nodeId,
				" is not included in this summary-only review."
			]
		});
		return /* @__PURE__ */ _jsxs("div", {
			ref: detailSurfaceRef,
			className: "insp-empty",
			role: "status",
			tabIndex: -1,
			children: [
				"Loading experiment #",
				nodeId,
				" details…"
			]
		});
	}
	// Detail may legitimately be one attempt ahead of the run summary. Bind recovery to the exact
	// rendered attempt, not detailScope's lagging summary attempt, so a catch-up poll cannot shed an
	// in-flight clear fence for the same lifecycle.
	const traceClearScope = `${runId}@${expectedGeneration || "?"}:${n.id}:${n.attempt ?? "?"}:trace-clear`;
	const traceClearRecoverySignal = sharedClearSnapshot?.signals?.get(traceClearScope) || fallbackClearSignal;
	// Metric-drift is run-level state (state.drifts), each entry tagged with its node_id — the
	// per-node detail payload has no `drifts` key, so filter the run state down to this node.
	// Reset keeps historical audit rows, so only the exact lifecycle may alarm the current Trust tab.
	// Legacy rows had no generation stamp and can only belong to the original (attempt-zero) node.
	const nodeDrifts = (state?.drifts || []).filter((d) => d.node_id === n.id && (Object.hasOwn(d, "generation") ? d.generation === n.attempt : n.attempt === 0));
	// Sweep nodes get a Trials tab (right after Overview). `activeTab` guards against a stale tab
	// (e.g. 'Trials' left selected after switching to a non-sweep node) falling through to nothing.
	const sweep = isSweep(n);
	const liveTabs = sweep ? [
		"Overview",
		"Comments",
		"Trials",
		...TABS.slice(2)
	] : TABS;
	const tabs = readOnly ? readOnlyReason === "review" ? reviewInspectorTabs(evidenceAvailable) : [
		"Overview",
		"Code",
		"Trust",
		"Cost"
	] : liveTabs;
	const activeTab = tabs.includes(tab) ? tab : "Overview";
	const tabSlug = (value) => value.toLowerCase().replace(/[^a-z0-9]+/g, "-");
	const tabId = (value) => `inspector-${nodeId}-tab-${tabSlug(value)}`;
	const panelId = (value) => `inspector-${nodeId}-panel-${tabSlug(value)}`;
	const onTabKeyDown = (event, index) => {
		const next = nextRovingIndex(event.key, index, tabs.length);
		if (next == null) return;
		event.preventDefault();
		const nextTab = tabs[next];
		setTab(nextTab);
		requestAnimationFrame(() => document.getElementById(tabId(nextTab))?.focus());
	};
	return /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
		className: "tabs",
		role: "tablist",
		"aria-label": "Inspector sections",
		children: tabs.map((t, index) => /* @__PURE__ */ _jsx("button", {
			id: tabId(t),
			type: "button",
			role: "tab",
			"aria-selected": t === activeTab,
			"aria-controls": t === activeTab ? panelId(t) : undefined,
			tabIndex: t === activeTab ? 0 : -1,
			className: "tab" + (t === activeTab ? " active" : "") + (t === "Trust" && (n.violations?.length || nodeDrifts.length) ? " alarm" : ""),
			onClick: () => setTab(t),
			onKeyDown: (event) => onTabKeyDown(event, index),
			children: t
		}, t))
	}), /* @__PURE__ */ _jsxs("div", {
		ref: detailSurfaceRef,
		className: "insp-body",
		id: panelId(activeTab),
		role: "tabpanel",
		"aria-labelledby": tabId(activeTab),
		tabIndex: 0,
		children: [
			visibleDetailStatus === "loading" && /* @__PURE__ */ _jsx("div", {
				className: "notice",
				role: "status",
				children: "Loading full node details…"
			}),
			visibleDetailStatus === "stale" && /* @__PURE__ */ _jsxs("div", {
				className: "notice resource-warning detail-resource-notice",
				role: "status",
				children: [/* @__PURE__ */ _jsx("span", { children: detailPending ? detailPending === "reconcile" ? "Trace cleared. Refreshing the remaining experiment details…" : "Retrying experiment details… Last loaded details remain visible." : `${detailError || "Experiment details could not be refreshed."} Last loaded details remain visible.` }), /* @__PURE__ */ _jsx("button", {
					type: "button",
					className: "btn sm",
					onClick: retryDetail,
					disabled: !!detailPending,
					children: detailPending ? detailPendingLabel : "Retry"
				})]
			}),
			visibleDetailStatus === "error" && /* @__PURE__ */ _jsxs("div", {
				className: "notice resource-error detail-resource-notice",
				role: "alert",
				children: [/* @__PURE__ */ _jsxs("span", { children: [detailError || "Full node details could not be loaded.", " The summary below may be incomplete."] }), /* @__PURE__ */ _jsx("button", {
					type: "button",
					className: "btn sm",
					onClick: retryDetail,
					disabled: !!detailPending,
					children: detailPending ? detailPendingLabel : "Retry"
				})]
			}),
			readOnly ? /* @__PURE__ */ _jsx("div", {
				className: "insp-hint history-inline",
				children: readOnlyReason === "review" ? evidenceAvailable ? "Read-only review with redacted source evidence. Live traces and actions stay hidden." : "Summary-only review. Source, live traces, and actions are not included." : readOnlyReason === "start-over" ? "Start over is unresolved. Actions and live traces stay locked until the exact request is recovered." : `Snapshot seq ${historySeq} · read-only. Live traces, metrics sidecars and actions are hidden.`
			}) : /* @__PURE__ */ _jsxs("div", {
				className: "insp-hint muted",
				children: [
					"Run actions (confirm · ablate · fork · promote) stay in chat. Use Comments for review, or attach ",
					/* @__PURE__ */ _jsxs("button", {
						className: "ctx-chip ctx-chip-action",
						title: "attach this node to assistant context",
						onClick: () => window.dispatchEvent(new CustomEvent("ll:attach-node", { detail: { id: n.id } })),
						children: ["＋ #", n.id]
					}),
					" as context.",
					/* @__PURE__ */ _jsx(ResetBtn, {
						runId,
						id: n.id,
						generation: n.attempt,
						onToast
					})
				]
			}),
			activeTab === "Overview" && /* @__PURE__ */ _jsx(Overview, {
				n,
				state,
				runId: readOnly ? null : runId,
				onToast,
				draftStore,
				expectedGeneration
			}),
			activeTab === "Comments" && /* @__PURE__ */ _jsx(CommentsThread, {
				runId,
				nodeId: n.id,
				nodeGeneration: n.attempt,
				expectedGeneration,
				refreshKey: commentsRevision,
				readOnly,
				reviewMode: readOnlyReason === "review",
				focusCommentId,
				draftStore,
				draftSurface: "inspector"
			}),
			activeTab === "Trials" && /* @__PURE__ */ _jsx(Trials, {
				n,
				detail,
				state
			}),
			activeTab === "Trace" && /* @__PURE__ */ _jsx(Trace, {
				n,
				runId,
				expectedGeneration,
				expectedTraceRevision: n.trace_revision,
				live,
				working: nodeWorking,
				detailStatus,
				reloadPending: !!detailPending,
				clearScope: traceClearScope,
				clearRecoveryStore: traceClearRecoveryStore,
				recoverClearState: traceClearRecoveryStore.current.get(traceClearScope) || null,
				clearRecoverySignal: traceClearRecoverySignal,
				publishClearRecovery,
				onReload: (reason) => {
					if (reason === "trace-cleared") {
						setTraceClearedScopes((current) => {
							if (current.has(detailScope)) return current;
							const next = new Set(current);
							next.add(detailScope);
							return next;
						});
						return requestDetail("reconcile", {
							supersede: true,
							mapLastGood: withoutNodeTrace,
							onSettled: (ok) => {
								if (ok) {
									traceClearRecoveryStore.current.delete(traceClearScope);
									publishClearRecovery(traceClearScope, "refresh-succeeded");
									return;
								}
								const message = {
									kind: "error",
									blocking: true,
									text: "Trace was cleared, but experiment details could not be refreshed. Clear remains unavailable until a refresh succeeds."
								};
								traceClearRecoveryStore.current.set(traceClearScope, {
									phase: "blocked",
									message
								});
								publishClearRecovery(traceClearScope, "refresh-failed");
							}
						});
					}
					if (reason === "trace-clear-recovery") {
						return retryDetailWith({ onSettled: (ok) => {
							if (ok) {
								traceClearRecoveryStore.current.delete(traceClearScope);
								publishClearRecovery(traceClearScope, "refresh-succeeded");
								return;
							}
							const message = {
								kind: "error",
								blocking: true,
								text: "Experiment refresh did not complete. Trace clear remains unavailable until a refresh succeeds."
							};
							traceClearRecoveryStore.current.set(traceClearScope, {
								phase: "blocked",
								message
							});
							publishClearRecovery(traceClearScope, "refresh-failed");
						} });
					}
					return reason === "retry" ? retryDetail() : requestDetail("refresh");
				}
			}, `trace:${detailScope}:${n.attempt ?? "pending"}`),
			activeTab === "Code" && (["ready", "stale"].includes(visibleDetailStatus) ? /* @__PURE__ */ _jsx(Code, {
				n,
				draftStore,
				draftScope: `code:${runId}@${expectedGeneration || "?"}:${n.id}:${n.attempt ?? "?"}`
			}) : visibleDetailStatus === "error" ? /* @__PURE__ */ _jsx("div", {
				className: "insp-empty",
				children: "Code is unavailable because full node details failed to load."
			}) : /* @__PURE__ */ _jsx("div", {
				className: "insp-empty",
				children: "Loading code…"
			})),
			activeTab === "Metrics" && /* @__PURE__ */ _jsx(Metrics, {
				n,
				detail,
				state,
				runId
			}),
			activeTab === "Trust" && /* @__PURE__ */ _jsx(Trust, {
				n,
				drifts: nodeDrifts
			}),
			activeTab === "Cost" && /* @__PURE__ */ _jsx(Cost, { state })
		]
	})] });
}
function KV({ k, v }) {
	return /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
		className: "k",
		children: k
	}), /* @__PURE__ */ _jsx("div", {
		className: "v",
		children: v
	})] });
}
// Summary for a COLLAPSED group's super-node (semantic zoom): aggregate + drill back to members.
export function GroupSummary({ groupKey, memberIds, state, themeFilter = null, highlightIds = null, onSelectNode, onClose }) {
	const dir = state.direction;
	// Keep the drill-down on exactly the same semantic projection as its collapsed super-node. Without
	// this, a truthful 2/8 card could open a cross-direction best, trajectory, and member table.
	const aggregate = themeFilteredGroupAggregate(memberIds || [], state.nodes, dir, themeFilter, state, highlightIds);
	const members = aggregate.matchedIds.map((id) => state.nodes[id]).filter(Boolean).sort((a, b) => a.id - b.id);
	const zeroMatch = aggregate.filterActive && aggregate.matchedCount === 0;
	const countLabel = aggregate.filterActive ? `${aggregate.matchedCount}/${aggregate.totalCount}` : String(aggregate.totalCount);
	const themes = [...new Set(members.map((node) => nodeTheme(node, state)).filter(Boolean))];
	return /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsxs("div", {
		className: "tabs",
		children: [
			/* @__PURE__ */ _jsxs("h2", {
				className: "tab active group-summary-title",
				tabIndex: -1,
				"data-group-summary-title": true,
				children: ["Group · ", groupKey]
			}),
			/* @__PURE__ */ _jsx("span", { className: "spacer" }),
			/* @__PURE__ */ _jsx("button", {
				className: "btn sm ghost",
				onClick: onClose,
				title: "close group view",
				"aria-label": "Close group details",
				children: "✕"
			})
		]
	}), /* @__PURE__ */ _jsxs("div", {
		className: "insp-body",
		children: [/* @__PURE__ */ _jsxs("div", {
			className: "kv",
			children: [
				/* @__PURE__ */ _jsx(KV, {
					k: aggregate.filterActive ? "matching experiments" : "experiments",
					v: countLabel
				}),
				aggregate.filterActive && /* @__PURE__ */ _jsx(KV, {
					k: "active filter",
					v: aggregate.filterDescription
				}),
				/* @__PURE__ */ _jsx(KV, {
					k: "best",
					v: zeroMatch ? "No matching result" : fmt(aggregate.best)
				}),
				themes.length > 0 && /* @__PURE__ */ _jsx(KV, {
					k: "primary concept axes",
					v: themes.join(", ")
				})
			]
		}), zeroMatch ? /* @__PURE__ */ _jsxs("div", {
			className: "insp-empty",
			role: "status",
			children: [
				"No experiments in this group match ",
				aggregate.filterDescription,
				"."
			]
		}) : /* @__PURE__ */ _jsxs(_Fragment, { children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "section-h",
				children: [
					"Best over ",
					aggregate.filterActive ? "matching " : "",
					"members"
				]
			}),
			/* @__PURE__ */ _jsx(Trajectory, {
				nodes: members,
				direction: dir,
				state,
				height: 150,
				onPick: onSelectNode
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "section-h",
				children: [
					aggregate.filterActive ? "Matching members" : "Members",
					" ",
					/* @__PURE__ */ _jsx("span", {
						className: "pill",
						children: countLabel
					})
				]
			}),
			/* @__PURE__ */ _jsx(DataTable, {
				caption: "Group member results",
				card: false,
				children: /* @__PURE__ */ _jsxs("table", {
					className: "tbl",
					children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("th", { children: "node" }),
						/* @__PURE__ */ _jsx("th", { children: "operator" }),
						/* @__PURE__ */ _jsx("th", { children: "metric" }),
						/* @__PURE__ */ _jsx("th", { children: "status" })
					] }) }), /* @__PURE__ */ _jsx("tbody", { children: members.map((n) => /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("td", { children: /* @__PURE__ */ _jsxs("button", {
							type: "button",
							className: "btn xs ghost",
							"data-group-member-id": n.id,
							"aria-label": `Open experiment #${n.id}`,
							onClick: () => onSelectNode(n.id),
							children: ["#", n.id]
						}) }),
						/* @__PURE__ */ _jsx("td", { children: n.operator }),
						/* @__PURE__ */ _jsx("td", { children: fmt(n.confirmed_mean ?? n.metric) }),
						/* @__PURE__ */ _jsx("td", { children: n.status })
					] }, n.id)) })]
				})
			})
		] })]
	})] });
}
// Phase 1: the node's declared eval pipeline as a coloured strip (data_prep ✓ → train ✓ → eval ✗), so a
// crash is pinpointed to its stage instead of hiding behind one opaque "evaluate". Empty on single-command
// evals. The failed stage is tinted red; a still-pending tail (not yet reached) shows muted.
function StagePipeline({ stages, failed, runId, id, generation, onToast }) {
	const [pendingStage, setPendingStage] = useState(null);
	if (!stages || !stages.length) return null;
	const tone = (s) => s.status === "ok" ? "var(--ok)" : s.status === "timeout" ? "var(--working)" : s.status === "reused" ? "var(--fg-mut)" : "var(--fail)";
	const ic = (s) => s.status === "ok" ? "✓" : s.status === "timeout" ? "⧗" : s.status === "reused" ? "↺" : "✗";
	const rerun = async (name) => {
		if (!runId || pendingStage) return;
		setPendingStage(name);
		try {
			await submitCommand(CONTROL.resetNode(runId, id, name, generation), {
				success: `Reset #${id} from '${name}' applied — the engine is processing it`,
				noop: `#${id} already reflects that reset`,
				executing: `Re-run of #${id} from '${name}' requested — waiting for the engine`,
				failure: "Re-run failed",
				transport: "Re-run could not be submitted. Try again."
			}, onToast);
		} finally {
			setPendingStage(null);
		}
	};
	return /* @__PURE__ */ _jsxs("div", {
		className: "eval-pipeline",
		children: [/* @__PURE__ */ _jsxs("div", {
			className: "muted eval-pipeline-label",
			children: [
				"eval pipeline",
				failed ? ` — failed at ${failed}` : "",
				runId ? " · click a stage to re-run from there" : " · historical result (read-only)"
			]
		}), /* @__PURE__ */ _jsx("div", {
			className: "eval-pipeline-stages",
			children: stages.map((s, i) => /* @__PURE__ */ _jsxs(React.Fragment, { children: [runId ? /* @__PURE__ */ _jsxs("button", {
				type: "button",
				disabled: pendingStage != null,
				onClick: () => rerun(s.name),
				className: "eval-pipeline-step",
				style: { "--stage-tone": tone(s) },
				title: `${s.name}: ${s.status}${s.seconds != null ? ` · ${s.seconds}s` : ""}${s.exit_code != null ? ` · exit ${s.exit_code}` : ""} — click to re-run the pipeline FROM here (reuse earlier stages)`,
				children: [
					ic(s),
					" ",
					s.name
				]
			}) : /* @__PURE__ */ _jsxs("span", {
				className: "eval-pipeline-step",
				style: { "--stage-tone": tone(s) },
				title: `${s.name}: ${s.status}${s.seconds != null ? ` · ${s.seconds}s` : ""}${s.exit_code != null ? ` · exit ${s.exit_code}` : ""} · historical result`,
				children: [
					ic(s),
					" ",
					s.name
				]
			}), i < stages.length - 1 && /* @__PURE__ */ _jsx("span", {
				className: "muted eval-pipeline-arrow",
				children: "→"
			})] }, i))
		})]
	});
}
// PART V Phase 2c: the node's concept tags with a direct operator re-tag affordance. The tags are
// displayed exactly as on the Dag (canonical, de-duped); on a LIVE run with an AUTHORITATIVE concept
// projection an operator can replace the whole set, which folds with `operator-edited` provenance the
// classifier re-tag cadence must not clobber (docs/guide/concepts.md). Read-only history (runId null),
// a partial/unavailable projection, and a still-building node stay display-only — a fabricated "current"
// set must never be presented as something to overwrite.
const commandRecordPending = (record) => record?.status === "accepted" || record?.status === "executing";
const recoveryCommandRecord = (error, boundRecord) => {
	const observed = error?.commandRecord;
	if (!observed || error?.code === "COMMAND_PROTOCOL_ERROR") return boundRecord;
	if (boundRecord?.id && observed.id !== boundRecord.id) return boundRecord;
	return observed;
};
function ConceptTags({ n, state, runId, onToast, draftStore, expectedGeneration }) {
	const draftScope = `concept-tags:${runId}@${expectedGeneration || "?"}:${n.id}:${n.attempt ?? "?"}`;
	const [editing, setEditing] = useInspectorDraftField(draftStore, draftScope, "editing", false);
	const [text, setText] = useInspectorDraftField(draftStore, draftScope, "text", "");
	const [busy, setBusy] = useInspectorDraftField(draftStore, draftScope, "busy", false);
	const [baseline, setBaseline] = useInspectorDraftField(draftStore, draftScope, "baseline", null);
	const [intent, setIntent] = useInspectorDraftField(draftStore, draftScope, "intent", null);
	const [error, setError] = useInspectorDraftField(draftStore, draftScope, "error", "");
	const [messageKind, setMessageKind] = useInspectorDraftField(draftStore, draftScope, "messageKind", "");
	const areaRef = useRef(null);
	const triggerRef = useRef(null);
	const focusEditorRef = useRef(false);
	// The editor is keyed and stored on the complete run-generation/node-attempt identity. Temporary
	// Inspector unmounts resume that exact scope; a replacement run or reset node starts clean.
	const current = useMemo(() => nodeCanonicalConcepts(state?.node_concepts || {}, n.id, state?.concept_consolidation || {}), [
		state?.node_concepts,
		state?.concept_consolidation,
		n.id
	]);
	const currentKey = current.join("\n");
	const status = conceptMaterializationStatus(state, n.id);
	// Editable only for a SETTLED experiment (terminal lifecycle) with an authoritative complete concept
	// projection. `status === 'complete'` is concept-PROJECTION completeness, NOT node lifecycle — a
	// still-building or reset-rebuilding node folds back to `pending` yet can keep a prior 'complete'
	// projection, so gating on the projection alone would wrongly expose Edit on a node whose concepts
	// aren't settled. Require a terminal node status too; read-only history (runId null) stays display-only.
	const canEdit = !!runId && /^[0-9a-f]{64}$/.test(expectedGeneration || "") && status === "complete" && (n.status === "evaluated" || n.status === "failed");
	const baselineChanged = editing && baseline != null && baseline !== currentKey;
	const exactIntent = intent?.text === text && intent?.baseline === baseline;
	const operationFenced = !!intent && (intent.unknown === true || commandRecordPending(intent.record));
	const exactRetry = exactIntent && commandCanRetry(intent?.record);
	useEffect(() => {
		// Restoring a conditional Inspector must not steal focus from the control that remounted it.
		if (!editing || !focusEditorRef.current) return;
		focusEditorRef.current = false;
		requestAnimationFrame(() => areaRef.current?.focus());
	}, [editing]);
	const open = () => {
		focusEditorRef.current = true;
		setText(currentKey);
		setBaseline(currentKey);
		setIntent(null);
		setError("");
		setMessageKind("");
		setEditing(true);
	};
	const cancel = () => {
		// A pending/unknown full replacement can still apply; keep its identity until terminal state.
		if (busy || operationFenced) {
			onToast?.("Check the re-tag command before closing this draft.");
			return;
		}
		draftStore.clear(draftScope);
		requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }));
	};
	const copyPendingInput = async () => {
		try {
			await navigator.clipboard.writeText(intent?.text || "");
			onToast?.("Pending re-tag input copied.");
		} catch {
			onToast?.("Clipboard is unavailable. The submitted tags remain visible in the editor.");
		}
	};
	const save = async () => {
		if (busy || !canEdit && !operationFenced) return;
		let submission;
		let concepts, dropped;
		if (operationFenced) {
			// Check the exact earlier operation even if the operator edited the visible next draft.
			submission = intent;
			concepts = submission.concepts;
			dropped = submission.dropped || 0;
		} else {
			if (baselineChanged) return;
			const parsed = parseConceptTagsInput(text);
			concepts = parsed.concepts;
			dropped = parsed.dropped;
			// "The operator cleared the tags" and "every token they typed was rejected" are NOT the same
			// intent. An explicit clear is blank input; a fully-rejected input is a typo to correct.
			if (concepts.length === 0 && dropped > 0) {
				onToast?.("No valid concept IDs — fix the input, or clear it to remove every tag.");
				return;
			}
			submission = exactRetry ? intent : {
				text,
				baseline,
				concepts,
				dropped,
				idempotencyKey: createIdempotencyKey(),
				record: null,
				unknown: false
			};
		}
		const checking = operationFenced;
		const retrying = !checking && commandCanRetry(submission.record);
		const observing = checking && submission.record?.id && commandRecordPending(submission.record);
		let closeAfterSuccess = false;
		setIntent(submission);
		setError("");
		setMessageKind("");
		setBusy(true);
		try {
			const record = observing ? await getRunCommand(runId, submission.record.id) : retrying ? await retryRunCommand(runId, submission.record.id, { waitMs: 12e3 }) : await CONTROL.retagConcepts(runId, {
				nodeId: n.id,
				nodeGeneration: n.attempt,
				concepts: submission.concepts
			}, {
				expectedGeneration,
				idempotencyKey: submission.idempotencyKey,
				waitMs: 12e3
			});
			const feedback = commandFeedback(record, {
				success: `Re-tagged #${n.id} → ${concepts.length} concept${concepts.length === 1 ? "" : "s"}` + `${dropped ? ` (${dropped} invalid dropped)` : ""} — the engine is processing it`,
				noop: `#${n.id} already carries exactly those concepts`,
				executing: `Re-tag of #${n.id} requested — waiting for the engine`,
				failure: `Re-tag of #${n.id} failed`
			});
			onToast?.(feedback.message);
			if (feedback.kind === "pending") {
				setIntent({
					...submission,
					record,
					unknown: false
				});
				setError(feedback.message);
				setMessageKind("status");
			} else if (feedback.kind === "success") {
				if (text === submission.text) {
					closeAfterSuccess = true;
					requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }));
				} else {
					setIntent(null);
					setError("The earlier re-tag completed. Review the current tags before saving this newer draft.");
					setMessageKind("status");
				}
			} else {
				const sameDraft = text === submission.text && baseline === submission.baseline;
				setIntent(commandCanRetry(record) && sameDraft ? {
					...submission,
					record,
					unknown: false
				} : null);
				setError(feedback.message);
				setMessageKind("error");
			}
		} catch (caught) {
			const record = recoveryCommandRecord(caught, submission.record);
			const pending = commandRecordPending(record);
			const unknown = caught?.commandUnknown === true || pending || checking;
			const sameDraft = text === submission.text && baseline === submission.baseline;
			setIntent(unknown ? {
				...submission,
				record,
				unknown: !pending
			} : commandCanRetry(record) && sameDraft ? {
				...submission,
				record,
				unknown: false
			} : null);
			const message = unknown ? `Re-tag of #${n.id} has an uncertain outcome. Retry will reuse the same command identity.` : `Re-tag of #${n.id} could not be submitted. Your draft is preserved.`;
			setError(message);
			setMessageKind("error");
			onToast?.(message);
		} finally {
			// Avoid recreating an empty entry by writing busy=false after clearing a completed scope.
			if (closeAfterSuccess) draftStore.clear(draftScope);
			else setBusy(false);
		}
	};
	return /* @__PURE__ */ _jsxs(_Fragment, { children: [
		/* @__PURE__ */ _jsxs("div", {
			className: "section-h",
			children: [
				"Concepts",
				status === "partial" ? " · partial (display-only)" : "",
				canEdit && !editing && /* @__PURE__ */ _jsx("button", {
					ref: triggerRef,
					type: "button",
					className: "ctx-chip ctx-chip-action",
					title: "replace this experiment's concept tags (operator authoring; the classifier re-tag will not overwrite it)",
					onClick: open,
					children: "✎ Edit tags"
				})
			]
		}),
		!editing && (current.length ? /* @__PURE__ */ _jsx("div", {
			className: "node-concepts-list",
			children: current.map((c) => /* @__PURE__ */ _jsx("span", {
				className: "nc-tag",
				title: c,
				children: c
			}, c))
		}) : /* @__PURE__ */ _jsx("div", {
			className: "muted",
			children: status === "unavailable" ? "concepts unavailable" : status === "partial" ? "none retained (partial projection)" : "none"
		})),
		editing && /* @__PURE__ */ _jsxs("div", {
			className: "concept-tag-editor",
			children: [
				/* @__PURE__ */ _jsxs("label", {
					className: "muted",
					htmlFor: `ct-${n.id}`,
					children: [
						"One concept id per line (or comma-separated), e.g. ",
						/* @__PURE__ */ _jsx("code", { children: "loss/contrastive" }),
						". Invalid ids are dropped."
					]
				}),
				/* @__PURE__ */ _jsx("textarea", {
					id: `ct-${n.id}`,
					ref: areaRef,
					className: "concept-tag-input",
					rows: 4,
					value: text,
					disabled: busy,
					"aria-describedby": operationFenced ? `ct-${n.id}-command-hint` : undefined,
					onChange: (event) => {
						const next = event.target.value;
						setText(next);
						if (intent && !operationFenced && next !== intent.text) setIntent(null);
						if (error && !operationFenced) {
							setError("");
							setMessageKind("");
						}
					},
					onKeyDown: (event) => {
						if (event.key !== "Escape") return;
						event.preventDefault();
						cancel();
					}
				}),
				baselineChanged && /* @__PURE__ */ _jsxs("div", {
					className: "notice warn compact concept-tag-recovery",
					children: [
						/* @__PURE__ */ _jsx("span", {
							role: "status",
							children: "Concept tags changed after this draft started. Review the latest set before replacing it."
						}),
						/* @__PURE__ */ _jsxs("div", {
							className: "concept-tag-latest",
							children: [
								/* @__PURE__ */ _jsx("b", { children: "Latest tags:" }),
								" ",
								current.length ? current.join(", ") : "none"
							]
						}),
						operationFenced && /* @__PURE__ */ _jsx("span", {
							className: "muted",
							children: "Check the earlier command before choosing a baseline for this next draft."
						}),
						/* @__PURE__ */ _jsxs("div", {
							className: "concept-tag-recovery-actions",
							children: [/* @__PURE__ */ _jsx("button", {
								type: "button",
								className: "btn xs",
								disabled: busy || operationFenced,
								onClick: () => {
									setText(currentKey);
									setBaseline(currentKey);
									setIntent(null);
									setError("");
									setMessageKind("");
									onToast?.("Latest tags loaded into the editor.");
								},
								children: "Use latest"
							}), /* @__PURE__ */ _jsx("button", {
								type: "button",
								className: "btn xs",
								disabled: busy || operationFenced,
								onClick: () => {
									setBaseline(currentKey);
									setIntent(null);
									setError("");
									setMessageKind("");
									onToast?.("Latest tags acknowledged. Your draft remains in the editor.");
								},
								children: "Continue with my draft"
							})]
						})
					]
				}),
				error && /* @__PURE__ */ _jsx("div", {
					className: `notice compact ${messageKind === "status" ? "warn" : "resource-error"}`,
					role: messageKind === "status" ? "status" : "alert",
					children: error
				}),
				operationFenced && /* @__PURE__ */ _jsxs("div", {
					className: "concept-command-recovery",
					"aria-label": "Pending concept command recovery",
					children: [
						/* @__PURE__ */ _jsx("span", {
							id: `ct-${n.id}-command-hint`,
							className: "muted",
							children: "The earlier re-tag is still unresolved. Check that command before closing this draft."
						}),
						/* @__PURE__ */ _jsxs("div", {
							className: "concept-tag-latest",
							children: [
								/* @__PURE__ */ _jsx("b", { children: "Submitted tags:" }),
								" ",
								intent.concepts?.length ? intent.concepts.join(", ") : "none (clear all)"
							]
						}),
						/* @__PURE__ */ _jsx("button", {
							type: "button",
							className: "btn xs",
							onClick: copyPendingInput,
							children: "Copy pending input"
						})
					]
				}),
				/* @__PURE__ */ _jsxs("div", {
					className: "concept-tag-actions",
					children: [/* @__PURE__ */ _jsx("button", {
						type: "button",
						className: "btn sm",
						disabled: busy || !operationFenced && (baselineChanged || !canEdit),
						"aria-describedby": operationFenced ? `ct-${n.id}-command-hint` : undefined,
						onClick: save,
						children: busy ? operationFenced ? "Checking…" : exactRetry ? "Retrying…" : "Saving…" : operationFenced ? "Check command" : exactRetry ? "Retry same command" : "Save tags"
					}), /* @__PURE__ */ _jsx("button", {
						type: "button",
						className: "btn sm ghost",
						disabled: busy || operationFenced,
						"aria-describedby": operationFenced ? `ct-${n.id}-command-hint` : undefined,
						title: operationFenced ? "Check the pending re-tag before closing this draft" : undefined,
						onClick: cancel,
						children: "Cancel"
					})]
				})
			]
		})
	] });
}
function Overview({ n, state, runId, onToast, draftStore, expectedGeneration }) {
	const p = n.idea?.params || {};
	const uses = mergeSummary(n, state.nodes || {}, state);
	const chg = nodeChip(n, state.nodes || {}, state);
	return /* @__PURE__ */ _jsxs(_Fragment, { children: [
		/* @__PURE__ */ _jsxs("div", {
			className: "kv",
			children: [
				/* @__PURE__ */ _jsx(KV, {
					k: "node",
					v: `#${n.id}`
				}),
				/* @__PURE__ */ _jsx(KV, {
					k: "operator",
					v: n.operator
				}),
				/* @__PURE__ */ _jsx(KV, {
					k: "parents",
					v: (n.parent_ids || []).join(", ") || "—"
				}),
				/* @__PURE__ */ _jsx(KV, {
					k: "status",
					v: n.status + (n.id === state.best_node_id ? " — champion" : "")
				}),
				/* @__PURE__ */ _jsx(KV, {
					k: "metric",
					v: fmt(n.metric)
				}),
				n.confirmed_mean != null && /* @__PURE__ */ _jsx(KV, {
					k: "robust mean",
					v: `${fmt(n.confirmed_mean)} ± ${fmt(n.confirmed_std)} (${n.confirmed_seeds}×)`
				}),
				/* @__PURE__ */ _jsx(KV, {
					k: "feasible",
					v: String(n.feasible)
				}),
				/* @__PURE__ */ _jsx(KV, {
					k: "eval seconds",
					v: fmt(n.eval_seconds)
				})
			]
		}),
		/* @__PURE__ */ _jsx(ConceptTags, {
			n,
			state,
			runId,
			onToast,
			draftStore,
			expectedGeneration
		}, `${runId}:${expectedGeneration || "?"}:${n.id}:${n.attempt}`),
		/* @__PURE__ */ _jsx(StagePipeline, {
			stages: n.stages,
			failed: n.failed_stage,
			runId,
			id: n.id,
			generation: n.attempt,
			onToast
		}),
		chg && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
			className: "section-h",
			children: "What this node did"
		}), /* @__PURE__ */ _jsx("div", {
			className: "v",
			children: chg
		})] }),
		uses.length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
			className: "section-h",
			children: "Merge — techniques fused"
		}), /* @__PURE__ */ _jsx("ul", {
			className: "bul",
			children: uses.map((u) => /* @__PURE__ */ _jsxs("li", { children: [
				/* @__PURE__ */ _jsxs("b", { children: ["#", u.parentId] }),
				u.theme ? ` · ${u.theme}` : "",
				u.change && u.change !== "—" ? ` — ${u.change}` : ""
			] }, u.parentId))
		})] }),
		/* @__PURE__ */ _jsx("div", {
			className: "section-h",
			children: "Idea params"
		}),
		Object.keys(p).length ? /* @__PURE__ */ _jsx("div", {
			className: "kv",
			children: Object.entries(p).map(([k, v]) => /* @__PURE__ */ _jsx(KV, {
				k,
				v: fmt(v)
			}, k))
		}) : /* @__PURE__ */ _jsx("div", {
			className: "muted",
			children: "none"
		}),
		n.idea?.rationale && !(chg && chg.includes(n.idea.rationale)) && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
			className: "section-h",
			children: "Rationale"
		}), /* @__PURE__ */ _jsx(Markdown, {
			className: "rationale-md",
			text: n.idea.rationale
		})] }),
		n.deleted?.length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
			className: "section-h",
			children: "Deleted files"
		}), /* @__PURE__ */ _jsx("div", {
			className: "v",
			children: n.deleted.join(", ")
		})] })
	] });
}
// Trace timeline bounds: earliest start + total wall-span across the forest, so every span bar can be
// positioned by its OFFSET from t0 (a langfuse-style waterfall) rather than just sized by duration.
function traceBounds(spans) {
	let lo = Infinity, hi = 0;
	const walk = (arr) => (arr || []).forEach((s) => {
		const st = typeof s.start === "number" ? s.start : null;
		const en = st != null ? st + (s.duration_s || 0) : s.duration_s || 0;
		if (st != null && st < lo) lo = st;
		if (en > hi) hi = en;
		walk(s.children);
	});
	walk(spans);
	if (!isFinite(lo)) lo = 0;
	return {
		t0: lo,
		total: Math.max(1e-9, hi - lo)
	};
}
// Friendly identity for each span kind — turns recorded span names into "who did what" so the trace
// reads as the node's life story rather than instrumentation. `tone` colours the waterfall bar so
// phases are distinguishable at a glance. (Span names come from orchestrator.py.)
// icon = an OpIcon glyph name (monochrome, inherits the stage tone via currentColor — no color emoji).
// Compact tuple schema: [icon, visible role, description, tone]. This metadata ships with every
// Inspector visit, so positional values avoid repeating four object keys for every trace operation.
const STAGE = {
	onboard: [
		"flag",
		"Onboarding",
		"task setup & eval spec",
		"#8a7bb0"
	],
	create_node: [
		"trending",
		"Author node",
		"propose an idea, then build the solution",
		"#6f8bb0"
	],
	propose: [
		"search",
		"Researcher · propose",
		"propose the next idea",
		"#6fa3b0"
	],
	// the Developer's own sub-phases (repo tasks): STAGES declares the eval pipeline, PLAN decomposes
	// the change into atomic steps — both read-only, before the write-capable implement session(s).
	stages: [
		"sliders",
		"Developer · stages",
		"declare the eval pipeline (prep → train → …)",
		"#5f9e8f"
	],
	plan: [
		"doc",
		"Developer · plan",
		"decompose into atomic steps",
		"#7fae8f"
	],
	"handoff-summary": [
		"doc",
		"Handoff summary",
		"distill this phase for the next (fewer re-reads downstream)",
		"#8fa8b8"
	],
	implement: [
		"gear",
		"Developer · implement",
		"write / edit the solution code",
		"#6fae97"
	],
	repair: [
		"bug",
		"Developer · repair",
		"fix a failed parent",
		"#b0936f"
	],
	inline_repair: [
		"bug",
		"Developer · inline repair",
		"quick in-eval fix attempts",
		"#b08a6f"
	],
	seed_workspace: [
		"gear",
		"Workspace",
		"materialize node files into the eval workdir",
		"#8b96a5"
	],
	evaluate: [
		"target",
		"Evaluate",
		"run the solution & score it",
		"#a87da8"
	],
	triage: [
		"bug",
		"Triage",
		"a failed node — decide repair / abandon / reject-idea",
		"#b07a7a"
	],
	// declared eval-pipeline stages (looplab_stages.json): each runs as its own block in the node story
	train: [
		"replay",
		"Train",
		"declared pipeline stage: train a fresh model",
		"#4e8f5d"
	],
	data_prep: [
		"sliders",
		"Data prep",
		"declared pipeline stage: prepare data/features",
		"#7a9e5f"
	],
	score: [
		"target",
		"Evaluate · score",
		"operator's protected scoring stage",
		"#a87da8"
	],
	confirm_seed: [
		"replay",
		"Confirmation",
		"multi-seed robustness check",
		"#9aa06f"
	],
	ablate: [
		"sliders",
		"Ablation",
		"sensitivity probe",
		"#6f8bb0"
	],
	// sub-operation traces the engine wraps in their own named span — give each a distinct hue so the
	// conversation reads as coloured bands (foresight vs strategy vs research vs merge) at a glance.
	// Two DISTINCT Researcher ranking steps — kept apart so the first doesn't read as a duplicate of
	// the second: `hyp_prioritize` runs BEFORE propose (pick which open hypothesis to pursue),
	// `foresight_rank` runs AFTER propose (predict the chosen proposal's payoff, best-of-N pick).
	hyp_prioritize: [
		"bulb",
		"Researcher · prioritize",
		"rank the open-hypothesis board",
		"#c2a24e"
	],
	foresight_rank: [
		"bulb",
		"Researcher · foresight",
		"predict payoff of the chosen idea",
		"#c2a24e"
	],
	foresight: [
		"bulb",
		"Researcher · foresight",
		"predict payoff of the chosen idea",
		"#c2a24e"
	],
	strategy_consult: [
		"trending",
		"Strategist",
		"pick policy / operators / fidelity",
		"#b0729e"
	],
	strategy_decision: [
		"trending",
		"Strategist",
		"pick policy / operators / fidelity",
		"#b0729e"
	],
	hypothesis_merge: [
		"confluence",
		"Hypothesis merge",
		"fold paraphrase hypotheses",
		"#5fa0a8"
	],
	deep_research: [
		"search",
		"Deep research",
		"read the literature first",
		"#6fb0a3"
	],
	lessons: [
		"doc",
		"Lessons",
		"reflect / distil cross-run lessons",
		"#9a8fb0"
	],
	lessons_distill: [
		"doc",
		"Lessons",
		"reflect / distil cross-run lessons",
		"#9a8fb0"
	],
	lessons_refresh: [
		"doc",
		"Lessons",
		"reflect / distil cross-run lessons",
		"#9a8fb0"
	],
	novelty: [
		"gitbranch",
		"Novelty gate",
		"dedup near-duplicate proposals",
		"#a89a6f"
	]
};
const stageMeta = (name) => STAGE[name] || [
	"dot",
	name,
	"",
	"var(--accent)"
];
// Compact info helpers so each trace row carries the data that DIFFERENTIATES it (langfuse/Phoenix
// convention: model · input→output tokens · a content preview), instead of a bare op name repeated.
const ktok = (n) => n == null ? "" : n >= 1e3 ? +(n / 1e3).toFixed(n >= 9950 ? 0 : 1) + "k" : String(n);
const shortModel = (m) => (m || "").split("/").pop();
// Roll the whole subtree of a span up to "how many model calls and how many tokens it cost" — shown on
// the stage/span header so you see the expensive steps without expanding anything. Counts first-class
// GENERATION spans. Projection schema 2 deliberately drops legacy event-embedded I/O.
function spanRollup(s) {
	// tok = SUM of every call's total (billed — a tool loop re-sends the growing context each turn, O(n²)).
	// ctx = the PEAK single prompt = the real context-window size. out = generated tokens. The UI shows
	// ctx + out (billed tok in the tooltip) so the number reads as "context", not the re-send sum.
	let calls = 0, tok = 0, ctx = 0, out = 0;
	const walk = (x) => {
		if (x.kind === "generation") {
			calls++;
			const u = (x.attributes || {}).usage || {};
			const p = u.prompt || 0;
			tok += u.total != null ? u.total : p + (u.completion || 0);
			ctx = Math.max(ctx, p);
			out += u.completion || 0;
		}
		;
		(x.children || []).forEach(walk);
	};
	walk(s);
	return {
		calls,
		tok,
		ctx,
		out
	};
}
// Adapt a first-class GENERATION span (kind='generation', I/O held in attributes) to the same
// {op,model,prompt,completion,tokens,thinking,tool_calls} shape the legacy llm_call renderer uses —
// so a generation span and an old llm_call event display identically.
function genToCall(s) {
	const a = s.attributes || {}, u = a.usage || {};
	return {
		op: a.op,
		model: a.model,
		prompt: a.input || [],
		completion: typeof a.output === "string" ? a.output : a.output != null ? JSON.stringify(a.output, null, 2) : "",
		thinking: a.thinking,
		tool_calls: a.tool_calls,
		model_parameters: a.model_parameters,
		cost: a.cost,
		tokens: u
	};
}
const asText = (v) => v == null ? "" : typeof v === "string" ? v : JSON.stringify(v, null, 2);
// The expandable body of a generation: the INPUT (prompt messages) and the OUTPUT (the model's text),
// plus a collapsed reasoning disclosure. Tool CALLS are NOT shown here — they render as their own
// indented tool observations directly beneath this chat (no duplication); when a turn produced only
// tool calls, its output is empty and we say so, pointing at the tools below.
function GenBody({ c }) {
	const [think, setThink] = useState(false);
	const nTools = (c.tool_calls || []).length;
	return /* @__PURE__ */ _jsxs("div", {
		className: "llm-io",
		children: [
			(c.model || c.model_parameters || c.cost != null) && /* @__PURE__ */ _jsxs("div", {
				className: "kv",
				children: [
					c.model && /* @__PURE__ */ _jsx(KV, {
						k: "model",
						v: c.model
					}),
					c.model_parameters && /* @__PURE__ */ _jsx(KV, {
						k: "params",
						v: JSON.stringify(c.model_parameters)
					}),
					c.cost ? /* @__PURE__ */ _jsx(KV, {
						k: "cost",
						v: "$" + c.cost
					}) : null
				]
			}),
			/* @__PURE__ */ _jsx("div", {
				className: "gen-sec-h",
				children: "input"
			}),
			(c.prompt || []).map((m, i) => /* @__PURE__ */ _jsxs("div", {
				className: "msg",
				children: [/* @__PURE__ */ _jsx("div", {
					className: "msg-role role-" + (m.role || "user"),
					children: m.role
				}), /* @__PURE__ */ _jsx("pre", {
					className: "code",
					children: m.content
				})]
			}, i)),
			/* @__PURE__ */ _jsx("div", {
				className: "gen-sec-h",
				children: "output"
			}),
			c.completion ? /* @__PURE__ */ _jsx("div", {
				className: "msg",
				children: /* @__PURE__ */ _jsx("pre", {
					className: "code",
					children: c.completion
				})
			}) : /* @__PURE__ */ _jsx("div", {
				className: "muted generation-empty",
				children: nTools ? `→ called ${nTools} tool${nTools > 1 ? "s" : ""} (shown below)` : "(no text output)"
			}),
			c.thinking && /* @__PURE__ */ _jsxs("div", {
				className: "msg think-debug",
				children: [/* @__PURE__ */ _jsxs("button", {
					type: "button",
					className: "msg-role role-think disclosure-button",
					"aria-expanded": think,
					onClick: () => setThink((v) => !v),
					children: [think ? "▾" : "▸", " reasoning (debug)"]
				}), think && /* @__PURE__ */ _jsx(Markdown, {
					className: "think-body",
					text: c.thinking
				})]
			})
		]
	});
}
// Render a list of sibling spans. Two behaviours:
//  • INDENT each tool observation one level under the generation before it — in the tool-loop the
//    sequence is (chat → tool → tool → chat → …), so a tool belongs to the last chat, making "which
//    chat called this tool" obvious without re-parenting the trace.
//  • CAP how many are rendered at once (a heavily-repaired node can have 800+ spans — rendering them
//    all freezes the browser / black screen). Show the first SPAN_CAP, then a "show N more" button.
//    This local reveal remains subject to the server's bounded/redacted projection and omission receipt.
const SPAN_CAP = 60;
export function TraceUnavailable({ label = "Trace unavailable.", onRetry, pending = false }) {
	return /* @__PURE__ */ _jsxs("div", {
		className: "notice resource-error compact",
		role: "alert",
		children: [/* @__PURE__ */ _jsx("span", { children: label }), onRetry && /* @__PURE__ */ _jsx("button", {
			type: "button",
			className: "btn sm",
			onClick: onRetry,
			disabled: pending,
			children: pending ? "Retrying…" : "Retry trace"
		})]
	});
}
function SpanList({ items, depth, t0, total, runId, parentOp = null }) {
	const [all, setAll] = useState(false);
	const rows = [];
	let genDepth = null;
	(items || []).forEach((c, i) => {
		const kind = c.kind || "operation";
		if (kind === "tool" && genDepth != null) {
			rows.push({
				c,
				d: genDepth + 1,
				i
			});
		} else {
			rows.push({
				c,
				d: depth,
				i
			});
			genDepth = kind === "generation" ? depth : null;
		}
	});
	const shown = all ? rows : rows.slice(0, SPAN_CAP);
	return /* @__PURE__ */ _jsxs(_Fragment, { children: [shown.map(({ c, d, i }) => /* @__PURE__ */ _jsx(SpanRow, {
		s: c,
		depth: d,
		t0,
		total,
		runId,
		parentOp
	}, `${runId}:${c.span_id || i}`)), !all && rows.length > SPAN_CAP && /* @__PURE__ */ _jsxs("button", {
		className: "span-more",
		style: { marginLeft: depth * 14 + 4 },
		onClick: () => setAll(true),
		children: [
			"… show ",
			rows.length - SPAN_CAP,
			" more observations"
		]
	})] });
}
// One span and its subtree, drawn as a langfuse-style waterfall row: the bar is positioned by the
// span's OFFSET from the trace start (t0) and sized by its duration, so sequence reads at a glance.
// Renders three observation kinds distinctly — GENERATION (an LLM call: op·model·in→out·preview, its
// prompt/output on expand), TOOL (name·arg, its input/output on expand), and OPERATION (a phase of
// work) — so the tree shows exactly what called what and what each bounded projection produced.
function SpanRow({ s, depth, t0, total, runId, parentOp = null }) {
	const [open, setOpen] = useState(false);
	const [io, setIo] = useState(null);
	const kind = s.kind || "operation";
	const err = s.status === "ERROR";
	const off = typeof s.start === "number" ? Math.max(0, (s.start - t0) / total * 100) : 0;
	const wid = Math.max(1.5, (s.duration_s || 0) / total * 100);
	const barTone = err ? "var(--fail)" : kind === "generation" ? "var(--accent)" : kind === "tool" ? "var(--working)" : stageMeta(s.name)[3];
	const bar = /* @__PURE__ */ _jsx("span", {
		className: "span-bar",
		children: /* @__PURE__ */ _jsx("span", {
			className: "span-fill",
			style: {
				marginLeft: Math.min(98, off) + "%",
				width: wid + "%",
				background: barTone
			}
		})
	});
	const kids = /* @__PURE__ */ _jsx(SpanList, {
		items: s.children,
		depth: depth + 1,
		t0,
		total,
		runId,
		parentOp: s.name
	});
	const rowIndent = { paddingLeft: depth * 14 };
	const detailIndent = { marginLeft: depth * 14 + 16 };
	// On first expand, pull the bounded/redacted detail projection; its omission receipt is rendered.
	useEffect(() => {
		if (open && io === null && runId && s.span_id && (kind === "generation" || kind === "tool")) {
			let on = true;
			spanDetail(runId, s.span_id).then((d) => on && setIo(traceDetailState(d))).catch(() => on && setIo(unavailableTraceDetail()));
			return () => {
				on = false;
			};
		}
	}, [
		open,
		io,
		runId,
		s.span_id,
		kind
	]);
	const retryIo = () => setIo(null);
	if (kind === "generation") {
		// Row header from the LIGHT span (op·model·tokens); the prompt/output come from the fetched `io`.
		const a = {
			...s.attributes || {},
			...io?.attributes || {}
		};
		const c = genToCall({
			...s,
			attributes: a
		}), t = c.tokens;
		return /* @__PURE__ */ _jsxs(_Fragment, { children: [
			/* @__PURE__ */ _jsxs("button", {
				type: "button",
				"aria-expanded": open,
				className: "span-row gen disclosure-button" + (err ? " err" : ""),
				style: rowIndent,
				onClick: () => setOpen((o) => !o),
				title: "expand for prompt & output",
				children: [
					/* @__PURE__ */ _jsx("span", {
						className: "span-tw",
						children: open ? "▾" : "▸"
					}),
					(() => {
						// call (under implement/repair) is "writing code"; the Researcher's (under propose) is "reasoning".
						const dev = parentOp === "implement" || parentOp === "repair";
						const label = dev ? "writing code" : parentOp === "propose" && a.op === "chat" ? "reasoning" : a.op || "llm";
						return /* @__PURE__ */ _jsxs("span", {
							className: "span-name gen",
							children: [
								/* @__PURE__ */ _jsx(OpIcon, {
									name: dev ? "pencil" : "bulb",
									className: "t-ic"
								}),
								" ",
								/* @__PURE__ */ _jsx("span", {
									className: "llm-op" + (dev ? " dev-code" : ""),
									children: label
								}),
								a.model && /* @__PURE__ */ _jsx("span", {
									className: "llm-model",
									title: a.model,
									children: shortModel(a.model)
								})
							]
						});
					})(),
					bar,
					/* @__PURE__ */ _jsxs("span", {
						className: "t",
						children: [fmt(s.duration_s, 3), "s"]
					}),
					(t.prompt != null || t.completion != null) && /* @__PURE__ */ _jsxs("span", {
						className: "badge",
						title: `${t.prompt || 0} prompt → ${t.completion || 0} completion tokens`,
						children: [
							ktok(t.prompt),
							"→",
							ktok(t.completion)
						]
					}),
					err && /* @__PURE__ */ _jsx("span", {
						className: "badge reason",
						children: "ERROR"
					})
				]
			}),
			open && /* @__PURE__ */ _jsx("div", {
				className: "span-detail",
				style: detailIndent,
				children: io === null ? /* @__PURE__ */ _jsx("div", {
					className: "muted trace-small",
					role: "status",
					children: "loading…"
				}) : io.status === "unavailable" ? /* @__PURE__ */ _jsx(TraceUnavailable, {
					label: "Trace detail unavailable.",
					onRetry: retryIo
				}) : /* @__PURE__ */ _jsxs(_Fragment, { children: [io.partial && /* @__PURE__ */ _jsx("div", {
					className: "notice compact",
					role: "status",
					children: "Trace detail truncated."
				}), /* @__PURE__ */ _jsx(GenBody, { c })] })
			}),
			kids
		] });
	}
	if (kind === "tool") {
		const a = {
			...s.attributes || {},
			...io?.attributes || {}
		};
		const inp = asText(a.input), outp = asText(a.output), name = (s.attributes || {}).tool || a.tool || "tool";
		return /* @__PURE__ */ _jsxs(_Fragment, { children: [
			/* @__PURE__ */ _jsxs("button", {
				type: "button",
				"aria-expanded": open,
				className: "span-row tool disclosure-button" + (err ? " err" : ""),
				style: rowIndent,
				onClick: () => setOpen((o) => !o),
				title: "expand for input & output",
				children: [
					/* @__PURE__ */ _jsx("span", {
						className: "span-tw",
						children: open ? "▾" : "▸"
					}),
					/* @__PURE__ */ _jsxs("span", {
						className: "span-name tool",
						children: [
							/* @__PURE__ */ _jsx(OpIcon, {
								name: "gear",
								className: "t-ic"
							}),
							" ",
							/* @__PURE__ */ _jsx("b", {
								className: "tool-name",
								children: name
							})
						]
					}),
					bar,
					/* @__PURE__ */ _jsxs("span", {
						className: "t",
						children: [fmt(s.duration_s, 3), "s"]
					}),
					err && /* @__PURE__ */ _jsx("span", {
						className: "badge reason",
						children: "ERROR"
					})
				]
			}),
			open && /* @__PURE__ */ _jsx("div", {
				className: "span-detail",
				style: detailIndent,
				children: io === null ? /* @__PURE__ */ _jsx("div", {
					className: "muted trace-small",
					role: "status",
					children: "loading…"
				}) : io.status === "unavailable" ? /* @__PURE__ */ _jsx(TraceUnavailable, {
					label: "Trace detail unavailable.",
					onRetry: retryIo
				}) : /* @__PURE__ */ _jsxs(_Fragment, { children: [
					io.partial && /* @__PURE__ */ _jsx("div", {
						className: "notice compact",
						role: "status",
						children: "Trace detail truncated."
					}),
					inp && /* @__PURE__ */ _jsxs("div", {
						className: "msg",
						children: [/* @__PURE__ */ _jsx("div", {
							className: "msg-role role-user",
							children: "input"
						}), /* @__PURE__ */ _jsx("pre", {
							className: "code",
							children: inp
						})]
					}),
					outp && /* @__PURE__ */ _jsxs("div", {
						className: "msg",
						children: [/* @__PURE__ */ _jsx("div", {
							className: "msg-role role-completion",
							children: "output"
						}), /* @__PURE__ */ _jsx("pre", {
							className: "code",
							children: outp
						})]
					}),
					!inp && !outp && /* @__PURE__ */ _jsx("div", {
						className: "muted trace-small",
						children: "(no input/output recorded)"
					})
				] })
			}),
			kids
		] });
	}
	// OPERATION span (a phase of work): bounded attributes and events.
	const attrs = Object.entries(s.attributes || {}).filter(([k]) => k !== "node_id");
	const events = s.events || [];
	const [icon, role, desc] = stageMeta(s.name);
	const detail = attrs.length || events.length;
	const OperationHeader = detail ? "button" : "div";
	return /* @__PURE__ */ _jsxs(_Fragment, { children: [
		/* @__PURE__ */ _jsxs(OperationHeader, {
			type: detail ? "button" : undefined,
			"aria-expanded": detail ? open : undefined,
			className: "span-row" + (detail ? " disclosure-button" : "") + (err ? " err" : ""),
			style: rowIndent,
			onClick: detail ? () => setOpen((o) => !o) : undefined,
			title: detail ? "click for step detail" : "",
			children: [
				/* @__PURE__ */ _jsx("span", {
					className: "span-tw",
					children: detail ? open ? "▾" : "▸" : "·"
				}),
				/* @__PURE__ */ _jsxs("span", {
					className: "span-name",
					title: desc,
					children: [
						/* @__PURE__ */ _jsx(OpIcon, {
							name: icon,
							className: "t-ic"
						}),
						" ",
						role !== s.name ? role : s.name
					]
				}),
				bar,
				/* @__PURE__ */ _jsxs("span", {
					className: "t",
					children: [fmt(s.duration_s, 3), "s"]
				}),
				err && /* @__PURE__ */ _jsx("span", {
					className: "badge reason",
					children: "ERROR"
				})
			]
		}),
		open && detail && /* @__PURE__ */ _jsxs("div", {
			className: "span-detail",
			style: detailIndent,
			children: [attrs.length > 0 && /* @__PURE__ */ _jsx("div", {
				className: "kv",
				children: attrs.map(([k, v]) => /* @__PURE__ */ _jsx(KV, {
					k,
					v: typeof v === "object" ? JSON.stringify(v) : String(v)
				}, k))
			}), events.map((e, i) => /* @__PURE__ */ _jsxs("div", {
				className: "span-ev",
				children: [/* @__PURE__ */ _jsx("span", {
					className: "ty",
					children: e.name
				}), e.error ? /* @__PURE__ */ _jsxs("span", {
					className: "flag",
					children: [" ", e.error]
				}) : /* @__PURE__ */ _jsxs("span", {
					className: "muted",
					children: [" ", Object.entries(e).filter(([k]) => k !== "name").map(([k, v]) => `${k}=${v}`).join(" ")]
				})]
			}, i))]
		}),
		kids
	] });
}
// A top-level lifecycle stage (one root span = one phase of work on this node), with its sub-steps.
// The header rolls up the stage's model-call count + token cost so the expensive phases stand out.
function StageBlock({ s, t0, total, runId }) {
	const [icon, role, desc] = stageMeta(s.name);
	const roll = spanRollup(s);
	return /* @__PURE__ */ _jsxs("div", {
		className: "stage" + (s.status === "ERROR" ? " err" : ""),
		children: [/* @__PURE__ */ _jsxs("div", {
			className: "stage-h",
			title: desc,
			children: [
				/* @__PURE__ */ _jsx("span", {
					className: "stage-ic",
					children: /* @__PURE__ */ _jsx(OpIcon, { name: icon })
				}),
				/* @__PURE__ */ _jsx("b", { children: role }),
				roll.calls > 0 && /* @__PURE__ */ _jsxs("span", {
					className: "stage-roll",
					title: `${roll.tok} billed tokens`,
					children: [
						roll.calls,
						" call",
						roll.calls > 1 ? "s" : "",
						roll.ctx ? ` · ${ktok(roll.ctx)} ctx` : "",
						roll.out ? ` · ${ktok(roll.out)} out` : ""
					]
				}),
				/* @__PURE__ */ _jsx("span", { className: "spacer" }),
				/* @__PURE__ */ _jsxs("span", {
					className: "t",
					children: [fmt(s.duration_s, 3), "s"]
				})
			]
		}), /* @__PURE__ */ _jsx("div", {
			className: "spans",
			children: (s.children || []).length ? /* @__PURE__ */ _jsx(SpanList, {
				items: s.children,
				depth: 0,
				t0,
				total,
				runId
			}) : /* @__PURE__ */ _jsx(SpanRow, {
				s,
				depth: 0,
				t0,
				total,
				runId
			})
		})]
	});
}
// Reusable langfuse-style trace for ONE node's span forest — the lifecycle stages on a shared
// timeline. Exported so the chat feed can show the same waterfall inline (Dock.jsx) as the Inspector.
export function NodeTrace({ spans, runId, projection = {}, onRetry, onLoadMore }) {
	const roots = spans || [];
	if (traceUnavailable(projection)) return /* @__PURE__ */ _jsx(TraceUnavailable, { onRetry });
	const partial = tracePartial(projection);
	// Prefer an ACTIONABLE control over a dead "projection is partial" notice. The receipt remains in
	// projection; repeating its optional count in this hot render path added branches without utility.
	const loadMore = partial && onLoadMore ? /* @__PURE__ */ _jsx("button", {
		type: "button",
		className: "trace-loadmore disclosure-button",
		onClick: onLoadMore,
		children: "↧ load more spans"
	}) : null;
	if (!roots.length) {
		if (loadMore) return loadMore;
		if (partial) return /* @__PURE__ */ _jsx("div", {
			className: "notice compact",
			role: "status",
			children: "Trace projection is partial; no observations were included."
		});
		return /* @__PURE__ */ _jsx("div", {
			className: "muted trace-small",
			children: "No execution spans captured yet."
		});
	}
	const { t0, total } = traceBounds(roots);
	return /* @__PURE__ */ _jsxs("div", {
		className: "trace",
		children: [loadMore || partial && /* @__PURE__ */ _jsx("div", {
			className: "notice compact",
			role: "status",
			children: "Trace projection is partial."
		}), roots.map((s, i) => /* @__PURE__ */ _jsx(StageBlock, {
			s,
			t0,
			total,
			runId
		}, `${runId}:${s.span_id || i}`))]
	});
}
// The coding-agent's own validation report (was its own tab) — folded into the lifecycle as the
// Developer stage's verification footnote, only when an external agent actually wrote the node.
function AgentReport({ r }) {
	return /* @__PURE__ */ _jsxs("div", {
		className: "stage",
		children: [/* @__PURE__ */ _jsxs("div", {
			className: "stage-h",
			children: [
				/* @__PURE__ */ _jsx("span", {
					className: "stage-ic",
					style: { color: r.ok && !r.fell_back ? "var(--ok)" : r.fell_back ? "var(--working)" : "var(--fail)" },
					children: /* @__PURE__ */ _jsx(OpIcon, { name: r.ok && !r.fell_back ? "check" : r.fell_back ? "replay" : "cross" })
				}),
				/* @__PURE__ */ _jsx("b", { children: "Developer · agent validation" }),
				/* @__PURE__ */ _jsx("span", {
					className: "muted",
					children: r.fell_back ? "fell back to template" : r.ok ? "shipped clean" : "failed checks"
				}),
				/* @__PURE__ */ _jsx("span", { className: "spacer" }),
				/* @__PURE__ */ _jsxs("span", {
					className: "muted",
					children: [
						r.attempts,
						" attempt",
						r.attempts === 1 ? "" : "s"
					]
				})
			]
		}), /* @__PURE__ */ _jsx(DataTable, {
			caption: "Agent attempt validation checks",
			card: false,
			children: /* @__PURE__ */ _jsxs("table", {
				className: "tbl",
				children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
					/* @__PURE__ */ _jsx("th", { children: "check" }),
					/* @__PURE__ */ _jsx("th", { children: "ok" }),
					/* @__PURE__ */ _jsx("th", { children: "detail" })
				] }) }), /* @__PURE__ */ _jsx("tbody", { children: (r.checks || []).map((c, i) => /* @__PURE__ */ _jsxs("tr", { children: [
					/* @__PURE__ */ _jsx("td", { children: c.name }),
					/* @__PURE__ */ _jsx("td", {
						style: { color: c.ok ? "var(--ok)" : "var(--fail)" },
						children: c.ok ? "✓" : "✗"
					}),
					/* @__PURE__ */ _jsx("td", {
						className: "muted",
						children: c.detail || c.severity || ""
					})
				] }, i)) })]
			})
		})]
	});
}
// ── linear conversation view ─────────────────────────────────────────────────────────────────────
// The span-tree projection can re-show the retained re-sent message list on every generation (a
// tool-loop re-sends growing history each turn). The conversation projection reconstructs the loop as
// a readable thread: the request once per sub-loop, then each retained generation delta + tool calls.
function ConvRequest({ t }) {
	const [open, setOpen] = useState(false);
	const roles = (t.messages || []).map((m) => m.role).join(" + ");
	return /* @__PURE__ */ _jsxs("div", {
		className: "conv-req",
		children: [/* @__PURE__ */ _jsxs("button", {
			type: "button",
			className: "conv-req-h disclosure-button",
			"aria-expanded": open,
			onClick: () => setOpen((o) => !o),
			title: "the system + user prompt for this sub-loop (shown once)",
			children: [
				/* @__PURE__ */ _jsx("span", {
					className: "span-tw",
					children: open ? "▾" : "▸"
				}),
				/* @__PURE__ */ _jsx(OpIcon, {
					name: "chat",
					className: "t-ic"
				}),
				" ",
				/* @__PURE__ */ _jsx("b", { children: "request" }),
				t.label && /* @__PURE__ */ _jsx("span", {
					className: "llm-op",
					children: t.label
				}),
				/* @__PURE__ */ _jsxs("span", {
					className: "muted conv-req-roles",
					children: [" ", roles]
				})
			]
		}), open && /* @__PURE__ */ _jsx("div", {
			className: "conv-req-body",
			children: (t.messages || []).map((m, i) => /* @__PURE__ */ _jsxs("div", {
				className: "msg",
				children: [/* @__PURE__ */ _jsx("div", {
					className: "msg-role role-" + (m.role || "user"),
					children: m.role
				}), /* @__PURE__ */ _jsx("pre", {
					className: "code",
					children: m.content
				})]
			}, i))
		})]
	});
}
function ConvGen({ t }) {
	const [think, setThink] = useState(false);
	const calls = t.tool_calls || [];
	const u = t.usage || {};
	const tok = u.total || (u.prompt || 0) + (u.completion || 0);
	// strip the trailing "[tool_calls: …]" marker — the calls are their own chip + the tool rows below
	const text = (t.output || "").replace(/\n*\[tool_calls:[^\]]*\]\s*$/, "").trim();
	return /* @__PURE__ */ _jsxs("div", {
		className: "conv-gen" + (t.status === "ERROR" ? " err" : ""),
		children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "conv-gen-h",
				children: [
					/* @__PURE__ */ _jsx(OpIcon, {
						name: "bulb",
						className: "t-ic"
					}),
					t.model && /* @__PURE__ */ _jsx("span", {
						className: "llm-model",
						title: t.model,
						children: shortModel(t.model)
					}),
					tok ? /* @__PURE__ */ _jsxs("span", {
						className: "badge",
						title: `${u.prompt || 0} prompt → ${u.completion || 0} completion tokens`,
						children: [ktok(tok), " tok"]
					}) : null,
					t.seconds != null && /* @__PURE__ */ _jsxs("span", {
						className: "t",
						children: [fmt(t.seconds, 2), "s"]
					}),
					t.status === "ERROR" && /* @__PURE__ */ _jsx("span", {
						className: "badge reason",
						children: "ERROR"
					})
				]
			}),
			t.think && /* @__PURE__ */ _jsxs("div", {
				className: "msg think-debug",
				children: [/* @__PURE__ */ _jsxs("button", {
					type: "button",
					className: "msg-role role-think disclosure-button",
					"aria-expanded": think,
					onClick: () => setThink((v) => !v),
					children: [think ? "▾" : "▸", " thinking"]
				}), think && /* @__PURE__ */ _jsx(Markdown, {
					className: "think-body",
					text: t.think
				})]
			}),
			text && /* @__PURE__ */ _jsx("div", {
				className: "conv-out",
				children: /* @__PURE__ */ _jsx(Markdown, { text })
			}),
			calls.length > 0 && /* @__PURE__ */ _jsxs("div", {
				className: "conv-calls muted",
				children: ["→ called ", calls.join(", ")]
			}),
			!text && !t.think && calls.length === 0 && /* @__PURE__ */ _jsx("div", {
				className: "muted trace-small",
				children: "(no output)"
			})
		]
	});
}
function ConvTool({ t }) {
	const [open, setOpen] = useState(false);
	const err = t.status === "ERROR";
	return /* @__PURE__ */ _jsxs("div", {
		className: "conv-tool" + (err ? " err" : ""),
		children: [/* @__PURE__ */ _jsxs("button", {
			type: "button",
			className: "conv-tool-h disclosure-button",
			"aria-expanded": open,
			onClick: () => setOpen((o) => !o),
			title: "tool call — expand for input & output",
			children: [
				/* @__PURE__ */ _jsx("span", {
					className: "span-tw",
					children: open ? "▾" : "▸"
				}),
				/* @__PURE__ */ _jsx(OpIcon, {
					name: "gear",
					className: "t-ic"
				}),
				" ",
				/* @__PURE__ */ _jsx("b", {
					className: "tool-name",
					children: t.name
				}),
				!open && t.input && /* @__PURE__ */ _jsxs("span", {
					className: "muted conv-tool-prev",
					children: [" ", t.input.slice(0, 60)]
				}),
				err && /* @__PURE__ */ _jsx("span", {
					className: "badge reason",
					children: "ERROR"
				}),
				t.seconds != null && /* @__PURE__ */ _jsxs("span", {
					className: "t",
					children: [fmt(t.seconds, 2), "s"]
				})
			]
		}), open && /* @__PURE__ */ _jsxs("div", {
			className: "conv-tool-body",
			children: [
				t.input && /* @__PURE__ */ _jsxs("div", {
					className: "msg",
					children: [/* @__PURE__ */ _jsx("div", {
						className: "msg-role role-user",
						children: "input"
					}), /* @__PURE__ */ _jsx("pre", {
						className: "code",
						children: t.input
					})]
				}),
				t.output && /* @__PURE__ */ _jsxs("div", {
					className: "msg",
					children: [/* @__PURE__ */ _jsx("div", {
						className: "msg-role role-completion",
						children: "output"
					}), /* @__PURE__ */ _jsx("pre", {
						className: "code",
						children: t.output
					})]
				}),
				!t.input && !t.output && /* @__PURE__ */ _jsx("div", {
					className: "muted trace-small",
					children: "(no input/output recorded)"
				})
			]
		})]
	});
}
// The live stdout/stderr of a stage's subprocess (training epochs, eval scoring), rendered INSIDE its
// trace band. Auto-scrolls to the newest line while the stage is live so a running train tails itself.
function StageLog({ text, live }) {
	const ref = useRef(null);
	const shown = text.length > 4e4 ? text.slice(-4e4) : text;
	// Auto-tail while live, but ONLY if the user is already parked near the bottom — otherwise scrolling
	// up to read an earlier epoch would be yanked back down on every 4s poll (no follow-toggle here).
	useEffect(() => {
		const el = ref.current;
		if (live && el && el.scrollHeight - el.scrollTop - el.clientHeight < 40) el.scrollTop = el.scrollHeight;
	}, [text, live]);
	return /* @__PURE__ */ _jsxs("div", {
		className: "stage-log",
		children: [/* @__PURE__ */ _jsxs("div", {
			className: "muted stage-log-label",
			children: ["📄 stage log", live ? " · live" : ""]
		}), /* @__PURE__ */ _jsx("pre", {
			ref,
			className: "training-log",
			children: shown
		})]
	});
}
function ConvStage({ st, defaultOpen = true, log = "", live = false }) {
	const [icon, role, desc, tone] = stageMeta(st.label);
	const [open, setOpen] = useState(defaultOpen);
	const [allTurns, setAllTurns] = useState(false);
	const roll = st.rollup || {};
	const tk = roll.tokens || {};
	const nTurns = (st.turns || []).length;
	const err = st.status === "ERROR";
	// Colour-band the stage by its tone: a left rail + a tinted header, so foresight/strategy/researcher/
	// developer/eval read as distinct bands. Click the header to collapse the whole band.
	return /* @__PURE__ */ _jsxs("div", {
		className: "stage stage-dynamic" + (err ? " err" : ""),
		style: { "--stage-tone": err ? "var(--fail)" : tone },
		children: [/* @__PURE__ */ _jsxs("button", {
			type: "button",
			className: "stage-h disclosure-button",
			"aria-expanded": open,
			title: desc + " — click to collapse",
			onClick: () => setOpen((o) => !o),
			children: [
				/* @__PURE__ */ _jsx("span", {
					className: "stage-caret",
					children: open ? "▾" : "▸"
				}),
				/* @__PURE__ */ _jsx("span", {
					className: "stage-ic",
					children: /* @__PURE__ */ _jsx(OpIcon, { name: icon })
				}),
				/* @__PURE__ */ _jsx("b", {
					className: "stage-role",
					children: role
				}),
				roll.generations || roll.tools ? /* @__PURE__ */ _jsxs("span", {
					className: "stage-roll",
					title: tk.total ? `context window peaked at ${tk.context || 0} tokens; the model generated ${tk.completion || 0}. Billed ${tk.total} total — a tool loop RE-SENDS the growing context every turn, so billed ≫ context.` : undefined,
					children: [
						roll.generations || 0,
						" turn",
						roll.generations === 1 ? "" : "s",
						roll.tools ? ` · ${roll.tools} tool call${roll.tools === 1 ? "" : "s"}` : "",
						tk.context ? ` · ${ktok(tk.context)} ctx` : "",
						tk.completion ? ` · ${ktok(tk.completion)} out` : ""
					]
				}) : null,
				!open && nTurns ? /* @__PURE__ */ _jsxs("span", {
					className: "muted stage-hidden-count",
					children: [
						"· ",
						nTurns,
						" step",
						nTurns === 1 ? "" : "s",
						" hidden"
					]
				}) : null
			]
		}), open && /* @__PURE__ */ _jsxs("div", {
			className: "conv-turns",
			children: [
				(allTurns ? st.turns || [] : (st.turns || []).slice(0, SPAN_CAP)).map((t, j) => t.type === "request" ? /* @__PURE__ */ _jsx(ConvRequest, { t }, j) : t.type === "tool" ? /* @__PURE__ */ _jsx(ConvTool, { t }, j) : /* @__PURE__ */ _jsx(ConvGen, { t }, j)),
				!allTurns && (st.turns || []).length > SPAN_CAP && /* @__PURE__ */ _jsxs("button", {
					className: "span-more",
					onClick: () => setAllTurns(true),
					children: [
						"… show ",
						(st.turns || []).length - SPAN_CAP,
						" more turns"
					]
				}),
				log ? /* @__PURE__ */ _jsx(StageLog, {
					text: log,
					live
				}) : null
			]
		})]
	});
}
function Conversation({ n, runId, working, allOpen = true, reloadNonce = 0, onRetry }) {
	const [conv, setConv] = useState(null);
	const [logs, setLogs] = useState({});
	useEffect(() => {
		setConv(null);
		setLogs({});
	}, [
		runId,
		n.id,
		working,
		reloadNonce
	]);
	usePoll((alive) => {
		const timed = deadlineRequest((signal) => Promise.allSettled([nodeConversation(runId, n.id, { signal }), get(runNodeApiPath(runId, n.id, "/logs"), {
			signal,
			cache: "no-store"
		})]), 8e3);
		timed.promise.then(([conversation, logs]) => {
			if (!alive()) return;
			setConv(conversation.status === "fulfilled" ? conversation.value || { stages: [] } : {
				stages: [],
				projection: { unavailable: true }
			});
			if (logs.status === "fulfilled") setLogs(logs.value || {});
		}).catch(() => {
			if (alive()) setConv({
				stages: [],
				projection: { unavailable: true }
			});
		});
		return timed;
	}, working ? 4e3 : null, [
		runId,
		n.id,
		working,
		reloadNonce
	]);
	if (conv === null) return /* @__PURE__ */ _jsx("div", {
		className: "muted trace-small",
		role: "status",
		children: "loading…"
	});
	const stages = conv.stages || [];
	const unavailable = traceUnavailable(conv.projection);
	const partial = tracePartial(conv.projection);
	if (unavailable) return /* @__PURE__ */ _jsx(TraceUnavailable, { onRetry });
	if (!stages.length) return /* @__PURE__ */ _jsx("div", {
		className: partial ? "notice compact" : "muted",
		role: partial ? "status" : undefined,
		children: partial ? "Trace projection is partial." : "No conversation captured for this node yet."
	});
	// The live log for a stage band: a multi-stage eval logs per stage (stages[label]); a single-command
	// eval logs to eval.log ("evaluate"/"command"); the dep-install step to setup.log. Anything else
	// (propose/implement/…) has no subprocess log.
	const logFor = (label) => logs.stages && logs.stages[label] || {
		setup: logs.setup,
		evaluate: logs.eval,
		command: logs.eval
	}[label] || "";
	// `allOpen` is owned by the sticky Trace header (so collapse-all lives in the pinned bar). It's folded
	// into each band's key so a collapse/expand-all click remounts them at the new default; a live poll
	// (allOpen unchanged) keeps the key stable, so per-band toggles survive the 4s refresh.
	return /* @__PURE__ */ _jsxs("div", {
		className: "conv",
		children: [
			partial && /* @__PURE__ */ _jsx("div", {
				className: "notice compact",
				role: "status",
				children: "Trace projection is partial."
			}),
			stages.map((st, i) => /* @__PURE__ */ _jsx(ConvStage, {
				st,
				defaultOpen: allOpen,
				log: logFor(st.label),
				live: working
			}, `${st.trace_id || ""}:${st.label || ""}:${st.start || i}:${allOpen}`)),
			logs.run_setup ? /* @__PURE__ */ _jsx(RunSetupLog, { text: logs.run_setup }) : null
		]
	});
}
// The run-level, one-time dependency install (shared by every node) — moved out of the old Training
// tab; a collapsed footnote under the trace so a setup failure is still inspectable without its own tab.
function RunSetupLog({ text }) {
	const [open, setOpen] = useState(false);
	return /* @__PURE__ */ _jsxs("div", {
		className: "stage run-setup-stage",
		children: [/* @__PURE__ */ _jsxs("button", {
			type: "button",
			className: "stage-h disclosure-button",
			"aria-expanded": open,
			onClick: () => setOpen((o) => !o),
			children: [/* @__PURE__ */ _jsx("span", {
				className: "stage-caret",
				children: open ? "▾" : "▸"
			}), /* @__PURE__ */ _jsxs("b", {
				className: "muted",
				children: ["Run setup ", /* @__PURE__ */ _jsx("span", {
					className: "normal-weight",
					children: "· deps install (run-level, once)"
				})]
			})]
		}), open && /* @__PURE__ */ _jsx("div", {
			className: "conv-turns",
			children: /* @__PURE__ */ _jsx(StageLog, {
				text,
				live: false
			})
		})]
	});
}
function Trace({ n, runId, expectedGeneration, expectedTraceRevision, live, working, onReload, detailStatus = "ready", reloadPending = false, clearScope, clearRecoveryStore, recoverClearState = null, clearRecoverySignal = null, publishClearRecovery }) {
	const [view, setView] = useState("conversation");
	const [allOpen, setAllOpen] = useState(false);
	const [nonce, setNonce] = useState(0);
	const initialClearRecovery = useRef(recoverClearState).current;
	const [clearing, setClearing] = useState(initialClearRecovery?.phase === "busy" ? "busy" : "");
	const initialOnReload = useRef(onReload).current;
	const handledClearRecoveryRevisionRef = useRef(clearRecoverySignal?.revision || 0);
	const initialClearMessage = useRef(!initialClearRecovery ? null : initialClearRecovery.phase === "busy" ? {
		kind: "status",
		blocking: true,
		pending: true,
		verifyOperation: initialClearRecovery.mode === "verify",
		text: initialClearRecovery.mode === "verify" ? "Checking whether the original trace clear completed…" : "Trace clear is still pending. This experiment will stay locked until the request settles."
	} : [
		"blocked",
		"reconcile",
		"ambiguous"
	].includes(initialClearRecovery.phase) ? initialClearRecovery.message : initialClearRecovery.phase === "confirm" ? {
		kind: "success",
		blocking: false,
		text: "Trace clear confirmation was cancelled because this view changed. Nothing was submitted."
	} : {
		kind: "error",
		blocking: true,
		text: "Trace clear state could not be recovered. Refresh this experiment before clearing."
	}).current;
	const [clearMessage, setClearMessage] = useState(initialClearMessage);
	const bodyRef = useRef(null);
	const clearTriggerRef = useRef(null);
	const clearConfirmRef = useRef(null);
	const clearRefreshRef = useRef(null);
	const nodeGeneration = Number.isSafeInteger(n.attempt) && n.attempt >= 0 ? n.attempt : null;
	const spans = n.trace?.nodes || [];
	const unavailable = traceUnavailable(n.trace?.projection);
	const partial = tracePartial(n.trace?.projection);
	const clearScopeReady = /^[0-9a-f]{64}$/.test(expectedGeneration || "") && /^[0-9a-f]{64}$/.test(expectedTraceRevision || "") && nodeGeneration != null && n.status !== "building" && detailStatus === "ready" && !reloadPending && !unavailable;
	const runWritingTrace = live?.engine_running === true;
	const runTraceOwnershipKnown = live?.engine_running === false;
	const clearAvailable = clearScopeReady && runTraceOwnershipKnown;
	const clearUnavailableText = n.status === "building" ? "Experiment creation is incomplete; trace clear is unavailable." : runWritingTrace ? "The run is active; trace clear is unavailable until it stops." : !runTraceOwnershipKnown ? "Run write ownership is being verified; trace clear is unavailable." : reloadPending ? "Experiment details are refreshing; clear is unavailable." : detailStatus === "loading" ? "Experiment details are loading; clear is unavailable." : detailStatus !== "ready" ? "Experiment details must refresh successfully before trace can be cleared." : unavailable ? "Trace diagnostics could not be loaded; clear is unavailable until a refresh succeeds." : "Trace identity is loading; clear is unavailable.";
	const agent = n.agent_report;
	// Live status: what the node is doing RIGHT NOW. Two live states: an LLM authoring the code
	// (building → writing / repairing / merging), or the sandbox running its eval pipeline (pending →
	// training / scoring). `_op` is only set in the building case (the eval has no operator), so it
	// cleanly disambiguates the two.
	// Read this node's OWN raw build marker (buildingMarkers covers EVERY concurrent build AND a
	// node_reset re-build of an existing node, which the spliced `building` flag misses because
	// withBuilding never overwrites an id already in state.nodes), not the singular `live.building`.
	const _bmarker = buildingMarkers(live).find((m) => Number(m?.node_id) === Number(n.id));
	const building = working && !!_bmarker;
	const _op = building ? _bmarker.operator || "" : "";
	const statusLabel = !working ? null : building ? /repair|debug/.test(_op) ? "🔧 repairing…" : /merge/.test(_op) ? "🔀 merging…" : "✍️ writing code…" : "🏋️ training / evaluating…";
	const status = statusLabel && /* @__PURE__ */ _jsxs("div", {
		className: "trace-live-status",
		role: "status",
		children: [
			/* @__PURE__ */ _jsx("span", { className: "tls-dot" }),
			statusLabel,
			/* @__PURE__ */ _jsx("span", {
				className: "muted trace-live-note",
				children: "live · auto-updates"
			})
		]
	});
	const retryParentTrace = () => onReload?.("retry");
	const scrollTo = (where) => {
		const c = bodyRef.current?.closest(".insp-body");
		if (c) c.scrollTop = where === "top" ? 0 : c.scrollHeight;
	};
	const storeClearRecovery = useCallback((next) => {
		const store = clearRecoveryStore?.current;
		if (!store || !clearScope) return;
		if (!next) {
			store.delete(clearScope);
			return;
		}
		store.delete(clearScope);
		store.set(clearScope, next);
		while (store.size > 64) store.delete(store.keys().next().value);
	}, [clearRecoveryStore, clearScope]);
	const setClearPhase = (phase) => {
		storeClearRecovery(phase ? { phase } : null);
		setClearing(phase);
	};
	const shouldRestoreClearFocus = () => {
		const active = document.activeElement;
		return active === document.body || !active?.isConnected || active === clearConfirmRef.current || active === clearTriggerRef.current && clearTriggerRef.current?.disabled;
	};
	useEffect(() => {
		if (!initialClearRecovery) return;
		if (initialClearRecovery.phase === "reconcile") {
			// Child effects run before the parent Inspector's request owner is installed on a full
			// remount. Defer one frame so this always starts (and supersedes) a post-POST detail read.
			const frame = requestAnimationFrame(() => {
				initialOnReload?.("trace-cleared");
				clearRefreshRef.current?.focus({ preventScroll: true });
			});
			return () => cancelAnimationFrame(frame);
		}
		if (initialClearRecovery.phase === "busy") {
			const frame = requestAnimationFrame(() => clearConfirmRef.current?.focus({ preventScroll: true }));
			return () => cancelAnimationFrame(frame);
		}
		if (initialClearRecovery.phase === "confirm") {
			storeClearRecovery(null);
			const frame = requestAnimationFrame(() => clearTriggerRef.current?.focus({ preventScroll: true }));
			return () => cancelAnimationFrame(frame);
		}
		if (!initialClearMessage) return;
		if (initialClearRecovery.phase !== "ambiguous") {
			storeClearRecovery({
				phase: "blocked",
				message: initialClearMessage
			});
		}
		const frame = requestAnimationFrame(() => clearRefreshRef.current?.focus({ preventScroll: true }));
		return () => cancelAnimationFrame(frame);
	}, [
		initialClearRecovery,
		initialClearMessage,
		initialOnReload,
		storeClearRecovery
	]);
	useEffect(() => {
		const revision = clearRecoverySignal?.revision || 0;
		if (clearRecoverySignal?.scope !== clearScope || revision <= handledClearRecoveryRevisionRef.current) return;
		handledClearRecoveryRevisionRef.current = revision;
		const recovery = clearRecoveryStore?.current?.get(clearScope);
		if (clearRecoverySignal.kind === "clear-succeeded") {
			const restoreFocus = shouldRestoreClearFocus();
			const message = recovery?.message || {
				kind: "success",
				blocking: true,
				text: "Trace was cleared. Refreshing experiment details before another clear is allowed."
			};
			setClearing("");
			setClearMessage(message);
			onReload?.("trace-cleared");
			if (restoreFocus) {
				requestAnimationFrame(() => clearRefreshRef.current?.focus({ preventScroll: true }));
			}
			return;
		}
		if (["clear-failed", "refresh-failed"].includes(clearRecoverySignal.kind)) {
			const restoreFocus = shouldRestoreClearFocus();
			const message = recovery?.message || {
				kind: "error",
				blocking: true,
				text: clearRecoverySignal.kind === "clear-failed" ? "Trace clear did not complete. Refresh this experiment before trying again." : "Experiment details could not be refreshed. Trace clear remains unavailable until a refresh succeeds."
			};
			setClearing("");
			setClearMessage(message);
			if (restoreFocus) {
				requestAnimationFrame(() => clearRefreshRef.current?.focus({ preventScroll: true }));
			}
			return;
		}
		if (clearRecoverySignal.kind !== "refresh-succeeded" || !clearMessage?.blocking) return;
		const active = document.activeElement;
		const restoreFocus = active === clearRefreshRef.current || active === document.body || !active?.isConnected;
		storeClearRecovery(null);
		setClearMessage(null);
		if (restoreFocus) {
			requestAnimationFrame(() => {
				const trigger = clearTriggerRef.current;
				const target = trigger && !trigger.disabled ? trigger : bodyRef.current?.closest(".insp-body");
				target?.focus({ preventScroll: true });
			});
		}
	}, [
		clearRecoverySignal,
		clearRecoveryStore,
		clearScope,
		clearMessage?.blocking,
		onReload,
		storeClearRecovery
	]);
	useEffect(() => {
		if (!["confirm", "busy"].includes(clearing)) return;
		const frame = requestAnimationFrame(() => {
			if (clearing === "confirm") {
				clearConfirmRef.current?.focus({ preventScroll: true });
				return;
			}
			const active = document.activeElement;
			if (active === document.body || !active?.isConnected) {
				clearConfirmRef.current?.focus({ preventScroll: true });
			}
		});
		return () => cancelAnimationFrame(frame);
	}, [clearing]);
	useEffect(() => {
		if (clearMessage?.kind !== "success" || clearMessage.blocking) return;
		const timer = setTimeout(() => setClearMessage(null), 4e3);
		return () => clearTimeout(timer);
	}, [clearMessage]);
	const finishClear = (message, recovery = null) => {
		setClearing("");
		storeClearRecovery(recovery || (message?.blocking ? {
			phase: "blocked",
			message
		} : null));
		setClearMessage(message);
		if (message?.blocking) publishClearRecovery?.(clearScope, "clear-failed");
		requestAnimationFrame(() => {
			const active = document.activeElement;
			if (active === document.body || !active?.isConnected) {
				const target = message?.blocking ? clearRefreshRef.current : clearTriggerRef.current && !clearTriggerRef.current.disabled ? clearTriggerRef.current : bodyRef.current?.closest(".insp-body");
				target?.focus({ preventScroll: true });
			}
		});
	};
	const refreshClearScope = () => {
		if (reloadPending || clearMessage?.refreshing) return;
		const recovery = clearRecoveryStore?.current?.get(clearScope);
		if (clearMessage?.verifyOperation && recovery?.operation) {
			void submitClear(recovery.operation, true);
			return;
		}
		setClearMessage((message) => message ? {
			...message,
			refreshing: true
		} : message);
		if (!onReload?.("trace-clear-recovery")) {
			const message = {
				kind: "error",
				blocking: true,
				text: "Experiment refresh could not start. Trace clear remains unavailable; use the experiment retry notice before trying again."
			};
			storeClearRecovery({
				phase: "blocked",
				message
			});
			setClearMessage(message);
		}
	};
	const submitClear = async (operation, verifying = false) => {
		const pendingMessage = verifying ? {
			kind: "status",
			blocking: true,
			pending: true,
			verifyOperation: true,
			text: "Checking whether the original trace clear completed…"
		} : null;
		storeClearRecovery({
			phase: "busy",
			operation,
			mode: verifying ? "verify" : "clear",
			...pendingMessage ? { message: pendingMessage } : {}
		});
		setClearing("busy");
		setClearMessage(pendingMessage);
		try {
			const timed = deadlineRequest((signal) => clearNodeTrace(runId, n.id, {
				expectedGeneration: operation.expectedGeneration,
				expectedTraceRevision: operation.expectedTraceRevision,
				nodeGeneration: operation.nodeGeneration,
				operationId: operation.operationId,
				signal
			}), 15e3);
			const result = await timed.promise;
			if (result?.status !== "succeeded" || result?.operation_id !== operation.operationId) {
				const error = new Error("Trace clear returned an invalid operation receipt.");
				error.code = "trace_clear_protocol_error";
				throw error;
			}
			setNonce((x) => x + 1);
			// Persist the acknowledged mutation above the conditionally mounted Inspector. Only a detail
			// read started after this POST settles may clear this fence and permit another mutation.
			const message = {
				kind: "success",
				blocking: true,
				text: `Trace cleared for #${n.id} · attempt ${operation.nodeGeneration}. Refreshing experiment details…`
			};
			storeClearRecovery({
				phase: "reconcile",
				message
			});
			setClearing("");
			setClearMessage(message);
			publishClearRecovery?.(clearScope, "clear-succeeded");
		} catch (e) {
			// Keep server free text out of the UI. Generation conflicts mean the exact confirmation scope
			// disappeared and are deliberately distinct from the ordinary "run is active" 409.
			const currentNodeGeneration = Number.isSafeInteger(e?.detail?.current_node_generation) ? e.detail.current_node_generation : null;
			const definitelyNotMutated = [
				"run_generation_changed",
				"node_generation_changed",
				"trace_revision_changed",
				"run_generation_unavailable",
				"node_generation_unavailable",
				"trace_revision_unavailable",
				"trace_clear_operation_unavailable",
				"trace_clear_operation_conflict",
				"engine_running",
				"engine_lock_unavailable",
				"run_lifecycle_lock_unavailable",
				"STALE_LINK_READ_ONLY"
			].includes(e?.code) || e?.status >= 400 && e.status < 500 && ![
				408,
				425,
				429
			].includes(e.status);
			const verificationIdentityChanged = verifying && [
				"run_generation_changed",
				"node_generation_changed",
				"trace_revision_changed"
			].includes(e?.code);
			const verificationTerminal = verificationIdentityChanged || verifying && ["trace_clear_operation_superseded", "trace_clear_operation_conflict"].includes(e?.code);
			// Once the first request may have escaped, a Verify response is contextual: even a normally
			// pre-mutation 4xx/503 cannot prove what the earlier handler did. Keep the exact operation
			// until the server returns success or a durable terminal receipt.
			const verificationUnresolved = verifying && !verificationTerminal;
			const outcomeUnknown = verificationUnresolved || !definitelyNotMutated && (e?.status == null || [
				408,
				425,
				429
			].includes(e.status) || e.status >= 500);
			const pendingOperationId = e?.code === "trace_clear_pending" && /^tc_[0-9a-f]{32}$/.test(e?.detail?.operation_id || "") ? e.detail.operation_id : null;
			const recoveryOperation = pendingOperationId ? {
				...operation,
				operationId: pendingOperationId
			} : operation;
			const text = verificationUnresolved ? e?.code === "trace_clear_pending" ? "The original trace clear is still being resolved. Check this same operation again; do not submit a new clear." : "The original trace clear could not be verified yet. Its outcome may already have changed the trace; check this same operation again." : verificationIdentityChanged ? "The trace lifecycle changed and no matching pending clear operation remains. No additional deletion was attempted. Refresh the current experiment before deciding what to do next." : e?.code === "run_generation_changed" || e?.code === "STALE_LINK_READ_ONLY" ? "The run changed before the trace was cleared. Nothing was cleared. Review the current run and confirm again." : e?.code === "trace_clear_operation_superseded" ? "The trace changed after an interrupted clear, so the original outcome cannot be reconstructed. No additional deletion was attempted. Refresh the current experiment before deciding what to do next." : e?.code === "trace_path_invalid" ? "Trace storage is not a valid run-owned file. Nothing was cleared. Restore the trace file, then refresh this experiment." : e?.code === "trace_clear_operation_conflict" ? "This clear operation belongs to a different trace lifecycle. Nothing new was cleared. Refresh this experiment before trying again." : e?.code === "node_generation_changed" ? currentNodeGeneration == null ? `Experiment #${n.id} changed attempts. Nothing was cleared. Review the current attempt and confirm again.` : `Experiment #${n.id} moved to attempt ${currentNodeGeneration}. Nothing was cleared. Review the current attempt and confirm again.` : e?.code === "trace_revision_changed" ? "Trace diagnostics changed before the clear acquired ownership. Nothing was cleared. Refresh this experiment and confirm against the current trace." : [
				"run_generation_unavailable",
				"node_generation_unavailable",
				"trace_revision_unavailable",
				"trace_clear_operation_unavailable"
			].includes(e?.code) || e?.status === 428 ? "The exact run, trace snapshot, or experiment attempt is unavailable. Nothing was cleared. Reload the run and confirm again." : e?.code === "trace_clear_pending" ? "The original trace clear is still being resolved. Verify this same operation again; do not submit a new clear." : e?.status === 409 ? "Trace wasn’t cleared because the run is active, busy, or its write ownership could not be verified. Wait for current work to settle, refresh, then try again." : outcomeUnknown ? "Trace clear outcome is unknown. Verify this same operation before trying again." : "Trace clear was rejected. Nothing was confirmed as deleted. Refresh this experiment before trying again.";
			const message = {
				kind: "error",
				blocking: true,
				verifyOperation: outcomeUnknown,
				text
			};
			finishClear(message, outcomeUnknown ? {
				phase: "ambiguous",
				message,
				operation: recoveryOperation
			} : null);
		}
	};
	const doClear = () => {
		try {
			return submitClear({
				expectedGeneration,
				expectedTraceRevision,
				nodeGeneration,
				operationId: newTraceClearOperationId()
			});
		} catch (error) {
			finishClear({
				kind: "error",
				blocking: true,
				text: "A secure trace clear operation could not be created. Reload the run before trying again."
			});
			return false;
		}
	};
	// "Clear trace" erases this node's spans (spans.jsonl is append-only, so a reset+rebuild would else
	// STACK new bands on the old attempt's). Two-click confirm; disabled while THIS node is being worked.
	const clearPrimaryBusy = clearing === "busy";
	const clearPrimaryVerifying = clearPrimaryBusy && clearMessage?.verifyOperation;
	const clearPrimaryConfirm = clearing === "confirm";
	const storedClearPhase = clearRecoveryStore?.current?.get(clearScope)?.phase;
	const clearFenced = !!clearMessage?.blocking || [
		"busy",
		"reconcile",
		"blocked",
		"ambiguous"
	].includes(storedClearPhase);
	const clearBtn = /* @__PURE__ */ _jsxs("span", {
		className: "trace-clear",
		children: [
			/* @__PURE__ */ _jsx("button", {
				type: "button",
				ref: clearing === "" ? clearTriggerRef : clearConfirmRef,
				className: "seg" + (clearing ? " on" : ""),
				title: clearPrimaryConfirm ? "confirm: erase this node’s spans" : clearing === "" && !clearAvailable ? clearUnavailableText : clearing === "" && clearFenced ? clearMessage?.verifyOperation ? "verify the original clear operation before clearing trace data again" : "refresh the experiment successfully before clearing trace data again" : clearing === "" ? "erase this node’s captured trace (spans) — useful before re-running the node so the new trace replaces the old" : undefined,
				disabled: clearing === "" && (!clearAvailable || clearFenced),
				"aria-disabled": clearPrimaryBusy || undefined,
				"aria-busy": clearPrimaryBusy || undefined,
				"aria-label": clearPrimaryConfirm ? `Confirm clear trace for experiment #${n.id}, attempt ${nodeGeneration}. Results and run history stay intact.` : clearPrimaryVerifying ? `Checking the original trace clear outcome for experiment #${n.id}, attempt ${nodeGeneration}.` : undefined,
				onClick: clearing === "" ? () => {
					setClearMessage(null);
					setClearPhase("confirm");
				} : clearPrimaryConfirm ? doClear : undefined,
				children: clearPrimaryVerifying ? "Checking…" : clearPrimaryBusy ? "Clearing…" : clearPrimaryConfirm ? "✕ confirm clear" : "✕ clear trace"
			}),
			clearPrimaryConfirm && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("button", {
				className: "seg",
				onClick: () => {
					setClearPhase("");
					requestAnimationFrame(() => {
						const trigger = clearTriggerRef.current;
						const target = trigger && !trigger.disabled ? trigger : bodyRef.current?.closest(".insp-body");
						target?.focus({ preventScroll: true });
					});
				},
				children: "cancel"
			}), /* @__PURE__ */ _jsxs("span", {
				className: "muted trace-clear-status",
				children: [
					"Clear #",
					n.id,
					" · attempt ",
					nodeGeneration,
					"? Results and run history stay intact."
				]
			})] }),
			clearPrimaryBusy && !clearMessage && /* @__PURE__ */ _jsx("span", {
				className: "sr-only",
				role: "status",
				children: "Clearing trace…"
			}),
			!clearAvailable && clearing === "" && /* @__PURE__ */ _jsx("span", {
				className: "muted trace-clear-status",
				role: "status",
				children: clearUnavailableText
			}),
			clearMessage && /* @__PURE__ */ _jsx("span", {
				className: "muted trace-clear-status" + (clearMessage.kind === "error" ? " trace-clear-error" : ""),
				role: clearMessage.kind === "error" ? "alert" : "status",
				children: clearMessage.text
			}),
			clearMessage?.blocking && !clearMessage.pending && /* @__PURE__ */ _jsx("button", {
				type: "button",
				className: "seg",
				ref: clearRefreshRef,
				"aria-disabled": reloadPending || clearMessage.refreshing || undefined,
				"aria-busy": reloadPending || clearMessage.refreshing || undefined,
				onClick: refreshClearScope,
				children: clearMessage.verifyOperation ? "↻ verify clear outcome" : reloadPending || clearMessage.refreshing ? "Refreshing…" : "↻ refresh experiment"
			})
		]
	});
	const nav = /* @__PURE__ */ _jsxs("span", {
		className: "trace-nav",
		children: [/* @__PURE__ */ _jsx("button", {
			className: "seg",
			"aria-label": "Scroll trace to top",
			title: "scroll to top",
			onClick: () => scrollTo("top"),
			children: "↑"
		}), /* @__PURE__ */ _jsx("button", {
			className: "seg",
			"aria-label": "Scroll trace to newest",
			title: "scroll to newest (bottom)",
			onClick: () => scrollTo("bottom"),
			children: "↓"
		})]
	});
	// STICKY control bar: pinned to the top of the scroll area (position:sticky in .trace-head) so the view
	// toggle / collapse-all / scroll nav stay reachable while you page through a long trace, instead of
	// scrolling off the top. collapse-all is shown only for the conversation view (it acts on the bands).
	const head = /* @__PURE__ */ _jsxs("div", {
		className: "trace-head",
		children: [status, /* @__PURE__ */ _jsxs("div", {
			className: "conv-toggle",
			children: [
				/* @__PURE__ */ _jsx("button", {
					"aria-pressed": view === "conversation",
					className: "seg" + (view === "conversation" ? " on" : ""),
					onClick: () => setView("conversation"),
					title: "Linear, de-duplicated reading: request once, then each turn's reasoning + tools",
					children: "conversation"
				}),
				/* @__PURE__ */ _jsx("button", {
					"aria-pressed": view === "raw",
					className: "seg" + (view === "raw" ? " on" : ""),
					onClick: () => setView("raw"),
					children: "span tree"
				}),
				view === "conversation" && /* @__PURE__ */ _jsx("button", {
					className: "seg trace-collapse",
					"aria-pressed": allOpen,
					title: "collapse or expand every stage",
					onClick: () => setAllOpen((o) => !o),
					children: allOpen ? "⊟ collapse all" : "⊞ expand all"
				}),
				/* @__PURE__ */ _jsx("span", { className: "spacer" }),
				clearBtn,
				nav
			]
		})]
	});
	if (view === "conversation") return /* @__PURE__ */ _jsxs("div", {
		className: "trace",
		ref: bodyRef,
		children: [
			head,
			/* @__PURE__ */ _jsx(Conversation, {
				n,
				runId,
				working,
				allOpen,
				reloadNonce: nonce,
				onRetry: () => setNonce((value) => value + 1)
			}),
			agent && /* @__PURE__ */ _jsx(AgentReport, { r: agent })
		]
	});
	if (!spans.length && !agent) {
		if (unavailable) return /* @__PURE__ */ _jsxs("div", {
			className: "trace",
			ref: bodyRef,
			children: [head, /* @__PURE__ */ _jsx(TraceUnavailable, {
				onRetry: retryParentTrace,
				pending: reloadPending
			})]
		});
		if (partial) return /* @__PURE__ */ _jsxs("div", {
			className: "trace",
			ref: bodyRef,
			children: [head, /* @__PURE__ */ _jsx("div", {
				className: "notice compact",
				role: "status",
				children: "Trace projection is partial; no observations were included."
			})]
		});
		return /* @__PURE__ */ _jsxs("div", {
			className: "trace",
			ref: bodyRef,
			children: [head, /* @__PURE__ */ _jsx("div", {
				className: "muted",
				children: "No execution spans yet. Offline nodes may have none; active nodes update here as they run."
			})]
		});
	}
	if (unavailable) return /* @__PURE__ */ _jsxs("div", {
		className: "trace",
		ref: bodyRef,
		children: [
			head,
			/* @__PURE__ */ _jsx(TraceUnavailable, {
				onRetry: retryParentTrace,
				pending: reloadPending
			}),
			agent && /* @__PURE__ */ _jsx(AgentReport, { r: agent })
		]
	});
	if (!spans.length && partial) return /* @__PURE__ */ _jsxs("div", {
		className: "trace",
		ref: bodyRef,
		children: [
			head,
			/* @__PURE__ */ _jsx("div", {
				className: "notice compact",
				role: "status",
				children: "Trace projection is partial; no observations were included."
			}),
			agent && /* @__PURE__ */ _jsx(AgentReport, { r: agent })
		]
	});
	const { t0, total } = traceBounds(spans);
	// create_node already nests propose→implement; if an agent wrote the node, the report belongs
	// right after that authoring stage (placed by index), otherwise it trails the whole lifecycle.
	const authorIdx = spans.findIndex((s) => [
		"create_node",
		"implement",
		"repair"
	].includes(s.name));
	const roll = n.trace?.rollup || {};
	const rtok = roll.tokens || {};
	return /* @__PURE__ */ _jsxs("div", {
		className: "trace",
		ref: bodyRef,
		children: [
			head,
			partial && /* @__PURE__ */ _jsx("div", {
				className: "notice compact",
				role: "status",
				children: "Trace projection is partial."
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "muted trace-rollup-intro",
				children: [
					"Node #",
					n.id,
					" lifecycle · offset = start, bar = duration. Expand an observation for bounded, redacted I/O.",
					roll.generations || roll.tools ? /* @__PURE__ */ _jsxs("span", {
						className: "trace-totals",
						title: rtok.total ? `context window peaked at ${rtok.context || 0} tokens; the model generated ${rtok.completion || 0}. Billed ${rtok.total} total — each turn RE-SENDS the growing context, so billed ≫ context.` : undefined,
						children: [
							" · ",
							roll.generations || 0,
							" generation",
							roll.generations === 1 ? "" : "s",
							roll.tools ? ` · ${roll.tools} tool call${roll.tools === 1 ? "" : "s"}` : "",
							rtok.context ? ` · ${ktok(rtok.context)} ctx` : "",
							rtok.completion ? ` · ${ktok(rtok.completion)} out` : "",
							roll.cost ? ` · $${roll.cost}` : ""
						]
					}) : null
				]
			}),
			spans.map((s, i) => /* @__PURE__ */ _jsxs(React.Fragment, { children: [/* @__PURE__ */ _jsx(StageBlock, {
				s,
				t0,
				total,
				runId
			}), agent && i === authorIdx && /* @__PURE__ */ _jsx(AgentReport, { r: agent })] }, `${n.attempt ?? ""}:${s.span_id || i}`)),
			agent && authorIdx < 0 && /* @__PURE__ */ _jsx(AgentReport, { r: agent })
		]
	});
}
function Code({ n, draftStore, draftScope }) {
	const [diff, setDiff] = useInspectorDraftField(draftStore, draftScope, "diff", false, { disposable: true });
	const files = n.files || {};
	const codeDiff = useMemo(() => diff && n.parent_code != null ? diffLines(n.parent_code, n.code) : null, [
		diff,
		n.parent_code,
		n.code
	]);
	return /* @__PURE__ */ _jsxs(_Fragment, { children: [
		/* @__PURE__ */ _jsx("div", {
			className: "toolbar code-toolbar",
			children: n.parent_code != null && /* @__PURE__ */ _jsxs("button", {
				className: "btn sm" + (diff ? " primary" : ""),
				onClick: () => setDiff((d) => !d),
				children: ["diff vs parent #", n.parent_id_diffed]
			})
		}),
		codeDiff ? /* @__PURE__ */ _jsx(CodeViewer, {
			diff: codeDiff,
			copyText: n.code || "",
			label: `Node ${n.id} diff`,
			draftStore,
			draftScope: `${draftScope}:main`
		}) : /* @__PURE__ */ _jsx(CodeViewer, {
			code: n.code || "(no solution.py — repo task or no code)",
			label: `Node ${n.id} code`,
			draftStore,
			draftScope: `${draftScope}:main`
		}),
		Object.keys(files).length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsxs("div", {
			className: "section-h",
			children: ["Helper files ", /* @__PURE__ */ _jsx("span", {
				className: "pill",
				children: Object.keys(files).length
			})]
		}), Object.entries(files).map(([fn, c]) => /* @__PURE__ */ _jsxs("div", { children: [/* @__PURE__ */ _jsx("div", {
			className: "muted helper-file-label",
			children: fn
		}), /* @__PURE__ */ _jsx(CodeViewer, {
			code: c,
			label: fn,
			maxHeight: 300,
			draftStore,
			draftScope: `${draftScope}:file:${fn}`
		})] }, fn))] })
	] });
}
// Live online metric curves (loss, recall@k, lr, grad norms, …) read from the node's TensorBoard
// events via the metrics adapters. Polls while the node is still running so the curves fill in as
// training progresses; keyed on n.status so a repair-retrain (pending→failed→pending) re-arms the poll.
export function MetricCurves({ runId, nodeId, attempt = 0, status }) {
	const done = [
		"evaluated",
		"failed",
		"confirmed"
	].includes(status);
	const metricAttempt = Number.isInteger(attempt) && attempt >= 0 ? attempt : 0;
	const [resource, setResource] = useState(null);
	const [retryNonce, setRetryNonce] = useState(0);
	const requestRef = useRef(0);
	// A terminal node's metrics are immutable — fetch ONCE (ms=null: immediate, no interval) instead of
	// polling every 15s forever. A running node still polls at 3s; a status change (via the `done` dep)
	// re-arms the effect, so a repair-retrain (pending→failed→pending) resumes live polling.
	usePoll((alive) => {
		const request = ++requestRef.current;
		const timed = deadlineGet(runNodeApiPath(runId, nodeId, `/metrics?attempt=${metricAttempt}`));
		timed.promise.then((d) => {
			if (!d?.metrics || Array.isArray(d.metrics)) throw 0;
			if (d.node_id !== nodeId || d.attempt !== metricAttempt) throw 0;
			if (alive() && request === requestRef.current) setResource(d.metrics);
		}).catch(() => {
			if (alive() && request === requestRef.current) setResource((r) => r ? Array.isArray(r) ? r : [r] : false);
		});
		return timed;
	}, done ? null : 3e3, [
		runId,
		nodeId,
		metricAttempt,
		done,
		retryNonce
	], { enabled: nodeId != null });
	const retry = () => {
		if (resource === false) setResource(null);
		setRetryNonce((n) => n + 1);
	};
	if (resource === null) return /* @__PURE__ */ _jsx("div", {
		className: "notice compact",
		role: "status",
		children: "Loading metric curves…"
	});
	const failed = resource === false, stale = Array.isArray(resource);
	return /* @__PURE__ */ _jsxs(_Fragment, { children: [(failed || stale) && /* @__PURE__ */ _jsxs("div", {
		className: `notice ${failed ? "resource-error" : "resource-warning"} compact`,
		role: failed ? "alert" : "status",
		children: [
			failed ? "Metric curves unavailable." : "Last loaded metric curves; refresh failed.",
			" ",
			/* @__PURE__ */ _jsx("button", {
				className: "btn sm",
				onClick: retry,
				children: "Retry"
			})
		]
	}), !failed && /* @__PURE__ */ _jsx(MetricLines, { series: stale ? resource[0] : resource })] });
}
function Metrics({ n, detail, state, runId }) {
	const seeds = detail?.confirm_seeds_detail || {};
	const vals = Object.entries(seeds).map(([s, v]) => ({
		s: Number(s),
		v
	})).filter((x) => x.v != null).sort((a, b) => a.s - b.s);
	// Every metric reported anywhere in the run (the objective ★ + all auto-captured extras), shown for
	// THIS node and for the champion (the run's best node), so "the metrics you wanted to see overall"
	// are all visible + comparable. Only the objective drives selection; extras are audit-only.
	const nodes = Object.values(state?.nodes || {});
	const extraKeys = [...new Set(nodes.flatMap((x) => Object.keys(x.extra_metrics || {})))];
	const champ = state?.best_node_id != null ? nodes.find((x) => x.id === state.best_node_id) : null;
	const showChamp = champ && champ.id !== n.id;
	const rows = [{
		k: "objective",
		mine: n.confirmed_mean ?? n.metric,
		best: champ ? champ.confirmed_mean ?? champ.metric : null,
		star: true
	}, ...extraKeys.map((k) => ({
		k,
		mine: n.extra_metrics?.[k],
		best: champ?.extra_metrics?.[k]
	}))];
	return /* @__PURE__ */ _jsxs(_Fragment, { children: [
		/* @__PURE__ */ _jsxs("div", {
			className: "section-h",
			children: ["Reported metrics", champ ? ` · best = #${champ.id}` : ""]
		}),
		/* @__PURE__ */ _jsx(DataTable, {
			caption: "Node metric comparison",
			card: false,
			children: /* @__PURE__ */ _jsxs("table", {
				className: "tbl",
				children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
					/* @__PURE__ */ _jsx("th", { children: "metric" }),
					/* @__PURE__ */ _jsx("th", { children: "this node" }),
					showChamp && /* @__PURE__ */ _jsxs("th", { children: ["best #", champ.id] })
				] }) }), /* @__PURE__ */ _jsx("tbody", { children: rows.map((r) => /* @__PURE__ */ _jsxs("tr", {
					className: r.star ? "chosen-row" : "",
					children: [
						/* @__PURE__ */ _jsxs("td", { children: [r.star ? "★ " : "", r.k] }),
						/* @__PURE__ */ _jsx("td", { children: fmt(r.mine) }),
						showChamp && /* @__PURE__ */ _jsx("td", { children: fmt(r.best) })
					]
				}, r.k)) })]
			})
		}),
		n.confirmed_mean != null && /* @__PURE__ */ _jsx("div", {
			className: "kv confirmed-metric",
			children: /* @__PURE__ */ _jsx(KV, {
				k: "robust mean ± std",
				v: `${fmt(n.confirmed_mean)} ± ${fmt(n.confirmed_std)} over ${n.confirmed_seeds || vals.length} seeds`
			})
		}),
		vals.length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
			className: "section-h",
			children: "Per-seed confirmation"
		}), /* @__PURE__ */ _jsx(DataTable, {
			caption: "Per-seed confirmation metrics",
			card: false,
			children: /* @__PURE__ */ _jsxs("table", {
				className: "tbl",
				children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [/* @__PURE__ */ _jsx("th", { children: "seed" }), /* @__PURE__ */ _jsx("th", { children: "metric" })] }) }), /* @__PURE__ */ _jsx("tbody", { children: vals.map((x) => /* @__PURE__ */ _jsxs("tr", { children: [/* @__PURE__ */ _jsx("td", { children: x.s }), /* @__PURE__ */ _jsx("td", { children: fmt(x.v) })] }, x.s)) })]
			})
		})] }),
		/* @__PURE__ */ _jsxs("div", {
			className: "section-h metric-curves-heading",
			children: ["Metric curves", /* @__PURE__ */ _jsx("span", {
				className: "muted metric-curves-note",
				children: "· live logged scalars · grouped"
			})]
		}),
		/* @__PURE__ */ _jsx(MetricCurves, {
			runId,
			nodeId: n.id,
			attempt: n.attempt ?? 0,
			status: n.status
		}, `${runId}:${n.id}:${n.attempt ?? 0}`)
	] });
}
// Intra-node sweep trials: a sortable table of every config the node ran in-process, plus
// parallel-coords / scatter views. Trials aren't backend nodes, so the charts get pseudo-node
// adapters ({id, metric, idea:{params}, feasible}) — no charts.jsx change needed.
function Trials({ n, detail, state }) {
	const trials = detail?.trials ?? n.trials ?? [];
	const summary = n.trials_summary;
	const [sortKey, setSortKey] = useState("metric");
	const [sortDir, setSortDir] = useState(state.direction === "min" ? "asc" : "desc");
	const [showAll, setShowAll] = useState(false);
	if (!trials.length) {
		return /* @__PURE__ */ _jsx("div", {
			className: "muted",
			children: summary ? `Sweep of ${summary.count} trial(s) — loading full results…` : "No trials recorded for this node."
		});
	}
	const dir = state.direction;
	const params = Array.from(new Set(trials.flatMap((t) => Object.keys(t.params || {}))));
	// best trial = best metric under direction (matches the node's scalar metric)
	let bestIdx = -1, bestV = null;
	trials.forEach((t, i) => {
		if (t.metric != null && (bestV == null || (dir === "min" ? t.metric < bestV : t.metric > bestV))) {
			bestV = t.metric;
			bestIdx = i;
		}
	});
	const setSort = (k) => {
		if (k === sortKey) setSortDir((d) => d === "asc" ? "desc" : "asc");
		else {
			setSortKey(k);
			setSortDir("asc");
		}
	};
	const val = (t, k) => k === "idx" ? t._i : k === "metric" ? t.metric : k === "seconds" ? t.seconds : t.params?.[k];
	const rowsAll = trials.map((t, i) => ({
		...t,
		_i: i
	})).sort((a, b) => {
		const av = val(a, sortKey), bv = val(b, sortKey);
		if (av == null) return 1;
		if (bv == null) return -1;
		const cmp = typeof av === "number" && typeof bv === "number" ? av - bv : String(av).localeCompare(String(bv));
		return sortDir === "asc" ? cmp : -cmp;
	});
	const CAP = 100;
	const rows = showAll ? rowsAll : rowsAll.slice(0, CAP);
	const okN = trials.filter((t) => t.metric != null).length;
	const totSec = trials.reduce((s, t) => s + (t.seconds || 0), 0);
	// pseudo-nodes for the existing charts (they read n.idea?.params and n.confirmed_mean ?? n.metric)
	const pseudo = trials.map((t, i) => ({
		id: i,
		metric: t.metric,
		confirmed_mean: null,
		idea: { params: t.params || {} },
		feasible: t.metric != null
	}));
	const scatter = params.length ? trials.map((t, i) => ({
		x: t.params?.[params[0]] ?? i,
		y: t.metric,
		feasible: t.metric != null,
		id: i
	})).filter((d) => d.y != null) : [];
	const Th = ({ k, children }) => /* @__PURE__ */ _jsx("th", {
		"aria-sort": sortKey === k ? sortDir === "asc" ? "ascending" : "descending" : undefined,
		children: /* @__PURE__ */ _jsxs("button", {
			type: "button",
			className: "table-sort",
			onClick: () => setSort(k),
			children: [children, sortKey === k && /* @__PURE__ */ _jsx("span", {
				"aria-hidden": "true",
				children: sortDir === "asc" ? " ▲" : " ▼"
			})]
		})
	});
	return /* @__PURE__ */ _jsxs(_Fragment, { children: [
		/* @__PURE__ */ _jsxs("div", {
			className: "kv",
			children: [
				/* @__PURE__ */ _jsx(KV, {
					k: "trials",
					v: trials.length
				}),
				/* @__PURE__ */ _jsx(KV, {
					k: "best metric",
					v: `${fmt(bestV)}${bestIdx >= 0 ? ` (#${bestIdx})` : ""}`
				}),
				/* @__PURE__ */ _jsx(KV, {
					k: "ok / failed",
					v: `${okN} / ${trials.length - okN}`
				}),
				/* @__PURE__ */ _jsx(KV, {
					k: "Σ seconds",
					v: fmt(totSec)
				})
			]
		}),
		params.length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
			className: "section-h",
			children: "Params → metric"
		}), /* @__PURE__ */ _jsx(ParallelCoords, {
			nodes: pseudo,
			direction: dir,
			height: 220
		})] }),
		scatter.length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsxs("div", {
			className: "section-h",
			children: [params[0], " vs metric"]
		}), /* @__PURE__ */ _jsx(Scatter, {
			data: scatter,
			xlab: params[0],
			ylab: "metric",
			height: 220
		})] }),
		/* @__PURE__ */ _jsxs("div", {
			className: "section-h",
			children: ["Trials ", /* @__PURE__ */ _jsx("span", {
				className: "pill",
				children: trials.length
			})]
		}),
		/* @__PURE__ */ _jsx(DataTable, {
			caption: "Hyperparameter sweep trial results",
			card: false,
			children: /* @__PURE__ */ _jsxs("table", {
				className: "tbl",
				children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
					/* @__PURE__ */ _jsx(Th, {
						k: "idx",
						children: "#"
					}),
					params.map((p) => /* @__PURE__ */ _jsx(Th, {
						k: p,
						children: p
					}, p)),
					/* @__PURE__ */ _jsx(Th, {
						k: "metric",
						children: "metric"
					}),
					/* @__PURE__ */ _jsx(Th, {
						k: "seconds",
						children: "s"
					})
				] }) }), /* @__PURE__ */ _jsx("tbody", { children: rows.map((t) => /* @__PURE__ */ _jsxs("tr", {
					className: t._i === bestIdx ? "best-row" : "",
					children: [
						/* @__PURE__ */ _jsxs("td", { children: [
							"#",
							t._i,
							t._i === bestIdx ? /* @__PURE__ */ _jsx(OpIcon, {
								name: "crown",
								size: 10
							}) : ""
						] }),
						params.map((p) => /* @__PURE__ */ _jsx("td", { children: t.params?.[p] != null ? fmt(t.params[p]) : "—" }, p)),
						/* @__PURE__ */ _jsx("td", { children: t.metric != null ? fmt(t.metric) : /* @__PURE__ */ _jsx("span", {
							className: "badge reason",
							children: t.error ? "error" : "failed"
						}) }),
						/* @__PURE__ */ _jsx("td", {
							className: "muted",
							children: fmt(t.seconds)
						})
					]
				}, t._i)) })]
			})
		}),
		rowsAll.length > CAP && /* @__PURE__ */ _jsx("button", {
			className: "btn sm ghost trials-reveal",
			onClick: () => setShowAll((s) => !s),
			children: showAll ? "show fewer" : `show all ${rowsAll.length}`
		})
	] });
}
function Trust({ n, drifts = [] }) {
	const feasibility = nodeFeasibilityStatus(n);
	const State = ({ tone, label, detail }) => /* @__PURE__ */ _jsxs("div", {
		className: `trust-state ${tone}`,
		role: tone === "alarm" ? "alert" : "status",
		children: [
			/* @__PURE__ */ _jsx(OpIcon, {
				name: tone === "alarm" ? "alert" : tone === "ok" ? "check" : "dot",
				size: 14
			}),
			/* @__PURE__ */ _jsx("strong", { children: label }),
			/* @__PURE__ */ _jsx("span", { children: detail })
		]
	});
	return /* @__PURE__ */ _jsxs("div", {
		className: "inspector-trust",
		children: [
			/* @__PURE__ */ _jsx("div", {
				className: "section-h",
				children: "Robustness"
			}),
			n.confirmed_mean != null ? /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx(State, {
				tone: "ok",
				label: "Multi-seed confirmed",
				detail: `${n.confirmed_seeds || "Multiple"} successful seeds are recorded for this node.`
			}), /* @__PURE__ */ _jsxs("div", {
				className: "kv",
				children: [
					/* @__PURE__ */ _jsx(KV, {
						k: "single",
						v: fmt(n.metric)
					}),
					/* @__PURE__ */ _jsx(KV, {
						k: "robust mean",
						v: fmt(n.confirmed_mean)
					}),
					/* @__PURE__ */ _jsx(KV, {
						k: "std",
						v: fmt(n.confirmed_std)
					}),
					/* @__PURE__ */ _jsx(KV, {
						k: "seeds",
						v: n.confirmed_seeds
					})
				]
			})] }) : /* @__PURE__ */ _jsx(State, {
				tone: "warn",
				label: "Single-evaluation only",
				detail: "This node is not multi-seed confirmed and could be seed-lucky."
			}),
			/* @__PURE__ */ _jsx("div", {
				className: "section-h",
				children: "Feasibility"
			}),
			/* @__PURE__ */ _jsx(State, { ...feasibility }),
			n.violations?.length ? /* @__PURE__ */ _jsx(DataTable, {
				caption: "Constraint violations",
				card: false,
				children: /* @__PURE__ */ _jsxs("table", {
					className: "tbl",
					children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("th", { children: "constraint" }),
						/* @__PURE__ */ _jsx("th", { children: "value" }),
						/* @__PURE__ */ _jsx("th", { children: "bound" })
					] }) }), /* @__PURE__ */ _jsx("tbody", { children: n.violations.map((v, i) => /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("td", {
							className: "flag",
							children: v.name
						}),
						/* @__PURE__ */ _jsx("td", { children: fmt(v.value) }),
						/* @__PURE__ */ _jsx("td", { children: v.max != null ? `≤ ${fmt(v.max)}` : `≥ ${fmt(v.min)}` })
					] }, i)) })]
				})
			}) : null,
			/* @__PURE__ */ _jsx("div", {
				className: "section-h",
				children: "Metric drift"
			}),
			drifts.length ? /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx(State, {
				tone: "alarm",
				label: `${drifts.length} divergence${drifts.length === 1 ? "" : "s"} recorded`,
				detail: "The independent metric reader disagreed with the primary metric."
			}), /* @__PURE__ */ _jsx(DataTable, {
				caption: "Metric drift cross-checks",
				card: false,
				children: /* @__PURE__ */ _jsxs("table", {
					className: "tbl",
					children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("th", { children: "seed" }),
						/* @__PURE__ */ _jsx("th", { children: "primary" }),
						/* @__PURE__ */ _jsx("th", { children: "cross-check" }),
						/* @__PURE__ */ _jsx("th", { children: "tol" })
					] }) }), /* @__PURE__ */ _jsx("tbody", { children: drifts.map((d, i) => /* @__PURE__ */ _jsxs("tr", { children: [
						/* @__PURE__ */ _jsx("td", { children: d.seed ?? "—" }),
						/* @__PURE__ */ _jsx("td", {
							className: "flag",
							children: fmt(d.primary)
						}),
						/* @__PURE__ */ _jsx("td", { children: fmt(d.cross) }),
						/* @__PURE__ */ _jsx("td", {
							className: "muted",
							children: fmt(d.tolerance)
						})
					] }, i)) })]
				})
			})] }) : /* @__PURE__ */ _jsx(State, {
				tone: "unknown",
				label: "No drift flag recorded",
				detail: "This does not prove that an independent cross-check ran for this node."
			}),
			n.status === "failed" && /* @__PURE__ */ _jsxs(_Fragment, { children: [
				/* @__PURE__ */ _jsx("div", {
					className: "section-h",
					children: "Failure"
				}),
				/* @__PURE__ */ _jsx("span", {
					className: "badge reason",
					children: n.error_reason
				}),
				/* @__PURE__ */ _jsx("pre", {
					className: "code",
					children: n.error
				})
			] })
		]
	});
}
function Cost({ state }) {
	const c = state.llm_cost;
	if (!c) return /* @__PURE__ */ _jsx("div", {
		className: "muted",
		children: "No LLM cost recorded (offline/toy run, or run not finished)."
	});
	return /* @__PURE__ */ _jsxs("div", {
		className: "kv",
		children: [
			/* @__PURE__ */ _jsx(KV, {
				k: "$ spent",
				v: fmt(c.cost)
			}),
			/* @__PURE__ */ _jsx(KV, {
				k: "calls",
				v: fmtInt(c.calls)
			}),
			/* @__PURE__ */ _jsx(KV, {
				k: "prompt tokens",
				v: fmtInt(c.prompt_tokens)
			}),
			/* @__PURE__ */ _jsx(KV, {
				k: "completion tokens",
				v: fmtInt(c.completion_tokens)
			}),
			/* @__PURE__ */ _jsx(KV, {
				k: "total tokens",
				v: fmtInt(c.total_tokens)
			})
		]
	});
}
