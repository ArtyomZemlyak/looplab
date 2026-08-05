import { useEffect, useRef } from "react";
const dialogStack = [];
const isolatedBackground = new Map();
const highestPriorityLayer = (layers) => layers.reduce((top, candidate) => !top || candidate.priority >= top.priority ? candidate : top, null);
const restoreBackgroundIsolation = () => {
	for (const [node, saved] of isolatedBackground) {
		// React may deliberately change either attribute while the modal is open (for example when a
		// destructive child dialog closes). Do not overwrite that newer state if it already differs
		// from the values this helper applied.
		if (node.inert === true) node.inert = saved.inert;
		if (node.getAttribute("aria-hidden") === "true") {
			if (saved.ariaHidden == null) node.removeAttribute("aria-hidden");
			else node.setAttribute("aria-hidden", saved.ariaHidden);
		}
	}
	isolatedBackground.clear();
};
const isolateBackgroundFor = (root) => {
	if (!root?.isConnected) return;
	// Walk from the active dialog to <body>, disabling every sibling branch along that path. Unlike
	// making a lower dialog root inert, this also works for a child dialog rendered inside its parent:
	// the child remains interactive while the parent's other controls disappear from both keyboard
	// and virtual-cursor navigation.
	let branch = root;
	while (branch && branch !== document.body) {
		const parent = branch.parentElement;
		if (!parent) break;
		for (const sibling of parent.children) {
			if (sibling === branch || isolatedBackground.has(sibling)) continue;
			isolatedBackground.set(sibling, {
				inert: sibling.inert,
				ariaHidden: sibling.getAttribute("aria-hidden")
			});
			sibling.inert = true;
			sibling.setAttribute("aria-hidden", "true");
		}
		branch = parent;
	}
};
const syncDialogIsolation = () => {
	const topModal = highestPriorityLayer(dialogStack.filter((candidate) => candidate.modal));
	restoreBackgroundIsolation();
	isolateBackgroundFor(topModal?.ref.current);
};
// Keep keyboard ownership aligned with the effective visual layer order. Equal priorities retain
// the usual "last mounted wins" behaviour, while a late lower surface cannot steal focus from a
// modal that is visibly above it.
export const DIALOG_PRIORITY = Object.freeze({
	NONMODAL: 0,
	NAVIGATION: 10,
	ASSISTANT_SIDE: 20,
	ASSISTANT_FULL: 30,
	OVERLAY: 40,
	START_OVER: 50,
	DESTRUCTIVE: 60,
	ATTENTION: 70,
	ASSISTANT_DELETE: 80
});
export function useDialogFocus(ref, onClose, active = true, { modal = true, priority = DIALOG_PRIORITY.OVERLAY } = {}) {
	const closeRef = useRef(onClose);
	closeRef.current = onClose;
	useEffect(() => {
		if (!active) return;
		const previous = document.activeElement;
		const layer = {
			ref,
			modal,
			priority
		};
		dialogStack.push(layer);
		// True modals always outrank coexisting nonmodal surfaces, even when a route update mounts the
		// latter after the modal. Visual priority wins among peers; equal layers use mount order.
		const topmost = () => {
			const modalLayers = dialogStack.filter((candidate) => candidate.modal);
			const candidates = modalLayers.length ? modalLayers : dialogStack;
			const topLayer = highestPriorityLayer(candidates);
			return topLayer === layer;
		};
		const root = ref.current;
		let focusWasInside = !!root?.contains(document.activeElement);
		const selector = "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [contenteditable=\"true\"], [tabindex]:not([tabindex=\"-1\"])";
		// React's autoFocus runs during commit. Preserve it; otherwise focus the first real action, falling
		// back to the dialog container only when the dialog genuinely has no controls.
		const blockingModal = !modal && [...document.querySelectorAll("[aria-modal=\"true\"]")].some((surface) => surface !== root && !root?.contains(surface));
		if (root && topmost() && !blockingModal && !root.contains(document.activeElement)) {
			const initial = root.querySelector("[autofocus], [data-dialog-initial-focus]") || root.querySelector(selector);
			(initial || root).focus();
			// The tracking listener is attached below, so record our own programmatic initial focus now.
			focusWasInside = root.contains(document.activeElement);
		}
		// Move focus before applying aria-hidden so browsers never have to reject hiding the element
		// that still owns focus. The modal now exclusively owns both keyboard and accessibility trees.
		syncDialogIsolation();
		const onKey = (event) => {
			if (!topmost()) return;
			if (event.defaultPrevented) return;
			if (event.key === "Escape") {
				// A nonmodal surface must not consume Escape from an unrelated header/timeline control.
				if (!modal && !ref.current?.contains(document.activeElement)) return;
				event.preventDefault();
				closeRef.current?.();
				return;
			}
			if (!modal || event.key !== "Tab" || !ref.current) return;
			const focusable = [...ref.current.querySelectorAll(selector)];
			if (!focusable.length) {
				event.preventDefault();
				ref.current.focus();
				return;
			}
			const first = focusable[0], last = focusable[focusable.length - 1];
			if (!ref.current.contains(document.activeElement)) {
				event.preventDefault();
				(event.shiftKey ? last : first).focus();
			} else if (document.activeElement === ref.current) {
				event.preventDefault();
				(event.shiftKey ? last : first).focus();
			} else if (event.shiftKey && document.activeElement === first) {
				event.preventDefault();
				last.focus();
			} else if (!event.shiftKey && document.activeElement === last) {
				event.preventDefault();
				first.focus();
			}
		};
		const onFocusIn = (event) => {
			// Preserve the last meaningful location if removing the focused surface makes the browser
			// focus body after the node has already disconnected.
			if (root?.isConnected) focusWasInside = root.contains(event.target);
			if (!modal) return;
			if (!topmost() || !ref.current || ref.current.contains(event.target)) return;
			const initial = ref.current.querySelector(selector) || ref.current;
			initial.focus({ preventScroll: true });
		};
		window.addEventListener("keydown", onKey);
		document.addEventListener("focusin", onFocusIn);
		return () => {
			window.removeEventListener("keydown", onKey);
			document.removeEventListener("focusin", onFocusIn);
			const wasTopmost = topmost();
			const index = dialogStack.indexOf(layer);
			if (index >= 0) dialogStack.splice(index, 1);
			syncDialogIsolation();
			if (wasTopmost && previous && document.contains(previous) && (modal || focusWasInside)) {
				previous.focus({ preventScroll: true });
			}
		};
	}, [
		active,
		modal,
		priority
	]);
}
