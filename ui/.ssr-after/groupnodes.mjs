import React from "react";
import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
// Shared chrome for the "group" visual language used by BOTH the in-run canvas (Dag.jsx) and the
// cross-run map (MapView.jsx), so the hull region and the collapsed super-node card are defined
// once. Bodies differ legitimately (experiment aggregate vs project rollup) and are passed in.
// Soft enclosing hull behind a group; `tab` is the (interactive) label the caller wires. Used by the
// cross-run Map (MapView.jsx); the in-run canvas (Dag.jsx) uses the slimmer GroupRegion below.
export function RegionShell({ w, h, path, tint, tab }) {
	return /* @__PURE__ */ _jsxs("div", {
		className: "grp-region",
		style: {
			width: w,
			height: h,
			"--grp-tint": tint
		},
		children: [/* @__PURE__ */ _jsx("svg", {
			width: w,
			height: h,
			"aria-hidden": "true",
			focusable: "false",
			children: /* @__PURE__ */ _jsx("path", {
				d: path,
				className: "grp-hull"
			})
		}), tab]
	});
}
// Group region for an EXPANDED cluster (round-8): a WHISPER-faint rounded band behind the members
// (the "objединяющий бокс" without the heavy old hull) + a compact label PILL hugging its text at the
// top-left (replaces the full-width bar that stretched across the cluster). Group identity reads from
// the members' shared muted fill; the band + pill just frame and name it. The band is click-through
// (pointer-events:none) so nodes on top stay clickable; only the pill collapses the group.
export function GroupRegion({ w, h, label, count, totalCount, matchedCount = count, filterActive = false, filterDescription = null, tint, onToggle }) {
	const splitAcrossBands = Number.isFinite(totalCount) && totalCount > count;
	const countText = filterActive ? `${matchedCount}/${count}${splitAcrossBands ? " · split" : ""}` : splitAcrossBands ? `${count}/${totalCount} · split` : String(count);
	const bandDescription = splitAcrossBands ? `${count} experiments in this topology band, ${totalCount} in this group across the graph` : `${count} experiments`;
	const countDescription = filterActive ? `${matchedCount} of ${count} experiments in this topology band match ${filterDescription}` + (splitAcrossBands ? `; ${totalCount} experiments in this group across the graph` : "") : bandDescription;
	return /* @__PURE__ */ _jsx("div", {
		className: "grp-band" + (filterActive && matchedCount === 0 ? " dim" : ""),
		style: {
			width: w,
			height: h,
			"--grp-tint": tint
		},
		children: /* @__PURE__ */ _jsxs("button", {
			type: "button",
			className: "grp-pill",
			onClick: (e) => {
				e.stopPropagation();
				onToggle && onToggle(label);
			},
			"aria-label": `Collapse group ${label}; ${countDescription}`,
			title: `${countDescription}. Collapse group`,
			children: [
				/* @__PURE__ */ _jsx("span", {
					className: "grp-chev",
					children: "▾"
				}),
				/* @__PURE__ */ _jsx("span", {
					className: "grp-pill-label",
					children: label
				}),
				/* @__PURE__ */ _jsx("span", {
					className: "grp-n",
					children: countText
				})
			]
		})
	});
}
// Collapsed-group card shell. Caller supplies the body (children) and the click/selected state.
export function SuperShell({ tint, selected, dimmed = false, onClick, title, selectKey, children }) {
	return /* @__PURE__ */ _jsxs("div", {
		className: "grp-super" + (selected ? " sel" : "") + (dimmed ? " dim" : ""),
		style: { "--grp-tint": tint },
		title,
		children: [onClick && /* @__PURE__ */ _jsx("button", {
			type: "button",
			className: "grp-super-select",
			onClick,
			"data-group-select-key": selectKey == null ? undefined : String(selectKey),
			"aria-label": title || "Open collapsed group",
			"aria-pressed": !!selected
		}), children]
	});
}
