import React from "react";
import { BaseEdge, getBezierPath, getSmoothStepPath } from "@xyflow/react";
import { motionOK } from "./fx.mjs";
import { jsx as _jsx, Fragment as _Fragment, jsxs as _jsxs } from "react/jsx-runtime";
// A custom React Flow edge for the Reactor / Energy FX mode. The PATH keeps its semantic stroke (the
// edge's className still drives colour + the crackle/glow filter via CSS: lineage→accent, champion→gold,
// charge→plasma, …); on top of that, glowing "energy packets" stream parent→child along the path via
// SVG <animateMotion>. Each packet is a comet — a bright head plus a couple of fading tracers a few ms
// behind — so the stream reads as fast, dense current rather than lone dots.
//
// Density is tiered. At "full", the semantic edges (charge into the working node, the champion spine,
// the selected lineage) run thick fast streams AND every non-faded plain edge gets a quiet current too,
// so the whole web looks live. At "subtle" the streams are thinned and plain edges carry no packets, so
// the element count stays modest. `onlyRenderVisibleElements` means even these mount only when on-screen.
// comet profile: head + fading tracers, each lagging a few ms behind so a packet looks like it streaks.
const TRAIL = [
	{
		r: 1,
		o: 1,
		lag: 0
	},
	{
		r: .7,
		o: .55,
		lag: .05
	},
	{
		r: .45,
		o: .3,
		lag: .1
	}
];
// per-flow stream spec — colour, period (dur), head radius, packet count (n), comet length (trail), base
// opacity. Tuned MUCH denser/faster than the first cut so the energy "frequency" reads many times higher.
const SPEC = {
	charge: {
		color: "var(--working)",
		dur: .5,
		r: 3.2,
		n: 5,
		trail: 3,
		op: 1
	},
	champion: {
		color: "var(--best)",
		dur: 1.1,
		r: 2.8,
		n: 4,
		trail: 3,
		op: 1
	},
	lineage: {
		color: "var(--accent)",
		dur: .9,
		r: 2.6,
		n: 4,
		trail: 3,
		op: 1
	},
	plain: {
		color: "var(--accent)",
		dur: 1.8,
		r: 1.8,
		n: 2,
		trail: 2,
		op: .5
	}
};
export default function EnergyEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, markerEnd, style, data }) {
	const params = {
		sourceX,
		sourceY,
		sourcePosition,
		targetX,
		targetY,
		targetPosition
	};
	// a merge "leader" edge routes orthogonally (matches the smoothstep used outside FX mode); the rest bezier
	const [edgePath] = data?.leader ? getSmoothStepPath(params) : getBezierPath(params);
	const full = data?.level === "full";
	let spec = data?.charging ? SPEC.charge : data?.flow === "champion" ? SPEC.champion : data?.flow === "lineage" ? SPEC.lineage : full && !data?.faded ? SPEC.plain : null;
	// subtle level → thin the storm back to a cue (half the packets, shorter comets)
	if (spec && !full) spec = {
		...spec,
		n: Math.max(1, Math.ceil(spec.n / 2)),
		trail: Math.min(spec.trail, 2)
	};
	const animate = spec && motionOK();
	const trail = spec ? TRAIL.slice(0, spec.trail) : [];
	return /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx(BaseEdge, {
		id,
		path: edgePath,
		markerEnd,
		style
	}), animate && Array.from({ length: spec.n }).flatMap((_, i) => {
		// stagger packets evenly across the period so they read as a steady stream
		const base = i * spec.dur / spec.n;
		return trail.map((t, j) => /* @__PURE__ */ _jsx("circle", {
			className: "ll-spark",
			r: spec.r * t.r,
			fill: spec.color,
			style: { opacity: spec.op * t.o },
			children: /* @__PURE__ */ _jsx("animateMotion", {
				dur: `${spec.dur}s`,
				begin: `${(base + t.lag).toFixed(3)}s`,
				repeatCount: "indefinite",
				path: edgePath
			})
		}, `${i}-${j}`));
	})] });
}
