import React, { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { attentionHref } from "./attentionModel.mjs";
import { createAttentionChannel, deliverAttentionNotifications, disableAttentionNotifications, enableAttentionNotifications, mutateAttentionState, notificationCapability } from "./attentionNotifications.mjs";
import { attentionIds, loadAttentionState, recordAttentionIds } from "./attentionStorage.mjs";
import { OpIcon } from "./icons.mjs";
import { useAttention } from "./useAttention.mjs";
import { DIALOG_PRIORITY, useDialogFocus } from "./useDialogFocus.mjs";
import { AttentionLauncher, claimAttentionIndicatorPublisher, openAttentionCenter, publishAttentionIndicator, releaseAttentionIndicatorPublisher } from "./attentionIndicator.mjs";

import { jsxs as _jsxs, jsx as _jsx, Fragment as _Fragment } from "react/jsx-runtime";
const ATTENTION_PREFERENCE_FAILURE = "This browser could not verify the saved attention preference.";
const NO_COMPLETE_ATTENTION_SNAPSHOT = "No complete verified snapshot is available yet.";
function isPlainRunActivation(event) {
	if (!event || event.defaultPrevented || event.button != null && event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
	const target = String(event.currentTarget?.target || "").toLowerCase();
	return (!target || target === "_self") && !event.currentTarget?.hasAttribute?.("download");
}
function collapseModalAssistantForRouteHandoff() {
	if (typeof document === "undefined" || typeof window === "undefined") return;
	const modalAssistant = document.querySelector(".asst-full[aria-modal=\"true\"], .asst-side-panel[aria-modal=\"true\"]");
	if (modalAssistant && typeof window.Event === "function") {
		window.dispatchEvent(new window.Event("ll:collapse-assistant-for-navigation"));
	}
}
function itemTime(seconds) {
	if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return null;
	const date = new Date(seconds * 1e3);
	if (Number.isNaN(date.getTime())) return null;
	let label;
	try {
		label = new Intl.DateTimeFormat(undefined, {
			dateStyle: "medium",
			timeStyle: "short"
		}).format(date);
	} catch {
		label = date.toLocaleString();
	}
	return {
		iso: date.toISOString(),
		label
	};
}
function snapshotAge(value, now = Date.now()) {
	const numeric = Number(value);
	if (!Number.isFinite(numeric) || numeric <= 0) return "";
	const timestamp = numeric < 0xe8d4a51000 ? numeric * 1e3 : numeric;
	const elapsed = Math.max(0, now - timestamp);
	if (elapsed < 6e4) return "just now";
	if (elapsed < 36e5) return `${Math.floor(elapsed / 6e4)}m ago`;
	if (elapsed < 864e5) return `${Math.floor(elapsed / 36e5)}h ago`;
	return `${Math.floor(elapsed / 864e5)}d ago`;
}
const itemWord = (count) => count === 1 ? "item" : "items";
function actionCountCopy(count, still = false) {
	if (count === 1) return `1 item${still ? " still" : ""} needs action`;
	return `${count} items${still ? " still" : ""} need action`;
}
function visualCount(count, incomplete = false) {
	if (count > 99) return "99+";
	return `${count}${incomplete ? "+" : ""}`;
}
function capabilityCopy(capability, preferences) {
	if (!preferences.available) {
		return "Desktop alerts are unavailable because this browser cannot safely persist notification state.";
	}
	if (capability === "unsupported") return "This browser does not support desktop notifications.";
	if (capability === "denied") {
		return "Desktop notifications are blocked in browser settings. In-app attention remains available.";
	}
	if (capability === "locks-unavailable") {
		return "Desktop alerts are unavailable because this browser cannot prevent duplicate delivery across tabs.";
	}
	if (!preferences.valid) {
		return "Saved notification preferences could not be verified. Alerts stay off until you enable them again.";
	}
	if (preferences.state.enabled && capability === "granted") {
		return "Enabled for new items only. Current items were used as the baseline and will not create a backlog burst.";
	}
	return capability === "default" ? "Off. Enabling will ask for browser permission from this click." : "Off. You can enable alerts for new items.";
}
const feedbackCopy = Object.freeze({
	granted: "Desktop notifications enabled for future items.",
	disabled: "Desktop notifications disabled.",
	denied: "The browser did not grant notification permission.",
	unsupported: "Desktop notifications are not supported by this browser.",
	"locks-unavailable": "Desktop notifications need cross-tab lock support to avoid duplicates.",
	"storage-unavailable": "Desktop notifications need safe local storage to avoid duplicate delivery.",
	"request-failed": "The browser could not complete the notification permission request.",
	"presentation-failed": "The browser could not display a desktop notification."
});
// Severity is shown visually by the coloured dot (aria-hidden). Carry the same meaning in TEXT so it is
// not conveyed by colour alone (WCAG 1.4.1) and is announced to screen readers (the dot is decorative).
const SEVERITY_LABEL = Object.freeze({
	action: "Needs action",
	warning: "Warning",
	danger: "Urgent",
	success: "Resolved"
});
function AttentionItem({ item, unread, sourceStale, onOpenRun, onMarkRead, onDismiss, onOpenPermission }) {
	const timestamp = itemTime(item.created);
	const stale = item.stale || sourceStale;
	const activeAction = item.needsAction && item.active;
	const actionLabel = stale ? "Verify current state" : item.actionLabel;
	// Reconstruct the destination from the normalized, generation-fenced fields. Never trust a URL
	// supplied by the feed (and permission cards never receive a link at all).
	const runHref = item.source === "run" ? attentionHref(item) : null;
	const permissionActionLabel = item.source === "permission" ? `${actionLabel} for approval request ${item.requestId.slice(0, 6)}` : undefined;
	return /* @__PURE__ */ _jsxs("li", {
		className: `attention-item severity-${item.severity}${unread ? " unread" : ""}`,
		"data-attention-item-id": item.id,
		tabIndex: -1,
		children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "attention-item-heading",
				children: [
					/* @__PURE__ */ _jsx("span", {
						className: "attention-severity-dot",
						"aria-hidden": "true"
					}),
					SEVERITY_LABEL[item.severity] && /* @__PURE__ */ _jsxs("span", {
						className: "sr-only",
						children: [SEVERITY_LABEL[item.severity], ": "]
					}),
					/* @__PURE__ */ _jsx("h4", { children: item.title }),
					stale && /* @__PURE__ */ _jsx("span", {
						className: "attention-stale-label",
						children: "Stale"
					}),
					unread && /* @__PURE__ */ _jsx("span", {
						className: "attention-new-label",
						children: stale ? "Unread" : "New"
					})
				]
			}),
			item.source === "run" && /* @__PURE__ */ _jsxs("p", {
				className: "attention-run-context",
				children: [/* @__PURE__ */ _jsx("strong", { children: item.contextLabel || item.runId }), item.taskId && item.taskId !== item.contextLabel && /* @__PURE__ */ _jsxs("span", { children: [" · task ", item.taskId] })]
			}),
			/* @__PURE__ */ _jsx("p", { children: item.detail }),
			timestamp && /* @__PURE__ */ _jsx("time", {
				dateTime: timestamp.iso,
				children: timestamp.label
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "attention-item-actions",
				children: [
					runHref && /* @__PURE__ */ _jsx("a", {
						className: "attention-button primary",
						href: runHref,
						"aria-label": `${actionLabel} for ${item.contextLabel || item.runId}`,
						onClick: (event) => onOpenRun(event, item.id, runHref),
						children: actionLabel
					}),
					item.source === "permission" && /* @__PURE__ */ _jsx("button", {
						type: "button",
						className: "attention-button primary",
						"aria-label": permissionActionLabel,
						onClick: () => onOpenPermission(item),
						children: actionLabel
					}),
					activeAction && unread && /* @__PURE__ */ _jsx("button", {
						type: "button",
						className: "attention-button subtle",
						"aria-label": `Mark ${item.title}${item.source === "run" ? ` for ${item.contextLabel || item.runId}` : ""} as read`,
						onClick: () => onMarkRead(item.id),
						children: "Mark read"
					}),
					!activeAction && /* @__PURE__ */ _jsx("button", {
						type: "button",
						className: "attention-button subtle",
						"aria-label": `Dismiss ${item.title}${item.source === "run" ? ` for ${item.contextLabel || item.runId}` : ""}`,
						onClick: () => onDismiss(item.id),
						children: "Dismiss"
					})
				]
			})
		]
	});
}
export default function AttentionCenter() {
	const { items, currentItems, initialized, runStale, permissionsStale, partial, truncated, hasMore, loadingMore, loadMoreError, loadMore, refresh, authoritative, verified, runVerified, permissionsVerified, runVerifiedGeneratedAt, runActiveActionCount } = useAttention();
	const [open, setOpen] = useState(false);
	const [preferences, setPreferences] = useState(() => loadAttentionState());
	const [capability, setCapability] = useState(() => notificationCapability());
	const [notificationBusy, setNotificationBusy] = useState(false);
	const [notificationFeedback, setNotificationFeedback] = useState("");
	const [focusRevision, setFocusRevision] = useState(0);
	const [liveMessage, setLiveMessageState] = useState({
		text: "",
		revision: 0
	});
	const setLiveMessage = useCallback((text) => {
		setLiveMessageState((previous) => ({
			text,
			revision: previous.revision + 1
		}));
	}, []);
	const dialogRef = useRef(null);
	const actionHeadingRef = useRef(null);
	const recentHeadingRef = useRef(null);
	const focusRequestRef = useRef(null);
	const pendingHandoffRef = useRef(null);
	const indicatorPublisherRef = useRef(Symbol("attention-indicator"));
	const channelRef = useRef(null);
	const seenItemIdsRef = useRef(new Set());
	const baselinedSourcesRef = useRef({
		run: false,
		permission: false
	});
	const titleId = useId();
	const descriptionId = useId();
	const drawerId = useId();
	const close = useCallback(() => {
		pendingHandoffRef.current = null;
		focusRequestRef.current = null;
		setOpen(false);
	}, []);
	const closeForHandoff = useCallback((handoff) => {
		focusRequestRef.current = null;
		pendingHandoffRef.current = handoff;
		setOpen(false);
	}, []);
	useDialogFocus(dialogRef, close, open, { priority: DIALOG_PRIORITY.ATTENTION });
	// Run the destination action only after the dialog focus trap has torn down and restored its
	// opener. Route/Assistant focus can then take ownership without a late Attention cleanup winning.
	useEffect(() => {
		if (open || !pendingHandoffRef.current) return;
		const handoff = pendingHandoffRef.current;
		pendingHandoffRef.current = null;
		handoff();
	}, [open]);
	const jumpToSection = useCallback((section) => {
		const heading = section === "action" ? actionHeadingRef.current : recentHeadingRef.current;
		if (!heading) return;
		heading.focus({ preventScroll: true });
		heading.closest(".attention-section")?.scrollIntoView({ block: "start" });
	}, []);
	const reloadPreferences = useCallback(() => {
		setPreferences(loadAttentionState());
		setCapability(notificationCapability());
	}, []);
	useEffect(() => {
		const channel = createAttentionChannel({ onInvalidate: reloadPreferences });
		channelRef.current = channel;
		return () => {
			channel.close();
			if (channelRef.current === channel) channelRef.current = null;
		};
	}, [reloadPreferences]);
	useEffect(() => {
		const onFocus = () => reloadPreferences();
		window.addEventListener("focus", onFocus);
		return () => window.removeEventListener("focus", onFocus);
	}, [reloadPreferences]);
	useLayoutEffect(() => {
		const onOpen = () => setOpen(true);
		window.addEventListener("ll:open-attention", onOpen);
		return () => window.removeEventListener("ll:open-attention", onOpen);
	}, []);
	const acknowledgedIds = useMemo(() => attentionIds(preferences.state, "acknowledged"), [preferences.state.acknowledged]);
	const dismissedIds = useMemo(() => attentionIds(preferences.state, "dismissed"), [preferences.state.dismissed]);
	const visibleItems = useMemo(() => items.filter((item) => item.needsAction && item.active || !dismissedIds.has(item.id)), [items, dismissedIds]);
	const actionItems = useMemo(() => visibleItems.filter((item) => item.needsAction && item.active), [visibleItems]);
	const recentItems = useMemo(() => visibleItems.filter((item) => !(item.needsAction && item.active)), [visibleItems]);
	const unreadItems = useMemo(() => visibleItems.filter((item) => !acknowledgedIds.has(item.id)), [visibleItems, acknowledgedIds]);
	const unreadCount = unreadItems.length;
	const permissionActiveActionCount = useMemo(() => currentItems.reduce((count, item) => count + (item.source === "permission" && item.needsAction && item.active ? 1 : 0), 0), [currentItems]);
	const runActionCountKnown = Number.isSafeInteger(runActiveActionCount) && runActiveActionCount >= 0;
	const loadedRunActionCount = actionItems.reduce((count, item) => count + (item.source === "run" ? 1 : 0), 0);
	const feedAuthoritative = authoritative === true;
	const displayedRunActionCount = feedAuthoritative && runActionCountKnown ? runActiveActionCount : Math.max(runActionCountKnown ? runActiveActionCount : 0, loadedRunActionCount);
	const activeActionCount = displayedRunActionCount + permissionActiveActionCount;
	const actionCountExact = feedAuthoritative && runActionCountKnown;
	const actionPhrase = actionCountCopy(activeActionCount);
	const stillActionPhrase = actionCountCopy(activeActionCount, true);
	const uncertainActionPhrase = activeActionCount === 1 ? "1 loaded or previously verified item may need action" : `${activeActionCount} loaded or previously verified items may need action`;
	const unreadComplete = feedAuthoritative && !truncated;
	const unreadPaginationIncomplete = feedAuthoritative && truncated;
	// Baseline each source's first successful snapshot silently. A source may recover after the other
	// one initialized the hook, so treating them separately prevents a delayed backlog avalanche.
	useEffect(() => {
		if (!initialized) return;
		const fresh = [];
		for (const source of ["run", "permission"]) {
			const sourceStale = source === "run" ? runStale || partial : permissionsStale;
			const sourceItems = currentItems.filter((item) => item.source === source);
			if (!baselinedSourcesRef.current[source]) {
				if (sourceStale) continue;
				for (const item of sourceItems) seenItemIdsRef.current.add(item.id);
				baselinedSourcesRef.current[source] = true;
				continue;
			}
			for (const item of sourceItems) {
				// Do not consume the in-tab occurrence identity from a stale/partial snapshot. The same
				// episode must still receive its one live announcement when this source is verified again.
				if (sourceStale || item.stale) continue;
				if (!seenItemIdsRef.current.has(item.id)) {
					seenItemIdsRef.current.add(item.id);
					if (!dismissedIds.has(item.id)) fresh.push(item);
				}
			}
		}
		if (fresh.length) setLiveMessage(`${fresh.length} new attention ${fresh.length === 1 ? "item" : "items"}.`);
	}, [
		initialized,
		currentItems,
		dismissedIds,
		runStale,
		permissionsStale,
		partial,
		setLiveMessage
	]);
	const broadcastInvalidation = useCallback((value) => {
		// Cross-tab messages are deliberately payload-free. The receiving tab reloads its own bounded,
		// validated envelope from storage instead of trusting data sent by another document.
		channelRef.current?.broadcast(value);
	}, []);
	const persistIds = useCallback(async (field, ids, message, includeAcknowledged = false) => {
		const result = await mutateAttentionState((state) => {
			let next = recordAttentionIds(state, field, ids);
			if (includeAcknowledged) next = recordAttentionIds(next, "acknowledged", ids);
			return next;
		}, { broadcast: broadcastInvalidation });
		if (!result.ok || !result.state) {
			setNotificationFeedback(ATTENTION_PREFERENCE_FAILURE);
			setLiveMessage(ATTENTION_PREFERENCE_FAILURE);
			return false;
		}
		setPreferences({
			state: result.state,
			available: true,
			valid: true
		});
		if (message) setLiveMessage(message);
		return true;
	}, [broadcastInvalidation, setLiveMessage]);
	const acknowledgeInBackground = useCallback((id) => {
		void persistIds("acknowledged", [id], "").catch(() => {
			setNotificationFeedback(ATTENTION_PREFERENCE_FAILURE);
			setLiveMessage(ATTENTION_PREFERENCE_FAILURE);
		});
	}, [persistIds, setLiveMessage]);
	// Opening an approval is acknowledged only after Assistant has loaded and focused that exact
	// request. A failed/superseded handoff therefore stays visibly unread and can be retried.
	useEffect(() => {
		const onPermissionOpened = (event) => {
			const requestId = event?.detail?.requestId;
			if (typeof requestId !== "string" || !/^[0-9a-f]{16}$/.test(requestId)) return;
			acknowledgeInBackground(`perm_${requestId}`);
		};
		window.addEventListener("ll:assistant-permission-opened", onPermissionOpened);
		return () => window.removeEventListener("ll:assistant-permission-opened", onPermissionOpened);
	}, [acknowledgeInBackground]);
	const openRun = useCallback((event, id, href) => {
		if (!isPlainRunActivation(event) || typeof href !== "string" || !href.startsWith("#/run/")) return;
		event.preventDefault();
		closeForHandoff(() => {
			// A run destination would otherwise change underneath a still-opaque full/compact Assistant.
			// Preserve nonmodal desktop side chat, but fold a modal Assistant before revealing the route.
			collapseModalAssistantForRouteHandoff();
			if (location.hash === href) requestAnimationFrame(() => {
				document.querySelector("[data-route-main]")?.focus({ preventScroll: true });
			});
			else location.hash = href;
			acknowledgeInBackground(id);
		});
	}, [acknowledgeInBackground, closeForHandoff]);
	const markRead = useCallback(async (id) => {
		focusRequestRef.current = {
			ids: [id],
			section: "action"
		};
		const saved = await persistIds("acknowledged", [id], "Attention item marked as read.");
		if (saved) setFocusRevision((value) => value + 1);
		else focusRequestRef.current = null;
	}, [persistIds]);
	const dismiss = useCallback(async (id) => {
		const index = recentItems.findIndex((item) => item.id === id);
		focusRequestRef.current = {
			ids: index < 0 ? [] : [recentItems[index + 1]?.id, recentItems[index - 1]?.id].filter(Boolean),
			section: "recent"
		};
		const saved = await persistIds("dismissed", [id], "Attention item dismissed.", true);
		if (saved) setFocusRevision((value) => value + 1);
		else focusRequestRef.current = null;
	}, [persistIds, recentItems]);
	const markAllRead = useCallback(async () => {
		if (!unreadCount) return;
		const loaded = unreadComplete ? "" : "loaded ";
		const unresolved = activeActionCount > 0 ? actionCountExact ? ` ${stillActionPhrase}.` : ` ${uncertainActionPhrase}. Current action total is unavailable.` : "";
		await persistIds("acknowledged", unreadItems.map((item) => item.id), `${unreadCount} ${loaded}${itemWord(unreadCount)} marked as read.${unresolved}`);
	}, [
		activeActionCount,
		actionCountExact,
		persistIds,
		stillActionPhrase,
		uncertainActionPhrase,
		unreadComplete,
		unreadCount,
		unreadItems
	]);
	const openPermission = useCallback((item) => {
		if (item?.source !== "permission" || !/^[0-9a-f]{16}$/.test(item.session || "") || !/^[0-9a-f]{16}$/.test(item.requestId || "")) return;
		closeForHandoff(() => {
			window.dispatchEvent(new CustomEvent("ll:open-assistant-session", { detail: {
				session: item.session,
				requestId: item.requestId
			} }));
		});
	}, [closeForHandoff]);
	const enableNotifications = useCallback(async () => {
		if (notificationBusy) return;
		setNotificationBusy(true);
		setNotificationFeedback("");
		try {
			const result = await enableAttentionNotifications(items, { broadcast: broadcastInvalidation });
			// Only assert a verified-available preference envelope when the write actually committed —
			// mirror disableNotifications, which gates on `result.ok`. A failed persist (quota/blocked)
			// returns ok:false with the safe old state; reload rather than claim valid:true it never wrote.
			if (result.state && result.ok) setPreferences({
				state: result.state,
				available: true,
				valid: true
			});
			else reloadPreferences();
			setCapability(notificationCapability());
			setNotificationFeedback(feedbackCopy[result.status] || "Desktop notification settings were not changed.");
		} catch {
			reloadPreferences();
			setNotificationFeedback("The browser could not complete the notification permission request.");
		} finally {
			setNotificationBusy(false);
		}
	}, [
		broadcastInvalidation,
		items,
		notificationBusy,
		reloadPreferences
	]);
	const disableNotifications = useCallback(async () => {
		if (notificationBusy) return;
		setNotificationBusy(true);
		setNotificationFeedback("");
		try {
			const result = await disableAttentionNotifications({ broadcast: broadcastInvalidation });
			if (result.state && result.ok) {
				setPreferences({
					state: result.state,
					available: true,
					valid: true
				});
			} else reloadPreferences();
			setCapability(notificationCapability());
			setNotificationFeedback(feedbackCopy[result.status] || "Desktop notification settings were not changed.");
		} catch {
			reloadPreferences();
			setNotificationFeedback("The browser could not update desktop notification settings.");
		} finally {
			setNotificationBusy(false);
		}
	}, [
		broadcastInvalidation,
		notificationBusy,
		reloadPreferences
	]);
	const deliveryItems = useMemo(() => currentItems.filter((item) => !dismissedIds.has(item.id) && !acknowledgedIds.has(item.id)), [
		currentItems,
		dismissedIds,
		acknowledgedIds
	]);
	// Include notifyEligible in the key so the delivery effect re-runs when an item flips
	// stale→fresh (same id+created): otherwise a genuinely-new item that first arrived while its
	// source was momentarily stale (notifyEligible=false → filtered out, never added to `notified`)
	// never gets its desktop notification once the source recovers. Re-delivery is idempotent
	// (deliverAttentionNotifications de-dupes via the persisted `notified` set), so this only surfaces
	// the previously-skipped one, never a duplicate.
	const deliveryKey = deliveryItems.map((item) => `${item.id}:${item.created}:${item.notifyEligible ? 1 : 0}`).join("|");
	useEffect(() => {
		if (!initialized || !preferences.state.enabled) return;
		let active = true;
		deliverAttentionNotifications(deliveryItems, {
			broadcast: broadcastInvalidation,
			// A Notification can outlive this React instance (for example after owner navigation). Route
			// the click through the payload-free global event so only the currently mounted owner center
			// handles it; a review route has no listener and remains isolated.
			onOpenCenter: openAttentionCenter
		}).then((result) => {
			if (!active) return;
			if (result.status === "storage-unavailable") reloadPreferences();
			if (feedbackCopy[result.status]) setNotificationFeedback(feedbackCopy[result.status]);
		}).catch(() => {
			if (active) setNotificationFeedback("Desktop notification delivery could not be completed.");
		});
		return () => {
			active = false;
		};
	}, [
		broadcastInvalidation,
		deliveryKey,
		initialized,
		preferences.state.enabled,
		reloadPreferences
	]);
	// If a focused read/dismiss control disappears from the DOM, keep keyboard focus in the dialog.
	useEffect(() => {
		if (!open) return;
		const frame = requestAnimationFrame(() => {
			const root = dialogRef.current;
			const request = focusRequestRef.current;
			if (root && request) {
				const cards = [...root.querySelectorAll("[data-attention-item-id]")];
				const target = request.ids.map((id) => cards.find((card) => card.dataset.attentionItemId === id)).find(Boolean) || (request.section === "action" ? actionHeadingRef.current : recentHeadingRef.current);
				focusRequestRef.current = null;
				if (target) {
					target.focus({ preventScroll: true });
					target.scrollIntoView({ block: "nearest" });
					return;
				}
			}
			if (root && !root.contains(document.activeElement)) {
				root.querySelector("[data-dialog-initial-focus]")?.focus({ preventScroll: true });
			}
		});
		return () => cancelAnimationFrame(frame);
	}, [
		focusRevision,
		open,
		unreadCount,
		visibleItems.length
	]);
	const feedVerified = feedAuthoritative;
	const verifiedRunAge = runVerified === true ? snapshotAge(runVerifiedGeneratedAt) : "";
	const sourceMessages = [];
	if (!initialized) sourceMessages.push("Updating attention items…");
	else {
		if (runStale) sourceMessages.push(runVerified ? "Run attention is temporarily stale. Loaded run items may be out of date; this source was verified previously." : "Run attention is temporarily unavailable. Any loaded run items are unverified.");
		if (permissionsStale) sourceMessages.push(permissionsVerified ? "Assistant approvals are temporarily stale. Loaded approval items may be out of date; this source was verified previously." : "Assistant approvals are temporarily unavailable. Any loaded approval items are unverified.");
		if (partial) sourceMessages.push("Some run logs could not be inspected, so loaded run-attention data may be incomplete.");
		if (!feedVerified && verifiedRunAge) {
			sourceMessages.push(`Run attention was last fully verified ${verifiedRunAge}.`);
		}
		if (!feedVerified && !verified) sourceMessages.push(NO_COMPLETE_ATTENTION_SNAPSHOT);
		if (hasMore && truncated) sourceMessages.push("More older attention items are available below.");
	}
	const notificationsEnabled = preferences.valid && preferences.state.enabled;
	const enableBlocked = notificationBusy || !preferences.available || capability === "unsupported" || capability === "denied" || capability === "locks-unavailable";
	const unreadPhrase = unreadCount === 1 ? "1 unread item" : `${unreadCount} unread items`;
	const loadedUnreadPhrase = unreadCount > 0 ? `${unreadCount} unread loaded ${itemWord(unreadCount)}` : "unread count incomplete";
	const actionAria = actionCountExact ? activeActionCount > 0 ? actionPhrase : "no items need action" : activeActionCount > 0 ? `${uncertainActionPhrase}; current action total is unavailable` : "current action total is unavailable; no action cards are loaded";
	const unreadAria = unreadComplete ? unreadCount > 0 ? unreadPhrase : "no unread items" : unreadCount > 0 ? `at least ${unreadCount} loaded ${itemWord(unreadCount)} ${unreadCount === 1 ? "is" : "are"} unread` : "no unread items are loaded; unread count is incomplete";
	const countAria = `${actionAria}; ${unreadAria}`;
	const triggerLabel = !initialized ? "Open attention center. Checking for updates." : feedVerified ? `Open attention center, ${countAria}.` : verified ? `Open attention center. Current status unavailable. ${countAria}.` : `Open attention center. Current status unavailable. No complete verified snapshot. ${countAria}.`;
	const badgeShowsActions = activeActionCount > 0;
	const showBadge = badgeShowsActions || unreadCount > 0 || unreadPaginationIncomplete;
	const badge = badgeShowsActions ? `!${visualCount(activeActionCount)}${actionCountExact ? "" : "?"}` : unreadCount > 0 ? visualCount(unreadCount, unreadPaginationIncomplete) : "?";
	const indicator = {
		active: true,
		ariaLabel: triggerLabel,
		badge,
		showBadge,
		tone: badgeShowsActions ? "action" : showBadge ? "unread" : "neutral",
		verified: feedVerified
	};
	const headerStatus = !initialized ? "Checking for updates" : !feedVerified ? "Current status unavailable" : activeActionCount > 0 ? unreadCount > 0 ? `${actionPhrase} · ${unreadComplete ? unreadPhrase : loadedUnreadPhrase}` : unreadComplete ? stillActionPhrase : `${stillActionPhrase} · unread count incomplete` : unreadCount > 0 ? unreadComplete ? unreadPhrase : loadedUnreadPhrase : unreadComplete ? "You are caught up" : "Unread count incomplete";
	const actionEmptyCopy = !initialized ? "Checking for items that need action…" : feedVerified ? activeActionCount > 0 ? `No action cards are shown yet, but ${actionPhrase} in total. Load older items to review them.` : "Nothing needs your action right now." : activeActionCount > 0 ? `No action cards are shown; ${uncertainActionPhrase}. Retry to verify the current list.` : verified ? "No action cards are shown. Current action status is unavailable; both sources were verified previously." : `No action cards are shown. Current action status is unavailable. ${NO_COMPLETE_ATTENTION_SNAPSHOT}`;
	const recentEmptyCopy = !initialized ? "Checking recent run notices…" : feedVerified ? truncated ? "No recent notices are shown yet. Load older items to continue." : "No recent completion or budget notices are shown." : verified ? "No recent notices are shown. Current notice status is unavailable; both sources were verified previously." : `No recent notices are shown. Current notice status is unavailable. ${NO_COMPLETE_ATTENTION_SNAPSHOT}`;
	useLayoutEffect(() => {
		const owner = indicatorPublisherRef.current;
		claimAttentionIndicatorPublisher(owner);
		return () => releaseAttentionIndicatorPublisher(owner);
	}, []);
	useLayoutEffect(() => {
		publishAttentionIndicator(indicatorPublisherRef.current, indicator);
	}, [
		badge,
		badgeShowsActions,
		feedVerified,
		showBadge,
		triggerLabel
	]);
	return /* @__PURE__ */ _jsxs(_Fragment, { children: [
		/* @__PURE__ */ _jsx(AttentionLauncher, {
			indicator,
			expanded: open,
			controls: drawerId,
			onClick: () => setOpen((value) => !value)
		}),
		/* @__PURE__ */ _jsx("div", {
			className: "sr-only",
			role: "status",
			"aria-live": "polite",
			"aria-atomic": "true",
			children: liveMessage.text && /* @__PURE__ */ _jsx("span", { children: liveMessage.text }, liveMessage.revision)
		}),
		open && /* @__PURE__ */ _jsxs("div", {
			className: "attention-layer",
			children: [/* @__PURE__ */ _jsx("div", {
				className: "attention-backdrop",
				"aria-hidden": "true",
				onMouseDown: close
			}), /* @__PURE__ */ _jsxs("section", {
				ref: dialogRef,
				id: drawerId,
				className: "attention-drawer",
				role: "dialog",
				"aria-modal": "true",
				"aria-labelledby": titleId,
				"aria-describedby": descriptionId,
				tabIndex: -1,
				children: [/* @__PURE__ */ _jsxs("header", {
					className: "attention-header",
					children: [
						/* @__PURE__ */ _jsxs("div", {
							className: "attention-title-wrap",
							children: [/* @__PURE__ */ _jsx("h2", {
								id: titleId,
								children: "Attention center"
							}), /* @__PURE__ */ _jsx("p", {
								id: descriptionId,
								children: headerStatus
							})]
						}),
						unreadCount > 0 && /* @__PURE__ */ _jsx("button", {
							type: "button",
							className: "attention-header-action",
							"aria-label": `${unreadComplete ? "Mark all" : "Mark"} ${unreadCount} ${unreadComplete ? "unread" : "loaded unread"} ${itemWord(unreadCount)} as read${activeActionCount > 0 ? `; ${actionCountExact ? stillActionPhrase : `${uncertainActionPhrase}; current action total is unavailable`}` : ""}`,
							onClick: markAllRead,
							children: unreadComplete ? "Mark all read" : "Mark loaded read"
						}),
						/* @__PURE__ */ _jsx("button", {
							type: "button",
							className: "attention-close",
							"aria-label": "Close attention center",
							"data-dialog-initial-focus": true,
							onClick: close,
							children: /* @__PURE__ */ _jsx(OpIcon, {
								name: "cross",
								size: 20
							})
						})
					]
				}), /* @__PURE__ */ _jsxs("div", {
					className: "attention-scroll",
					children: [
						/* @__PURE__ */ _jsxs("nav", {
							className: "attention-jump-nav",
							"aria-label": "Attention sections",
							children: [/* @__PURE__ */ _jsxs("button", {
								type: "button",
								className: "attention-jump",
								"aria-label": `Jump to Needs action, ${actionAria}`,
								onClick: () => jumpToSection("action"),
								children: [/* @__PURE__ */ _jsx("span", { children: "Needs action" }), /* @__PURE__ */ _jsx("span", {
									className: "attention-jump-count",
									"aria-hidden": "true",
									children: actionCountExact ? visualCount(activeActionCount) : activeActionCount > 99 ? "99+?" : `${activeActionCount}?`
								})]
							}), /* @__PURE__ */ _jsxs("button", {
								type: "button",
								className: "attention-jump",
								"aria-label": `Jump to Recent, ${recentItems.length} loaded`,
								onClick: () => jumpToSection("recent"),
								children: [/* @__PURE__ */ _jsx("span", { children: "Recent" }), /* @__PURE__ */ _jsx("span", {
									className: "attention-jump-count",
									"aria-hidden": "true",
									children: visualCount(recentItems.length)
								})]
							})]
						}),
						sourceMessages.length > 0 && /* @__PURE__ */ _jsxs("div", {
							className: "attention-source-status",
							children: [/* @__PURE__ */ _jsx("div", {
								role: "status",
								"aria-live": "polite",
								children: /* @__PURE__ */ _jsx("ul", { children: sourceMessages.map((message) => /* @__PURE__ */ _jsx("li", { children: message }, message)) })
							}), !feedVerified && /* @__PURE__ */ _jsx("button", {
								type: "button",
								className: "attention-button subtle",
								onClick: () => refresh?.(),
								children: "Retry now"
							})]
						}),
						/* @__PURE__ */ _jsxs("section", {
							className: "attention-section",
							"aria-labelledby": `${titleId}-action`,
							children: [/* @__PURE__ */ _jsxs("div", {
								className: "attention-section-heading",
								children: [/* @__PURE__ */ _jsx("h3", {
									ref: actionHeadingRef,
									id: `${titleId}-action`,
									tabIndex: -1,
									children: "Needs action"
								}), /* @__PURE__ */ _jsxs("span", {
									className: `attention-section-count${activeActionCount > 0 ? " has-action" : ""}`,
									children: [/* @__PURE__ */ _jsx("span", {
										"aria-hidden": "true",
										children: actionCountExact ? activeActionCount : activeActionCount > 99 ? "99+?" : `${activeActionCount}?`
									}), /* @__PURE__ */ _jsx("span", {
										className: "sr-only",
										children: actionCountExact ? `${actionPhrase} in total` : activeActionCount > 0 ? `${uncertainActionPhrase}. Current total unavailable.` : "No action cards are loaded. Current total unavailable."
									})]
								})]
							}), actionItems.length ? /* @__PURE__ */ _jsx("ul", {
								className: "attention-list",
								children: actionItems.map((item) => /* @__PURE__ */ _jsx(AttentionItem, {
									item,
									unread: !acknowledgedIds.has(item.id),
									sourceStale: item.source === "run" ? runStale : permissionsStale,
									onOpenRun: openRun,
									onMarkRead: markRead,
									onDismiss: dismiss,
									onOpenPermission: openPermission
								}, item.id))
							}) : /* @__PURE__ */ _jsx("p", {
								className: "attention-empty",
								children: actionEmptyCopy
							})]
						}),
						/* @__PURE__ */ _jsxs("section", {
							className: "attention-notifications",
							"aria-labelledby": `${titleId}-notifications`,
							children: [/* @__PURE__ */ _jsxs("div", { children: [/* @__PURE__ */ _jsx("h3", {
								id: `${titleId}-notifications`,
								children: "Desktop notifications"
							}), /* @__PURE__ */ _jsx("p", { children: capabilityCopy(capability, preferences) })] }), notificationsEnabled ? /* @__PURE__ */ _jsx("button", {
								type: "button",
								className: "attention-button subtle",
								disabled: notificationBusy,
								onClick: disableNotifications,
								children: "Disable"
							}) : /* @__PURE__ */ _jsx("button", {
								type: "button",
								className: "attention-button",
								disabled: enableBlocked,
								onClick: enableNotifications,
								children: notificationBusy ? "Enabling…" : "Enable"
							})]
						}),
						notificationFeedback && /* @__PURE__ */ _jsx("p", {
							className: "attention-feedback",
							role: "status",
							children: notificationFeedback
						}),
						/* @__PURE__ */ _jsxs("section", {
							className: "attention-section",
							"aria-labelledby": `${titleId}-recent`,
							children: [/* @__PURE__ */ _jsxs("div", {
								className: "attention-section-heading",
								children: [/* @__PURE__ */ _jsx("h3", {
									ref: recentHeadingRef,
									id: `${titleId}-recent`,
									tabIndex: -1,
									children: "Recent"
								}), /* @__PURE__ */ _jsxs("span", {
									className: "attention-section-count",
									children: [/* @__PURE__ */ _jsxs("span", {
										"aria-hidden": "true",
										children: [recentItems.length, " loaded"]
									}), /* @__PURE__ */ _jsxs("span", {
										className: "sr-only",
										children: [
											recentItems.length,
											" recent ",
											itemWord(recentItems.length),
											" loaded"
										]
									})]
								})]
							}), recentItems.length ? /* @__PURE__ */ _jsx("ul", {
								className: "attention-list",
								children: recentItems.map((item) => /* @__PURE__ */ _jsx(AttentionItem, {
									item,
									unread: !acknowledgedIds.has(item.id),
									sourceStale: item.source === "run" ? runStale : permissionsStale,
									onOpenRun: openRun,
									onMarkRead: markRead,
									onDismiss: dismiss,
									onOpenPermission: openPermission
								}, item.id))
							}) : /* @__PURE__ */ _jsx("p", {
								className: "attention-empty",
								children: recentEmptyCopy
							})]
						}),
						hasMore && /* @__PURE__ */ _jsx("div", {
							className: "attention-load-more",
							children: /* @__PURE__ */ _jsx("button", {
								type: "button",
								className: "attention-button",
								disabled: loadingMore,
								onClick: loadMore,
								children: loadingMore ? "Loading…" : "Load older items"
							})
						}),
						loadMoreError && /* @__PURE__ */ _jsx("p", {
							className: "attention-feedback",
							role: "status",
							children: loadMoreError
						}),
						initialized && items.length > 0 && visibleItems.length === 0 && /* @__PURE__ */ _jsx("p", {
							className: "attention-all-dismissed",
							children: feedVerified ? truncated ? "All loaded items are dismissed. Load older items to continue." : "All current items are dismissed. New IDs will appear here normally." : verified ? "All loaded items are dismissed. Current status is unavailable; retry to verify the current list." : `All loaded items are dismissed. ${NO_COMPLETE_ATTENTION_SNAPSHOT} Retry to check the current state.`
						})
					]
				})]
			})]
		})
	] });
}
