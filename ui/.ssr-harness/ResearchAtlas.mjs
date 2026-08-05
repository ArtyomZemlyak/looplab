import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { getCrossRunAtlas, getCrossRunClaims, getCrossRunCurationLog, getCrossRunClaimCurationLog, projectResearchAtlasSource } from "./api.mjs";
import { atlasPortfolioId, buildResearchAtlasView, mergeCurationLogs, mergeResearchAtlasPayload, isValidAtlasSourceEnvelope, reconcileAtlasSourceStatuses, researchAtlasPayloadPortfolioId } from "./researchAtlasModel.mjs";

import { deadlineRequest } from "./requestDeadline.mjs";
import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
const SOURCES = [
	[
		"atlas",
		(signal) => getCrossRunAtlas(24, { signal }),
		"Concept + evidence"
	],
	[
		"claims",
		(signal) => getCrossRunClaims(40, 0, { signal }),
		"Claim records"
	],
	[
		"conceptCuration",
		(signal) => getCrossRunCurationLog(20, { signal }),
		"Concept steward log"
	],
	[
		"claimCuration",
		(signal) => getCrossRunClaimCurationLog(20, { signal }),
		"Claim steward log"
	]
];
const SOURCE_TIMEOUT_MS = 15e3;
const countLabel = (count, singular, plural = `${singular}s`) => `${count} ${count === 1 ? singular : plural}`;
const EPISTEMIC_COPY = {
	supported: "support-only evidence",
	refuted: "opposition-only evidence",
	mixed: "mixed evidence",
	inconclusive: "insufficient evidence"
};
export function AtlasRunReference({ run }) {
	const context = [run.task && `task: ${run.task}`, run.scope && `scope: ${run.scope}`].filter(Boolean).join(" · ");
	const disclosure = run.metricSuppressed ? "Metric hidden · missing task/scope or objective direction." : run.metric != null && run.optimizationOrientation ? `Not comparable across runs · metric name/unit unknown · ${run.optimizationOrientation} · ${run.metric.toLocaleString(undefined, { maximumSignificantDigits: 6 })}` : "";
	return /* @__PURE__ */ _jsxs("a", {
		href: `#/run/${encodeURIComponent(run.runId)}`,
		children: [
			/* @__PURE__ */ _jsx("span", {
				className: "atlas-runref-id",
				children: run.runId
			}),
			context && /* @__PURE__ */ _jsx("span", {
				className: "atlas-runref-context",
				children: context
			}),
			disclosure && /* @__PURE__ */ _jsx("span", {
				className: run.metricSuppressed ? "atlas-runref-suppressed" : "atlas-runref-warning",
				children: disclosure
			})
		]
	});
}
function SourceWatermark({ sourceKey, label, source, retry, busy, pending, activeRetry = false, retryable = true, children }) {
	const state = source.state;
	return /* @__PURE__ */ _jsxs("p", {
		className: `atlas-source-note atlas-source-focus-target atlas-source-${pending ? "loading" : state}`,
		"data-atlas-source": sourceKey,
		tabIndex: -1,
		children: [
			/* @__PURE__ */ _jsx("strong", { children: label }),
			" · ",
			pending ? "loading" : state === "retained-stale" ? "stale" : state === "failed" ? "unavailable" : "loaded",
			" · ",
			"revision ",
			source.revision || "unknown",
			" · ",
			children,
			retryable && state !== "current" && /* @__PURE__ */ _jsxs(_Fragment, { children: [" · ", /* @__PURE__ */ _jsx("button", {
				type: "button",
				className: "btn sm atlas-retry-action",
				disabled: busy && !activeRetry,
				"aria-disabled": activeRetry || undefined,
				"aria-busy": activeRetry || undefined,
				onClick: (event) => retry(sourceKey, "watermark", event.currentTarget),
				"aria-label": `${activeRetry ? "Retrying" : "Retry"} ${label}`,
				children: activeRetry ? "Retrying…" : "Retry"
			})] })
		]
	});
}
export function EvidenceSourceNotice({ concept, claims }) {
	if (concept.status === "complete" && claims.status === "complete") return null;
	return /* @__PURE__ */ _jsxs("div", {
		className: "notice resource-warning atlas-degraded",
		role: "status",
		children: [/* @__PURE__ */ _jsx("b", { children: "Evidence source incomplete." }), /* @__PURE__ */ _jsxs("span", { children: [
			"Concepts ",
			concept.status,
			"; claims ",
			claims.status,
			".",
			concept.status === "partial" && ` Concept bounds ${concept.counts.join("/")}.`,
			" Absence and one-sided claim state withheld."
		] })]
	});
}
export function AtlasEmptyState({ sourceStates, conceptSource, claimSource = { status: "unknown" }, pending = [], retry, busy, activeRetryKey = "", memorySettingsNeeded = false }) {
	const pendingSources = new Set(pending);
	const evidenceCurrent = ["atlas", "claims"].every((key) => sourceStates[key]?.state === "current" && !pendingSources.has(key));
	const completeEmpty = evidenceCurrent && conceptSource.status === "complete" && claimSource.status === "complete";
	return /* @__PURE__ */ _jsxs("section", {
		className: "atlas-empty",
		"aria-labelledby": "atlas-empty-title",
		children: [/* @__PURE__ */ _jsxs("div", {
			className: "atlas-empty-copy",
			children: [/* @__PURE__ */ _jsxs("div", {
				className: "atlas-empty-message",
				role: "status",
				"aria-atomic": "true",
				children: [/* @__PURE__ */ _jsx("h2", {
					id: "atlas-empty-title",
					children: completeEmpty ? "No cross-run evidence" : evidenceCurrent ? "No retained evidence" : "Atlas evidence unavailable"
				}), /* @__PURE__ */ _jsx("p", { children: completeEmpty ? "No evidence returned; runs may still exist." : evidenceCurrent ? "Empty rows do not prove absence." : "Retry unavailable or stale sources." })]
			}), memorySettingsNeeded && /* @__PURE__ */ _jsx("div", {
				className: "atlas-empty-actions",
				children: /* @__PURE__ */ _jsx("a", {
					className: "btn",
					href: "#/settings",
					children: "Memory settings"
				})
			})]
		}), /* @__PURE__ */ _jsx("ul", {
			className: "atlas-source-readiness",
			"aria-label": "Atlas source readiness",
			children: SOURCES.map(([key, , label]) => {
				const state = sourceStates[key]?.state || "failed";
				const loading = pendingSources.has(key);
				const status = loading ? "loading" : state === "current" ? key === "atlas" ? conceptSource.status : key === "claims" ? claimSource.status : "complete" : state === "retained-stale" ? "stale" : "unavailable";
				const retryable = state !== "current";
				const activeRetry = loading && activeRetryKey === key;
				return /* @__PURE__ */ _jsxs("li", {
					className: `atlas-empty-source atlas-source-focus-target atlas-empty-source-${loading ? "loading" : state}`,
					"data-atlas-source": key,
					tabIndex: -1,
					children: [
						/* @__PURE__ */ _jsx("span", {
							className: "atlas-readiness-dot",
							"aria-hidden": "true"
						}),
						/* @__PURE__ */ _jsxs("span", {
							className: "atlas-empty-source-head",
							children: [/* @__PURE__ */ _jsx("strong", { children: label }), /* @__PURE__ */ _jsx("span", {
								className: "atlas-readiness-state",
								children: status
							})]
						}),
						retryable && /* @__PURE__ */ _jsx("button", {
							type: "button",
							className: "btn sm atlas-retry-action",
							disabled: busy && !activeRetry,
							"aria-disabled": activeRetry || undefined,
							"aria-busy": activeRetry || undefined,
							onClick: (event) => retry(key, "empty-source", event.currentTarget),
							"aria-label": `${activeRetry ? "Retrying" : "Retry"} ${label}`,
							children: activeRetry ? "Retrying…" : "Retry"
						})
					]
				}, key);
			})
		})]
	});
}
export function ClaimCard({ claim, compact = false }) {
	const groups = [
		[
			"support",
			claim.support,
			claim.nSupport
		],
		[
			"oppose",
			claim.oppose,
			claim.nOppose
		],
		[
			"unverified",
			claim.unverified,
			claim.nUnverified
		],
		[
			"contradiction",
			claim.contradicts,
			claim.nContradicts
		]
	];
	const evidence = groups.flatMap(([kind, values]) => values.map((value) => [kind, value]));
	const hiddenEvidence = groups.reduce((sum, [, values, total]) => sum + Math.max(0, total - values.length), 0);
	const context = [...claim.scopes.map((value) => `claim grouping · ${value}`), ...claim.runs.map((value) => `run · ${value}`)];
	const epistemicCopy = EPISTEMIC_COPY[claim.epistemic] || EPISTEMIC_COPY.inconclusive;
	const decisionWarning = claim.maturity !== "machine-proposed" && claim.decisionFresh !== true;
	return /* @__PURE__ */ _jsxs("article", {
		className: `atlas-claim atlas-state-${claim.epistemic}`,
		children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "atlas-claim-head",
				children: [/* @__PURE__ */ _jsx("span", {
					className: `chip xs atlas-epistemic ${claim.epistemic}`,
					children: epistemicCopy
				}), /* @__PURE__ */ _jsxs("span", {
					className: "pill",
					children: [claim.maturity.replaceAll("-", " "), decisionWarning && ` · ⚠ ${claim.decisionFresh === false ? "stale" : "freshness unknown"}`]
				})]
			}),
			/* @__PURE__ */ _jsx("p", { children: claim.statement }),
			/* @__PURE__ */ _jsxs("div", {
				className: "atlas-claim-counts",
				"aria-label": "Claim evidence counts",
				children: [groups.map(([kind, , total], index) => (index < 2 || total > 0) && /* @__PURE__ */ _jsxs("span", { children: [
					kind,
					kind === "contradiction" ? "s" : " refs",
					" ",
					/* @__PURE__ */ _jsx("b", { children: total })
				] }, kind)), claim.scopes.length > 0 && /* @__PURE__ */ _jsx("span", {
					title: claim.scopes.join(", "),
					children: countLabel(claim.scopes.length, "claim grouping")
				})]
			}),
			context.length > 0 && /* @__PURE__ */ _jsxs("div", {
				className: "atlas-claim-context",
				"aria-label": "Claim groups and runs",
				children: [context.slice(0, 3).map((value, index) => /* @__PURE__ */ _jsx("span", {
					className: "pill",
					children: value
				}, index)), context.length > 3 && /* @__PURE__ */ _jsxs("span", {
					className: "muted",
					children: [
						"+",
						context.length - 3,
						" more"
					]
				})]
			}),
			!compact && (evidence.length > 0 || hiddenEvidence > 0) && /* @__PURE__ */ _jsxs("details", { children: [/* @__PURE__ */ _jsx("summary", { children: "Show evidence context" }), /* @__PURE__ */ _jsxs("div", {
				className: "atlas-evidence",
				children: [
					evidence.length === 0 && /* @__PURE__ */ _jsx("span", {
						className: "atlas-evidence-boundary",
						children: "No context returned."
					}),
					evidence.map(([kind, value], index) => /* @__PURE__ */ _jsxs("code", { children: [
						kind,
						" · ",
						value
					] }, `${kind}-${index}`)),
					hiddenEvidence > 0 && /* @__PURE__ */ _jsxs("span", {
						className: "atlas-evidence-boundary",
						children: [countLabel(hiddenEvidence, "additional reference"), " not shown (claim limit)."]
					})
				]
			})] })
		]
	});
}
function RouteState({ kind, errors, errorKind, onRetry, busy }) {
	if (kind === "loading") return /* @__PURE__ */ _jsxs("div", {
		className: "run-resource-state",
		role: "status",
		children: [/* @__PURE__ */ _jsx("span", {
			className: "dag-empty-spinner",
			"aria-hidden": "true"
		}), /* @__PURE__ */ _jsx("h1", {
			id: "atlas-route-state-title",
			children: "Loading Atlas"
		})]
	});
	const memoryMissing = errorKind ? errorKind === "memory" : errors.length > 0 && errors.every((error) => error.status === 400);
	return /* @__PURE__ */ _jsxs("div", {
		className: "run-resource-state",
		children: [/* @__PURE__ */ _jsxs("div", {
			className: "atlas-route-state-message",
			role: memoryMissing ? "status" : "alert",
			"aria-atomic": "true",
			children: [/* @__PURE__ */ _jsx("h1", {
				id: "atlas-route-state-title",
				children: memoryMissing ? "Atlas not configured" : "Research Atlas couldn’t load"
			}), /* @__PURE__ */ _jsx("p", { children: memoryMissing ? "Set Memory dir in Settings, then try again. Your runs were not changed." : "None of the Atlas sources returned usable data. Your runs were not changed." })]
		}), /* @__PURE__ */ _jsxs("div", {
			className: "resource-state-actions",
			children: [memoryMissing && /* @__PURE__ */ _jsx("a", {
				className: "btn primary",
				href: "#/settings",
				children: "Open Settings"
			}), /* @__PURE__ */ _jsx("button", {
				type: "button",
				className: `btn ${memoryMissing ? "" : "primary"}`,
				"data-atlas-retry": "route",
				"aria-disabled": busy || undefined,
				"aria-busy": busy || undefined,
				onClick: onRetry,
				children: busy ? "Trying again…" : "Try again"
			})]
		})]
	});
}
export default function ResearchAtlas({ onBack }) {
	const [request, setRequest] = useState({
		key: "",
		attempt: 0
	});
	const requestId = useRef(0);
	const busyRef = useRef(true);
	const focusAttemptRef = useRef(0);
	const retryFocusRef = useRef(null);
	const atlasMainRef = useRef(null);
	const [resource, setResource] = useState({
		status: "loading",
		view: null,
		payload: null,
		errors: [],
		pending: [],
		attempt: 0,
		routeErrorKind: "",
		sourceStates: reconcileAtlasSourceStatuses({}, {}, "")
	});
	const clearRetryFocus = () => {
		retryFocusRef.current?.cleanup?.();
		retryFocusRef.current = null;
	};
	useEffect(() => {
		let active = true;
		const controllers = [];
		const id = ++requestId.current;
		const requestedSources = request.key ? SOURCES.filter(([key]) => key === request.key) : SOURCES;
		const keys = requestedSources.map(([key]) => key);
		const fullRefresh = keys.length === SOURCES.length;
		let batchPortfolioId = "";
		let remaining = requestedSources.length;
		busyRef.current = true;
		setResource((current) => ({
			...current,
			status: current.view ? "refreshing" : current.errors.length > 0 ? "retrying" : "loading",
			errors: current.errors.filter((error) => !keys.includes(error.key)),
			routeErrorKind: !current.view && current.errors.length > 0 ? current.errors.every((error) => error.status === 400) ? "memory" : "generic" : current.routeErrorKind,
			pending: keys,
			attempt: request.attempt
		}));
		const settle = (key, value, error) => {
			if (!active || id !== requestId.current) return;
			let valid = isValidAtlasSourceEnvelope(key, value);
			const incomingPortfolioId = valid ? atlasPortfolioId(value) : "";
			if (valid && batchPortfolioId && incomingPortfolioId !== batchPortfolioId) valid = false;
			if (valid && !batchPortfolioId) batchPortfolioId = incomingPortfolioId;
			const projected = valid ? projectResearchAtlasSource(key, value) : null;
			const last = --remaining === 0;
			if (last) busyRef.current = false;
			setResource((current) => {
				const priorPortfolioId = researchAtlasPayloadPortfolioId(current.payload);
				const candidate = valid ? mergeResearchAtlasPayload(current.payload, { [key]: projected }, { allowPortfolioSwitch: fullRefresh }) : current.payload;
				const accepted = valid && candidate?.[key] === projected;
				const successful = accepted ? { [key]: projected } : {};
				const failed = accepted ? [] : [{
					key,
					status: error?.status
				}];
				const switched = accepted && priorPortfolioId && priorPortfolioId !== incomingPortfolioId;
				const payload = accepted ? candidate : current.payload;
				const view = accepted ? buildResearchAtlasView(payload.atlas, payload.claims, mergeCurationLogs(payload.conceptCuration, payload.claimCuration)) : current.view;
				const nextErrors = [...current.errors.filter((item) => item.key !== key), ...failed];
				return {
					...current,
					payload,
					view,
					status: last ? view ? "ready" : "error" : view ? "refreshing" : current.status === "retrying" ? "retrying" : "loading",
					errors: nextErrors,
					routeErrorKind: last && !view ? nextErrors.length > 0 && nextErrors.every((item) => item.status === 400) ? "memory" : "generic" : current.routeErrorKind,
					pending: current.pending.filter((item) => item !== key),
					sourceStates: reconcileAtlasSourceStatuses(switched ? {} : current.sourceStates, successful, new Date().toISOString(), [key])
				};
			});
		};
		requestedSources.forEach(([key, read]) => {
			const timed = deadlineRequest(read, SOURCE_TIMEOUT_MS);
			controllers.push(timed.controller);
			timed.promise.then((value) => settle(key, value), (error) => settle(key, null, error));
		});
		return () => {
			active = false;
			controllers.forEach((controller) => controller.abort());
		};
	}, [request]);
	useEffect(() => () => clearRetryFocus(), []);
	useLayoutEffect(() => {
		const intent = retryFocusRef.current;
		if (!intent || resource.attempt !== intent.attempt) return;
		const routeBecameUsable = intent.origin === "route" && Boolean(resource.view);
		if (!routeBecameUsable && resource.pending.length > 0) return;
		const active = document.activeElement;
		const focusStillOwned = active === intent.triggerNode || active === document.body || active === document.documentElement || !active?.isConnected;
		if (intent.yielded || !focusStillOwned || !atlasMainRef.current?.isConnected || document.querySelector("[aria-modal=\"true\"]")) {
			clearRetryFocus();
			return;
		}
		if (routeBecameUsable) {
			clearRetryFocus();
			atlasMainRef.current.focus({ preventScroll: true });
			return;
		}
		const failed = intent.keys.some((key) => resource.errors.some((error) => error.key === key));
		let target = null;
		if (failed) {
			target = intent.triggerNode?.isConnected ? intent.triggerNode : document.querySelector(resource.view ? "[data-atlas-retry=\"topbar\"]" : "[data-atlas-retry=\"route\"]");
		} else if (intent.key) {
			target = intent.surfaceNode?.isConnected ? intent.surfaceNode : atlasMainRef.current;
		} else if (intent.origin === "topbar" && intent.triggerNode?.isConnected) {
			target = intent.triggerNode;
		} else {
			target = atlasMainRef.current;
		}
		clearRetryFocus();
		target?.focus({ preventScroll: true });
	}, [
		resource.attempt,
		resource.pending,
		resource.errors,
		resource.view
	]);
	const retry = (key = "", origin = "topbar", triggerNode = null) => {
		if (busyRef.current) return;
		const attempt = ++focusAttemptRef.current;
		clearRetryFocus();
		const intent = {
			attempt,
			key,
			origin,
			triggerNode,
			keys: key ? [key] : SOURCES.map(([sourceKey]) => sourceKey),
			surfaceNode: triggerNode?.closest("[data-atlas-source]") || null,
			yielded: false,
			cleanup: null
		};
		const markYielded = (event) => {
			if (event.type === "focusin" && (event.target === document.body || event.target === document.documentElement)) return;
			if (event.target !== triggerNode && !triggerNode?.contains(event.target)) intent.yielded = true;
		};
		document.addEventListener("focusin", markYielded, true);
		document.addEventListener("pointerdown", markYielded, true);
		intent.cleanup = () => {
			document.removeEventListener("focusin", markYielded, true);
			document.removeEventListener("pointerdown", markYielded, true);
		};
		retryFocusRef.current = intent;
		busyRef.current = true;
		setRequest({
			key,
			attempt
		});
	};
	const view = resource.view;
	const sourceStates = resource.sourceStates;
	const atlasLoaded = sourceStates.atlas.state !== "failed";
	const claimsLoaded = sourceStates.claims.state !== "failed";
	const curationCurrent = sourceStates.conceptCuration.state === "current" && sourceStates.claimCuration.state === "current";
	const hasRetainedStale = Object.values(sourceStates).some((source) => source.state === "retained-stale");
	const hasMissing = SOURCES.some(([key]) => sourceStates[key].state === "failed" && !resource.pending.includes(key));
	const evidenceErrors = resource.errors.filter((error) => error.key === "atlas" || error.key === "claims");
	const memorySettingsNeeded = evidenceErrors.length > 0 && evidenceErrors.every((error) => error.status === 400);
	const busy = busyRef.current;
	const topbarOwnsRefresh = busy && request.key === "" && retryFocusRef.current?.origin === "topbar";
	return /* @__PURE__ */ _jsxs("div", {
		className: "app atlas-route",
		children: [/* @__PURE__ */ _jsxs("div", {
			className: "topbar",
			children: [
				/* @__PURE__ */ _jsxs("span", {
					className: "brand",
					children: [/* @__PURE__ */ _jsx("span", {
						className: "dot",
						children: "◉"
					}), " LoopLab"]
				}),
				/* @__PURE__ */ _jsx("button", {
					type: "button",
					className: "btn sm ghost",
					"aria-label": "Back to runs",
					onClick: onBack,
					children: "← runs"
				}),
				/* @__PURE__ */ _jsx("span", {
					className: "ttl",
					children: "Research Atlas preview"
				}),
				/* @__PURE__ */ _jsx("span", {
					className: "chip xs warn",
					children: "Experimental · bounded · read-only"
				}),
				/* @__PURE__ */ _jsx("span", { className: "spacer" }),
				view && /* @__PURE__ */ _jsx("button", {
					type: "button",
					className: "btn sm",
					"data-atlas-retry": "topbar",
					"aria-label": "Refresh all Research Atlas sources",
					disabled: busy && !topbarOwnsRefresh,
					"aria-disabled": topbarOwnsRefresh || undefined,
					"aria-busy": busy || undefined,
					onClick: (event) => retry("", "topbar", event.currentTarget),
					children: busy ? "Refreshing…" : "Refresh all"
				})
			]
		}), /* @__PURE__ */ _jsx("main", {
			ref: atlasMainRef,
			className: "research-atlas-page",
			"data-route-main": true,
			tabIndex: -1,
			"aria-busy": busy,
			"aria-labelledby": view ? "atlas-title" : "atlas-route-state-title",
			children: !view ? /* @__PURE__ */ _jsx(RouteState, {
				kind: resource.status,
				errors: resource.errors,
				errorKind: resource.routeErrorKind,
				busy,
				onRetry: (event) => retry("", "route", event.currentTarget)
			}) : /* @__PURE__ */ _jsxs("div", {
				className: "atlas-content",
				children: [
					/* @__PURE__ */ _jsxs("header", {
						className: "atlas-intro",
						children: [/* @__PURE__ */ _jsx("h1", {
							id: "atlas-title",
							children: "Portfolio evidence"
						}), /* @__PURE__ */ _jsx("p", { children: "Rows do not prove coverage. D8 receipts cover processed rows, not every run." })]
					}),
					resource.errors.length > 0 && /* @__PURE__ */ _jsxs("div", {
						className: "notice resource-warning atlas-degraded",
						role: "status",
						children: [/* @__PURE__ */ _jsx("b", { children: hasRetainedStale ? `Refresh incomplete; showing stale last-good data${hasMissing ? "; some sources unavailable" : ""}.` : "Some sources unavailable." }), /* @__PURE__ */ _jsxs("span", { children: [countLabel(resource.errors.length, "source refresh", "source refreshes"), " failed."] })]
					}),
					view.invalidRows.total > 0 && /* @__PURE__ */ _jsxs("div", {
						className: "notice resource-warning atlas-degraded",
						role: "alert",
						children: [/* @__PURE__ */ _jsx("b", { children: "Some portfolio records were ignored." }), /* @__PURE__ */ _jsxs("span", { children: [countLabel(view.invalidRows.total, "record"), "; totals may include them."] })]
					}),
					/* @__PURE__ */ _jsx(EvidenceSourceNotice, {
						concept: view.conceptSource,
						claims: view.claimSource
					}),
					/* @__PURE__ */ _jsx("section", {
						className: "atlas-summary",
						"aria-label": "Portfolio summary",
						children: [
							[
								"Referenced runs",
								view.totals.runs,
								atlasLoaded
							],
							[
								"Concepts",
								view.totals.concepts,
								atlasLoaded
							],
							[
								"Claims",
								view.totals.claims,
								claimsLoaded
							],
							[
								"Mixed evidence",
								view.totals.contested,
								atlasLoaded
							]
						].map(([label, value, loaded]) => /* @__PURE__ */ _jsxs("div", {
							className: "atlas-stat",
							children: [/* @__PURE__ */ _jsx("span", { children: label }), /* @__PURE__ */ _jsx("strong", { children: loaded ? value : "not loaded" })]
						}, label))
					}),
					view.empty && /* @__PURE__ */ _jsx(AtlasEmptyState, {
						sourceStates,
						conceptSource: view.conceptSource,
						claimSource: view.claimSource,
						pending: resource.pending,
						retry,
						busy,
						activeRetryKey: busy ? request.key : "",
						memorySettingsNeeded
					}),
					!view.empty && /* @__PURE__ */ _jsxs("div", {
						className: "atlas-grid",
						children: [
							/* @__PURE__ */ _jsxs("section", {
								className: "atlas-panel atlas-coverage",
								"aria-labelledby": "atlas-concepts",
								children: [
									/* @__PURE__ */ _jsxs("div", {
										className: "atlas-panel-head",
										children: [/* @__PURE__ */ _jsx("h2", {
											id: "atlas-concepts",
											children: "Concepts seen across runs"
										}), /* @__PURE__ */ _jsx("span", {
											className: "muted",
											children: atlasLoaded ? `showing ${view.concepts.length} of ${view.totals.concepts}` : "not loaded"
										})]
									}),
									atlasLoaded && /* @__PURE__ */ _jsxs(_Fragment, { children: [
										view.concepts.length === 0 ? /* @__PURE__ */ _jsx("p", {
											className: "atlas-section-empty",
											children: "No retained concepts returned."
										}) : /* @__PURE__ */ _jsx("ul", {
											className: "atlas-concepts",
											tabIndex: 0,
											"aria-label": "Bounded explored concepts",
											children: view.concepts.map((concept, index) => /* @__PURE__ */ _jsxs("li", { children: [/* @__PURE__ */ _jsxs("div", { children: [/* @__PURE__ */ _jsx("strong", { children: concept.concept }), /* @__PURE__ */ _jsxs("span", { children: [
												countLabel(concept.nRuns, "run"),
												" · ",
												"helped ",
												concept.nHelped,
												" · neutral ",
												concept.nNeutral,
												" · hurt ",
												concept.nHurt
											] })] }), concept.runs.length > 0 && /* @__PURE__ */ _jsx("div", {
												className: "atlas-runrefs",
												children: concept.runs.map((run, runIndex) => run.runId ? /* @__PURE__ */ _jsx(AtlasRunReference, { run }, `${run.runId}-${runIndex}`) : null)
											})] }, `${concept.concept}-${index}`))
										}),
										view.hiddenConcepts > 0 && /* @__PURE__ */ _jsxs("p", {
											className: "atlas-boundary-note",
											children: [countLabel(view.hiddenConcepts, "additional concept"), " not shown (bounded projection)."]
										}),
										/* @__PURE__ */ _jsxs("div", {
											className: "atlas-thin",
											children: [
												/* @__PURE__ */ _jsxs("h3", { children: ["Observed in one run ", /* @__PURE__ */ _jsx("span", { children: view.thin.length })] }),
												view.thin.length > 0 ? /* @__PURE__ */ _jsx("div", {
													className: "atlas-tags",
													children: view.thin.map((name, index) => /* @__PURE__ */ _jsx("span", {
														className: "pill",
														children: name
													}, `${name}-${index}`))
												}) : /* @__PURE__ */ _jsx("p", {
													className: "atlas-section-empty",
													children: "None returned."
												}),
												view.hiddenThin > 0 && /* @__PURE__ */ _jsxs("p", {
													className: "atlas-inline-boundary",
													children: [
														"+",
														view.hiddenThin,
														" more single-run concept",
														view.hiddenThin === 1 ? "" : "s"
													]
												})
											]
										})
									] }),
									/* @__PURE__ */ _jsx(SourceWatermark, {
										sourceKey: "atlas",
										label: "Concept projection",
										source: sourceStates.atlas,
										retry,
										busy,
										pending: resource.pending.includes("atlas"),
										activeRetry: busy && request.key === "atlas",
										children: "bounded observations, not coverage."
									})
								]
							}),
							/* @__PURE__ */ _jsxs("section", {
								className: "atlas-panel atlas-contradictions",
								"aria-labelledby": "atlas-mixed",
								children: [
									/* @__PURE__ */ _jsxs("div", {
										className: "atlas-panel-head",
										children: [/* @__PURE__ */ _jsx("h2", {
											id: "atlas-mixed",
											children: "Mixed-evidence claim records"
										}), /* @__PURE__ */ _jsx("span", {
											className: "chip xs warn",
											children: atlasLoaded ? `${view.totals.contested} mixed` : "not loaded"
										})]
									}),
									atlasLoaded && (view.contradictions.length > 0 ? /* @__PURE__ */ _jsx("div", {
										className: "atlas-claim-list compact",
										role: "region",
										tabIndex: 0,
										"aria-label": "Bounded mixed-evidence claim records",
										children: view.contradictions.map((claim, index) => /* @__PURE__ */ _jsx(ClaimCard, {
											claim,
											compact: true
										}, `${claim.uid || claim.statement}-${index}`))
									}) : /* @__PURE__ */ _jsx("p", {
										className: "atlas-section-empty",
										children: "None returned."
									})),
									atlasLoaded && view.hiddenContradictions > 0 && /* @__PURE__ */ _jsxs("p", {
										className: "atlas-boundary-note",
										children: [countLabel(view.hiddenContradictions, "additional mixed-evidence record"), " not shown (bounded projection)."]
									}),
									/* @__PURE__ */ _jsx(SourceWatermark, {
										sourceKey: "atlas",
										label: "Mixed claims",
										source: sourceStates.atlas,
										retry,
										busy,
										pending: resource.pending.includes("atlas"),
										retryable: false,
										children: "not a verdict or applicability decision."
									})
								]
							}),
							/* @__PURE__ */ _jsxs("section", {
								className: "atlas-panel atlas-all-claims",
								"aria-labelledby": "atlas-claims",
								children: [
									/* @__PURE__ */ _jsxs("div", {
										className: "atlas-panel-head",
										children: [/* @__PURE__ */ _jsx("h2", {
											id: "atlas-claims",
											children: "Claim records"
										}), /* @__PURE__ */ _jsx("span", {
											className: "muted",
											children: claimsLoaded ? `showing ${view.claims.length} of ${view.totals.claims}` : "not loaded"
										})]
									}),
									claimsLoaded && (view.claims.length > 0 ? /* @__PURE__ */ _jsx("div", {
										className: "atlas-claim-list",
										role: "region",
										tabIndex: 0,
										"aria-label": "Bounded portfolio claims",
										children: view.claims.map((claim, index) => /* @__PURE__ */ _jsx(ClaimCard, { claim }, `${claim.uid || claim.statement}-${index}`))
									}) : /* @__PURE__ */ _jsx("p", {
										className: "atlas-section-empty",
										children: "No claims returned."
									})),
									claimsLoaded && view.hiddenClaims > 0 && /* @__PURE__ */ _jsxs("p", {
										className: "atlas-boundary-note",
										children: [countLabel(view.hiddenClaims, "additional claim"), " not shown (render limit)."]
									}),
									/* @__PURE__ */ _jsx(SourceWatermark, {
										sourceKey: "claims",
										label: "Claim records",
										source: sourceStates.claims,
										retry,
										busy,
										pending: resource.pending.includes("claims"),
										activeRetry: busy && request.key === "claims",
										children: "maturity differs from evidence."
									})
								]
							}),
							/* @__PURE__ */ _jsxs("section", {
								className: "atlas-panel atlas-curation",
								"aria-labelledby": "atlas-curation",
								children: [
									/* @__PURE__ */ _jsxs("div", {
										className: "atlas-panel-head",
										children: [/* @__PURE__ */ _jsx("h2", {
											id: "atlas-curation",
											children: "Recent proposals + outcomes"
										}), /* @__PURE__ */ _jsx("span", {
											className: "muted",
											children: curationCurrent ? `showing ${view.curation.length} of ${view.totals.curation}` : "incomplete merge"
										})]
									}),
									view.curation.length > 0 ? /* @__PURE__ */ _jsx("ol", {
										className: "atlas-curation-list",
										children: view.curation.map((entry, index) => /* @__PURE__ */ _jsxs("li", { children: [
											/* @__PURE__ */ _jsxs("b", { children: [entry.kind, " steward"] }),
											" · ",
											countLabel(entry.proposals, "proposal"),
											" ·",
											" ",
											entry.applied ? `${entry.applied} applied` : entry.outcome.replaceAll("-", " ")
										] }, `${entry.kind}-${index}`))
									}) : /* @__PURE__ */ _jsx("p", {
										className: "atlas-section-empty",
										children: curationCurrent ? "No steward records returned." : "No records shown; merge incomplete."
									}),
									view.hiddenCuration > 0 && /* @__PURE__ */ _jsxs("p", {
										className: "atlas-boundary-note",
										children: [countLabel(view.hiddenCuration, "older entry", "older entries"), " not shown (render limit)."]
									}),
									[["conceptCuration", "Concept"], ["claimCuration", "Claim"]].map(([sourceKey, kind]) => /* @__PURE__ */ _jsx(SourceWatermark, {
										sourceKey,
										label: `${kind} steward log`,
										source: sourceStates[sourceKey],
										retry,
										busy,
										pending: resource.pending.includes(sourceKey),
										activeRetry: busy && request.key === sourceKey,
										children: "history, not current governance."
									}, sourceKey))
								]
							})
						]
					})
				]
			})
		})]
	});
}
