import React, { Suspense, useEffect, useRef } from "react";
import { useDialogFocus } from "./useDialogFocus.mjs";
import { jsx as _jsx, Fragment as _Fragment, jsxs as _jsxs } from "react/jsx-runtime";
const reloadPage = () => window.location.reload();
function LoadSurface({ label, mode, failed = false, onReload = reloadPage, onClose }) {
	const surfaceRef = useRef(null);
	const reloadRef = useRef(null);
	useDialogFocus(surfaceRef, onClose, mode === "overlay");
	useEffect(() => {
		if (mode === "overlay" || mode === "inline" && !failed) return undefined;
		const frame = requestAnimationFrame(() => {
			const target = failed ? reloadRef.current : surfaceRef.current;
			target?.focus({ preventScroll: true });
		});
		return () => cancelAnimationFrame(frame);
	}, [failed, mode]);
	const body = /* @__PURE__ */ _jsxs(_Fragment, { children: [
		mode === "route" && /* @__PURE__ */ _jsx("h1", { children: failed ? `${label} unavailable` : `Opening ${label}…` }),
		mode !== "route" && /* @__PURE__ */ _jsx("b", { children: failed ? `${label} could not be opened.` : `Loading ${label}…` }),
		failed ? /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("p", { children: "This section failed while loading or rendering. Reload LoopLab to fetch a consistent build and retry." }), /* @__PURE__ */ _jsx("button", {
			ref: reloadRef,
			type: "button",
			className: "btn primary",
			onClick: onReload,
			children: "Reload LoopLab"
		})] }) : mode === "route" && /* @__PURE__ */ _jsx("p", { children: "The rest of the application remains available while this route downloads." })
	] });
	if (mode === "route") return /* @__PURE__ */ _jsx("main", {
		ref: surfaceRef,
		className: "auth-gate lazy-route-state",
		"data-route-main": true,
		tabIndex: -1,
		role: failed ? "alert" : "status",
		"aria-live": failed ? "assertive" : "polite",
		children: /* @__PURE__ */ _jsx("div", {
			className: "auth-card",
			children: body
		})
	});
	if (mode === "overlay") return /* @__PURE__ */ _jsx("div", {
		className: "overlay lazy-overlay-state",
		children: /* @__PURE__ */ _jsx("div", {
			ref: surfaceRef,
			className: "panel",
			role: failed ? "alertdialog" : "dialog",
			"aria-modal": "true",
			"aria-label": `${failed ? "Load failure" : "Loading"}: ${label}`,
			tabIndex: -1,
			children: /* @__PURE__ */ _jsxs("div", {
				className: "panel-b lazy-load-state",
				children: [/* @__PURE__ */ _jsx("button", {
					className: "btn sm ghost",
					onClick: onClose,
					children: "Close"
				}), body]
			})
		})
	});
	return /* @__PURE__ */ _jsx("div", {
		ref: surfaceRef,
		className: `notice lazy-load-state${failed ? " resource-error" : ""}`,
		role: failed ? "alert" : "status",
		"aria-live": failed ? "assertive" : "polite",
		children: body
	});
}
function LoadedFocus({ focusOnReady, children }) {
	useEffect(() => {
		if (!focusOnReady) return undefined;
		const frame = requestAnimationFrame(() => {
			if (!document.querySelector("[aria-modal=\"true\"]")) {
				document.querySelector("[data-route-main]")?.focus({ preventScroll: true });
			}
		});
		return () => cancelAnimationFrame(frame);
	}, [focusOnReady]);
	return children;
}
class LoadErrorBoundary extends React.Component {
	constructor(props) {
		super(props);
		this.state = { error: null };
	}
	static getDerivedStateFromError(error) {
		return { error };
	}
	componentDidUpdate(previous) {
		if (previous.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
	}
	render() {
		if (this.state.error) return /* @__PURE__ */ _jsx(LoadSurface, {
			label: this.props.label,
			mode: this.props.mode,
			failed: true,
			onReload: this.props.onReload,
			onClose: this.props.onClose
		});
		return this.props.children;
	}
}
/** A local Suspense + error boundary. A failed chunk never blanks the surrounding route. */
export default function LazyBoundary({ label, children, mode = "inline", focusOnReady = false, resetKey = label, onReload = reloadPage, onClose }) {
	return /* @__PURE__ */ _jsx(LoadErrorBoundary, {
		label,
		mode,
		resetKey,
		onReload,
		onClose,
		children: /* @__PURE__ */ _jsx(Suspense, {
			fallback: /* @__PURE__ */ _jsx(LoadSurface, {
				label,
				mode,
				onReload,
				onClose
			}),
			children: /* @__PURE__ */ _jsx(LoadedFocus, {
				focusOnReady,
				children
			})
		})
	});
}
