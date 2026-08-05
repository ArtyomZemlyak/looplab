import { useCallback, useEffect, useRef, useState } from "react";
// One transient-notice timer for every surface that shows one (doc 25 UI-13). RunView, the Assistant
// bar and Settings each had their own, and the differences between them were not features — they were
// the two guards each copy happened to remember:
//
//   * RESET the pending timer on every new notice. Without it, a second toast inherits the first
//     one's remaining time and can vanish almost immediately. `toastTimerDiscipline.test.js` exists
//     because that bug is easy to reintroduce and invisible until an operator misses a message.
//   * Do not clear state after UNMOUNT. A 5-second timer outlives a closed drawer; firing into a
//     dead component is a React warning at best and, on a route change, a write into the wrong tree.
//
// Presentation stays at the call site — the three surfaces render different elements with different
// classes, and that difference is real. Only the timing discipline is shared.
export function useToast(ms = 5e3) {
	const [toast, setToast] = useState(null);
	const timer = useRef(null);
	const mounted = useRef(true);
	useEffect(() => () => {
		mounted.current = false;
		if (timer.current) clearTimeout(timer.current);
		timer.current = null;
	}, []);
	const show = useCallback((message) => {
		if (timer.current) clearTimeout(timer.current);
		setToast(message);
		timer.current = setTimeout(() => {
			if (mounted.current) setToast(null);
		}, ms);
	}, [ms]);
	// Retire a notice EARLY (a run change makes the previous surface's message misleading, not just
	// stale). Cancels the pending timer too, so a later `show` is not cut short by this one's timeout.
	const clearToast = useCallback(() => {
		if (timer.current) clearTimeout(timer.current);
		timer.current = null;
		setToast(null);
	}, []);
	return [
		toast,
		show,
		clearToast
	];
}
