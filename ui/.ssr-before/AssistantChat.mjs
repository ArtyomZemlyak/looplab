import React, { useEffect, useId, useRef } from "react";
import Markdown from "./markdown.mjs";
import { fmt } from "./util.mjs";
import { assistantErrorInfo } from "./assistantErrors.mjs";
import { permissionPresentation } from "./assistantPermission.mjs";
import LaunchCard from "./LaunchCard.mjs";
import { launchDraftKey } from "./launchDraftStore.mjs";
import { jsxs as _jsxs, jsx as _jsx, Fragment as _Fragment } from "react/jsx-runtime";
const RUN_MENTION_MAX = 32;
const runMentions = (value) => {
	const text = String(value || "");
	const re = /@run:([^\s.,;:!?)\]]{1,255})(?=$|[\s.,;:!?)\]])/g;
	const seen = new Set();
	const mentions = [];
	// Stop as soon as the chip budget is full. Besides bounding DOM, this avoids scanning the rest of
	// a provider-controlled answer after it can no longer affect the UI.
	let match;
	while (mentions.length < RUN_MENTION_MAX && (match = re.exec(text)) !== null) {
		if (seen.has(match[1])) continue;
		seen.add(match[1]);
		mentions.push(match[1]);
	}
	return mentions;
};
// A live inline card for a run referenced with @run:<id> — so a running run shows up right in the chat
// (a direct ask). Links to the run view; the dot pulses while its engine is live.
function RunChip({ id, run, interactive = true, onOpen }) {
	const phase = run ? run.phase || (run.finished ? "finished" : "running") : "—";
	const href = `#/run/${encodeURIComponent(id)}`;
	const content = /* @__PURE__ */ _jsxs(_Fragment, { children: [
		/* @__PURE__ */ _jsx("span", { className: "asst-run-dot" + (run && run.engine_running ? " live" : "") }),
		/* @__PURE__ */ _jsx("b", { children: id }),
		run && /* @__PURE__ */ _jsxs("span", {
			className: "muted",
			children: [
				" ",
				phase,
				run.best_metric != null ? " · " + fmt(run.best_metric) : ""
			]
		})
	] });
	return interactive ? /* @__PURE__ */ _jsx("a", {
		className: "asst-runchip",
		href,
		onClick: onOpen ? (event) => onOpen(event, href) : undefined,
		title: run ? run.goal || id : id,
		children: content
	}) : /* @__PURE__ */ _jsx("span", {
		className: "asst-runchip inert",
		title: "Run references are not links in a public transcript",
		children: content
	});
}
// One assistant/user turn in the feed. Tool steps (what the agent read) render as a compact sub-line;
// any @run:<id> mention gets a live inline run card.
// Strip the invisible "[UI context: …]" preamble the bar appends to a user turn for the model, so it
// never shows in the bubble — including messages persisted before the server started storing the clean
// display text. The injected context is still surfaced separately as the faint caption plaque.
const stripCtx = (s) => typeof s === "string" ? s.replace(/\n*\[UI context:[^\]]*\]\s*$/, "").trimEnd() : s;
const exactRecoveryAvailable = (action) => !!action && typeof action.abs_path === "string" && action.abs_path.length > 0 && typeof action.recovery_id === "string" && action.recovery_id.length > 0 && typeof action.recovery_postimage_exists === "boolean" && (action.recovery_postimage_exists ? typeof action.recovery_postimage_digest === "string" && /^[0-9a-f]{64}$/.test(action.recovery_postimage_digest) && Number.isInteger(action.recovery_postimage_mode) && action.recovery_postimage_mode >= 0 && action.recovery_postimage_mode <= 4095 : action.recovery_postimage_digest == null && action.recovery_postimage_mode == null);
function AssistantErrorCard({ error, onRetry, retryLabel = "Retry", retryBusy = false, onOpenSettings }) {
	return /* @__PURE__ */ _jsxs("div", {
		className: `assistant-error-card ${error.kind}`,
		role: "alert",
		children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "assistant-error-card__head",
				children: [/* @__PURE__ */ _jsx("span", {
					className: "assistant-error-card__icon",
					"aria-hidden": "true",
					children: "!"
				}), /* @__PURE__ */ _jsxs("div", { children: [/* @__PURE__ */ _jsx("strong", { children: error.title }), /* @__PURE__ */ _jsx("p", { children: error.message })] })]
			}),
			error.technical && /* @__PURE__ */ _jsx("code", {
				className: "assistant-error-card__technical",
				children: error.technical
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "assistant-error-card__actions",
				children: [error.retryable && onRetry && /* @__PURE__ */ _jsx("button", {
					className: "btn xs primary",
					"aria-disabled": retryBusy || undefined,
					onClick: (event) => {
						if (!retryBusy) onRetry(event);
					},
					children: retryLabel
				}), onOpenSettings && /* @__PURE__ */ _jsx("button", {
					className: "btn xs ghost",
					onClick: onOpenSettings,
					children: "Open Settings"
				})]
			})
		]
	});
}
export function Turn({ m, runsById, onRevert, onRetry, retryLabel, retryBusy, onOpenSettings, onRunOpen, launchChat, readOnly = false, audience = "owner", launchSessionId, launchMessageId, launchMessageIndex, launchDrafts, launchDisclosures, onLaunchDraft, onLaunchDisclosure, onLaunchStarted, revertState }) {
	const publicAudience = audience === "public";
	const who = m.role === "user" ? publicAudience ? "chat owner" : "you" : "assistant";
	const content = m.role === "user" ? stripCtx(m.content) : m.content;
	// Provider payloads can contain URLs, model routing, account ids and other implementation details.
	// Classify the raw persisted text, but render only the fixed, allow-listed copy returned here.
	const assistantError = m.role === "assistant" && !m.streaming ? assistantErrorInfo(content, m.error_kind) : null;
	const mentions = assistantError ? [] : runMentions(content);
	const hasLaunch = !readOnly && Array.isArray(m.proposals) && m.proposals.length > 0;
	return /* @__PURE__ */ _jsx("div", {
		className: "feed-msg chat " + m.role + (hasLaunch ? " has-launch" : "") + (publicAudience ? " audience-public" : ""),
		children: /* @__PURE__ */ _jsxs("div", {
			className: "fm-body",
			children: [
				/* @__PURE__ */ _jsx("div", {
					className: "chat-who",
					children: who
				}),
				m.role === "assistant" && Array.isArray(m.activity) && m.activity.length > 0 && /* @__PURE__ */ _jsx("div", {
					className: "asst-activity",
					children: m.activity.map((seg, i) => seg.type === "text" ? /* @__PURE__ */ _jsx(Markdown, {
						text: seg.content,
						className: "asst-inter",
						externalOnly: publicAudience
					}, i) : /* @__PURE__ */ _jsxs("div", {
						className: "asst-status",
						children: [/* @__PURE__ */ _jsx("span", {
							className: "asst-status-ic",
							children: "⚙"
						}), seg.labels.length > 3 ? /* @__PURE__ */ _jsxs("span", { children: [
							" ",
							seg.labels.length,
							" steps · ",
							seg.labels[seg.labels.length - 1]
						] }) : /* @__PURE__ */ _jsxs("span", { children: [" ", seg.labels.join(" · ")] })]
					}, i))
				}),
				m.role === "assistant" && !(m.activity && m.activity.length) && Array.isArray(m.steps) && m.steps.length > 0 && /* @__PURE__ */ _jsx("div", {
					className: "asst-steps",
					children: m.steps.map((s, i) => /* @__PURE__ */ _jsx("span", {
						className: "asst-step",
						children: s.label || s.tool
					}, i))
				}),
				m.role === "assistant" && m.streaming && !m.content && !(m.activity && m.activity.length) && /* @__PURE__ */ _jsxs("div", {
					className: "asst-status thinking",
					children: [/* @__PURE__ */ _jsx("span", {
						className: "asst-status-ic",
						children: "…"
					}), /* @__PURE__ */ _jsx("span", { children: " thinking" })]
				}),
				m.role === "assistant" && Array.isArray(m.applied) && m.applied.length > 0 && /* @__PURE__ */ _jsx("div", {
					className: "asst-steps",
					children: m.applied.map((a, i) => {
						const recovery = revertState?.(a) || {};
						const canRevert = onRevert && exactRecoveryAvailable(a);
						return /* @__PURE__ */ _jsxs("span", {
							className: "asst-step done",
							children: [
								"✓ ",
								a.label || a.tool,
								canRevert && /* @__PURE__ */ _jsx("button", {
									className: "asst-undo",
									title: recovery.done ? "this exact file change was reverted" : recovery.busy ? "reverting this exact file change" : "undo this exact file change",
									"aria-label": `${recovery.done ? "Reverted" : recovery.busy ? "Reverting" : "Undo"} ${a.label || a.tool || "file change"}`,
									disabled: recovery.busy || recovery.done,
									onClick: () => onRevert(a),
									children: recovery.done ? "reverted" : recovery.busy ? "reverting…" : "undo"
								})
							]
						}, i);
					})
				}),
				m.role === "assistant" && /* @__PURE__ */ _jsx(Todos, { items: m.todos }),
				(m.content || !m.streaming) && /* @__PURE__ */ _jsx("div", {
					className: "chat-bubble" + (assistantError ? " assistant-error-bubble" : "") + (m.recoveryBlocked ? " assistant-recovery-blocked" : ""),
					role: m.recoveryBlocked ? "alert" : undefined,
					children: m.role === "assistant" ? assistantError ? /* @__PURE__ */ _jsx(AssistantErrorCard, {
						error: assistantError,
						onRetry,
						retryLabel,
						retryBusy,
						onOpenSettings
					}) : /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx(Markdown, {
						text: m.content || "",
						className: "chat-text",
						externalOnly: publicAudience
					}), m.streaming && /* @__PURE__ */ _jsx("span", {
						className: "asst-cursor",
						children: "▍"
					})] }) : /* @__PURE__ */ _jsx("div", {
						className: "chat-text",
						children: content
					})
				}),
				mentions.length > 0 && /* @__PURE__ */ _jsx("div", {
					className: "asst-runchips",
					children: mentions.map((id) => /* @__PURE__ */ _jsx(RunChip, {
						id,
						run: runsById && runsById[id],
						interactive: !publicAudience,
						onOpen: onRunOpen
					}, id))
				}),
				!readOnly && Array.isArray(m.proposals) && m.proposals.map((sp, i) => {
					const draftKey = launchDraftKey({
						sessionId: launchSessionId,
						messageId: launchMessageId,
						messageIndex: launchMessageIndex,
						proposalId: sp.proposal_id,
						proposalIndex: i
					});
					return /* @__PURE__ */ _jsx(LaunchCard, {
						spec: sp,
						chat: launchChat,
						launchIdentity: draftKey,
						retainedDraft: launchDrafts?.[draftKey],
						retainedConfigOpen: launchDisclosures?.[draftKey] === true,
						onOpenSettings,
						onDraftChange: (draft) => onLaunchDraft?.(draftKey, draft),
						onConfigOpenChange: (open) => onLaunchDisclosure?.(draftKey, open),
						onStarted: () => onLaunchStarted?.(draftKey)
					}, draftKey);
				})
			]
		})
	});
}
// The assistant's live TODO list for a multi-step task.
function Todos({ items }) {
	if (!items || !items.length) return null;
	const mark = (s) => s === "completed" ? "✓" : s === "in_progress" ? "▸" : "○";
	return /* @__PURE__ */ _jsx("div", {
		className: "asst-todos",
		children: items.map((t, i) => /* @__PURE__ */ _jsxs("div", {
			className: "asst-todo " + (t.status || "pending"),
			children: [/* @__PURE__ */ _jsx("span", {
				className: "asst-todo-box",
				children: mark(t.status)
			}), /* @__PURE__ */ _jsx("span", { children: t.content })]
		}, i))
	});
}
// A human-in-the-loop confirm card: the turn paused to ask before a mutating action. Approve/Reject
// resolves it server-side and the turn resumes.
export function PermCard({ req, onResolve, busy = false, autoFocus = false, suppressAutoFocus = false, focusRequest = 0, onFocused = null, onFocusFailed = null }) {
	const titleId = useId();
	const detailsId = useId();
	const cardRef = useRef(null);
	const rejectRef = useRef(null);
	const skipSuppressionReleaseRef = useRef(false);
	const permission = permissionPresentation(req);
	const a = permission.action;
	const isDiff = a.tool === "write_file" || a.tool === "edit_file" || a.tool === "apply_patch";
	const requiresCaution = permission.risk === "HIGH" || permission.risk === "UNKNOWN";
	useEffect(() => {
		if (focusRequest) {
			skipSuppressionReleaseRef.current = true;
			let cancelled = false;
			let retryFrame = null;
			const focusExact = (remaining) => {
				if (cancelled) return;
				const card = cardRef.current;
				const target = busy ? card : rejectRef.current;
				target?.scrollIntoView?.({
					block: "nearest",
					inline: "nearest"
				});
				const rect = target?.getBoundingClientRect();
				const feedRect = card?.closest("[role=\"log\"]")?.getBoundingClientRect();
				const visible = !!rect && !!feedRect && rect.bottom > feedRect.top && rect.top < feedRect.bottom && rect.right > feedRect.left && rect.left < feedRect.right;
				if (visible) target?.focus({ preventScroll: true });
				if (visible && document.activeElement === target) {
					onFocused?.(req.id, focusRequest);
					return;
				}
				if (remaining > 0) {
					retryFrame = requestAnimationFrame(() => focusExact(remaining - 1));
					return;
				}
				onFocusFailed?.(req.id, focusRequest);
			};
			focusExact(2);
			return () => {
				cancelled = true;
				if (retryFrame != null) cancelAnimationFrame(retryFrame);
			};
		}
		if (suppressAutoFocus) {
			// Existing cards must not steal focus when a failed exact handoff releases its suppression.
			// Skip only that release render; later busy -> ready and newly-last transitions stay usable.
			skipSuppressionReleaseRef.current = true;
			return;
		}
		if (skipSuppressionReleaseRef.current) {
			skipSuppressionReleaseRef.current = false;
			return;
		}
		if (!autoFocus || busy) return;
		rejectRef.current?.focus({ preventScroll: true });
	}, [
		autoFocus,
		busy,
		focusRequest,
		onFocusFailed,
		onFocused,
		req.id,
		suppressAutoFocus
	]);
	return /* @__PURE__ */ _jsxs("div", {
		ref: cardRef,
		className: "asst-perm risk-" + permission.risk.toLowerCase(),
		"data-assistant-permission-id": req.id,
		tabIndex: -1,
		role: "alertdialog",
		"aria-modal": "false",
		"aria-labelledby": titleId,
		"aria-describedby": detailsId,
		"aria-busy": busy ? "true" : "false",
		children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "asst-perm-h",
				id: titleId,
				children: [
					/* @__PURE__ */ _jsx("span", {
						className: "asst-perm-badge",
						children: "approval required"
					}),
					/* @__PURE__ */ _jsx("b", { children: a.label || a.tool || "Unknown mutation" }),
					/* @__PURE__ */ _jsxs("span", {
						className: "asst-perm-risk " + permission.risk.toLowerCase(),
						children: [permission.risk, " risk"]
					})
				]
			}),
			/* @__PURE__ */ _jsxs("dl", {
				className: "asst-perm-details",
				id: detailsId,
				children: [
					/* @__PURE__ */ _jsxs("div", { children: [/* @__PURE__ */ _jsx("dt", { children: "Scope" }), /* @__PURE__ */ _jsxs("dd", { children: [permission.scope, permission.scopeDigest ? /* @__PURE__ */ _jsxs("span", {
						className: "asst-perm-digest",
						title: permission.scopeDigest,
						children: [
							" · ID ",
							permission.scopeDigest.slice(0, 12),
							"…"
						]
					}) : null] })] }),
					/* @__PURE__ */ _jsxs("div", { children: [/* @__PURE__ */ _jsx("dt", { children: "Consequence" }), /* @__PURE__ */ _jsx("dd", { children: permission.consequence })] }),
					/* @__PURE__ */ _jsxs("div", { children: [/* @__PURE__ */ _jsx("dt", { children: "Mode" }), /* @__PURE__ */ _jsx("dd", { children: permission.modeLabel })] }),
					/* @__PURE__ */ _jsxs("div", { children: [/* @__PURE__ */ _jsx("dt", { children: "Expiry" }), /* @__PURE__ */ _jsx("dd", { children: permission.expiresIso ? /* @__PURE__ */ _jsx("time", {
						dateTime: permission.expiresIso,
						children: permission.expiryLabel
					}) : permission.expiryLabel })] }),
					permission.canAlways && /* @__PURE__ */ _jsxs("div", { children: [/* @__PURE__ */ _jsx("dt", { children: "Remembered grant" }), /* @__PURE__ */ _jsxs("dd", { children: [
						"This exact action and scope · current turn · ",
						permission.modeLabel,
						" mode · ",
						permission.grantDurationLabel
					] })] })
				]
			}),
			a.preview && /* @__PURE__ */ _jsx("pre", {
				className: "asst-perm-pre" + (isDiff ? " diff" : ""),
				"aria-label": "Proposed action preview",
				children: a.preview
			}),
			permission.expired && /* @__PURE__ */ _jsx("div", {
				className: "asst-perm-state",
				role: "status",
				children: "This approval has expired; waiting for the server to close it."
			}),
			requiresCaution && /* @__PURE__ */ _jsxs("div", {
				className: "asst-perm-state warning",
				children: [
					"Persistent approval is unavailable for ",
					permission.risk === "HIGH" ? "high-risk actions" : "actions without a verified risk classification",
					"."
				]
			}),
			busy && /* @__PURE__ */ _jsx("div", {
				className: "asst-perm-state",
				role: "status",
				children: "Submitting your decision…"
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "asst-perm-actions",
				children: [
					/* @__PURE__ */ _jsx("button", {
						ref: rejectRef,
						"data-permission-reject": true,
						className: "btn xs " + (requiresCaution ? "primary" : "ghost"),
						disabled: busy,
						onClick: () => onResolve(req.id, "deny"),
						children: "Reject"
					}),
					permission.canAlways && /* @__PURE__ */ _jsxs("button", {
						className: "btn xs",
						disabled: busy || permission.expired,
						onClick: () => onResolve(req.id, "allow_always"),
						title: `Remember only this exact action and security scope for the current turn and ${permission.modeLabel} mode (${permission.grantDurationLabel})`,
						children: ["Allow exact scope · ", permission.grantDurationLabel]
					}),
					/* @__PURE__ */ _jsx("button", {
						className: "btn xs " + (requiresCaution ? "danger" : "primary"),
						disabled: busy || permission.expired,
						onClick: () => onResolve(req.id, "allow_once"),
						children: "Approve once"
					})
				]
			})
		]
	});
}
export { LaunchCard };
