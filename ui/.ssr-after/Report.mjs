import React, { useEffect, useMemo, useRef, useState } from "react";
import { peekReportRefreshIntent, reportRefreshIntent, isTransientCommandReadError, deadlineGet, fmt, fmtInt, CONTROL, runNodeApiPath } from "./util.mjs";
import { Trajectory, ImprovementWaterfall } from "./charts.mjs";
import { analyze, buildModelCard, verdict, paramDiffLabel, toMarkdown, hyperImportance } from "./report.mjs";
import MemoCard from "./MemoCard.mjs";
import Markdown from "./markdown.mjs";
import { OpIcon } from "./icons.mjs";
import { reportStepIdentity } from "./trustSemantics.mjs";
import { DataTable, downloadBlob } from "./accessibility.mjs";
import { normalizeResearchMemos } from "./researchMemoModel.mjs";
import { normalizeReportNodeDetail, normalizeRunReport, reportCoverageText, reportNarrativeCoverage } from "./reportModel.mjs";
import { nodeTheme } from "./conceptId.mjs";
import { nodeIsActive } from "./nodeProjection.mjs";

import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
const TRUST_CLASS = {
	unverified: "neutral",
	caveats: "warn",
	suspect: "alarm"
};
const TRUST_LABEL = {
	unverified: "not fully verified",
	caveats: "with caveats",
	suspect: "flags found"
};
const SOLUTION_DETAIL_TIMEOUT_MS = 12e3;
const RUN_GENERATION_RE = /^[0-9a-f]{64}$/;
const OUTCOME_LABEL = {
	improved: "▲ improved",
	flat: "— flat",
	regressed: "▼ regressed",
	none: "no result"
};
export const reportRefreshFailure = (failure, thrown = false) => {
	const code = failure?.code;
	if (code === "run_generation_changed" || code === "run_generation_unavailable") {
		return ["The run changed during report generation. Reload it.", false];
	}
	if ([
		401,
		403,
		404
	].includes(Number(failure?.status))) {
		return [
			"Report refresh is unavailable in this run or session. Reload.",
			false,
			true
		];
	}
	if (code === "job_unknown") {
		return [
			"Report receipt expired. Retry checks the same paid request.",
			true,
			true
		];
	}
	if (code === "job_capacity") {
		return ["The report service is busy. Retry shortly.", true];
	}
	if (code === "report_refresh_in_progress") {
		return ["Another paid report refresh owns this run. Wait for it to finish, then reload.", false];
	}
	if (code === "report_refresh_uncertain") {
		return [
			"Outcome unknown. Resume rechecks the saved paid request; never start another. A crashed worker may need operator recovery.",
			true,
			true
		];
	}
	if (code === "REPORT_REFRESH_PROTOCOL_ERROR" || code === "job_protocol_error") {
		return [
			"Receipt invalid. Resume rechecks the saved paid request; completion remains unknown.",
			true,
			true
		];
	}
	const status = Number(failure?.status);
	const transientThrow = thrown && (failure?.submissionMayHaveSucceeded === true || isTransientCommandReadError(failure));
	if (failure?.ambiguous === true || transientThrow) {
		return [
			"Report-job connection lost. Retry resumes the same paid job.",
			true,
			true
		];
	}
	if (thrown && status >= 400 && status < 500) {
		return ["The report request was rejected. Reload before retrying.", false];
	}
	const kind = failure?.error_kind;
	if (kind === "credentials") return ["Check report-provider credentials in Settings.", true];
	if (kind === "rate_limit") return ["The report provider is busy. Retry shortly.", true];
	if (kind === "accounting_pending") {
		return ["Durable cost accounting is pending. Retry after storage recovers.", true];
	}
	return ["Report generation failed. Check provider settings and retry.", true];
};
// The authority banner is deterministic. Provider prose is rendered only in AgentNarrative below.
function VerdictBanner({ v, onOpenPanel, canOpenPanel }) {
	const cls = TRUST_CLASS[v.trust] || "warn";
	const canOpen = (panel) => !!onOpenPanel && canOpenPanel?.(panel) !== false;
	return /* @__PURE__ */ _jsxs("section", {
		className: "verdict-banner " + cls,
		"aria-labelledby": "report-verdict-heading",
		children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "verdict-row",
				children: [
					/* @__PURE__ */ _jsx("span", {
						className: "verdict-pill " + (v.outcome === "improved" ? "ok" : v.outcome === "regressed" ? "fail" : ""),
						children: OUTCOME_LABEL[v.outcome] || v.outcome
					}),
					v.robustness && v.robustness !== "n/a" && /* @__PURE__ */ _jsx("span", {
						className: "pill",
						children: v.robustness
					}),
					/* @__PURE__ */ _jsx("span", {
						className: "pill verdict-trust-label",
						children: TRUST_LABEL[v.trust] || v.trust
					})
				]
			}),
			/* @__PURE__ */ _jsx("h2", {
				id: "report-verdict-heading",
				className: "verdict-headline",
				children: v.headline
			}),
			v.caveats.length > 0 && /* @__PURE__ */ _jsx("div", {
				className: "caveat-chips",
				children: v.caveats.map((c, i) => {
					const openable = canOpen(c.panel);
					return /* @__PURE__ */ _jsxs("button", {
						className: "caveat-chip " + c.severity,
						disabled: !openable,
						title: openable ? `see ${c.panel} →` : "Unavailable in this read-only view",
						onClick: (event) => {
							if (canOpen(c.panel)) onOpenPanel(c.panel, event.currentTarget);
						},
						children: [
							/* @__PURE__ */ _jsx(OpIcon, {
								name: "alert",
								size: 11
							}),
							" ",
							c.text
						]
					}, i);
				})
			})
		]
	});
}
function AgentNarrative({ rep, coverage, generation, snapshotSeq }) {
	if (!rep) return null;
	const publishedIso = rep.published_at == null ? null : new Date(rep.published_at * 1e3).toISOString();
	const warning = coverage.status === "stale" ? "This narrative predates visible nodes. Refresh it before relying on its claims." : coverage.status === "inconsistent" ? "The report watermark is ahead of this view. Treat the narrative as stale and verify the run generation." : coverage.status === "unknown" ? "The publication did not record a valid node watermark. Its coverage cannot be established." : "";
	const groups = [
		["What worked", rep.what_worked],
		["Learnings", rep.learnings],
		["What didn't work", rep.what_didnt],
		["Next directions", rep.next_directions]
	];
	return /* @__PURE__ */ _jsxs("section", {
		className: `agent-report ${coverage.status}`,
		role: "note",
		"aria-labelledby": "agent-report-heading",
		children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "agent-report-head",
				children: [/* @__PURE__ */ _jsx("h2", {
					id: "agent-report-heading",
					children: "Agent narrative"
				}), /* @__PURE__ */ _jsx("span", {
					className: "pill",
					children: "advisory · not deterministic"
				})]
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "report-provenance",
				"aria-label": "Agent narrative publication provenance",
				children: [
					/* @__PURE__ */ _jsx("span", {
						className: `report-coverage ${coverage.status}`,
						children: reportCoverageText(coverage)
					}),
					rep.published_seq != null && /* @__PURE__ */ _jsxs("span", { children: ["published event ", /* @__PURE__ */ _jsxs("b", { children: ["#", rep.published_seq] })] }),
					publishedIso && /* @__PURE__ */ _jsxs("span", { children: ["at ", /* @__PURE__ */ _jsx("time", {
						dateTime: publishedIso,
						children: new Date(rep.published_at * 1e3).toLocaleString()
					})] }),
					rep.trigger && /* @__PURE__ */ _jsxs("span", { children: ["trigger ", /* @__PURE__ */ _jsx("b", { children: rep.trigger })] }),
					Number.isSafeInteger(snapshotSeq) && snapshotSeq >= 0 && /* @__PURE__ */ _jsxs("span", { children: ["view snapshot ", /* @__PURE__ */ _jsxs("b", { children: ["#", snapshotSeq] })] }),
					/^[0-9a-f]{64}$/.test(generation || "") && /* @__PURE__ */ _jsxs("span", { children: ["generation ", /* @__PURE__ */ _jsxs("code", {
						title: generation,
						children: [generation.slice(0, 12), "…"]
					})] })
				]
			}),
			warning && /* @__PURE__ */ _jsxs("div", {
				className: "report-coverage-warning",
				role: "status",
				children: [
					/* @__PURE__ */ _jsx(OpIcon, {
						name: "alert",
						size: 13
					}),
					" ",
					warning
				]
			}),
			rep.headline && /* @__PURE__ */ _jsx("h3", {
				className: "agent-report-headline",
				children: rep.headline
			}),
			(rep.verdict || rep.summary) && /* @__PURE__ */ _jsx("div", {
				className: "agent-report-text",
				children: /* @__PURE__ */ _jsx(Markdown, { text: rep.verdict || rep.summary })
			}),
			rep.caveats.length > 0 && /* @__PURE__ */ _jsxs("div", {
				className: "agent-report-caveats",
				children: [/* @__PURE__ */ _jsxs("div", {
					className: "agent-report-caveats-title",
					children: [
						/* @__PURE__ */ _jsx(OpIcon, {
							name: "alert",
							size: 12
						}),
						/* @__PURE__ */ _jsx("strong", { children: "Agent caveats" }),
						/* @__PURE__ */ _jsx("span", {
							className: "muted",
							children: "advisory narrative"
						})
					]
				}), /* @__PURE__ */ _jsx(List, { items: rep.caveats })]
			}),
			rep.champion_summary && /* @__PURE__ */ _jsxs("div", {
				className: "agent-report-group",
				children: [/* @__PURE__ */ _jsx("h3", { children: "Champion note at publication" }), /* @__PURE__ */ _jsx(Markdown, { text: rep.champion_summary })]
			}),
			groups.map(([label, items]) => items?.length > 0 && /* @__PURE__ */ _jsxs("div", {
				className: "agent-report-group",
				children: [/* @__PURE__ */ _jsx("h3", { children: label }), /* @__PURE__ */ _jsx(List, { items })]
			}, label))
		]
	});
}
function ChampionCard({ best, state }) {
	if (!best) return null;
	const m = best.confirmed_mean ?? best.metric;
	const direction = nodeTheme(best, state);
	return /* @__PURE__ */ _jsx("div", {
		className: "champion-card",
		children: /* @__PURE__ */ _jsxs("div", {
			className: "kv",
			children: [
				/* @__PURE__ */ _jsx("div", {
					className: "k",
					children: "champion"
				}),
				/* @__PURE__ */ _jsxs("div", {
					className: "v",
					children: [
						"#",
						best.id,
						" · ",
						best.operator,
						direction ? ` · primary concept axis ${direction}` : ""
					]
				}),
				/* @__PURE__ */ _jsx("div", {
					className: "k",
					children: "metric"
				}),
				/* @__PURE__ */ _jsxs("div", {
					className: "v",
					children: [/* @__PURE__ */ _jsx("b", { children: fmt(m) }), best.confirmed_mean != null ? /* @__PURE__ */ _jsxs("span", {
						className: "muted",
						children: [
							" ±",
							fmt(best.confirmed_std),
							" over ",
							best.confirmed_seeds,
							" seed",
							best.confirmed_seeds === 1 ? "" : "s"
						]
					}) : /* @__PURE__ */ _jsx("span", {
						className: "muted",
						children: " (single-seed)"
					})]
				}),
				/* @__PURE__ */ _jsx("div", {
					className: "k",
					children: "params"
				}),
				/* @__PURE__ */ _jsx("div", {
					className: "v",
					children: Object.keys(best.idea?.params || {}).length ? Object.entries(best.idea.params).map(([k, val]) => `${k}=${fmt(val)}`).join(", ") : "—"
				}),
				(best.parent_ids || []).length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
					className: "k",
					children: "lineage"
				}), /* @__PURE__ */ _jsx("div", {
					className: "v",
					children: best.parent_ids.map((p) => "#" + p).join(" → ")
				})] }),
				/* @__PURE__ */ _jsx("div", {
					className: "k",
					children: "feasible"
				}),
				/* @__PURE__ */ _jsx("div", {
					className: "v",
					children: best.feasible === true ? "yes" : best.feasible === false ? "no — constraint violated" : "unknown — not established"
				})
			]
		})
	});
}
function List({ items }) {
	if (!items || !items.length) return null;
	return /* @__PURE__ */ _jsx("ul", {
		className: "bul",
		children: items.map((x, i) => /* @__PURE__ */ _jsx("li", { children: x }, i))
	});
}
export default function ReportView({ state, runId, onOpenPanel, canOpenPanel, onToast, onPickNode, readOnly = false, historySeq = null, expectedGeneration = null, observedSeq = null, readOnlyReason = "history", evidenceAvailable = true }) {
	const candidate = state.best_node_id != null ? state.nodes[state.best_node_id] : null;
	const best = nodeIsActive(candidate, state) ? candidate : null;
	const failed = Object.values(state.nodes).filter((n) => nodeIsActive(n, state) && n.status === "failed");
	const a = useMemo(() => analyze(state), [state]);
	const v = useMemo(() => verdict(state, a), [state, a]);
	const rep = useMemo(() => normalizeRunReport(state.report), [state.report]);
	const nodeCount = Object.keys(state.nodes).length;
	const coverage = useMemo(() => reportNarrativeCoverage(rep, nodeCount), [rep, nodeCount]);
	const imp = useMemo(() => hyperImportance(state).slice(0, 6), [state]);
	const memoProjection = useMemo(() => normalizeResearchMemos(state.research), [state.research]);
	const memos = memoProjection.memos;
	const solutionRunReady = typeof runId === "string" && runId.length > 0 && state.run_id === runId;
	const solutionGeneration = RUN_GENERATION_RE.test(expectedGeneration || "") ? expectedGeneration : null;
	const solutionSeq = Number.isSafeInteger(observedSeq) && observedSeq >= 0 ? observedSeq : null;
	const solutionHistoryAligned = !(readOnly && historySeq != null) || historySeq === solutionSeq;
	const solutionIdentityReady = solutionRunReady && solutionGeneration != null && solutionSeq != null && solutionHistoryAligned;
	// Scope the async source projection to the exact report identity. In particular, a late live
	// response from an older event must not become the winning code for a newer report render even
	// when the run generation and champion id happen to be unchanged.
	const bestCodeScope = useMemo(() => JSON.stringify({
		runId: String(runId),
		stateRunId: state.run_id ?? null,
		generation: solutionGeneration,
		nodeId: best?.id ?? null,
		access: {
			readOnlyReason,
			evidenceAvailable: evidenceAvailable !== false
		},
		snapshot: {
			kind: readOnly && historySeq != null ? "history" : readOnly ? "read-only" : "live",
			seq: solutionSeq,
			routeSeq: readOnly ? historySeq : null
		}
	}), [
		runId,
		state.run_id,
		solutionGeneration,
		best?.id,
		readOnlyReason,
		evidenceAvailable,
		readOnly,
		historySeq,
		solutionSeq
	]);
	const [bestCodeResource, setBestCodeResource] = useState({
		scope: null,
		status: "idle",
		data: null,
		error: null
	});
	const [bestCodeNonce, setBestCodeNonce] = useState(0);
	const bestCodeRequestRef = useRef(null);
	const [openMemo, setOpenMemo] = useState(memos.length ? memos[memos.length - 1].sourceIndex : null);
	const [refreshing, setRefreshing] = useState(false);
	const [refreshError, setRefreshError] = useState("");
	const [refreshRetryAllowed, setRefreshRetryAllowed] = useState(true);
	const [savedRefreshIntent, setSavedRefreshIntent] = useState(null);
	const refreshStorageReady = savedRefreshIntent !== false;
	const refreshGenerationReady = RUN_GENERATION_RE.test(expectedGeneration || "");
	const refreshRequestRef = useRef({
		token: 0,
		receiptSeq: null,
		timer: null,
		busy: false,
		idempotencyKey: null,
		generation: null,
		controller: null
	});
	const observedSeqRef = useRef(observedSeq);
	observedSeqRef.current = observedSeq;
	useEffect(() => {
		bestCodeRequestRef.current?.controller.abort();
		bestCodeRequestRef.current = null;
		if (!best) {
			setBestCodeResource({
				scope: bestCodeScope,
				status: "idle",
				data: null,
				error: null
			});
			return;
		}
		if (readOnlyReason === "review" && !evidenceAvailable) {
			setBestCodeResource({
				scope: bestCodeScope,
				status: "restricted",
				data: null,
				error: null
			});
			return;
		}
		if (!solutionIdentityReady) {
			setBestCodeResource({
				scope: bestCodeScope,
				status: "waiting",
				data: null,
				error: null
			});
			return;
		}
		const params = new URLSearchParams();
		params.set("seq", String(solutionSeq));
		params.set("expected_generation", solutionGeneration);
		const at = `?${params.toString()}`;
		const timed = deadlineGet(runNodeApiPath(runId, best.id, at), SOLUTION_DETAIL_TIMEOUT_MS);
		const request = {
			scope: bestCodeScope,
			controller: timed.controller
		};
		bestCodeRequestRef.current = request;
		setBestCodeResource({
			scope: bestCodeScope,
			status: "loading",
			data: null,
			error: null
		});
		timed.promise.then((data) => {
			if (bestCodeRequestRef.current !== request) return;
			bestCodeRequestRef.current = null;
			const exact = normalizeReportNodeDetail(data, {
				nodeId: best.id,
				historySeq: solutionSeq,
				expectedGeneration: solutionGeneration
			});
			setBestCodeResource(exact ? {
				scope: request.scope,
				status: "ready",
				data: exact,
				error: null
			} : {
				scope: request.scope,
				status: "error",
				data: null,
				error: "The node detail response did not match this run, generation, and snapshot."
			});
		}, () => {
			if (bestCodeRequestRef.current !== request) return;
			bestCodeRequestRef.current = null;
			const timeout = timed.timedOut();
			setBestCodeResource({
				scope: request.scope,
				status: timeout ? "timeout" : "error",
				data: null,
				error: timeout ? `The node detail request timed out after ${SOLUTION_DETAIL_TIMEOUT_MS / 1e3} seconds.` : "The node detail request failed. Check the connection and retry."
			});
		});
		return () => {
			if (bestCodeRequestRef.current !== request) return;
			bestCodeRequestRef.current = null;
			request.controller.abort();
		};
	}, [
		runId,
		best?.id,
		readOnlyReason,
		evidenceAvailable,
		solutionIdentityReady,
		solutionSeq,
		solutionGeneration,
		bestCodeScope,
		bestCodeNonce
	]);
	const bestCodeCurrent = bestCodeResource.scope === bestCodeScope;
	const bestCodeStatus = bestCodeCurrent ? bestCodeResource.status : !best ? "idle" : readOnlyReason === "review" && !evidenceAvailable ? "restricted" : !solutionIdentityReady ? "waiting" : "loading";
	const solutionWaitingMessage = !solutionRunReady ? "Waiting for this report to match the requested run before loading solution code…" : solutionGeneration == null ? "Waiting for the exact run generation before loading solution code…" : solutionSeq == null ? "Waiting for the exact report snapshot before loading solution code…" : "Waiting for the historical snapshot to finish reconciling…";
	const bestCode = bestCodeCurrent ? bestCodeResource.data : null;
	const finishRefresh = (token, { error = "", canRetry = true, preserveIntent = false } = {}) => {
		const request = refreshRequestRef.current;
		if (request.token !== token) return;
		if (request.timer) clearTimeout(request.timer);
		let finalError = error;
		let finalCanRetry = canRetry;
		let finalPreserveIntent = preserveIntent;
		if (!finalPreserveIntent && request.idempotencyKey && request.generation) {
			try {
				reportRefreshIntent(runId, request.generation, request.idempotencyKey);
			} catch {
				finalError = "The completed report identity could not be cleared. Reload.";
				finalCanRetry = false;
				finalPreserveIntent = true;
			}
		}
		request.timer = null;
		request.controller?.abort();
		request.controller = null;
		request.receiptSeq = null;
		request.busy = false;
		if (!finalPreserveIntent) {
			request.idempotencyKey = null;
			request.generation = null;
		}
		setSavedRefreshIntent(finalPreserveIntent && request.idempotencyKey && request.generation ? {
			generation: request.generation,
			idempotencyKey: request.idempotencyKey
		} : null);
		setRefreshing(false);
		setRefreshError(finalError);
		setRefreshRetryAllowed(finalCanRetry);
	};
	// The endpoint receipt names the exact report event; content fields such as at_node and trigger
	// may legitimately repeat, so they are not completion identities.
	useEffect(() => {
		const request = refreshRequestRef.current;
		if (refreshing && Number.isSafeInteger(request.receiptSeq) && request.generation === expectedGeneration && Number.isSafeInteger(observedSeq) && observedSeq >= request.receiptSeq) {
			finishRefresh(request.token);
		}
	}, [
		observedSeq,
		refreshing,
		expectedGeneration
	]);
	useEffect(() => {
		const request = refreshRequestRef.current;
		request.token += 1;
		if (request.timer) clearTimeout(request.timer);
		request.timer = null;
		request.controller?.abort();
		request.controller = null;
		request.receiptSeq = null;
		request.busy = false;
		request.idempotencyKey = null;
		request.generation = null;
		setRefreshing(false);
		setRefreshError("");
		setRefreshRetryAllowed(true);
		setSavedRefreshIntent(null);
		if (!readOnly && refreshGenerationReady) {
			try {
				setSavedRefreshIntent(peekReportRefreshIntent(runId, expectedGeneration));
			} catch {
				setSavedRefreshIntent(false);
				setRefreshError("Paid report refresh needs working session storage to preserve one request identity.");
				setRefreshRetryAllowed(false);
			}
		}
		return () => {
			request.token += 1;
			if (request.timer) clearTimeout(request.timer);
			request.timer = null;
			request.controller?.abort();
			request.controller = null;
			request.receiptSeq = null;
			request.busy = false;
			request.idempotencyKey = null;
			request.generation = null;
		};
	}, [
		runId,
		expectedGeneration,
		readOnly,
		refreshGenerationReady
	]);
	const dl = (name, text, type) => downloadBlob(name, [text], type);
	const refresh = async () => {
		const request = refreshRequestRef.current;
		if (request.busy) return;
		// Bind paid work to the generation visible at click time. Until the result is authoritative,
		// remounts and retries retain this identity and rejoin the same server job.
		let intent;
		try {
			intent = reportRefreshIntent(runId, expectedGeneration);
		} catch {
			const message = "Report refresh needs working session storage.";
			setRefreshError(message);
			setRefreshRetryAllowed(false);
			onToast?.(message);
			return;
		}
		if (!intent) {
			const message = "Reload the run before generating its report; its generation is not verified.";
			setRefreshError(message);
			setRefreshRetryAllowed(false);
			onToast?.(message);
			return;
		}
		request.token += 1;
		const token = request.token;
		request.busy = true;
		request.receiptSeq = null;
		request.generation = intent.generation;
		request.idempotencyKey = intent.idempotencyKey;
		setSavedRefreshIntent(intent);
		request.controller = typeof AbortController === "undefined" ? null : new AbortController();
		if (request.timer) clearTimeout(request.timer);
		request.timer = null;
		setRefreshing(true);
		setRefreshError("");
		setRefreshRetryAllowed(true);
		try {
			const r = await CONTROL.refreshReport(runId, {
				expectedGeneration: intent.generation,
				idempotencyKey: intent.idempotencyKey,
				signal: request.controller?.signal
			});
			if (refreshRequestRef.current.token !== token) return;
			if (r && r.ok === false) {
				const [message, canRetry, preserveIntent] = reportRefreshFailure(r);
				finishRefresh(token, {
					error: message,
					canRetry,
					preserveIntent
				});
				onToast?.(message);
				return;
			}
			if (!Number.isSafeInteger(r?.seq) || r.seq < 0) {
				const message = "No durable report receipt was returned. Reload and reconcile it.";
				finishRefresh(token, {
					error: message,
					canRetry: false,
					preserveIntent: true
				});
				onToast?.(message);
				return;
			}
			request.receiptSeq = r.seq;
			if (Number.isSafeInteger(observedSeqRef.current) && observedSeqRef.current >= r.seq) {
				finishRefresh(token);
				return;
			}
			request.timer = setTimeout(() => {
				const message = "The report was generated, but this view did not observe its event. Reload the run before generating again.";
				finishRefresh(token, {
					error: message,
					canRetry: false
				});
				onToast?.(message);
			}, 3e4);
		} catch (error) {
			if (refreshRequestRef.current.token !== token) return;
			const [message, canRetry, preserveIntent] = reportRefreshFailure(error, true);
			finishRefresh(token, {
				error: message,
				canRetry,
				preserveIntent
			});
			onToast?.(message);
		}
	};
	const refreshStatus = readOnly ? "" : !refreshGenerationReady ? "Paid report refresh is disabled: reload the run and wait for its verified generation." : !refreshStorageReady ? "Paid refresh is disabled: this tab cannot safely save its request identity. Enable session storage, then reload." : refreshing ? "Paid refresh is running with the saved request. You can safely leave and resume it later." : savedRefreshIntent ? "Paid request saved. Resume rechecks the same request; it cannot start a second job. You can safely leave." : "Paid AI action: provider charges may apply. One request identity is saved so you can safely leave and resume.";
	const refreshButtonLabel = refreshing ? "Paid refresh running…" : savedRefreshIntent ? "Resume paid refresh" : "Refresh report · paid";
	const refreshDisabledReason = !refreshGenerationReady ? "Reload the run and wait for its verified generation before starting a paid refresh." : !refreshStorageReady ? "Paid refresh is unavailable because this tab cannot safely store its request identity." : !refreshRetryAllowed ? refreshError || "This paid refresh cannot be resumed safely yet." : "";
	const impr = (s) => s.delta == null || (state.direction === "min" ? s.delta < 0 : s.delta > 0);
	const exportContext = {
		generation: expectedGeneration,
		snapshotSeq: observedSeq
	};
	const modelCard = () => JSON.stringify(buildModelCard({
		...state,
		report: rep
	}, best, exportContext), null, 2);
	return /* @__PURE__ */ _jsxs("div", {
		className: "report-view",
		"aria-busy": refreshing || undefined,
		children: [
			/* @__PURE__ */ _jsx("h2", {
				className: "report-title",
				children: state.goal || state.task_id
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "report-sub muted",
				children: [
					state.run_id,
					" · ",
					state.direction,
					" · ",
					state.phase || (state.finished ? "finished" : "running"),
					state.stop_reason ? ` (${state.stop_reason})` : "",
					" · ",
					nodeCount,
					" nodes (",
					a.nEval,
					" evaluated, ",
					failed.length,
					" failed)",
					state.llm_cost && ` · ${fmtInt(state.llm_cost.total_tokens)} tokens · $${fmt(state.llm_cost.cost)}`
				]
			}),
			/* @__PURE__ */ _jsx(VerdictBanner, {
				v,
				onOpenPanel,
				canOpenPanel
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "toolbar report-toolbar",
				role: "group",
				"aria-label": "Report actions",
				children: [
					!readOnly && /* @__PURE__ */ _jsxs("button", {
						className: "btn sm primary",
						disabled: refreshing || !refreshRetryAllowed || !refreshGenerationReady || !refreshStorageReady,
						onClick: refresh,
						"aria-describedby": "paid-report-refresh-status",
						title: refreshDisabledReason || refreshStatus,
						children: [
							/* @__PURE__ */ _jsx(OpIcon, {
								name: "replay",
								size: 12
							}),
							" ",
							refreshButtonLabel
						]
					}),
					readOnly && /* @__PURE__ */ _jsx("span", {
						className: "history-inline",
						children: readOnlyReason === "review" ? "Read-only review · report refresh disabled" : readOnlyReason === "start-over" ? "Start over unresolved · report refresh disabled" : `Snapshot seq ${historySeq} · report refresh disabled`
					}),
					/* @__PURE__ */ _jsx("span", {
						className: "spacer",
						style: { flex: 1 }
					}),
					/* @__PURE__ */ _jsxs("button", {
						className: "btn sm",
						onClick: () => window.print(),
						children: [/* @__PURE__ */ _jsx(OpIcon, {
							name: "printer",
							size: 12
						}), " Print / PDF"]
					}),
					/* @__PURE__ */ _jsxs("button", {
						className: "btn sm",
						onClick: () => dl(`${state.run_id}_report.md`, toMarkdown({
							...state,
							report: rep
						}, best, exportContext), "text/markdown"),
						children: [/* @__PURE__ */ _jsx(OpIcon, {
							name: "download",
							size: 12
						}), " Markdown"]
					}),
					best && evidenceAvailable && /* @__PURE__ */ _jsxs("button", {
						className: "btn sm",
						disabled: !bestCode?.code,
						onClick: () => dl(`solution_node${best.id}.py`, bestCode.code, "text/x-python"),
						children: [/* @__PURE__ */ _jsx(OpIcon, {
							name: "download",
							size: 12
						}), " Solution"]
					}),
					/* @__PURE__ */ _jsxs("button", {
						className: "btn sm",
						onClick: () => dl(`${state.run_id}_model_card.json`, modelCard(), "application/json"),
						children: [/* @__PURE__ */ _jsx(OpIcon, {
							name: "download",
							size: 12
						}), " Model card"]
					})
				]
			}),
			!readOnly && /* @__PURE__ */ _jsxs("div", {
				id: "paid-report-refresh-status",
				className: "report-inline-state paid",
				role: "status",
				"aria-live": "polite",
				"aria-atomic": "true",
				children: [/* @__PURE__ */ _jsx(OpIcon, {
					name: savedRefreshIntent || refreshing ? "replay" : "bolt",
					size: 14
				}), /* @__PURE__ */ _jsx("span", { children: refreshStatus })]
			}),
			refreshError && /* @__PURE__ */ _jsxs("div", {
				className: "report-inline-state error",
				role: "alert",
				children: [
					/* @__PURE__ */ _jsx(OpIcon, {
						name: "alert",
						size: 14
					}),
					/* @__PURE__ */ _jsx("span", { children: refreshError }),
					!readOnly && refreshRetryAllowed && refreshGenerationReady && /* @__PURE__ */ _jsx("button", {
						className: "btn sm",
						onClick: refresh,
						children: savedRefreshIntent ? "Resume paid request" : "Retry paid refresh"
					})
				]
			}),
			!best && /* @__PURE__ */ _jsxs("div", {
				className: "report-empty-state",
				role: "status",
				children: [/* @__PURE__ */ _jsx("h2", { children: a.nEval ? "No feasible champion yet" : "No champion yet" }), /* @__PURE__ */ _jsx("p", { children: a.nEval ? "Evaluations exist, but none currently qualifies for winner selection. Review constraints and failed checks." : "The report will add a champion, trajectory, and reproducible solution after the first successful evaluation." })]
			}),
			best && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("h2", {
				className: "section-h",
				children: "Champion — the answer"
			}), /* @__PURE__ */ _jsx(ChampionCard, {
				best,
				state
			})] }),
			a.steps.length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [
				/* @__PURE__ */ _jsx("h2", {
					className: "section-h",
					children: a.steps.length > 1 ? "How the metric got better" : "Metric baseline"
				}),
				/* @__PURE__ */ _jsx(Trajectory, {
					nodes: Object.values(state.nodes),
					direction: state.direction,
					state,
					steps: a.steps,
					onPick: onPickNode
				}),
				/* @__PURE__ */ _jsx(ImprovementWaterfall, {
					steps: a.steps,
					direction: state.direction
				}),
				/* @__PURE__ */ _jsx(DataTable, {
					caption: "Metric trajectory steps",
					card: false,
					children: /* @__PURE__ */ _jsxs("table", {
						className: "tbl",
						children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
							/* @__PURE__ */ _jsx("th", { children: "#" }),
							/* @__PURE__ */ _jsx("th", { children: "node" }),
							/* @__PURE__ */ _jsx("th", { children: "operator" }),
							/* @__PURE__ */ _jsx("th", { children: "metric" }),
							/* @__PURE__ */ _jsx("th", { children: "Δ" }),
							/* @__PURE__ */ _jsx("th", { children: "what changed" })
						] }) }), /* @__PURE__ */ _jsx("tbody", { children: a.steps.map((s, i) => /* @__PURE__ */ _jsxs("tr", { children: [
							/* @__PURE__ */ _jsx("td", { children: i + 1 }),
							/* @__PURE__ */ _jsxs("td", { children: ["#", s.id] }),
							/* @__PURE__ */ _jsxs("td", { children: [/* @__PURE__ */ _jsxs("span", {
								className: "report-step-kind",
								"aria-hidden": "true",
								children: [s.operator || "unknown operator", s.theme && s.theme !== s.operator && /* @__PURE__ */ _jsx("span", {
									className: "pill report-step-theme",
									children: s.theme
								})]
							}), /* @__PURE__ */ _jsx("span", {
								className: "sr-only",
								children: reportStepIdentity(s.operator, s.theme)
							})] }),
							/* @__PURE__ */ _jsx("td", { children: fmt(s.to) }),
							/* @__PURE__ */ _jsx("td", {
								className: `report-delta ${s.delta == null ? "baseline" : impr(s) ? "improved" : "regressed"}`,
								children: s.delta == null ? "baseline" : fmt(s.delta)
							}),
							/* @__PURE__ */ _jsx("td", {
								className: "muted",
								children: paramDiffLabel(s.diff)
							})
						] }, s.id)) })]
					})
				}),
				a.steps.length > 1 && /* @__PURE__ */ _jsxs("div", {
					className: "muted",
					children: [
						"Total improvement ",
						/* @__PURE__ */ _jsx("b", { children: fmt(a.totalGain) }),
						" over ",
						a.steps.length,
						" steps (baseline ",
						fmt(a.firstBest),
						" → best ",
						fmt(a.finalBest),
						")."
					]
				})
			] }),
			memos.length || imp.length ? /* @__PURE__ */ _jsxs(_Fragment, { children: [
				/* @__PURE__ */ _jsx("h2", {
					className: "section-h",
					children: "What we learned"
				}),
				imp.length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
					className: "muted",
					style: { marginTop: 6 },
					children: "which knobs mattered (|correlation| with the metric)"
				}), /* @__PURE__ */ _jsx(DataTable, {
					caption: "Report hyperparameter importance",
					card: false,
					children: /* @__PURE__ */ _jsxs("table", {
						className: "tbl",
						children: [/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsxs("tr", { children: [
							/* @__PURE__ */ _jsx("th", { children: "param" }),
							/* @__PURE__ */ _jsx("th", { children: "importance" }),
							/* @__PURE__ */ _jsx("th", { children: "r" }),
							/* @__PURE__ */ _jsx("th", { children: "n" })
						] }) }), /* @__PURE__ */ _jsx("tbody", { children: imp.map((row) => /* @__PURE__ */ _jsxs("tr", { children: [
							/* @__PURE__ */ _jsx("td", { children: row.k }),
							/* @__PURE__ */ _jsx("td", { children: fmt(row.imp, 3) }),
							/* @__PURE__ */ _jsxs("td", {
								className: "muted",
								children: [row.r >= 0 ? "+" : "", fmt(row.r, 3)]
							}),
							/* @__PURE__ */ _jsx("td", {
								className: "muted",
								children: row.n
							})
						] }, row.k)) })]
					})
				})] }),
				memos.length > 0 && /* @__PURE__ */ _jsxs("div", {
					style: { marginTop: 8 },
					children: [memoProjection.omitted > 0 && /* @__PURE__ */ _jsxs("div", {
						className: "muted",
						children: [
							"Showing the latest ",
							memos.length,
							" of ",
							memoProjection.total,
							" research memos; older, malformed, or over-budget entries are omitted."
						]
					}), memos.map((m) => /* @__PURE__ */ _jsx(MemoCard, {
						memo: m,
						idx: m.sourceIndex,
						open: openMemo === m.sourceIndex,
						onToggle: (key) => setOpenMemo((current) => current === key ? null : key)
					}, m.sourceIndex))]
				})
			] }) : null,
			/* @__PURE__ */ _jsx("h2", {
				className: "section-h",
				children: "What didn't work"
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "cardgrid",
				style: { marginBottom: 10 },
				children: [
					Object.entries(a.failures).map(([r, ns]) => /* @__PURE__ */ _jsxs("div", {
						className: "stat",
						children: [/* @__PURE__ */ _jsx("div", {
							className: "n",
							children: ns.length
						}), /* @__PURE__ */ _jsxs("div", {
							className: "l",
							children: ["failed · ", r]
						})]
					}, r)),
					a.regressions.length > 0 && /* @__PURE__ */ _jsxs("div", {
						className: "stat",
						children: [/* @__PURE__ */ _jsx("div", {
							className: "n",
							children: a.regressions.length
						}), /* @__PURE__ */ _jsx("div", {
							className: "l",
							children: "regressions"
						})]
					}),
					a.infeasible.length > 0 && /* @__PURE__ */ _jsxs("div", {
						className: "stat",
						children: [/* @__PURE__ */ _jsx("div", {
							className: "n",
							children: a.infeasible.length
						}), /* @__PURE__ */ _jsx("div", {
							className: "l",
							children: "infeasible"
						})]
					}),
					!Object.keys(a.failures).length && !a.regressions.length && !a.infeasible.length && /* @__PURE__ */ _jsxs("div", {
						className: "stat",
						children: [/* @__PURE__ */ _jsx("div", {
							className: "n",
							children: "0"
						}), /* @__PURE__ */ _jsx("div", {
							className: "l",
							children: "nothing notably failed"
						})]
					})
				]
			}),
			best && /* @__PURE__ */ _jsxs(_Fragment, { children: [
				/* @__PURE__ */ _jsx("h2", {
					className: "section-h",
					children: "Reproduce — winning solution"
				}),
				bestCodeStatus === "restricted" && /* @__PURE__ */ _jsx("div", {
					className: "report-inline-state report-code-state",
					role: "status",
					children: "Solution source was not included in this summary-only review link."
				}),
				bestCodeStatus === "waiting" && /* @__PURE__ */ _jsx("div", {
					className: "report-inline-state report-code-state",
					role: "status",
					"aria-live": "polite",
					children: solutionWaitingMessage
				}),
				bestCodeStatus === "loading" && /* @__PURE__ */ _jsx("div", {
					className: "report-inline-state report-code-state",
					role: "status",
					children: "Loading solution code…"
				}),
				(bestCodeStatus === "error" || bestCodeStatus === "timeout") && /* @__PURE__ */ _jsxs("div", {
					className: "report-inline-state report-code-state error",
					role: "alert",
					children: [/* @__PURE__ */ _jsxs("span", { children: ["Couldn’t load the winning code: ", bestCodeResource.error] }), /* @__PURE__ */ _jsx("button", {
						type: "button",
						className: "btn sm",
						onClick: () => setBestCodeNonce((n) => n + 1),
						children: "Retry"
					})]
				}),
				bestCodeStatus === "ready" && (bestCode?.code ? /* @__PURE__ */ _jsx("pre", {
					className: "code",
					children: bestCode.code
				}) : /* @__PURE__ */ _jsx("div", {
					className: "report-inline-state report-code-state",
					role: "status",
					children: "No solution source was recorded for this node (for example, a repository task may not use solution.py)."
				}))
			] }),
			/* @__PURE__ */ _jsx(AgentNarrative, {
				rep,
				coverage,
				generation: expectedGeneration,
				snapshotSeq: observedSeq
			})
		]
	});
}
