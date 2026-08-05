import React, { useEffect, useState } from "react";
import { FX_LEVELS, readFx, applyFx } from "./fx.mjs";
import { OpIcon } from "./icons.mjs";
import { useRovingRadioMenu } from "./accessibility.mjs";
import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
// Topbar control for the Energy / Reactor FX mode (Off / Subtle / Full). Mirrors ThemeSwitcher's
// popover so it reads as a sibling of the theme picker. Mounted in both the run-view and run-list
// topbars; only one is on screen at a time (separate routes), so they never fight over the level.
export default function EnergyToggle() {
	const [open, setOpen] = useState(false);
	const [level, setLevel] = useState(readFx);
	const { triggerRef, menuRef, close, onKeyDown } = useRovingRadioMenu(open, setOpen);
	useEffect(() => {
		applyFx(level);
	}, [level]);
	// stay in sync if some other surface flips the level (e.g. the other topbar, another tab)
	useEffect(() => {
		const on = (e) => setLevel(e && typeof e.detail === "string" ? e.detail : readFx());
		window.addEventListener("ll-fx", on);
		window.addEventListener("storage", on);
		return () => {
			window.removeEventListener("ll-fx", on);
			window.removeEventListener("storage", on);
		};
	}, []);
	const on = !!level;
	const cur = FX_LEVELS.find((l) => l.id === level) || FX_LEVELS[0];
	const pick = (id) => {
		setLevel(id);
		close(true);
	};
	return /* @__PURE__ */ _jsxs("div", {
		className: "fx-switch",
		children: [/* @__PURE__ */ _jsxs("button", {
			type: "button",
			ref: triggerRef,
			className: "btn sm ghost" + (on ? " primary" : ""),
			title: "Energy / Reactor FX — animated graph",
			"aria-haspopup": "menu",
			"aria-expanded": open,
			"aria-controls": "energy-switcher-menu",
			"aria-label": `Energy effects: ${cur.name}`,
			onClick: () => setOpen(!open),
			children: [/* @__PURE__ */ _jsx(OpIcon, {
				name: "bolt",
				size: 12
			}), /* @__PURE__ */ _jsxs("span", {
				className: "fx-switch-label",
				children: ["Energy", on ? `: ${cur.name}` : ""]
			})]
		}), open && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
			className: "th-backdrop",
			"aria-hidden": "true",
			onClick: () => close(true)
		}), /* @__PURE__ */ _jsxs("div", {
			ref: menuRef,
			id: "energy-switcher-menu",
			className: "th-menu fx-menu",
			role: "menu",
			"aria-label": "Energy effects",
			onKeyDown,
			onBlur: (event) => {
				if (!event.currentTarget.contains(event.relatedTarget)) close(false);
			},
			children: [/* @__PURE__ */ _jsx("div", {
				className: "th-menu-h",
				children: "Energy FX"
			}), FX_LEVELS.map((l) => /* @__PURE__ */ _jsxs("button", {
				type: "button",
				role: "menuitemradio",
				"aria-checked": l.id === level,
				tabIndex: -1,
				className: "th-opt" + (l.id === level ? " on" : ""),
				onClick: () => pick(l.id),
				children: [/* @__PURE__ */ _jsxs("span", {
					className: "th-name",
					children: [/* @__PURE__ */ _jsx("b", { children: l.name }), /* @__PURE__ */ _jsx("span", {
						className: "th-sub",
						children: l.sub
					})]
				}), l.id === level && /* @__PURE__ */ _jsx("span", {
					className: "th-check",
					children: "✓"
				})]
			}, l.id || "off"))]
		})] })]
	});
}
