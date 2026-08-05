import React, { useMemo } from "react";
import { safeExternalHref, safeMarkdownHref } from "./urlSafety.mjs";
import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
// Provider text is untrusted input. Character limits alone do not bound React-node amplification,
// so each Markdown instance owns independent hard budgets. These are intentionally generous for
// ordinary answers; the public transcript adds a second whole-session fence before mounting Turns.
const MARKDOWN_SOURCE_MAX = 256e3;
const MARKDOWN_LINE_MAX = 2048;
const MARKDOWN_BLOCK_MAX = 512;
const MARKDOWN_LIST_ITEM_MAX = 1024;
const MARKDOWN_INLINE_TOKEN_MAX = 2048;
const MARKDOWN_LIMIT_NOTICE = "Some content was not rendered because this message exceeded the safe display limit.";
const safePublicMarkdownHref = (value) => {
	const href = safeExternalHref(value);
	if (!href) return null;
	try {
		// A public transcript must not become a disguised jump into the viewer's authenticated owner
		// plane. Relative/hash links are already rejected by safeExternalHref; reject an absolute link
		// back to this LoopLab origin as well while keeping genuine external references useful.
		if (typeof window !== "undefined" && new URL(href).origin === window.location.origin) return null;
		return href;
	} catch {
		return null;
	}
};
// A tiny dependency-free Markdown renderer — just enough for chat answers: headings, bold/italic,
// inline code, fenced code blocks, bullet/numbered lists, blockquotes, links, and paragraphs. We
// avoid a library (local-first, zero-dep UI) and we don't use dangerouslySetInnerHTML, so user/LLM
// text can never inject HTML — every node is built as React elements from parsed tokens.
// Retain the old named export for callers while the policy itself lives in the shared URL trust
// boundary. React does not neutralize javascript:/data: links for us.
export const safeHref = safeMarkdownHref;
// Flatten inline Markdown to plain text for the one-line surfaces that show a rationale RAW (not rendered)
// — DAG card captions, feed row narrations, the pending-experiment queue. Now that the Researcher authors
// rationale as Markdown, these would otherwise show literal `**`/`` ` ``/`#`/`- ` markers. Keeps the words,
// drops the markers. Pure/deterministic; slice AFTER calling this so a cut never lands mid-marker.
export function stripMd(text) {
	return String(text ?? "").replace(/```[\s\S]*?```/g, " ").replace(/`([^`]+)`/g, "$1").replace(/(^|[^\w*])\*\*([^*]+)\*\*(?=[^\w*]|$)/g, "$1$2").replace(/(^|[^\w_])__([^_]+)__(?=[^\w_]|$)/g, "$1$2").replace(/(^|[^\w*])\*([^*]+)\*(?=[^\w*]|$)/g, "$1$2").replace(/(^|[^\w_])_([^_]+)_(?=[^\w_]|$)/g, "$1$2").replace(/^\s{0,3}#{1,6}\s+/gm, "").replace(/^\s*([-*+]|\d+\.)\s+/gm, "").replace(/^\s*>\s?/gm, "").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/\s+/g, " ").trim();
}
// --- inline: split a line into bold / italic / code / link spans (escapes are literal text) ---
function inline(text, keyBase, externalOnly = false, maxTokens = MARKDOWN_INLINE_TOKEN_MAX) {
	const source = String(text || "");
	const out = [];
	let cursor = 0, used = 0;
	// A global regex plus a monotonically increasing cursor makes this pass linear. Never repeatedly
	// copy and re-scan the unread tail of a provider-controlled string.
	const re = /(`[^`]+`)|(\[[^\]]+\]\([^)\s]+\))|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*]+\*)|(_[^_]+_)/g;
	let match;
	let truncated = false;
	while ((match = re.exec(source)) !== null) {
		// Preserve the remainder as one inert text node once the formatting budget is full. Continuing
		// to tokenise it would recreate the CPU/DOM amplification this boundary exists to stop.
		if (used >= maxTokens) {
			truncated = true;
			break;
		}
		if (match.index > cursor) out.push(source.slice(cursor, match.index));
		const tok = match[0];
		if (tok.startsWith("`")) out.push(/* @__PURE__ */ _jsx("code", { children: tok.slice(1, -1) }, `${keyBase}-c${used}`));
		else if (tok.startsWith("[")) {
			const mm = /\[([^\]]+)\]\(([^)\s]+)\)/.exec(tok);
			// Public shared transcripts are a separate audience: document-relative/hash/mail links could
			// escape into owner-only LoopLab routes. On that surface only ordinary external web URLs stay
			// interactive; all other Markdown links retain their readable label as inert text.
			const href = externalOnly ? safePublicMarkdownHref(mm[2]) : safeMarkdownHref(mm[2]);
			out.push(href ? /* @__PURE__ */ _jsx("a", {
				href,
				target: "_blank",
				rel: "noreferrer noopener",
				children: mm[1]
			}, `${keyBase}-l${used}`) : /* @__PURE__ */ _jsx("span", { children: mm[1] }, `${keyBase}-l${used}`));
		} else if (tok.startsWith("**") || tok.startsWith("__")) out.push(/* @__PURE__ */ _jsx("strong", { children: tok.slice(2, -2) }, `${keyBase}-b${used}`));
		else out.push(/* @__PURE__ */ _jsx("em", { children: tok.slice(1, -1) }, `${keyBase}-i${used}`));
		used += 1;
		cursor = re.lastIndex;
	}
	if (cursor < source.length) out.push(source.slice(cursor));
	return {
		nodes: out,
		used,
		truncated
	};
}
// --- block: fold lines into headings / code fences / lists / quotes / paragraphs ---
function parse(src) {
	const source = String(src || "");
	let truncated = source.length > MARKDOWN_SOURCE_MAX;
	const normalized = source.slice(0, MARKDOWN_SOURCE_MAX).replace(/\r\n?/g, "\n");
	let lines = normalized.split("\n");
	if (lines.length > MARKDOWN_LINE_MAX) {
		lines = lines.slice(0, MARKDOWN_LINE_MAX);
		truncated = true;
	}
	const blocks = [];
	let i = 0, listItemsUsed = 0;
	while (i < lines.length && blocks.length < MARKDOWN_BLOCK_MAX) {
		const line = lines[i];
		// fenced code block
		if (/^```/.test(line)) {
			const lang = line.slice(3).trim();
			const buf = [];
			i++;
			while (i < lines.length && !/^```/.test(lines[i])) {
				buf.push(lines[i]);
				i++;
			}
			i++;
			blocks.push({
				t: "code",
				lang,
				text: buf.join("\n")
			});
			continue;
		}
		// heading
		const h = /^(#{1,6})\s+(.*)$/.exec(line);
		if (h) {
			blocks.push({
				t: "h",
				level: h[1].length,
				text: h[2]
			});
			i++;
			continue;
		}
		// horizontal rule
		if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
			blocks.push({ t: "hr" });
			i++;
			continue;
		}
		// blockquote (consecutive > lines)
		if (/^\s*>\s?/.test(line)) {
			const buf = [];
			while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
				buf.push(lines[i].replace(/^\s*>\s?/, ""));
				i++;
			}
			blocks.push({
				t: "quote",
				text: buf.join("\n")
			});
			continue;
		}
		// list (bullets or ordered) — consecutive item lines
		if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
			const ordered = /^\s*\d+\.\s+/.test(line);
			const items = [];
			while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
				if (listItemsUsed < MARKDOWN_LIST_ITEM_MAX) {
					items.push(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, ""));
					listItemsUsed += 1;
				} else truncated = true;
				i++;
			}
			if (items.length) blocks.push({
				t: "list",
				ordered,
				items
			});
			continue;
		}
		// blank line → separator
		if (/^\s*$/.test(line)) {
			i++;
			continue;
		}
		// paragraph: gather until a blank line or a block starter
		const buf = [];
		while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^```/.test(lines[i]) && !/^(#{1,6})\s+/.test(lines[i]) && !/^\s*([-*+]|\d+\.)\s+/.test(lines[i]) && !/^\s*>\s?/.test(lines[i])) {
			buf.push(lines[i]);
			i++;
		}
		blocks.push({
			t: "p",
			text: buf.join("\n")
		});
	}
	if (i < lines.length) truncated = true;
	return {
		blocks,
		truncated
	};
}
export default function Markdown({ text, className, externalOnly = false }) {
	const parsed = useMemo(() => parse(text), [text]);
	let inlineRemaining = MARKDOWN_INLINE_TOKEN_MAX;
	let inlineTruncated = false;
	const renderInline = (value, key) => {
		const rendered = inline(value, key, externalOnly, inlineRemaining);
		inlineRemaining -= rendered.used;
		inlineTruncated = inlineTruncated || rendered.truncated;
		return rendered.nodes;
	};
	const content = parsed.blocks.map((b, bi) => {
		if (b.t === "code") return /* @__PURE__ */ _jsx("pre", {
			className: "code md-code",
			children: b.text
		}, bi);
		if (b.t === "hr") return /* @__PURE__ */ _jsx("hr", { className: "md-hr" }, bi);
		if (b.t === "h") {
			const H = `h${Math.min(b.level + 2, 6)}`;
			return /* @__PURE__ */ _jsx(H, {
				className: "md-h",
				children: renderInline(b.text, bi)
			}, bi);
		}
		if (b.t === "quote") return /* @__PURE__ */ _jsx("blockquote", {
			className: "md-quote",
			children: renderInline(b.text, bi)
		}, bi);
		if (b.t === "list") {
			const items = b.items.map((it, ii) => /* @__PURE__ */ _jsx("li", { children: renderInline(it, `${bi}-${ii}`) }, ii));
			return b.ordered ? /* @__PURE__ */ _jsx("ol", {
				className: "md-list",
				children: items
			}, bi) : /* @__PURE__ */ _jsx("ul", {
				className: "md-list",
				children: items
			}, bi);
		}
		// paragraph — keep single newlines as <br/> (LLMs often wrap mid-thought)
		const parts = b.text.split("\n");
		return /* @__PURE__ */ _jsx("p", {
			className: "md-p",
			children: parts.map((ln, li) => /* @__PURE__ */ _jsxs(React.Fragment, { children: [li > 0 && /* @__PURE__ */ _jsx("br", {}), renderInline(ln, `${bi}-${li}`)] }, li))
		}, bi);
	});
	const truncated = parsed.truncated || inlineTruncated;
	return /* @__PURE__ */ _jsxs("div", {
		className: "md" + (className ? " " + className : ""),
		children: [content, truncated && /* @__PURE__ */ _jsx("p", {
			className: "muted md-p",
			role: "status",
			children: MARKDOWN_LIMIT_NOTICE
		})]
	});
}
