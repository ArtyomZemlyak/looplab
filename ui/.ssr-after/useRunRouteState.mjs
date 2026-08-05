import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { emptyRunRouteState, hashWithRunRouteState, hrefWithRunRouteState, parseRunRouteState, reconcileRunRouteStateUpdate, sameRunRouteState, sanitizeRunRouteState } from "./runRouteState.mjs";
const browserLocation = () => typeof location === "undefined" ? {
	pathname: "",
	search: "",
	hash: ""
} : location;
export const RUN_PANEL_HISTORY_STATE_KEY = "__looplabRunPanelEntry";
let fallbackPanelHistoryEntry = null;
const ROUTE_WRITE_FAILED = "Browser navigation was unavailable, so the requested workspace change was not opened.";
const ROUTE_REPLACE_FAILED = "The workspace changed, but its address could not be updated. Back or reload may restore the previous view.";
const ROUTE_CANONICAL_FAILED = "This link opened, but the browser could not normalize its address.";
const appendIssue = (issues, issue) => issues.includes(issue) ? issues : [...issues, issue];
const browserHref = () => {
	const current = browserLocation();
	return `${current.pathname}${current.search}${current.hash}`;
};
const rememberFallbackPanelEntry = (href, previousHref) => {
	fallbackPanelHistoryEntry = {
		href,
		version: 1,
		previousHref
	};
};
export function takeRunPanelHistoryEntry() {
	if (typeof window === "undefined") return null;
	const nativeEntry = window.history.state?.[RUN_PANEL_HISTORY_STATE_KEY];
	if (nativeEntry?.version === 1) return nativeEntry;
	if (fallbackPanelHistoryEntry?.href !== browserHref()) return null;
	const entry = {
		version: fallbackPanelHistoryEntry.version,
		previousHref: fallbackPanelHistoryEntry.previousHref
	};
	// A Location-created entry cannot carry history state. Its marker is deliberately current-only
	// and one-shot so a later deep link with the same URL can never impersonate this history entry.
	fallbackPanelHistoryEntry = null;
	return entry;
}
function read(reviewMode) {
	const parsed = parseRunRouteState(browserLocation().hash, { reviewMode });
	return {
		...parsed,
		navigationRevision: 0
	};
}
export function useRunRouteState({ generation = null, reviewMode = false } = {}) {
	const [resource, setResource] = useState(() => read(reviewMode));
	const stateRef = useRef(resource.state);
	const hydratedHashRef = useRef(null);
	stateRef.current = resource.state;
	const write = useCallback((state, mode = "push", { forceGeneration = false, clearPanelHistory = false } = {}) => {
		if (typeof window === "undefined") return false;
		const previousHref = `${window.location.pathname}${window.location.search}${window.location.hash}`;
		const nextHash = hashWithRunRouteState(window.location.hash, state, {
			reviewMode,
			forceGeneration
		});
		if (nextHash === window.location.hash) return true;
		const href = `${window.location.pathname}${window.location.search}${nextHash}`;
		const currentHistoryState = window.history.state;
		const nextHistoryState = currentHistoryState && typeof currentHistoryState === "object" ? { ...currentHistoryState } : {};
		const fallbackPanelEntry = fallbackPanelHistoryEntry?.href === previousHref ? fallbackPanelHistoryEntry : null;
		if (fallbackPanelHistoryEntry && !fallbackPanelEntry) fallbackPanelHistoryEntry = null;
		if (clearPanelHistory || !state.panel) delete nextHistoryState[RUN_PANEL_HISTORY_STATE_KEY];
		else if (mode !== "replace") {
			nextHistoryState[RUN_PANEL_HISTORY_STATE_KEY] = {
				version: 1,
				previousHref
			};
		} else if (fallbackPanelEntry) {
			// A prior Location fallback could not attach state to the history entry. Promote its marker
			// into native history state as soon as a later replace succeeds, so Back-close remains durable.
			nextHistoryState[RUN_PANEL_HISTORY_STATE_KEY] = {
				version: fallbackPanelEntry.version,
				previousHref: fallbackPanelEntry.previousHref
			};
		}
		try {
			window.history[mode === "replace" ? "replaceState" : "pushState"](nextHistoryState, "", href);
			fallbackPanelHistoryEntry = null;
			hydratedHashRef.current = nextHash;
			return true;
		} catch {
			// WebKit throttles history writes (~100 per 30s). A same-document Location fallback still
			// creates/replaces the URL entry, so push navigation never has to commit a view that Back and
			// reload cannot reproduce. Location cannot carry our optional panel marker, so preserve that
			// marker only for the current navigation until a later History write can promote it.
			try {
				if (mode === "replace") window.location.replace(href);
				else window.location.hash = nextHash;
				if (window.location.hash !== nextHash) return false;
				if (mode !== "replace" && state.panel && !clearPanelHistory) {
					rememberFallbackPanelEntry(href, previousHref);
				} else if (mode === "replace" && fallbackPanelEntry && state.panel && !clearPanelHistory) {
					rememberFallbackPanelEntry(href, fallbackPanelEntry.previousHref);
				} else fallbackPanelHistoryEntry = null;
				hydratedHashRef.current = nextHash;
				return true;
			} catch {
				return false;
			}
		}
	}, [reviewMode]);
	const hydrate = useCallback(() => {
		const currentHref = browserHref();
		const fallbackPanelEntry = fallbackPanelHistoryEntry?.href === currentHref ? fallbackPanelHistoryEntry : null;
		// A fallback marker represents only the entry that was just created. Drop it as soon as the
		// browser moves elsewhere; URL equality alone must never identify a later history entry.
		if (fallbackPanelHistoryEntry && !fallbackPanelEntry) fallbackPanelHistoryEntry = null;
		if (hydratedHashRef.current === browserLocation().hash) return;
		const parsed = parseRunRouteState(browserLocation().hash, { reviewMode });
		// `gen=A` with otherwise-default state is still an explicit exact-view fence. Preserve it while
		// canonicalizing so refresh/recopy cannot silently turn a generation-bound link into a live alias.
		const canonical = hashWithRunRouteState(browserLocation().hash, parsed.state, {
			reviewMode,
			forceGeneration: !!parsed.state.generation
		});
		let canonicalFailed = false;
		if (typeof window !== "undefined" && canonical !== window.location.hash) {
			const href = `${window.location.pathname}${window.location.search}${canonical}`;
			try {
				const historyState = fallbackPanelEntry ? {
					...window.history.state || {},
					[RUN_PANEL_HISTORY_STATE_KEY]: {
						version: fallbackPanelEntry.version,
						previousHref: fallbackPanelEntry.previousHref
					}
				} : window.history.state;
				window.history.replaceState(historyState, "", href);
				fallbackPanelHistoryEntry = null;
			} catch {
				// Hydration is authoritative even when the address bar cannot be normalized. Never leave a
				// Back/Forward URL paired with the previous React state or throw into the route boundary.
				canonicalFailed = true;
			}
		}
		hydratedHashRef.current = canonicalFailed ? browserLocation().hash : canonical;
		stateRef.current = parsed.state;
		setResource((current) => ({
			...parsed,
			issues: canonicalFailed ? appendIssue(parsed.issues, ROUTE_CANONICAL_FAILED) : parsed.issues,
			navigationRevision: current.navigationRevision + 1
		}));
	}, [reviewMode]);
	useEffect(() => {
		hydrate();
		window.addEventListener("popstate", hydrate);
		window.addEventListener("hashchange", hydrate);
		return () => {
			window.removeEventListener("popstate", hydrate);
			window.removeEventListener("hashchange", hydrate);
		};
	}, [hydrate]);
	const mismatch = !!(resource.state.generation && generation && resource.state.generation !== generation);
	const pendingFence = !!(resource.state.generation && !generation);
	const update = useCallback((patch, { mode = "push", preserveIssues = false, forceGeneration = false } = {}) => {
		const current = stateRef.current;
		if (current.generation && generation && current.generation !== generation) return current;
		const raw = typeof patch === "function" ? patch(current) : {
			...current,
			...patch
		};
		const candidate = reconcileRunRouteStateUpdate(current, raw, {
			generation,
			reviewMode,
			forceGeneration
		});
		if (candidate === current || sameRunRouteState(current, candidate)) return current;
		const written = write(candidate, mode, { forceGeneration });
		// A push is a discrete user navigation. If neither History nor the fragment fallback can create
		// its entry, keep the old UI too; otherwise Back/reload would disagree with what is on screen.
		if (!written && mode !== "replace") {
			setResource((value) => ({
				...value,
				issues: appendIssue(value.issues, ROUTE_WRITE_FAILED)
			}));
			return current;
		}
		stateRef.current = candidate;
		setResource((value) => {
			const issues = preserveIssues ? value.issues : [];
			return {
				...value,
				state: candidate,
				issues: !written ? appendIssue(issues, ROUTE_REPLACE_FAILED) : issues
			};
		});
		return candidate;
	}, [
		generation,
		reviewMode,
		write
	]);
	const openCurrentGeneration = useCallback(({ mode = "push", panel = null } = {}) => {
		const state = {
			...emptyRunRouteState(),
			panel,
			generation: panel && generation ? generation : null
		};
		if (!write(state, mode, { clearPanelHistory: !!panel })) {
			setResource((value) => ({
				...value,
				issues: appendIssue(value.issues, ROUTE_WRITE_FAILED)
			}));
			return false;
		}
		stateRef.current = state;
		setResource((value) => ({
			...value,
			state,
			issues: []
		}));
		return true;
	}, [generation, write]);
	const clearIssues = useCallback(() => {
		setResource((value) => ({
			...value,
			issues: []
		}));
	}, []);
	const exactHref = useCallback((state = stateRef.current, { forReview = reviewMode } = {}) => {
		const exact = sanitizeRunRouteState({
			...state,
			generation: generation || state.generation
		}, { reviewMode: forReview });
		return hrefWithRunRouteState(browserLocation(), exact, {
			reviewMode: forReview,
			forceGeneration: true
		});
	}, [generation, reviewMode]);
	return useMemo(() => ({
		state: resource.state,
		issues: resource.issues,
		hadState: resource.hadState,
		navigationRevision: resource.navigationRevision,
		generationMismatch: mismatch,
		generationPending: pendingFence,
		update,
		openCurrentGeneration,
		clearIssues,
		exactHref
	}), [
		resource,
		mismatch,
		pendingFence,
		update,
		openCurrentGeneration,
		clearIssues,
		exactHref
	]);
}
