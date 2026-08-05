import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { apiPrefix, createRunReview, listRunReviews, revokeRunReview } from "./util.mjs";
import { encodeRunRouteState, reviewRouteStateForScope } from "./runRouteState.mjs";
import { assertReviewLinkCrypto, beginReviewCreateIntent, clearReviewCreateIntent, discardInvalidReviewCreateIntent, deriveReviewLinkId, readReviewCreateIntent, reviewCreateBody, reviewRecoveryGenerationValid, reviewRecoveryLinkId, reviewRecoveryScope, reviewRecoveryTerminalStatus, reviewUrlForIntent, transitionReviewCreateIntent, validateReviewCreateReceipt, validateReviewReplayTerminal, validateStoredReviewCreateIntent } from "./reviewLinkRecovery.mjs";
import { OpIcon } from "./icons.mjs";
import CommentsThread from "./CommentsThread.mjs";
import PanelShell from "./PanelShell.mjs";
import { DEFAULT_REQUEST_TIMEOUT_MS, deadlineRequest } from "./requestDeadline.mjs";
import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
// Named locally because these three call sites want the HANDLE (they cancel a stale list/create
// on a newer one), not just the promise — but the bound itself is the shared default, not a
// second opinion about how long a component read may take (doc 25 UI-12).
const REVIEW_LINK_TIMEOUT_MS = DEFAULT_REQUEST_TIMEOUT_MS;
const boundedLinkRequest = (read) => deadlineRequest(read, REVIEW_LINK_TIMEOUT_MS);
const activeLink = (link) => link?.status === "active" || link?.status === "stale";
const terminalCopy = (status) => `The saved review link is ${status} and can no longer be shared. Create a new link.`;
const createFailureCopy = (error) => error?.code === "review_generation_changed" ? "The run changed before the link was created. Refresh the run and create a new link." : error?.status === 409 && /LOOPLAB_UI_TOKEN/i.test(error?.message || "") ? "Owner authentication is required for sharing. The exact request remains saved in this tab." : error?.status === 400 || error?.status === 422 ? "The server rejected the recovery request. Its exact identity remains saved because an earlier attempt may have succeeded." : error?.status === 401 || error?.status === 403 ? "Owner access was rejected. Restore access, then recover this exact saved link." : error?.status === 404 ? "Review-link recovery is unavailable on this server. The exact request remains saved." : "Link creation was not confirmed. Recovering replays this exact link and cannot create a duplicate.";
const provenNoWriteFailure = (error) => error?.code === "review_generation_changed";
const dateLabel = (epoch) => {
	const value = Number(epoch);
	if (!Number.isFinite(value) || value <= 0) return "at an unknown time";
	try {
		const date = new Date(value * 1e3);
		return Number.isFinite(date.getTime()) ? date.toLocaleString() : "at an unknown time";
	} catch {
		return "at an unknown time";
	}
};
const emptyRecovery = (key) => ({
	key,
	loaded: false,
	intent: null,
	invalid: null,
	busy: false,
	error: "",
	message: ""
});
/**
* Comments and review-link management intentionally live outside the owner panel hub. A read-only
* review may open this panel, but must never download charts, settings, raw events, or owner tools.
*/
export default function CollabPanel({ runId, onSelect, onOpenComment, onClose, onToast, reviewRouteState = null, reviewMode = false, expectedGeneration = null, refreshKey = null, PanelComponent = PanelShell, draftStore = null }) {
	const [ttl, setTtl] = useState(7 * 24 * 60 * 60);
	const [includeEvidence, setIncludeEvidence] = useState(false);
	const [linksResource, setLinksResource] = useState({
		key: "",
		links: [],
		status: "loading",
		error: ""
	});
	const [recoveryResource, setRecoveryResource] = useState(() => emptyRecovery(""));
	const [revokeResource, setRevokeResource] = useState({
		key: "",
		ids: new Set(),
		error: ""
	});
	const [actionFocusRequest, setActionFocusRequest] = useState({
		key: "",
		target: "",
		targetId: "",
		sequence: 0
	});
	const listRequestRef = useRef(null);
	const createRequestRef = useRef(null);
	const revokeRequestsRef = useRef(new Map());
	const createdInputRef = useRef(null);
	const createdSurfaceRef = useRef(null);
	const recoveryStatusRef = useRef(null);
	const createButtonRef = useRef(null);
	const linksSectionRef = useRef(null);
	const linksStatusRef = useRef(null);
	const linksRetryRef = useRef(null);
	const linkRowRefs = useRef(new Map());
	const linkActionRefs = useRef(new Map());
	const prefix = apiPrefix();
	const origin = typeof location === "undefined" ? "" : location.origin;
	const recoveryScope = reviewRecoveryScope(origin, prefix);
	const viewKey = `${reviewMode ? "review" : "owner"}\u0000${recoveryScope}\u0000${String(runId)}\u0000${String(expectedGeneration || "")}`;
	const renderRef = useRef({
		key: viewKey,
		runId: String(runId)
	});
	renderRef.current = {
		key: viewKey,
		runId: String(runId)
	};
	const requestActionFocus = useCallback((key, target, targetId = "") => {
		setActionFocusRequest((previous) => ({
			key,
			target,
			targetId,
			sequence: previous.sequence + 1
		}));
	}, []);
	const actionFocusOwned = () => {
		if (typeof document === "undefined") return false;
		const active = document.activeElement;
		if (!active || active === document.body || !active.isConnected) return true;
		return [
			createButtonRef,
			recoveryStatusRef,
			createdSurfaceRef
		].some((ref) => ref.current && (ref.current === active || ref.current.contains(active)));
	};
	const idleActionFocusTarget = () => reviewRecoveryGenerationValid(expectedGeneration) ? "create" : "recovery";
	const elementFocusOwned = (element) => {
		if (typeof document === "undefined") return false;
		const active = document.activeElement;
		if (!active || active === document.body || !active.isConnected) return true;
		return !!element && (element === active || element.contains(active));
	};
	const linkFocusOwned = (linkId) => elementFocusOwned(linkRowRefs.current.get(linkId));
	const linksFocusOwned = () => [
		linksSectionRef,
		linksStatusRef,
		linksRetryRef
	].some((ref) => elementFocusOwned(ref.current));
	useLayoutEffect(() => {
		if (!actionFocusRequest.target || actionFocusRequest.key !== viewKey) return;
		const target = actionFocusRequest.target === "created" ? createdInputRef.current : actionFocusRequest.target === "recovery" ? recoveryStatusRef.current : actionFocusRequest.target === "link-row" ? linkRowRefs.current.get(actionFocusRequest.targetId) : actionFocusRequest.target === "link-action" ? linkActionRefs.current.get(actionFocusRequest.targetId) : actionFocusRequest.target === "links-status" ? linksStatusRef.current : actionFocusRequest.target === "links-retry" ? linksRetryRef.current : actionFocusRequest.target === "links-section" ? linksSectionRef.current : createButtonRef.current;
		if (!target) return;
		target.focus({ preventScroll: true });
		if (actionFocusRequest.target === "created") target.select();
	}, [actionFocusRequest, viewKey]);
	const recovery = recoveryResource.key === viewKey ? recoveryResource : emptyRecovery(viewKey);
	const linksView = linksResource.key === viewKey ? linksResource : {
		key: viewKey,
		links: [],
		status: "loading",
		error: ""
	};
	const revokeView = revokeResource.key === viewKey ? revokeResource : {
		key: viewKey,
		ids: new Set(),
		error: ""
	};
	const updateRecovery = useCallback((key, update) => {
		setRecoveryResource((previous) => previous.key === key ? update(previous) : previous);
	}, []);
	const refreshLinks = useCallback(async ({ preserveFocus = false, preserveLinkId = "" } = {}) => {
		if (reviewMode) return null;
		const key = viewKey;
		const previousRequest = listRequestRef.current;
		const inheritFocus = previousRequest?.key === key && previousRequest.preserveFocus;
		const focusLinkId = preserveLinkId || (inheritFocus ? previousRequest.preserveLinkId : "");
		const refreshFocusOwned = () => linksFocusOwned() || focusLinkId && linkFocusOwned(focusLinkId);
		const restoreFocus = (preserveFocus || !!preserveLinkId || inheritFocus) && refreshFocusOwned();
		previousRequest?.timed.controller.abort();
		const timed = boundedLinkRequest((signal) => listRunReviews(runId, { signal }));
		const operation = {
			key,
			timed,
			preserveFocus: restoreFocus,
			preserveLinkId: focusLinkId
		};
		listRequestRef.current = operation;
		setLinksResource((previous) => {
			const links = previous.key === key ? previous.links : [];
			return {
				key,
				links,
				status: links.length ? "refreshing" : "loading",
				error: ""
			};
		});
		if (restoreFocus) requestActionFocus(key, "links-status");
		try {
			const result = await timed.promise;
			if (listRequestRef.current !== operation || renderRef.current.key !== key) return null;
			if (!Array.isArray(result?.links)) throw new Error("invalid review-link list");
			const followFocus = restoreFocus && refreshFocusOwned();
			setLinksResource({
				key,
				links: result.links,
				status: "ready",
				error: ""
			});
			if (followFocus) requestActionFocus(key, "links-section");
			return result.links;
		} catch (caught) {
			if (listRequestRef.current !== operation || renderRef.current.key !== key) return null;
			const followFocus = restoreFocus && refreshFocusOwned();
			setLinksResource((previous) => {
				const links = previous.key === key ? previous.links : [];
				return {
					key,
					links,
					status: links.length ? "stale" : "error",
					error: links.length ? "Showing the last loaded links. Refresh failed." : "Review links are unavailable."
				};
			});
			if (followFocus) requestActionFocus(key, "links-retry");
			return null;
		} finally {
			if (listRequestRef.current === operation) listRequestRef.current = null;
		}
	}, [
		reviewMode,
		requestActionFocus,
		runId,
		viewKey
	]);
	useEffect(() => {
		let active = true;
		createRequestRef.current?.timed.controller.abort();
		createRequestRef.current = null;
		for (const operation of revokeRequestsRef.current.values()) operation.timed.controller.abort();
		revokeRequestsRef.current.clear();
		setRevokeResource({
			key: viewKey,
			ids: new Set(),
			error: ""
		});
		if (reviewMode) {
			setRecoveryResource({
				...emptyRecovery(viewKey),
				loaded: true
			});
			return () => {
				active = false;
			};
		}
		const restored = readReviewCreateIntent(recoveryScope, runId);
		if (restored?.invalid) {
			setRecoveryResource({
				...emptyRecovery(viewKey),
				loaded: true,
				invalid: restored.code,
				error: restored.code === "REVIEW_RECOVERY_STORAGE_UNAVAILABLE" ? "Session recovery storage is unavailable. No review-link request will be sent." : "Saved review-link recovery data is unreadable. Creating another link is blocked until it is deliberately discarded."
			});
		} else {
			if (restored) {
				setTtl(restored.ttlSeconds);
				setIncludeEvidence(restored.includeEvidence);
			}
			if (restored && [
				"confirmed",
				"revoking",
				"conflict"
			].includes(restored.phase)) {
				setRecoveryResource({
					...emptyRecovery(viewKey),
					intent: restored
				});
				void validateStoredReviewCreateIntent(restored).then((validated) => {
					if (active && renderRef.current.key === viewKey) {
						setRecoveryResource({
							...emptyRecovery(viewKey),
							loaded: true,
							intent: validated
						});
					}
				}).catch((caught) => {
					if (!active || renderRef.current.key !== viewKey) return;
					const code = caught?.code === "REVIEW_RECOVERY_CRYPTO_UNAVAILABLE" ? caught.code : "REVIEW_RECOVERY_INVALID";
					setRecoveryResource({
						...emptyRecovery(viewKey),
						loaded: true,
						invalid: code,
						error: caught?.message || "Saved review-link recovery could not be verified."
					});
				});
			} else setRecoveryResource({
				...emptyRecovery(viewKey),
				loaded: true,
				intent: restored
			});
		}
		return () => {
			active = false;
		};
	}, [
		recoveryScope,
		reviewMode,
		runId,
		viewKey
	]);
	useEffect(() => {
		if (reviewMode) {
			setLinksResource({
				key: viewKey,
				links: [],
				status: "ready",
				error: ""
			});
			return undefined;
		}
		void refreshLinks();
		return () => {
			const current = listRequestRef.current;
			if (current?.key === viewKey) current.timed.controller.abort();
		};
	}, [
		refreshLinks,
		reviewMode,
		viewKey
	]);
	useEffect(() => () => {
		renderRef.current = {
			key: "__unmounted__",
			runId: ""
		};
		listRequestRef.current?.timed.controller.abort();
		createRequestRef.current?.timed.controller.abort();
		for (const operation of revokeRequestsRef.current.values()) operation.timed.controller.abort();
	}, []);
	const copy = async (url, key = viewKey) => {
		try {
			if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
			await navigator.clipboard.writeText(url);
			if (renderRef.current.key === key) onToast?.("review link copied");
			return true;
		} catch {
			if (renderRef.current.key === key) {
				if (actionFocusOwned()) requestActionFocus(key, "created");
				onToast?.("Copy the visible link manually");
			}
			return false;
		}
	};
	const finishIntent = (intent, key, message) => {
		const restoreFocus = actionFocusOwned();
		if (!clearReviewCreateIntent(intent)) {
			if (renderRef.current.key === key) updateRecovery(key, (previous) => ({
				...previous,
				busy: false,
				error: "The link reached a terminal state, but its saved recovery could not be cleared."
			}));
			if (renderRef.current.key === key && restoreFocus) {
				requestActionFocus(key, intent.phase === "confirmed" ? "created" : "recovery");
			}
			return false;
		}
		if (renderRef.current.key === key) setRecoveryResource({
			...emptyRecovery(key),
			loaded: true,
			message
		});
		if (renderRef.current.key === key && restoreFocus) {
			requestActionFocus(key, idleActionFocusTarget());
		}
		return true;
	};
	const submitIntent = async (intent, { copyOnSuccess = false } = {}) => {
		if (!intent || !["pending", "confirmed"].includes(intent.phase)) return;
		const key = viewKey;
		try {
			assertReviewLinkCrypto();
		} catch (caught) {
			updateRecovery(key, (previous) => ({
				...previous,
				error: caught.message
			}));
			return;
		}
		const current = createRequestRef.current;
		if (current?.intent.requestId === intent.requestId) return;
		current?.timed.controller.abort();
		const timed = boundedLinkRequest((signal) => createRunReview(intent.runId, reviewCreateBody(intent), { signal }));
		const operation = {
			key,
			intent,
			timed
		};
		createRequestRef.current = operation;
		updateRecovery(key, (previous) => ({
			...previous,
			intent,
			busy: true,
			error: "",
			message: ""
		}));
		requestActionFocus(key, "recovery");
		try {
			const raw = await timed.promise;
			const receipt = await validateReviewCreateReceipt(raw, intent);
			if (reviewRecoveryTerminalStatus(receipt.status)) {
				finishIntent(intent, key, terminalCopy(receipt.status));
				if (renderRef.current.key === key) void refreshLinks();
				return;
			}
			const confirmed = transitionReviewCreateIntent(intent, {
				phase: "confirmed",
				linkId: receipt.id,
				token: receipt.token,
				expiresAt: receipt.expiresAt
			});
			const url = reviewUrlForIntent(confirmed, {
				origin,
				prefix
			});
			if (!url) throw Object.assign(new Error("Review URL could not be reconstructed."), { code: "REVIEW_RECOVERY_PROTOCOL_ERROR" });
			if (renderRef.current.key === key) {
				const restoreFocus = actionFocusOwned();
				setRecoveryResource({
					...emptyRecovery(key),
					loaded: true,
					intent: confirmed,
					message: receipt.replayed ? "The same review link was recovered." : "Review link created."
				});
				if (restoreFocus) requestActionFocus(key, "created");
				if (copyOnSuccess) await copy(url, key);
				void refreshLinks();
			}
		} catch (caught) {
			if (createRequestRef.current !== operation || renderRef.current.key !== key) return;
			if (caught?.status === 410 && caught?.code === "review_replay_terminal") {
				try {
					const terminal = await validateReviewReplayTerminal(caught, intent);
					finishIntent(intent, key, terminalCopy(terminal.kind));
					void refreshLinks();
					return;
				} catch {}
			}
			if (caught?.code === "review_idempotency_conflict") {
				const linkId = reviewRecoveryLinkId(caught?.detail?.existing_link_id);
				let expectedLinkId = null;
				try {
					expectedLinkId = await deriveReviewLinkId(intent.runId, intent.requestId);
				} catch {}
				if (linkId && linkId === expectedLinkId) {
					try {
						const restoreFocus = actionFocusOwned();
						const conflicted = transitionReviewCreateIntent(intent, {
							phase: "conflict",
							linkId,
							token: null,
							expiresAt: null
						});
						setRecoveryResource({
							...emptyRecovery(key),
							loaded: true,
							intent: conflicted,
							error: "A different saved request already owns this recovery identity. Revoke that link before creating another."
						});
						if (restoreFocus) requestActionFocus(key, "recovery");
					} catch (storageError) {
						updateRecovery(key, (previous) => ({
							...previous,
							busy: false,
							error: storageError.message
						}));
					}
					void refreshLinks();
					return;
				}
			}
			if (provenNoWriteFailure(caught)) {
				const message = createFailureCopy(caught);
				if (!finishIntent(intent, key, message)) return;
				void refreshLinks();
				return;
			}
			const restoreFocus = actionFocusOwned();
			updateRecovery(key, (previous) => ({
				...previous,
				intent,
				busy: false,
				error: createFailureCopy(caught)
			}));
			if (restoreFocus) requestActionFocus(key, "recovery");
		} finally {
			if (createRequestRef.current === operation) {
				createRequestRef.current = null;
				if (renderRef.current.key === key) updateRecovery(key, (previous) => ({
					...previous,
					busy: false
				}));
			}
		}
	};
	const create = () => {
		if (!recovery.loaded || recovery.busy || recovery.intent || recovery.invalid) return;
		if (!reviewRecoveryGenerationValid(expectedGeneration)) {
			updateRecovery(viewKey, (previous) => ({
				...previous,
				error: "Wait for the current run version before creating a review link."
			}));
			return;
		}
		const scopedState = reviewRouteStateForScope({
			...reviewRouteState || {},
			generation: expectedGeneration
		}, { evidence: includeEvidence });
		const routeQuery = encodeRunRouteState(scopedState, {
			reviewMode: true,
			forceGeneration: true
		});
		try {
			const acquired = beginReviewCreateIntent({
				scope: recoveryScope,
				runId,
				expectedGeneration,
				ttlSeconds: ttl,
				includeEvidence,
				routeQuery
			});
			setRecoveryResource({
				...emptyRecovery(viewKey),
				loaded: true,
				intent: acquired.intent
			});
			void submitIntent(acquired.intent, { copyOnSuccess: true });
		} catch (caught) {
			const code = caught?.code;
			const blocksRecovery = code === "REVIEW_RECOVERY_STORAGE_UNAVAILABLE" || code === "REVIEW_RECOVERY_INVALID";
			const restoreFocus = blocksRecovery && actionFocusOwned();
			updateRecovery(viewKey, (previous) => ({
				...previous,
				loaded: true,
				invalid: blocksRecovery ? code : null,
				error: caught?.message || "Review-link recovery could not be saved. No request was sent."
			}));
			if (restoreFocus) requestActionFocus(viewKey, "recovery");
		}
	};
	const dismissConfirmed = () => {
		const key = viewKey;
		const intent = recovery.intent;
		if (!intent || intent.phase !== "confirmed") return;
		if (!clearReviewCreateIntent(intent)) {
			updateRecovery(viewKey, (previous) => ({
				...previous,
				error: "The saved link could not be cleared. It remains available for recovery."
			}));
			return;
		}
		setRecoveryResource({
			...emptyRecovery(viewKey),
			loaded: true,
			message: "Saved recovery cleared. The existing review link remains active until expiry or revocation."
		});
		requestActionFocus(key, idleActionFocusTarget());
	};
	const discardInvalid = () => {
		if (!discardInvalidReviewCreateIntent(recoveryScope, runId)) {
			updateRecovery(viewKey, (previous) => ({
				...previous,
				error: "Saved recovery data could not be discarded. Session storage may be unavailable."
			}));
			return;
		}
		setRecoveryResource({
			...emptyRecovery(viewKey),
			loaded: true,
			message: "Unreadable recovery data discarded. Check Existing links before creating another link."
		});
		requestActionFocus(viewKey, idleActionFocusTarget());
	};
	const revoke = async (id) => {
		const linkId = reviewRecoveryLinkId(id);
		if (!linkId) return;
		const key = viewKey;
		const requestKey = `${key}\u0000${linkId}`;
		if (revokeRequestsRef.current.has(requestKey)) return;
		let durableIntent = recovery.intent?.linkId === linkId ? recovery.intent : null;
		const restoreDurableFocus = !!durableIntent && actionFocusOwned();
		const restoreRowFocus = linkFocusOwned(linkId);
		const focusSurface = restoreDurableFocus ? "durable" : restoreRowFocus ? "row" : "";
		const revokeFocusOwned = () => focusSurface === "durable" ? actionFocusOwned() : focusSurface === "row" && linkFocusOwned(linkId);
		let restoreRowAction = false;
		if (durableIntent?.phase === "confirmed") {
			try {
				durableIntent = transitionReviewCreateIntent(durableIntent, {
					phase: "revoking",
					linkId,
					token: durableIntent.token,
					expiresAt: durableIntent.expiresAt
				});
				setRecoveryResource({
					...emptyRecovery(key),
					loaded: true,
					intent: durableIntent,
					message: "Revoking the recovered review link…"
				});
			} catch (caught) {
				updateRecovery(key, (previous) => ({
					...previous,
					error: caught.message
				}));
				return;
			}
		} else if (durableIntent) updateRecovery(key, (previous) => ({
			...previous,
			error: "",
			message: durableIntent.phase === "revoking" ? "Revoking the recovered review link…" : previous.message
		}));
		const timed = boundedLinkRequest((signal) => revokeRunReview(runId, linkId, { signal }));
		const operation = {
			key,
			linkId,
			intent: durableIntent,
			timed
		};
		revokeRequestsRef.current.set(requestKey, operation);
		setRevokeResource((previous) => {
			const ids = new Set(previous.key === key ? previous.ids : []);
			ids.add(linkId);
			return {
				key,
				ids,
				error: ""
			};
		});
		if (focusSurface === "durable") requestActionFocus(key, "recovery");
		else if (focusSurface === "row") requestActionFocus(key, "link-row", linkId);
		try {
			const result = await timed.promise;
			if (result?.ok !== true || result.id !== linkId || result.run_id !== String(runId) || !Number.isFinite(Number(result.revoked_at))) {
				throw new Error("The server returned an invalid revocation receipt.");
			}
			if (durableIntent && !clearReviewCreateIntent(durableIntent)) {
				throw new Error("The link was revoked, but its saved recovery could not be cleared.");
			}
			if (renderRef.current.key === key) {
				const restoreFocus = revokeFocusOwned();
				if (durableIntent) setRecoveryResource({
					...emptyRecovery(key),
					loaded: true,
					message: "Review link revoked."
				});
				if (restoreFocus) requestActionFocus(key, focusSurface === "durable" ? idleActionFocusTarget() : "link-row", focusSurface === "row" ? linkId : "");
				setLinksResource((previous) => previous.key === key ? {
					...previous,
					links: previous.links.map((link) => link.id === linkId ? {
						...link,
						status: "revoked",
						revoked_at: result.revoked_at
					} : link)
				} : previous);
				onToast?.("review link revoked");
				void refreshLinks({ preserveLinkId: restoreFocus && focusSurface === "row" ? linkId : "" });
			}
		} catch {
			if (revokeRequestsRef.current.get(requestKey) !== operation || renderRef.current.key !== key) return;
			restoreRowAction = focusSurface === "row" && revokeFocusOwned();
			const message = "Revocation was not confirmed. Retry the same revoke before sharing or creating another link.";
			if (durableIntent) updateRecovery(key, (previous) => ({
				...previous,
				intent: durableIntent,
				error: message
			}));
			else setRevokeResource((previous) => previous.key === key ? {
				...previous,
				error: message
			} : previous);
		} finally {
			if (revokeRequestsRef.current.get(requestKey) === operation) {
				revokeRequestsRef.current.delete(requestKey);
				if (renderRef.current.key === key) setRevokeResource((previous) => {
					if (previous.key !== key) return previous;
					const ids = new Set(previous.ids);
					ids.delete(linkId);
					return {
						...previous,
						ids
					};
				});
				if (renderRef.current.key === key && restoreRowAction) {
					requestActionFocus(key, "link-action", linkId);
				}
			}
		}
	};
	const intent = recovery.intent;
	const controlsLocked = !recovery.loaded || !!recovery.invalid || !!intent || recovery.busy || !reviewRecoveryGenerationValid(expectedGeneration);
	const createdUrl = intent?.phase === "confirmed" ? reviewUrlForIntent(intent, {
		origin,
		prefix
	}) : "";
	const recovering = intent?.phase === "pending";
	const recoveryConflict = intent?.phase === "conflict";
	const revokePending = intent?.phase === "revoking";
	return /* @__PURE__ */ _jsxs(PanelComponent, {
		title: "Comments & sharing",
		onClose,
		children: [
			!reviewMode && /* @__PURE__ */ _jsxs("div", {
				className: "review-link-builder",
				"aria-busy": recovery.busy ? "true" : "false",
				children: [
					/* @__PURE__ */ _jsx("div", {
						className: "section-h",
						children: "Create a read-only review link"
					}),
					/* @__PURE__ */ _jsx("p", {
						className: "muted",
						children: "The link is bound to this run, expires automatically, can be revoked, and never carries owner controls."
					}),
					/* @__PURE__ */ _jsxs("div", {
						className: "review-link-options",
						children: [/* @__PURE__ */ _jsxs("label", { children: ["Expires", /* @__PURE__ */ _jsxs("select", {
							value: ttl,
							disabled: controlsLocked,
							onChange: (event) => setTtl(Number(event.target.value)),
							children: [
								/* @__PURE__ */ _jsx("option", {
									value: 60 * 60,
									children: "1 hour"
								}),
								/* @__PURE__ */ _jsx("option", {
									value: 24 * 60 * 60,
									children: "1 day"
								}),
								/* @__PURE__ */ _jsx("option", {
									value: 7 * 24 * 60 * 60,
									children: "7 days"
								}),
								/* @__PURE__ */ _jsx("option", {
									value: 30 * 24 * 60 * 60,
									children: "30 days"
								})
							]
						})] }), /* @__PURE__ */ _jsxs("label", {
							className: "review-evidence-option",
							children: [/* @__PURE__ */ _jsx("input", {
								type: "checkbox",
								checked: includeEvidence,
								disabled: controlsLocked,
								onChange: (event) => setIncludeEvidence(event.target.checked)
							}), " Include redacted source evidence"]
						})]
					}),
					includeEvidence && /* @__PURE__ */ _jsx("div", {
						className: "notice warn",
						children: "Source and result details can still contain sensitive project information. Known credential patterns are redacted; raw logs, prompts, traces, and artifacts remain excluded."
					}),
					!recovery.loaded && /* @__PURE__ */ _jsx("div", {
						className: "muted",
						role: "status",
						children: "Checking this tab for a saved review link…"
					}),
					recovery.loaded && !intent && !recovery.invalid && !reviewRecoveryGenerationValid(expectedGeneration) && /* @__PURE__ */ _jsx("div", {
						ref: recoveryStatusRef,
						tabIndex: -1,
						className: "muted",
						role: "status",
						children: "Waiting for the current run version before sharing…"
					}),
					recovery.invalid && /* @__PURE__ */ _jsxs("div", {
						ref: recoveryStatusRef,
						tabIndex: -1,
						className: "notice resource-error review-recovery",
						role: "alert",
						children: [/* @__PURE__ */ _jsx("span", { children: recovery.error }), !["REVIEW_RECOVERY_STORAGE_UNAVAILABLE", "REVIEW_RECOVERY_CRYPTO_UNAVAILABLE"].includes(recovery.invalid) && /* @__PURE__ */ _jsx("button", {
							type: "button",
							className: "btn sm danger",
							onClick: discardInvalid,
							children: "Discard unreadable recovery"
						})]
					}),
					recovering && /* @__PURE__ */ _jsxs("div", {
						ref: recoveryStatusRef,
						tabIndex: -1,
						className: "notice warn review-recovery",
						role: "status",
						"aria-live": "polite",
						children: [
							/* @__PURE__ */ _jsx("b", { children: recovery.busy ? "Recovering the same review link…" : "Review-link creation was not confirmed." }),
							/* @__PURE__ */ _jsx("span", { children: "The exact run version, expiry, evidence scope, and secret are saved in this tab. Recovery replays them without minting a second identity." }),
							!recovery.busy && /* @__PURE__ */ _jsx("button", {
								type: "button",
								className: "btn sm primary",
								onClick: () => submitIntent(intent, { copyOnSuccess: true }),
								children: "Recover the same review link"
							})
						]
					}),
					recoveryConflict && /* @__PURE__ */ _jsxs("div", {
						ref: recoveryStatusRef,
						tabIndex: -1,
						className: "notice resource-error review-recovery",
						role: "alert",
						children: [
							/* @__PURE__ */ _jsx("b", { children: "Saved link identity conflict" }),
							/* @__PURE__ */ _jsx("span", { children: recovery.error || `A different request already owns ${intent.linkId}. Revoke it before creating another link.` }),
							/* @__PURE__ */ _jsx("button", {
								type: "button",
								className: "btn sm danger",
								disabled: revokeView.ids.has(intent.linkId),
								onClick: () => revoke(intent.linkId),
								children: revokeView.ids.has(intent.linkId) ? "Revoking…" : "Revoke conflicting link"
							})
						]
					}),
					revokePending && /* @__PURE__ */ _jsxs("div", {
						ref: recoveryStatusRef,
						tabIndex: -1,
						className: "notice warn review-recovery",
						role: "status",
						"aria-live": "polite",
						children: [
							/* @__PURE__ */ _jsx("b", { children: "Revocation is not confirmed." }),
							/* @__PURE__ */ _jsx("span", { children: recovery.error || "Retrying targets the same recovered link and cannot revoke a different link." }),
							/* @__PURE__ */ _jsx("button", {
								type: "button",
								className: "btn sm danger",
								disabled: revokeView.ids.has(intent.linkId),
								onClick: () => revoke(intent.linkId),
								children: revokeView.ids.has(intent.linkId) ? "Revoking…" : "Retry revoke"
							})
						]
					}),
					recovery.error && !recovery.invalid && !recoveryConflict && !revokePending && /* @__PURE__ */ _jsx("div", {
						className: "notice resource-error",
						role: "alert",
						children: recovery.error
					}),
					recovery.message && /* @__PURE__ */ _jsx("div", {
						className: "notice",
						role: "status",
						"aria-live": "polite",
						children: recovery.message
					}),
					!intent && !recovery.invalid && recovery.loaded && /* @__PURE__ */ _jsxs("button", {
						ref: createButtonRef,
						type: "button",
						className: "btn sm primary",
						disabled: controlsLocked,
						onClick: create,
						children: [/* @__PURE__ */ _jsx(OpIcon, {
							name: "link",
							size: 12
						}), " Create and copy review link"]
					}),
					createdUrl && /* @__PURE__ */ _jsxs("div", {
						ref: createdSurfaceRef,
						className: "review-created",
						role: "status",
						"aria-live": "polite",
						children: [
							/* @__PURE__ */ _jsx("label", {
								htmlFor: "created-review-url",
								children: "Review link ready"
							}),
							/* @__PURE__ */ _jsxs("div", { children: [/* @__PURE__ */ _jsx("input", {
								ref: createdInputRef,
								id: "created-review-url",
								readOnly: true,
								value: createdUrl,
								"aria-describedby": "created-review-url-note",
								onFocus: (event) => event.target.select()
							}), /* @__PURE__ */ _jsx("button", {
								type: "button",
								className: "btn sm",
								"aria-label": "Copy recovered review link",
								onClick: () => copy(createdUrl),
								children: "Copy"
							})] }),
							/* @__PURE__ */ _jsxs("div", {
								id: "created-review-url-note",
								className: "muted",
								children: [
									"Expires ",
									dateLabel(intent.expiresAt),
									". Available for recovery in this browser tab until you confirm it is saved."
								]
							}),
							/* @__PURE__ */ _jsxs("div", {
								className: "review-recovery-actions",
								children: [/* @__PURE__ */ _jsx("button", {
									type: "button",
									className: "btn sm",
									onClick: dismissConfirmed,
									children: "I’ve saved it"
								}), /* @__PURE__ */ _jsx("button", {
									type: "button",
									className: "btn sm danger",
									disabled: revokeView.ids.has(intent.linkId),
									onClick: () => revoke(intent.linkId),
									children: revokeView.ids.has(intent.linkId) ? "Revoking…" : "Revoke this link"
								})]
							})
						]
					}),
					/* @__PURE__ */ _jsx("div", {
						ref: linksSectionRef,
						tabIndex: -1,
						className: "section-h",
						children: "Existing links"
					}),
					linksView.status === "loading" && !linksView.links.length && /* @__PURE__ */ _jsx("div", {
						ref: linksStatusRef,
						tabIndex: -1,
						className: "muted",
						role: "status",
						children: "Loading review links…"
					}),
					linksView.status === "refreshing" && /* @__PURE__ */ _jsx("div", {
						ref: linksStatusRef,
						tabIndex: -1,
						className: "muted",
						role: "status",
						children: "Refreshing review links…"
					}),
					linksView.links.length > 0 && /* @__PURE__ */ _jsx("div", {
						className: "review-link-list",
						children: linksView.links.map((link) => {
							const expires = dateLabel(link.expires_at);
							const evidence = (link.scopes || []).includes("evidence");
							const rowBusy = revokeView.ids.has(link.id);
							const rowId = reviewRecoveryLinkId(link.id) || String(link.id);
							return /* @__PURE__ */ _jsxs("div", {
								ref: (element) => {
									if (element) linkRowRefs.current.set(rowId, element);
									else linkRowRefs.current.delete(rowId);
								},
								tabIndex: -1,
								className: "review-link-row",
								children: [/* @__PURE__ */ _jsxs("div", { children: [
									/* @__PURE__ */ _jsx("b", { children: link.status }),
									" · ",
									evidence ? "summary + evidence" : "summary",
									/* @__PURE__ */ _jsxs("div", {
										className: "muted",
										children: ["expires ", expires]
									})
								] }), activeLink(link) && /* @__PURE__ */ _jsx("button", {
									type: "button",
									className: "btn sm danger",
									disabled: rowBusy,
									ref: (element) => {
										if (element) linkActionRefs.current.set(rowId, element);
										else linkActionRefs.current.delete(rowId);
									},
									"aria-label": `Revoke ${evidence ? "summary and evidence" : "summary"} review link expiring ${expires}`,
									onClick: () => revoke(link.id),
									children: rowBusy ? "Revoking…" : "Revoke"
								})]
							}, link.id);
						})
					}),
					linksView.status === "ready" && !linksView.links.length && /* @__PURE__ */ _jsx("div", {
						className: "muted",
						children: "No review links created yet."
					}),
					["error", "stale"].includes(linksView.status) && /* @__PURE__ */ _jsxs("div", {
						className: "review-links-error",
						role: linksView.status === "error" ? "alert" : "status",
						children: [/* @__PURE__ */ _jsx("span", {
							className: "muted",
							children: linksView.error
						}), /* @__PURE__ */ _jsx("button", {
							ref: linksRetryRef,
							type: "button",
							className: "btn sm",
							onClick: () => refreshLinks({ preserveFocus: true }),
							children: "Retry"
						})]
					}),
					revokeView.error && /* @__PURE__ */ _jsx("div", {
						className: "notice resource-error",
						role: "alert",
						children: revokeView.error
					})
				]
			}),
			!reviewMode && /* @__PURE__ */ _jsx("div", {
				className: "muted",
				style: { margin: "16px 0 8px" },
				children: "Comments are append-only run events. Review-link recipients can read redacted current comments, but cannot add, edit, resolve, reopen, or inspect owner-only version history."
			}),
			/* @__PURE__ */ _jsx(CommentsThread, {
				runId,
				expectedGeneration,
				refreshKey,
				readOnly: reviewMode,
				reviewMode,
				global: true,
				draftStore,
				draftSurface: "collab",
				onOpenComment: (comment) => {
					if (onOpenComment) {
						onOpenComment(comment);
						return;
					}
					onSelect?.(comment.nodeId);
					onClose?.();
				}
			})
		]
	});
}
