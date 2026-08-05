import React, { useRef } from "react";
import { useDialogFocus } from "./useDialogFocus.mjs";
import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
/** Shared modal shell kept separate so a small public-safe panel need not download the owner hub. */
export default function PanelShell({ title, sub, onClose, children, wide }) {
	const dialogRef = useRef(null);
	useDialogFocus(dialogRef, onClose);
	return /* @__PURE__ */ _jsx("div", {
		className: "overlay",
		onMouseDown: (event) => {
			if (event.target === event.currentTarget) onClose?.();
		},
		children: /* @__PURE__ */ _jsxs("div", {
			ref: dialogRef,
			className: "panel",
			role: "dialog",
			"aria-modal": "true",
			"aria-label": title,
			tabIndex: -1,
			style: wide ? { width: "min(1100px, 95%)" } : {},
			children: [/* @__PURE__ */ _jsxs("div", {
				className: "panel-h",
				children: [
					/* @__PURE__ */ _jsx("span", {
						className: "ttl panel-title",
						children: title
					}),
					sub && /* @__PURE__ */ _jsx("span", {
						className: "pill panel-sub",
						title: typeof sub === "string" ? sub : undefined,
						children: sub
					}),
					/* @__PURE__ */ _jsx("span", { className: "right" }),
					/* @__PURE__ */ _jsx("button", {
						className: "btn sm ghost panel-close",
						"aria-label": `Close ${title}`,
						onClick: onClose,
						children: "✕"
					})
				]
			}), /* @__PURE__ */ _jsx("div", {
				className: "panel-b",
				children
			})]
		})
	});
}
