import React, { useId, useState } from "react";
import { OpIcon } from "./icons.mjs";
import { jsxs as _jsxs, jsx as _jsx } from "react/jsx-runtime";
// Compact, always-visible narration of the loop's latest autonomous decisions. Keeping this pure
// projection outside panels.jsx means the core run canvas does not download every optional panel.
export default function WhyStrip({ state, onSelect }) {
	const detailBaseId = useId();
	const [expandedKey, setExpandedKey] = useState(null);
	const items = [];
	const strategies = state.strategy_history || [];
	const strat = strategies[strategies.length - 1];
	if (strat && (strat.strategy?.rationale || strat.strategy?.policy)) {
		items.push({
			icon: "compass",
			label: "strategy",
			text: strat.strategy.rationale || `policy -> ${strat.strategy.policy}`,
			at: strat.at_node
		});
	}
	const decisions = state.agent_decisions || [];
	const decision = decisions[decisions.length - 1];
	if (decision && (decision.rationale || decision.chosen)) {
		const chosen = decision.chosen;
		const label = chosen && typeof chosen === "object" ? `${chosen.kind || "action"}${chosen.node_id != null ? ` #${chosen.node_id}` : chosen.parent_id != null ? ` from #${chosen.parent_id}` : ""}` : chosen || "action";
		items.push({
			icon: "bolt",
			label,
			text: decision.rationale || "",
			at: decision.at_node
		});
	}
	if (state.policy_reason) {
		items.push({
			icon: "target",
			label: "policy",
			node: state.policy_chosen,
			text: `${state.policy_reason}${state.policy_chosen != null ? ` -> #${state.policy_chosen}` : ""}`
		});
	}
	if (!items.length) return null;
	return /* @__PURE__ */ _jsx("div", {
		className: "why-strip",
		role: "region",
		"aria-label": "Why the loop is doing what it is doing live",
		children: items.slice(0, 3).map((item, index) => {
			const Item = item.node != null ? "button" : "span";
			const itemKey = `${index}:${item.label}:${item.node ?? ""}:${item.at ?? ""}`;
			const expanded = expandedKey === itemKey;
			const detailId = `${detailBaseId}-${index}`;
			return /* @__PURE__ */ _jsxs("div", {
				className: "why-entry" + (expanded ? " open" : ""),
				children: [/* @__PURE__ */ _jsxs("div", {
					className: "why-item-row",
					children: [/* @__PURE__ */ _jsxs(Item, {
						type: item.node != null ? "button" : undefined,
						className: "why-item" + (item.node != null ? " disclosure-button" : ""),
						title: item.text || undefined,
						onClick: item.node != null ? () => onSelect?.(item.node) : undefined,
						children: [
							/* @__PURE__ */ _jsx(OpIcon, {
								name: item.icon,
								size: 12,
								className: "why-ic"
							}),
							/* @__PURE__ */ _jsx("b", { children: item.label }),
							" ",
							item.text,
							item.at != null ? /* @__PURE__ */ _jsxs("span", {
								className: "muted",
								children: [" @", item.at]
							}) : null
						]
					}), item.text && /* @__PURE__ */ _jsx("button", {
						type: "button",
						className: "why-disclosure disclosure-button",
						"aria-expanded": expanded,
						"aria-controls": detailId,
						"aria-label": `${expanded ? "Hide" : "Show"} full rationale for ${item.label}`,
						onClick: () => setExpandedKey((current) => current === itemKey ? null : itemKey),
						children: /* @__PURE__ */ _jsx(OpIcon, {
							name: expanded ? "chevron-up" : "chevron-down",
							size: 12
						})
					})]
				}), item.text && /* @__PURE__ */ _jsxs("div", {
					id: detailId,
					className: "why-detail",
					role: "region",
					hidden: !expanded,
					"aria-label": `Full rationale for ${item.label}`,
					children: [
						/* @__PURE__ */ _jsx("b", { children: item.label }),
						" ",
						item.text,
						item.at != null ? /* @__PURE__ */ _jsxs("span", {
							className: "muted",
							children: [" @", item.at]
						}) : null
					]
				})]
			}, itemKey);
		})
	});
}
