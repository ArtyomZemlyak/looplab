import React, { useEffect, useState } from "react";
import { useRovingRadioMenu } from "./accessibility.mjs";
import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
// Selectable design themes — each is a CSS-variable re-skin applied via <html data-theme>.
export const THEMES = [
	{
		id: "current",
		name: "Dark",
		sub: "default",
		bg: "#0c0e12",
		ac: "#4aa3ff",
		fg: "#e6e9ef"
	},
	{
		id: "retrowave",
		name: "Retro Wave",
		sub: "neon synthwave",
		bg: "#150c28",
		ac: "#ff2e97",
		fg: "#f6e9ff"
	},
	{
		id: "paper",
		name: "Paper",
		sub: "warm light",
		bg: "#f4f1ea",
		ac: "#346ba3",
		fg: "#2b2722"
	},
	{
		id: "white-scifi",
		name: "White Sci-Fi",
		sub: "clean HUD",
		bg: "#f6f9fd",
		ac: "#0a7d97",
		fg: "#0f1b2d"
	},
	{
		id: "old-green",
		name: "Old Green",
		sub: "phosphor CRT",
		bg: "#030a05",
		ac: "#5bff95",
		fg: "#3ff06d"
	},
	{
		id: "reactor",
		name: "Reactor",
		sub: "sci-fi · pair with Energy FX",
		bg: "#05060f",
		ac: "#36e6ff",
		fg: "#eaf2ff"
	}
];
const KEY = "ll.theme";
// Apply a theme to <html> (and persist). 'current' clears the attribute (the default :root palette).
export function applyTheme(id) {
	const root = document.documentElement;
	if (!id || id === "current") root.removeAttribute("data-theme");
	else root.setAttribute("data-theme", id);
	try {
		localStorage.setItem(KEY, id || "current");
	} catch {}
}
// Restore the theme on load — call once at app start. A `?theme=<id>` query param wins (handy for
// sharing / previewing a design via a link); otherwise the saved choice; otherwise the default.
export function initTheme() {
	let id = "current";
	try {
		const q = new URLSearchParams(location.search).get("theme");
		id = q && THEMES.some((t) => t.id === q) ? q : localStorage.getItem(KEY) || "current";
	} catch {}
	applyTheme(id);
	return id;
}
export default function ThemeSwitcher() {
	const [open, setOpen] = useState(false);
	const { triggerRef, menuRef, close, onKeyDown } = useRovingRadioMenu(open, setOpen);
	const [active, setActive] = useState(() => {
		try {
			return localStorage.getItem(KEY) || "current";
		} catch {
			return "current";
		}
	});
	useEffect(() => {
		applyTheme(active);
	}, [active]);
	useEffect(() => {
		const onStorage = (event) => {
			if (event.key && event.key !== KEY) return;
			const next = event.newValue || "current";
			setActive(THEMES.some((theme) => theme.id === next) ? next : "current");
		};
		window.addEventListener("storage", onStorage);
		return () => window.removeEventListener("storage", onStorage);
	}, []);
	const cur = THEMES.find((t) => t.id === active) || THEMES[0];
	const pick = (id) => {
		setActive(id);
		close(true);
	};
	return /* @__PURE__ */ _jsxs("div", {
		className: "theme-switch",
		children: [/* @__PURE__ */ _jsxs("button", {
			type: "button",
			ref: triggerRef,
			className: "btn sm ghost",
			title: "UI theme",
			"aria-haspopup": "menu",
			"aria-expanded": open,
			"aria-controls": "theme-switcher-menu",
			"aria-label": `UI theme: ${cur.name}`,
			onClick: () => setOpen(!open),
			children: [/* @__PURE__ */ _jsx("span", {
				className: "th-dot",
				style: {
					background: cur.ac,
					boxShadow: `0 0 0 2px ${cur.bg}`
				}
			}), " Theme"]
		}), open && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
			className: "th-backdrop",
			"aria-hidden": "true",
			onClick: () => close(true)
		}), /* @__PURE__ */ _jsxs("div", {
			ref: menuRef,
			id: "theme-switcher-menu",
			className: "th-menu",
			role: "menu",
			"aria-label": "UI theme",
			onKeyDown,
			onBlur: (event) => {
				if (!event.currentTarget.contains(event.relatedTarget)) close(false);
			},
			children: [/* @__PURE__ */ _jsx("div", {
				className: "th-menu-h",
				children: "Design"
			}), THEMES.map((t) => /* @__PURE__ */ _jsxs("button", {
				type: "button",
				role: "menuitemradio",
				"aria-checked": t.id === active,
				tabIndex: -1,
				className: "th-opt" + (t.id === active ? " on" : ""),
				onClick: () => pick(t.id),
				children: [
					/* @__PURE__ */ _jsxs("span", {
						className: "th-sw",
						style: {
							background: t.bg,
							borderColor: t.ac
						},
						children: [/* @__PURE__ */ _jsx("span", {
							className: "th-sw-ac",
							style: { background: t.ac }
						}), /* @__PURE__ */ _jsx("span", {
							className: "th-sw-fg",
							style: { background: t.fg }
						})]
					}),
					/* @__PURE__ */ _jsxs("span", {
						className: "th-name",
						children: [/* @__PURE__ */ _jsx("b", { children: t.name }), /* @__PURE__ */ _jsx("span", {
							className: "th-sub",
							children: t.sub
						})]
					}),
					t.id === active && /* @__PURE__ */ _jsx("span", {
						className: "th-check",
						children: "✓"
					})
				]
			}, t.id))]
		})] })]
	});
}
