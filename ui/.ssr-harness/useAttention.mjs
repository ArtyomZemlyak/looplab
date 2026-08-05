import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { assistantPermissions, attentionFeed } from "./api.mjs";
import { deadlineRequest } from "./requestDeadline.mjs";
import { normalizePermissionAttention, normalizeRunAttention, sortAttentionItems } from "./attentionModel.mjs";
const SNAPSHOT_RE = /^[0-9a-f]{64}$/;
const ATTENTION_REQUEST_TIMEOUT_MS = 8e3;
const RUN_POLL_MS = 8e3;
const PERMISSION_POLL_MS = 4e3;
const MAX_BACKOFF_MS = 6e4;
const sourceStaleItem = (item) => item?.stale && item?.notifyEligible === false ? item : {
	...item,
	stale: true,
	notifyEligible: false
};
const sourceUnverifiedItem = (item) => item?.notifyEligible === false ? item : {
	...item,
	notifyEligible: false
};
const sourceStalePages = (pages) => pages.map((page) => page.map(sourceStaleItem));
// A stale cursor is detected two ways — the server answers the older-page request with a 409, or the
// feed moved under us between issuing the request and applying it — and both mean exactly the same
// thing: the archival pages are invalid, the first page is no longer known-fresh, and only a refresh
// can recover. They used to be handled by two near-identical inline blocks that disagreed on the two
// things the OPERATOR sees: one showed "the list changed, try again" and never refreshed, the other
// refreshed and said nothing at all. Since `hasMore` is false while stale, the first spelling told
// the user to retry using a button it had just removed, and left recovery to the next poll tick.
const STALE_CURSOR_MESSAGE = "The attention list changed before the next page loaded. Try again.";
const staleCursorState = (previous) => ({
	...previous,
	runPages: sourceStalePages(previous.runPages.slice(0, 1)),
	nextCursor: previous.firstNextCursor,
	truncated: !!previous.firstNextCursor,
	runStale: true,
	loadingMore: false,
	loadMoreError: STALE_CURSOR_MESSAGE
});
function normalizeRunPage(payload) {
	if (!payload || typeof payload !== "object" || Array.isArray(payload) || !Array.isArray(payload.items) || !SNAPSHOT_RE.test(payload.snapshot_id || "") || typeof payload.generated_at !== "number" || !Number.isFinite(payload.generated_at) || payload.generated_at <= 0 || typeof payload.stale !== "boolean" || typeof payload.truncated !== "boolean" || typeof payload.partial !== "boolean" || !Number.isSafeInteger(payload.active_action_count) || payload.active_action_count < 0) return null;
	const normalized = payload.items.map(normalizeRunAttention);
	// A protocol-invalid item must not turn an actionable last-safe snapshot into an
	// authoritative empty page. Treat the whole source read as stale and retry it.
	if (normalized.some((item) => item == null)) return null;
	const pageActiveActionCount = normalized.reduce((count, item) => count + (item.needsAction && item.active ? 1 : 0), 0);
	if (payload.active_action_count < pageActiveActionCount) return null;
	const nextCursor = typeof payload.next_cursor === "string" && SNAPSHOT_RE.test(payload.next_cursor) ? payload.next_cursor : null;
	if (payload.truncated && !nextCursor || !payload.truncated && payload.next_cursor != null) return null;
	const items = payload.stale ? normalized.map(sourceStaleItem) : payload.partial ? normalized.map(sourceUnverifiedItem) : normalized;
	return {
		items,
		truncated: payload.truncated,
		nextCursor,
		partial: payload.partial,
		stale: payload.stale,
		snapshotId: payload.snapshot_id,
		generatedAt: payload.generated_at,
		activeActionCount: payload.active_action_count
	};
}
function normalizePermissionPage(payload, now) {
	if (!payload || !Array.isArray(payload.pending)) return null;
	const items = [];
	for (const raw of payload.pending) {
		const item = normalizePermissionAttention(raw, now);
		if (item) {
			items.push(item);
			continue;
		}
		// Expiry is an authoritative removal, not a malformed response. Other invalid
		// records make the independent permission source stale so the prior safe list stays.
		const expired = raw && typeof raw === "object" && !Array.isArray(raw) && typeof raw.expires_at === "number" && Number.isFinite(raw.expires_at) && raw.expires_at > 0 && raw.expires_at * 1e3 <= now;
		if (!expired) return null;
	}
	return items;
}
const hiddenDocument = () => typeof document !== "undefined" && document.hidden;
function retryDelay(baseMs, streak, retryAfterMs = 0) {
	const nominal = Math.min(MAX_BACKOFF_MS, baseMs * 2 ** Math.max(0, streak - 1));
	const jitterRoom = Math.max(0, Math.min(nominal * .2, MAX_BACKOFF_MS - nominal));
	const jittered = nominal + Math.random() * jitterRoom;
	return Math.min(MAX_BACKOFF_MS, Math.max(jittered, Number(retryAfterMs) || 0));
}
// Schedule the next read only after the previous browser request settles. Hidden tabs finish their
// current read but do not start another; becoming visible triggers one immediate catch-up read.
// `settle` returns true for a valid, non-stale source response. A partial run read keeps the normal
// recovery cadence but is still non-authoritative and notification-ineligible; invalid and stale
// responses retain the last safe rows and advance the independent bounded backoff.
function useSerializedAttentionPoll(read, settle, baseMs, refreshToken) {
	useEffect(() => {
		let active = true;
		let timer = null;
		let request = null;
		let running = false;
		let degradedStreak = 0;
		const clearTimer = () => {
			if (timer != null) clearTimeout(timer);
			timer = null;
		};
		const schedule = (delay) => {
			if (!active) return;
			clearTimer();
			if (hiddenDocument()) return;
			timer = setTimeout(() => {
				void tick();
			}, delay);
		};
		const tick = async () => {
			if (!active || running) return;
			if (hiddenDocument()) return;
			running = true;
			request = deadlineRequest(read, ATTENTION_REQUEST_TIMEOUT_MS);
			let fresh = false;
			let retryAfterMs = 0;
			try {
				const payload = await request.promise;
				if (!active) return;
				fresh = settle(payload) === true;
			} catch (error) {
				if (!active) return;
				retryAfterMs = Number(error?.retryAfterMs) || 0;
				settle();
			} finally {
				request = null;
				running = false;
			}
			if (!active) return;
			degradedStreak = fresh ? 0 : degradedStreak + 1;
			schedule(fresh ? baseMs : retryDelay(baseMs, degradedStreak, retryAfterMs));
		};
		const onVisibility = () => {
			if (!active || hiddenDocument() || running) return;
			clearTimer();
			void tick();
		};
		if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVisibility);
		if (!hiddenDocument()) void tick();
		return () => {
			active = false;
			clearTimer();
			request?.controller.abort();
			if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVisibility);
		};
	}, [baseMs, refreshToken]);
}
export function useAttention({ intervalMs = RUN_POLL_MS } = {}) {
	const runPollMs = Number.isFinite(intervalMs) && intervalMs > 0 ? intervalMs : RUN_POLL_MS;
	const [state, setState] = useState({
		runPages: [],
		permissions: [],
		runAttempted: false,
		permissionsAttempted: false,
		runVerified: false,
		permissionsVerified: false,
		runStale: false,
		permissionsStale: false,
		partial: false,
		truncated: false,
		runSnapshotId: null,
		runGeneratedAt: null,
		runVerifiedGeneratedAt: null,
		runActiveActionCount: null,
		nextCursor: null,
		firstNextCursor: null,
		loadingMore: false,
		loadMoreError: ""
	});
	const [refreshToken, setRefreshToken] = useState(0);
	const loadMoreRequestRef = useRef(null);
	const runSnapshotRef = useRef(null);
	const runActiveActionCountRef = useRef({
		snapshotId: null,
		count: null
	});
	const cancelLoadMore = useCallback(() => {
		const request = loadMoreRequestRef.current;
		loadMoreRequestRef.current = null;
		request?.controller.abort();
	}, []);
	useSerializedAttentionPoll((signal) => attentionFeed(200, null, { signal }), (payload) => {
		const firstPage = normalizeRunPage(payload);
		if (!firstPage) {
			cancelLoadMore();
			setState((previous) => ({
				...previous,
				runPages: sourceStalePages(previous.runPages),
				runAttempted: true,
				runStale: true,
				loadingMore: false
			}));
			return false;
		}
		const snapshotChanged = runSnapshotRef.current != null && runSnapshotRef.current !== firstPage.snapshotId;
		const priorCount = runActiveActionCountRef.current;
		const countMismatch = priorCount.snapshotId === firstPage.snapshotId && priorCount.count !== firstPage.activeActionCount;
		if (countMismatch) {
			cancelLoadMore();
			setState((previous) => ({
				...previous,
				runPages: sourceStalePages(previous.runPages),
				runAttempted: true,
				runStale: true,
				loadingMore: false
			}));
			return false;
		}
		const archiveInvalidated = snapshotChanged || firstPage.stale || firstPage.partial;
		const completeFresh = !firstPage.stale && !firstPage.partial;
		if (archiveInvalidated) cancelLoadMore();
		runSnapshotRef.current = firstPage.snapshotId;
		if (completeFresh) {
			runActiveActionCountRef.current = {
				snapshotId: firstPage.snapshotId,
				count: firstPage.activeActionCount
			};
		}
		setState((previous) => {
			const preserveArchive = !snapshotChanged && previous.runPages.length > 1 && !previous.runStale && !previous.partial && !firstPage.stale && !firstPage.partial;
			const preserveStaleArchive = !snapshotChanged && previous.runPages.length > 1 && firstPage.stale;
			let runPages;
			let nextCursor;
			let truncated;
			if (preserveArchive) {
				runPages = [firstPage.items, ...previous.runPages.slice(1)];
				nextCursor = previous.nextCursor;
				truncated = previous.truncated;
			} else if (preserveStaleArchive) {
				runPages = sourceStalePages([firstPage.items, ...previous.runPages.slice(1)]);
				nextCursor = previous.nextCursor;
				truncated = previous.truncated;
			} else {
				runPages = [firstPage.items];
				nextCursor = firstPage.nextCursor;
				truncated = firstPage.truncated;
			}
			return {
				...previous,
				runPages,
				nextCursor,
				firstNextCursor: firstPage.nextCursor,
				truncated,
				// A background poll must not wipe a load-more failure the operator has not acted on — that
				// message means "your last click failed", and polling the FIRST page proves nothing about
				// the older one. The stale-cursor message is the exception: it says "try again", and a
				// complete fresh first page is exactly the moment trying again became possible, so it is
				// cleared on recovery rather than waiting for a snapshot change that may never come (the
				// client-side detection fires on cursor/count drift within one snapshot too).
				loadMoreError: snapshotChanged || completeFresh && previous.loadMoreError === STALE_CURSOR_MESSAGE ? "" : previous.loadMoreError,
				loadingMore: archiveInvalidated ? false : previous.loadingMore,
				runAttempted: true,
				runVerified: previous.runVerified || completeFresh,
				runStale: firstPage.stale,
				partial: firstPage.partial,
				runSnapshotId: firstPage.snapshotId,
				runGeneratedAt: firstPage.generatedAt,
				runActiveActionCount: completeFresh ? firstPage.activeActionCount : previous.runActiveActionCount,
				runVerifiedGeneratedAt: completeFresh ? firstPage.generatedAt : previous.runVerifiedGeneratedAt
			};
		});
		return !firstPage.stale;
	}, runPollMs, refreshToken);
	useSerializedAttentionPoll((signal) => assistantPermissions(null, { signal }), (payload) => {
		const now = Date.now();
		const permissionPage = normalizePermissionPage(payload, now);
		setState((previous) => ({
			...previous,
			// Drop raw action/scope/preview immediately. Assistant re-reads the authoritative card.
			permissions: permissionPage != null ? permissionPage : previous.permissions.filter((item) => !item.expiresAt || item.expiresAt * 1e3 > now).map(sourceStaleItem),
			permissionsAttempted: true,
			permissionsVerified: previous.permissionsVerified || permissionPage != null,
			permissionsStale: permissionPage == null
		}));
		return permissionPage != null;
	}, PERMISSION_POLL_MS, refreshToken);
	useEffect(() => () => cancelLoadMore(), [cancelLoadMore]);
	const loadMore = useCallback(async () => {
		const cursor = state.nextCursor;
		const snapshotId = state.runSnapshotId;
		const activeActionCount = state.runActiveActionCount;
		if (!cursor || !snapshotId || !Number.isSafeInteger(activeActionCount) || activeActionCount < 0 || state.runStale || state.partial || loadMoreRequestRef.current) return;
		setState((previous) => ({
			...previous,
			loadingMore: true,
			loadMoreError: ""
		}));
		const request = deadlineRequest((signal) => attentionFeed(200, cursor, { signal }), ATTENTION_REQUEST_TIMEOUT_MS);
		loadMoreRequestRef.current = request;
		try {
			const page = normalizeRunPage(await request.promise);
			if (loadMoreRequestRef.current !== request) return;
			if (!page) throw new Error("invalid attention page");
			if (page.snapshotId !== snapshotId || page.activeActionCount !== activeActionCount) {
				const mismatch = new Error("attention snapshot changed");
				mismatch.status = 409;
				throw mismatch;
			}
			setState((previous) => {
				if (previous.runStale || previous.partial || previous.runSnapshotId !== snapshotId || previous.runActiveActionCount !== activeActionCount || previous.nextCursor !== cursor) {
					return staleCursorState(previous);
				}
				const runPages = [...previous.runPages, page.items];
				return {
					...previous,
					runPages: page.stale ? sourceStalePages(runPages) : runPages,
					nextCursor: page.nextCursor,
					truncated: page.truncated,
					partial: previous.partial || page.partial,
					runStale: page.stale,
					loadingMore: false,
					loadMoreError: ""
				};
			});
		} catch (error) {
			if (loadMoreRequestRef.current !== request) return;
			setState((previous) => error?.status === 409 ? staleCursorState(previous) : {
				...previous,
				loadingMore: false,
				loadMoreError: "Older attention items could not be loaded. Try again."
			});
		} finally {
			if (loadMoreRequestRef.current === request) loadMoreRequestRef.current = null;
		}
	}, [
		state.nextCursor,
		state.partial,
		state.runActiveActionCount,
		state.runSnapshotId,
		state.runStale
	]);
	// Stale-cursor recovery is armed HERE, not at the two detection sites, for a correctness reason
	// rather than a tidiness one: a `setState` updater must be pure, and React runs it during a later
	// render phase — so a flag written inside the updater and read on the line after `setState` is not
	// reliably set, and the refresh could simply never be armed. Both detections converge on the same
	// OBSERVABLE state, so one effect covers them and cannot double-fire. It also cannot storm: if the
	// refresh itself fails the state does not change, the dep stays true, and recovery falls back to
	// the normal poll cadence.
	const staleCursorArmed = state.runStale && state.loadMoreError === STALE_CURSOR_MESSAGE;
	useEffect(() => {
		if (staleCursorArmed) setRefreshToken((value) => value + 1);
	}, [staleCursorArmed]);
	const refresh = useCallback(() => {
		cancelLoadMore();
		setState((previous) => ({
			...previous,
			loadingMore: false,
			loadMoreError: ""
		}));
		setRefreshToken((value) => value + 1);
	}, [cancelLoadMore]);
	const runs = useMemo(() => state.runPages.flat(), [state.runPages]);
	const currentItems = useMemo(() => sortAttentionItems([...state.runPages[0] || [], ...state.permissions]), [state.runPages, state.permissions]);
	const items = useMemo(() => sortAttentionItems([...runs, ...state.permissions]), [runs, state.permissions]);
	const attempted = state.runAttempted && state.permissionsAttempted;
	const verified = state.runVerified && state.permissionsVerified;
	const authoritative = verified && !state.runStale && !state.permissionsStale && !state.partial;
	const runSnapshotAgeMs = state.runGeneratedAt == null ? null : Math.max(0, Date.now() - state.runGeneratedAt * 1e3);
	return {
		...state,
		initialized: attempted,
		attempted,
		verified,
		authoritative,
		runs,
		items,
		currentItems,
		runSnapshotAgeMs,
		hasMore: !state.runStale && !state.partial && state.truncated && !!state.nextCursor,
		loadMore,
		refresh
	};
}
