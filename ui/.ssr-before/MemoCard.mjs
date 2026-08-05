import React, { useMemo, useState } from "react";
import Markdown from "./markdown.mjs";
import { OpIcon } from "./icons.mjs";
import { safeExternalHref } from "./urlSafety.mjs";
import { normalizeResearchMemo } from "./researchMemoModel.mjs";
import { jsxs as _jsxs, jsx as _jsx, Fragment as _Fragment } from "react/jsx-runtime";
function MemoVerification({ verification }) {
	const verdicts = verification?.verdicts || [];
	const omitted = verification?.omittedVerdicts || 0;
	if (!verdicts.length && !omitted) return null;
	return /* @__PURE__ */ _jsxs(_Fragment, { children: [
		/* @__PURE__ */ _jsxs("div", {
			className: "section-h memo-verification-title",
			children: [
				"Verification",
				verification.unsupported > 0 && /* @__PURE__ */ _jsxs("span", {
					className: "chip warn",
					title: "claims whose cited evidence does not support them",
					children: [verification.unsupported, " unsupported"]
				}),
				omitted > 0 && /* @__PURE__ */ _jsx("span", {
					className: "chip warn",
					children: "verification incomplete"
				}),
				/* @__PURE__ */ _jsxs("span", {
					className: "muted",
					children: [
						" (",
						verification.method,
						")"
					]
				})
			]
		}),
		omitted > 0 && /* @__PURE__ */ _jsxs("p", {
			className: "memo-verification-incomplete",
			role: "note",
			children: [
				"Showing ",
				verdicts.length,
				" of ",
				verification.totalVerdicts,
				" verifier verdicts; omitted verdicts make this check incomplete."
			]
		}),
		/* @__PURE__ */ _jsx("ul", {
			className: "bul memo-verification-list",
			children: verdicts.map((item, index) => /* @__PURE__ */ _jsxs("li", {
				className: item.verdict === "supported" ? "ok" : item.verdict === "unclear" || item.verdict === "cited" ? "" : "bad",
				children: [
					/* @__PURE__ */ _jsx("span", {
						className: "pill",
						children: item.verdict
					}),
					" ",
					item.statement || "(statement unavailable)",
					item.note && /* @__PURE__ */ _jsxs("span", {
						className: "muted",
						children: [" — ", item.note]
					})
				]
			}, index))
		})
	] });
}
// Report-local research memo. This must not live in panels.jsx: importing the report alone should
// never pull the optional panel hub and its owner-only dependencies into the report closure.
export default function MemoCard({ memo, idx, open, onToggle }) {
	const [think, setThink] = useState(false);
	const value = useMemo(() => normalizeResearchMemo(memo), [memo]);
	const memoIndex = Number.isSafeInteger(idx) && idx >= 0 ? idx : 0;
	return /* @__PURE__ */ _jsxs("div", {
		className: "memo-card",
		children: [/* @__PURE__ */ _jsxs("button", {
			type: "button",
			className: "memo-head disclosure-button",
			"aria-expanded": open,
			onClick: () => onToggle?.(memoIndex),
			children: [
				/* @__PURE__ */ _jsx("span", {
					className: "span-tw",
					children: open ? "▾" : "▸"
				}),
				/* @__PURE__ */ _jsxs("b", { children: [
					/* @__PURE__ */ _jsx(OpIcon, {
						name: "search",
						className: "t-ic"
					}),
					" memo #",
					memoIndex + 1
				] }),
				value.trigger && /* @__PURE__ */ _jsx("span", {
					className: "pill",
					children: value.trigger
				}),
				value.at_node != null && /* @__PURE__ */ _jsxs("span", {
					className: "muted",
					children: [
						" @",
						value.at_node,
						" nodes"
					]
				}),
				/* @__PURE__ */ _jsx("span", {
					className: "spacer",
					style: { flex: 1 }
				}),
				/* @__PURE__ */ _jsxs("span", {
					className: "muted",
					children: [
						value.sources.length,
						" source",
						value.sources.length === 1 ? "" : "s"
					]
				})
			]
		}), open && /* @__PURE__ */ _jsxs("div", {
			className: "memo-body",
			children: [
				/* @__PURE__ */ _jsx("div", {
					className: "section-h",
					children: "Conclusion"
				}),
				/* @__PURE__ */ _jsx("div", {
					className: "v",
					children: value.summary || "—"
				}),
				value.findings.length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
					className: "section-h",
					children: "Findings"
				}), /* @__PURE__ */ _jsx("ul", {
					className: "bul",
					children: value.findings.map((finding, index) => /* @__PURE__ */ _jsx("li", { children: finding }, index))
				})] }),
				/* @__PURE__ */ _jsx(MemoVerification, { verification: value.verification }),
				value.recommended_directions.length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
					className: "section-h",
					children: "Recommended directions (fed to the Researcher)"
				}), /* @__PURE__ */ _jsx("ul", {
					className: "bul",
					children: value.recommended_directions.map((direction, index) => /* @__PURE__ */ _jsx("li", { children: direction }, index))
				})] }),
				value.sources.length > 0 && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("div", {
					className: "section-h",
					children: "Sources consulted"
				}), /* @__PURE__ */ _jsx("ul", {
					className: "bul",
					children: value.sources.map((source, index) => {
						// Research/provider output is untrusted. Only credential-free HTTP(S) URLs become links;
						// unsafe, oversized or malformed values remain bounded inert text.
						const href = safeExternalHref(source.url);
						const label = (source.title || source.url || "—").slice(0, 300);
						const snippet = source.snippet.slice(0, 120);
						return /* @__PURE__ */ _jsxs("li", { children: [href ? /* @__PURE__ */ _jsx("a", {
							href,
							target: "_blank",
							rel: "noreferrer noopener",
							children: label
						}) : label, snippet && /* @__PURE__ */ _jsxs("span", {
							className: "muted",
							children: [" — ", snippet]
						})] }, index);
					})
				})] }),
				value.reasoning && /* @__PURE__ */ _jsxs("div", {
					className: "think-debug",
					style: { marginTop: 8 },
					children: [/* @__PURE__ */ _jsxs("button", {
						type: "button",
						className: "role-think disclosure-button",
						"aria-expanded": think,
						onClick: () => setThink((value) => !value),
						style: {
							fontSize: 11,
							textTransform: "uppercase",
							letterSpacing: ".5px"
						},
						children: [think ? "▾" : "▸", " reasoning (debug)"]
					}), think && /* @__PURE__ */ _jsx(Markdown, {
						className: "think-body",
						text: value.reasoning
					})]
				})
			]
		})]
	});
}
