import React from "react";
import { highlightSegments } from "./conceptSearch.mjs";
import { jsx as _jsx } from "react/jsx-runtime";
// Render `text` with the matched slice of `query` wrapped in <mark> — pure segments, no
// dangerouslySetInnerHTML. Shared by the concept search surfaces (View 2 chip bar + View 1 tree) so
// the highlight markup stays in one place. Empty query / no match renders plain text. The segmenter
// (highlightSegments) lives in conceptSearch.js and is unit-tested.
export function Marked({ text, query }) {
	return highlightSegments(text, query).map((seg, i) => seg.hit ? /* @__PURE__ */ _jsx("mark", { children: seg.text }, i) : /* @__PURE__ */ _jsx(React.Fragment, { children: seg.text }, i));
}
