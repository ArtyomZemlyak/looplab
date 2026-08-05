import React, { useMemo, useRef, useState } from "react";
import { createInspectorDraftStore, useInspectorDraftField } from "./inspectorDraftStore.mjs";
import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
function highlighted(text, query) {
	if (!query) return text || " ";
	const lower = text.toLowerCase(), needle = query.toLowerCase();
	const parts = [];
	let from = 0, index;
	while ((index = lower.indexOf(needle, from)) >= 0) {
		if (index > from) parts.push(text.slice(from, index));
		parts.push(/* @__PURE__ */ _jsx("mark", { children: text.slice(index, index + query.length) }, `${index}:${parts.length}`));
		from = index + query.length;
	}
	if (from < text.length) parts.push(text.slice(from));
	return parts.length ? parts : text || " ";
}
export default function CodeViewer({ code = "", diff = null, label = "Code", maxHeight = 420, copyText = null, draftStore: sharedDraftStore = null, draftScope = null }) {
	const fallbackDraftStoreRef = useRef(null);
	if (!fallbackDraftStoreRef.current) fallbackDraftStoreRef.current = createInspectorDraftStore();
	const draftStore = sharedDraftStore || fallbackDraftStoreRef.current;
	const scope = draftScope || `code-viewer:${label}`;
	const [query, setQuery] = useInspectorDraftField(draftStore, scope, "query", "", { disposable: true });
	const [wrap, setWrap] = useInspectorDraftField(draftStore, scope, "wrap", false, { disposable: true });
	const [copied, setCopied] = useState(false);
	const rows = useMemo(() => diff || String(code || "").split("\n").map((line, index) => ({
		line,
		l: line,
		kind: "same",
		cls: "",
		oldNo: null,
		newNo: index + 1
	})), [code, diff]);
	const matches = query ? rows.filter((row) => String(row.line ?? row.l ?? "").toLowerCase().includes(query.toLowerCase())).length : 0;
	const copy = async () => {
		try {
			await navigator.clipboard.writeText(copyText ?? code);
			setCopied(true);
			setTimeout(() => setCopied(false), 1400);
		} catch {
			setCopied(false);
		}
	};
	return /* @__PURE__ */ _jsxs("div", {
		className: "code-viewer" + (wrap ? " wrap" : "") + (diff ? " has-diff" : ""),
		style: { "--code-max-h": `${maxHeight}px` },
		children: [/* @__PURE__ */ _jsxs("div", {
			className: "code-tools",
			children: [
				/* @__PURE__ */ _jsxs("label", {
					className: "code-search",
					children: [/* @__PURE__ */ _jsxs("span", {
						className: "sr-only",
						children: ["Search ", label]
					}), /* @__PURE__ */ _jsx("input", {
						value: query,
						onChange: (event) => setQuery(event.target.value),
						placeholder: `Search ${label.toLowerCase()}…`
					})]
				}),
				query && /* @__PURE__ */ _jsxs("span", {
					className: "muted",
					children: [
						matches,
						" line",
						matches === 1 ? "" : "s"
					]
				}),
				/* @__PURE__ */ _jsx("span", { className: "spacer" }),
				/* @__PURE__ */ _jsx("button", {
					className: "btn sm ghost" + (wrap ? " on" : ""),
					onClick: () => setWrap((value) => !value),
					"aria-pressed": wrap,
					children: "Wrap"
				}),
				/* @__PURE__ */ _jsx("button", {
					className: "btn sm ghost",
					onClick: copy,
					children: copied ? "Copied" : "Copy"
				})
			]
		}), /* @__PURE__ */ _jsx("div", {
			className: "code-lines",
			role: "region",
			"aria-label": label,
			tabIndex: 0,
			children: rows.map((row, index) => /* @__PURE__ */ _jsxs("div", {
				className: "code-line " + (row.cls || ""),
				children: [
					diff && /* @__PURE__ */ _jsx("span", {
						className: "code-old-no",
						children: row.oldNo ?? ""
					}),
					/* @__PURE__ */ _jsx("span", {
						className: "code-new-no",
						children: row.newNo ?? ""
					}),
					/* @__PURE__ */ _jsx("span", {
						className: "code-sign",
						"aria-hidden": "true",
						children: row.kind === "add" ? "+" : row.kind === "del" ? "−" : " "
					}),
					/* @__PURE__ */ _jsx("code", { children: highlighted(String(row.line ?? row.l ?? ""), query) })
				]
			}, index))
		})]
	});
}
