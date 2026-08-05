import React from "react";
import { fmt, operatorMeta } from "./util.mjs";
import { ChartFrame } from "./accessibility.mjs";
import { nodeTheme } from "./conceptId.mjs";
import { nodeIsActive } from "./nodeProjection.mjs";
import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
const AX = "var(--fg-mut)", GRID = "var(--line)";
const MARK_SHAPES = [
	"circle",
	"square",
	"diamond",
	"triangle",
	"triangle-down",
	"pentagon",
	"hexagon",
	"star",
	"plus",
	"cross",
	"bar-horizontal",
	"bar-vertical"
];
const polygonPoints = (x, y, radius, sides, rotation = -Math.PI / 2) => Array.from({ length: sides }, (_, index) => {
	const angle = rotation + index * Math.PI * 2 / sides;
	return `${x + Math.cos(angle) * radius},${y + Math.sin(angle) * radius}`;
}).join(" ");
const starPoints = (x, y, radius) => Array.from({ length: 10 }, (_, index) => {
	const angle = -Math.PI / 2 + index * Math.PI / 5;
	const r = index % 2 ? radius * .44 : radius;
	return `${x + Math.cos(angle) * r},${y + Math.sin(angle) * r}`;
}).join(" ");
function PointMark({ x, y, size = 4, color, shape = "circle", className = "", opacity = 1, variant = "solid", feasibility = "feasible", onClick = null, title }) {
	const common = {
		className: "chart-point-shape",
		fill: variant === "outline" ? "var(--bg-1)" : color,
		stroke: variant === "outline" ? color : "var(--fg)",
		strokeWidth: variant === "outline" ? 1.5 : .65
	};
	const mark = shape === "square" ? /* @__PURE__ */ _jsx("rect", {
		...common,
		x: x - size,
		y: y - size,
		width: size * 2,
		height: size * 2,
		rx: "1"
	}) : shape === "diamond" ? /* @__PURE__ */ _jsx("rect", {
		...common,
		x: x - size * .78,
		y: y - size * .78,
		width: size * 1.56,
		height: size * 1.56,
		transform: `rotate(45 ${x} ${y})`
	}) : shape === "triangle" ? /* @__PURE__ */ _jsx("path", {
		...common,
		d: `M ${x} ${y - size - .5} L ${x + size} ${y + size} L ${x - size} ${y + size} Z`
	}) : shape === "triangle-down" ? /* @__PURE__ */ _jsx("path", {
		...common,
		d: `M ${x - size} ${y - size} L ${x + size} ${y - size} L ${x} ${y + size + .5} Z`
	}) : shape === "pentagon" ? /* @__PURE__ */ _jsx("polygon", {
		...common,
		points: polygonPoints(x, y, size + .3, 5)
	}) : shape === "hexagon" ? /* @__PURE__ */ _jsx("polygon", {
		...common,
		points: polygonPoints(x, y, size + .3, 6)
	}) : shape === "star" ? /* @__PURE__ */ _jsx("polygon", {
		...common,
		points: starPoints(x, y, size + 1)
	}) : shape === "plus" || shape === "cross" ? /* @__PURE__ */ _jsx("path", {
		...common,
		d: `M ${x - size} ${y - size * .3} H ${x - size * .3} V ${y - size} H ${x + size * .3} V ${y - size * .3} H ${x + size} V ${y + size * .3} H ${x + size * .3} V ${y + size} H ${x - size * .3} V ${y + size * .3} H ${x - size} Z`,
		transform: shape === "cross" ? `rotate(45 ${x} ${y})` : undefined
	}) : shape === "bar-horizontal" ? /* @__PURE__ */ _jsx("rect", {
		...common,
		x: x - size - 1,
		y: y - size * .35,
		width: (size + 1) * 2,
		height: size * .7,
		rx: "1"
	}) : shape === "bar-vertical" ? /* @__PURE__ */ _jsx("rect", {
		...common,
		x: x - size * .35,
		y: y - size - 1,
		width: size * .7,
		height: (size + 1) * 2,
		rx: "1"
	}) : /* @__PURE__ */ _jsx("circle", {
		...common,
		cx: x,
		cy: y,
		r: size
	});
	return /* @__PURE__ */ _jsxs("g", {
		className,
		opacity,
		onClick,
		children: [
			onClick && /* @__PURE__ */ _jsx("circle", {
				className: "chart-hit-area",
				cx: x,
				cy: y,
				r: "15",
				fill: "transparent"
			}),
			mark,
			variant === "dot" && /* @__PURE__ */ _jsx("circle", {
				cx: x,
				cy: y,
				r: Math.max(1.1, size * .28),
				fill: "var(--bg-1)"
			}),
			feasibility === "infeasible" && /* @__PURE__ */ _jsx("circle", {
				className: "chart-feasibility-ring",
				cx: x,
				cy: y,
				r: size + 2.2,
				fill: "none",
				stroke: "var(--fg)",
				strokeWidth: "1.2",
				strokeDasharray: "2 1.7"
			}),
			feasibility === "unknown" && /* @__PURE__ */ _jsx("circle", {
				className: "chart-feasibility-ring",
				cx: x,
				cy: y,
				r: size + 2.2,
				fill: "none",
				stroke: "var(--fg-dim)",
				strokeWidth: "1.2"
			}),
			/* @__PURE__ */ _jsx("title", { children: title })
		]
	});
}
// Grouping palette: one stable hue per operator so scatter points read as families ("группировка").
const OP_COLORS = {
	draft: "#4aa3ff",
	improve: "#2ecc71",
	debug: "#ef4444",
	merge: "#9a6bff",
	refine_block: "#f0b429",
	fork: "#22c5c5",
	random: "#e06fae",
	tune: "#7f8cff",
	sweep: "#f59e42"
};
const opColor = (op) => OP_COLORS[op] || "#6b8cc0";
// Stable hue per theme slug (for grouping BY theme) — hashed so a theme always gets the same colour.
function themeColor(t) {
	let h = 0;
	const s = String(t || "untagged");
	for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
	return `hsl(${h}, 60%, 58%)`;
}
// A compact colour legend rendered under a chart (operators present in the data). When `onPick` is
// given the swatches are clickable to FOCUS one operator group (dim the rest) — interactive grouping.
function ChartLegend({ items, active = null, onPick = null }) {
	if (!items || items.length < 2) return null;
	return /* @__PURE__ */ _jsx("div", {
		className: "chart-legend",
		children: items.map((it, i) => onPick ? /* @__PURE__ */ _jsxs("button", {
			type: "button",
			className: "chart-leg pick" + (active && active !== it.key ? " dim" : ""),
			"aria-pressed": active === it.key,
			onClick: () => onPick(active === it.key ? null : it.key),
			title: active === it.key ? "show all" : `show only ${it.label}`,
			children: [/* @__PURE__ */ _jsx("span", {
				className: `chart-leg-dot shape-${it.shape || "circle"} variant-${it.variant || "solid"}`,
				style: {
					"--marker-color": it.color,
					background: it.color
				}
			}), it.label]
		}, i) : /* @__PURE__ */ _jsxs("span", {
			className: "chart-leg" + (active && active !== it.key ? " dim" : ""),
			children: [/* @__PURE__ */ _jsx("span", {
				className: `chart-leg-dot shape-${it.shape || "circle"} variant-${it.variant || "solid"}`,
				style: {
					"--marker-color": it.color,
					background: it.color
				}
			}), it.label]
		}, i))
	});
}
// Best-metric-over-time + all-node scatter. Pass `steps` (from report.improvements) to annotate
// the nodes that moved the frontier with a marker + a "what changed" label — so the chart shows
// not just the metric curve but WHICH improvement caused each drop/rise.
// `onPick(id)` (optional) makes every point + frontier marker clickable to drill into that node.
// Points are coloured BY OPERATOR (grouping), with a legend; the frontier keeps its green line + a
// soft area fill under it.
export function Trajectory({ nodes, direction, state = null, width = 760, height = 220, steps = null, onPick = null, selected = null }) {
	const evald = nodes.filter((n) => nodeIsActive(n, state) && (n.metric ?? null) !== null).sort((a, b) => a.id - b.id);
	const [logY, setLogY] = React.useState(false);
	const [hoverId, setHoverId] = React.useState(null);
	const [focusGrp, setFocusGrp] = React.useState(null);
	const [groupBy, setGroupBy] = React.useState("operator");
	const groupDimensionLabel = (value) => value === "theme" ? "primary concept axis" : value;
	const themeOf = (n) => nodeTheme(n, state);
	const grpKey = (n) => groupBy === "theme" ? themeOf(n) || "untagged" : n.operator || "—";
	const grpColor = (n) => groupBy === "theme" ? themeColor(grpKey(n)) : opColor(n.operator);
	const grpLabel = (g) => groupBy === "theme" ? g : operatorMeta(g).label;
	const grpSwatch = (g) => groupBy === "theme" ? themeColor(g) : opColor(g);
	if (!evald.length) return /* @__PURE__ */ _jsx(Empty, { children: "no evaluated nodes yet" });
	const xs = evald.map((n) => n.id);
	const ys = evald.map((n) => n.confirmed_mean ?? n.metric);
	const minY = Math.min(...ys), maxY = Math.max(...ys);
	const canLog = minY > 0;
	const useLog = logY && canLog;
	const tf = (v) => useLog ? Math.log10(v) : v;
	const tMin = tf(minY), tMax = tf(maxY);
	const pad = 34, w = width, h = height;
	const x0 = Math.min(...xs), x1 = Math.max(...xs);
	const X = (id) => pad + (id - x0) / Math.max(1, x1 - x0) * (w - pad - 10);
	const Y = (v) => h - pad - (tf(v) - tMin) / Math.max(1e-9, tMax - tMin) * (h - pad - 12);
	const nearest = (px) => {
		let best = null, bd = 1e9;
		for (const n of evald) {
			const d = Math.abs(X(n.id) - px);
			if (d < bd) {
				bd = d;
				best = n;
			}
		}
		return best;
	};
	const hn = hoverId != null ? evald.find((n) => n.id === hoverId) : null;
	// running best — exclude infeasible (constraint-violating) nodes, mirroring engine selection
	// (replay.fold ranks only feasible nodes), so the line never claims a best the engine rejected.
	let best = null;
	const bestPts = [];
	const tableRows = [];
	evald.forEach((n) => {
		const v = n.confirmed_mean ?? n.metric;
		if (n.feasible !== false && (best === null || (direction === "min" ? v < best : v > best))) best = v;
		if (best !== null) bestPts.push([X(n.id), Y(best)]);
		tableRows.push({
			node: n.id,
			operator: n.operator || "—",
			theme: themeOf(n) || "untagged",
			metric: v,
			best,
			feasible: n.feasible === false ? "infeasible" : n.feasible === true ? "feasible" : "not reported"
		});
	});
	const line = bestPts.map((p, i) => (i ? "L" : "M") + p[0] + " " + p[1]).join(" ");
	const area = bestPts.length > 1 ? `${line} L ${bestPts[bestPts.length - 1][0]} ${h - pad} L ${bestPts[0][0]} ${h - pad} Z` : "";
	const marks = steps || [];
	const pick = onPick || null;
	// Dense trajectories use one nearest-x interaction surface. Per-point 30 px hit circles overlap
	// after roughly 25 nodes and make the DOM's last point win instead of the visually nearest point.
	const eventX = (e) => {
		const r = e.currentTarget.getBoundingClientRect();
		return (e.clientX - r.left) / Math.max(1, r.width) * w;
	};
	const markLabels = new Set();
	const markPositions = marks.map((s, i) => ({
		i,
		x: X(s.id)
	})).sort((a, b) => a.x - b.x);
	let lastLabel = null;
	markPositions.slice(0, -1).forEach((mark) => {
		if (!lastLabel || mark.x - lastLabel.x >= 40) {
			markLabels.add(mark.i);
			lastLabel = mark;
		}
	});
	const endLabel = markPositions.at(-1);
	if (endLabel) {
		if (lastLabel && endLabel.x - lastLabel.x < 40 && markLabels.size > 1) markLabels.delete(lastLabel.i);
		markLabels.add(endLabel.i);
	}
	const groupsPresent = [...new Set(evald.map(grpKey).filter(Boolean))];
	const groupMarker = (group) => {
		const index = Math.max(0, groupsPresent.indexOf(group));
		return {
			shape: MARK_SHAPES[index % MARK_SHAPES.length],
			variant: [
				"solid",
				"outline",
				"dot"
			][Math.floor(index / MARK_SHAPES.length) % 3]
		};
	};
	const hasThemes = evald.some(themeOf);
	const columns = [
		{
			key: "node",
			label: "Node",
			firstColumnHeader: true,
			render: (value) => pick ? /* @__PURE__ */ _jsxs("button", {
				type: "button",
				className: "btn xs ghost",
				onClick: () => pick(value),
				children: ["#", value]
			}) : `#${value}`
		},
		{
			key: "operator",
			label: "Operator"
		},
		{
			key: "theme",
			label: "Primary concept axis"
		},
		{
			key: "metric",
			label: "Metric",
			numeric: true
		},
		{
			key: "best",
			label: "Best so far",
			numeric: true
		},
		{
			key: "feasible",
			label: "Constraint status"
		}
	];
	return /* @__PURE__ */ _jsx(ChartFrame, {
		className: "chart",
		title: "Metric trajectory",
		description: `Evaluated experiments and running ${direction === "min" ? "minimum" : "maximum"}; colour, shape, fill and rings encode group and constraint status.${pick ? " Click the plot for the nearest node; keyboard users can use View data." : ""}`,
		columns,
		rows: tableRows,
		csvName: "metric-trajectory.csv",
		children: ({ labelledBy }) => /* @__PURE__ */ _jsxs(_Fragment, { children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "chart-tools",
				children: [hasThemes && /* @__PURE__ */ _jsxs("span", {
					className: "chart-grp",
					children: ["group:", ["operator", "theme"].map((g) => /* @__PURE__ */ _jsx("button", {
						type: "button",
						"aria-pressed": groupBy === g,
						className: "btn xs ghost" + (groupBy === g ? " primary" : ""),
						onClick: () => {
							setGroupBy(g);
							setFocusGrp(null);
						},
						title: `colour points by ${groupDimensionLabel(g)}`,
						children: groupDimensionLabel(g)
					}, g))]
				}), canLog && /* @__PURE__ */ _jsx("button", {
					type: "button",
					"aria-pressed": logY,
					className: "btn xs ghost" + (logY ? " primary" : ""),
					onClick: () => setLogY((v) => !v),
					title: "toggle a logarithmic Y axis",
					children: "log Y"
				})]
			}),
			/* @__PURE__ */ _jsxs("svg", {
				width: "100%",
				viewBox: `0 0 ${w} ${h}`,
				className: pick ? "pickable" : "",
				role: "img",
				"aria-labelledby": labelledBy,
				onClick: pick ? (e) => {
					const n = nearest(eventX(e));
					if (n) pick(n.id);
				} : undefined,
				onPointerMove: (e) => {
					const n = nearest(eventX(e));
					setHoverId(n ? n.id : null);
				},
				onPointerLeave: () => setHoverId(null),
				children: [
					/* @__PURE__ */ _jsx("defs", { children: /* @__PURE__ */ _jsxs("linearGradient", {
						id: "ll-traj-fill",
						x1: "0",
						y1: "0",
						x2: "0",
						y2: "1",
						children: [/* @__PURE__ */ _jsx("stop", {
							offset: "0%",
							stopColor: "#2ecc71",
							stopOpacity: ".20"
						}), /* @__PURE__ */ _jsx("stop", {
							offset: "100%",
							stopColor: "#2ecc71",
							stopOpacity: "0"
						})]
					}) }),
					[
						0,
						.25,
						.5,
						.75,
						1
					].map((t, i) => {
						const y = pad / 2 + t * (h - pad - 12);
						return /* @__PURE__ */ _jsx("line", {
							x1: pad,
							x2: w - 10,
							y1: y,
							y2: y,
							stroke: GRID
						}, i);
					}),
					area && /* @__PURE__ */ _jsx("path", {
						d: area,
						fill: "url(#ll-traj-fill)"
					}),
					selected != null && (() => {
						const sn = evald.find((n) => n.id === selected);
						if (!sn) return null;
						const v = sn.confirmed_mean ?? sn.metric;
						return /* @__PURE__ */ _jsx("circle", {
							cx: X(sn.id),
							cy: Y(v),
							r: "7.5",
							fill: "none",
							stroke: "var(--fg)",
							strokeWidth: "2",
							opacity: ".9",
							pointerEvents: "none"
						});
					})(),
					evald.map((n) => {
						const v = n.confirmed_mean ?? n.metric;
						const theme = themeOf(n);
						const c = grpColor(n);
						const dim = focusGrp && grpKey(n) !== focusGrp;
						const status = n.feasible === false ? "infeasible" : n.feasible === true ? "feasible" : "unknown";
						const marker = groupMarker(grpKey(n));
						return /* @__PURE__ */ _jsx(PointMark, {
							className: "chart-pt" + (pick ? " pick" : ""),
							x: X(n.id),
							y: Y(v),
							size: n.id === selected ? 5 : 4,
							color: c,
							shape: marker.shape,
							variant: marker.variant,
							feasibility: status,
							opacity: dim ? .12 : .88,
							title: `#${n.id} ${n.operator || ""}${theme ? ` (${theme})` : ""} → ${fmt(v)} · ${status === "unknown" ? "constraint status not reported" : status}`
						}, n.id);
					}),
					/* @__PURE__ */ _jsx("path", {
						d: line,
						fill: "none",
						stroke: "var(--fg)",
						strokeWidth: "4",
						opacity: ".78"
					}),
					/* @__PURE__ */ _jsx("path", {
						d: line,
						fill: "none",
						stroke: "var(--ok)",
						strokeWidth: "2.2"
					}),
					marks.map((s, i) => {
						const v = s.to, x = X(s.id), y = Y(v);
						return /* @__PURE__ */ _jsxs("g", {
							className: pick ? "chart-mark pick" : "chart-mark",
							children: [
								/* @__PURE__ */ _jsx("circle", {
									cx: x,
									cy: y,
									r: "5",
									fill: "none",
									stroke: "var(--fg)",
									strokeWidth: "3.2",
									opacity: ".78"
								}),
								/* @__PURE__ */ _jsx("circle", {
									cx: x,
									cy: y,
									r: "5",
									fill: "none",
									stroke: "var(--ok)",
									strokeWidth: "1.6"
								}),
								markLabels.has(i) && /* @__PURE__ */ _jsxs(_Fragment, { children: [
									/* @__PURE__ */ _jsx("line", {
										x1: x,
										x2: x,
										y1: y - 6,
										y2: Math.max(14, y - 20),
										stroke: "var(--fg)",
										strokeWidth: "2.4",
										opacity: ".78"
									}),
									/* @__PURE__ */ _jsx("line", {
										x1: x,
										x2: x,
										y1: y - 6,
										y2: Math.max(14, y - 20),
										stroke: "var(--ok)",
										strokeWidth: "1"
									}),
									/* @__PURE__ */ _jsxs("text", {
										className: "trajectory-step-label",
										x,
										y: Math.max(11, y - 22),
										fill: "var(--ok)",
										fontSize: "9.5",
										textAnchor: "middle",
										children: ["#", s.id]
									})
								] }),
								/* @__PURE__ */ _jsx("title", { children: `#${s.id} ${s.operator || ""}${s.theme ? ` (${s.theme})` : ""} → ${fmt(v)}${s.delta != null ? ` (Δ ${fmt(s.delta)})` : " baseline"}` })
							]
						}, i);
					}),
					hn && (() => {
						const v = hn.confirmed_mean ?? hn.metric, hx = X(hn.id), hy = Y(v);
						const label = `#${hn.id} ${hn.operator || ""} → ${fmt(v)}`;
						const tw = Math.max(64, label.length * 6), tx = Math.min(w - 10 - tw, Math.max(pad, hx - tw / 2));
						return /* @__PURE__ */ _jsxs("g", {
							pointerEvents: "none",
							children: [
								/* @__PURE__ */ _jsx("line", {
									x1: hx,
									x2: hx,
									y1: pad / 2,
									y2: h - pad,
									stroke: AX,
									strokeDasharray: "3 3",
									opacity: ".6"
								}),
								/* @__PURE__ */ _jsx("circle", {
									cx: hx,
									cy: hy,
									r: "5",
									fill: "none",
									stroke: "var(--fg)",
									strokeWidth: "1.5"
								}),
								/* @__PURE__ */ _jsx("rect", {
									x: tx,
									y: 2,
									width: tw,
									height: 16,
									rx: "3",
									fill: "var(--bg-1)",
									stroke: GRID
								}),
								/* @__PURE__ */ _jsx("text", {
									x: tx + tw / 2,
									y: 13,
									fill: "var(--fg)",
									fontSize: "10.5",
									textAnchor: "middle",
									children: label
								})
							]
						});
					})(),
					/* @__PURE__ */ _jsxs("text", {
						x: pad,
						y: 12,
						fill: AX,
						fontSize: "11",
						children: [
							"best so far: ",
							fmt(best),
							useLog ? " · log Y" : ""
						]
					}),
					/* @__PURE__ */ _jsx("text", {
						x: pad,
						y: h - 8,
						fill: AX,
						fontSize: "11",
						children: "node id →"
					})
				]
			}),
			/* @__PURE__ */ _jsx(ChartLegend, {
				items: groupsPresent.map((g) => ({
					key: g,
					label: grpLabel(g),
					color: grpSwatch(g),
					...groupMarker(g)
				})),
				active: focusGrp,
				onPick: setFocusGrp
			}),
			(evald.some((n) => n.feasible === false) || evald.some((n) => n.feasible == null)) && /* @__PURE__ */ _jsxs("div", {
				className: "chart-status-legend",
				"aria-label": "Constraint marker legend",
				children: [evald.some((n) => n.feasible === false) && /* @__PURE__ */ _jsxs("span", { children: [/* @__PURE__ */ _jsx("span", { className: "chart-status-ring dashed" }), " dashed ring · infeasible"] }), evald.some((n) => n.feasible == null) && /* @__PURE__ */ _jsxs("span", { children: [/* @__PURE__ */ _jsx("span", { className: "chart-status-ring" }), " solid ring · status not reported"] })]
			})
		] })
	});
}
// Waterfall of the key improvements: each bar is the metric the frontier reached at that step;
// the baseline is the first best, and each subsequent bar's coloured segment is the gain it added.
export function ImprovementWaterfall({ steps, direction, width = 760 }) {
	if (!steps || !steps.length) return /* @__PURE__ */ _jsx(Empty, { children: "no improvement steps yet" });
	// Bound only the visual layer: the named table and CSV below retain every exact row. Keeping the
	// baseline plus the latest 99 steps gives long runs a useful endpoint without an unbounded SVG.
	const shown = steps.length > 100 ? [steps[0], ...steps.slice(-99)] : steps;
	const vals = shown.flatMap((s) => [s.from, s.to]).filter(Number.isFinite);
	const lo = Math.min(...vals), hi = Math.max(...vals);
	const pad = 40, plotW = Math.max(width, pad * 2 + shown.length * 32);
	const slot = (plotW - 2 * pad) / shown.length;
	const bw = Math.max(4, Math.min(64, slot - 8));
	const h = 200, base = h - 26;
	const Y = (v) => hi === lo ? (base + 16) / 2 : 16 + (1 - (v - lo) / (hi - lo)) * (base - 16);
	const rows = steps.map((step) => ({
		node: step.id,
		operator: step.operator || "—",
		from: step.from,
		to: step.to,
		delta: step.delta
	}));
	const columns = [
		{
			key: "node",
			label: "Node",
			firstColumnHeader: true,
			render: (value) => `#${value}`
		},
		{
			key: "operator",
			label: "Operator"
		},
		{
			key: "from",
			label: "Previous",
			numeric: true
		},
		{
			key: "to",
			label: "Metric",
			numeric: true
		},
		{
			key: "delta",
			label: "Delta",
			numeric: true
		}
	];
	const baselineOnly = steps.length === 1 && steps[0].from == null;
	const labelEvery = Math.max(1, Math.ceil(48 / slot));
	const labelled = (i) => i === 0 || i === shown.length - 1 || i % labelEvery === 0 && i * slot >= 48 && (shown.length - 1 - i) * slot >= 48;
	return /* @__PURE__ */ _jsx(ChartFrame, {
		title: baselineOnly ? "Metric baseline" : "Improvement waterfall",
		description: baselineOnly ? "First feasible metric; no improvement is recorded yet." : `Frontier changes for a ${direction === "min" ? "minimization" : "maximization"} objective.`,
		columns,
		rows,
		csvName: "improvement-waterfall.csv",
		children: ({ labelledBy }) => /* @__PURE__ */ _jsxs(_Fragment, { children: [shown.length < steps.length && /* @__PURE__ */ _jsxs("div", {
			className: "muted",
			role: "note",
			children: [
				"Showing the baseline and latest 99 of ",
				steps.length,
				" steps; View data and CSV include all ",
				steps.length,
				"."
			]
		}), /* @__PURE__ */ _jsxs("svg", {
			width: plotW,
			viewBox: `0 0 ${plotW} ${h}`,
			role: "img",
			"aria-labelledby": labelledBy,
			children: [/* @__PURE__ */ _jsx("line", {
				x1: pad,
				x2: plotW - pad,
				y1: base,
				y2: base,
				stroke: GRID
			}), shown.map((s, i) => {
				const x = pad + i * slot + (slot - bw) / 2;
				const yTo = Y(s.to), yFrom = s.from == null ? base : Y(s.from);
				const top = Math.min(yTo, yFrom), hgt = Math.max(3, Math.abs(yTo - yFrom));
				const improved = s.delta == null || (direction === "min" ? s.delta < 0 : s.delta > 0);
				return /* @__PURE__ */ _jsxs("g", { children: [
					/* @__PURE__ */ _jsx("rect", {
						className: "waterfall-bar",
						x,
						y: s.from == null ? yTo : top,
						width: bw,
						height: s.from == null ? Math.max(3, base - yTo) : hgt,
						rx: "3",
						fill: s.from == null ? "#4aa3ff" : improved ? "#2ecc71" : "#ef4444",
						stroke: "var(--fg)",
						strokeWidth: ".75",
						opacity: s.from == null ? .72 : .9
					}),
					labelled(i) && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsxs("text", {
						className: "waterfall-step-label",
						x: x + bw / 2,
						y: Math.max(11, top - 4),
						fill: "var(--fg-dim)",
						fontSize: "9.5",
						textAnchor: "middle",
						children: [
							s.from == null ? "" : improved ? "▲ " : "▼ ",
							"#",
							s.id
						]
					}), /* @__PURE__ */ _jsx("text", {
						x: x + bw / 2,
						y: base + 12,
						fill: AX,
						fontSize: "9.5",
						textAnchor: "middle",
						children: fmt(s.to)
					})] }),
					/* @__PURE__ */ _jsx("title", { children: `#${s.id} ${s.operator || ""} → ${fmt(s.to)}${s.delta != null ? ` (Δ ${fmt(s.delta)}, ${improved ? "improved" : "regressed"})` : " (baseline)"}` })
				] }, i);
			})]
		})] })
	});
}
export function Bars({ data, width = 760, height = 220, color = "#4aa3ff", fmtv = fmt }) {
	// data: [{label, value}]
	if (!data || !data.length) return /* @__PURE__ */ _jsx(Empty, { children: "no data" });
	const max = Math.max(...data.map((d) => Math.abs(d.value)), 1e-9);
	const bh = 22, gap = 8, lab = 150, w = width;
	const h = Math.max(height, data.length * (bh + gap) + 10);
	const columns = [{
		key: "label",
		label: "Label",
		firstColumnHeader: true
	}, {
		key: "value",
		label: "Value",
		numeric: true
	}];
	return /* @__PURE__ */ _jsx(ChartFrame, {
		title: "Value comparison",
		description: "Bar lengths and exact values compare each item.",
		columns,
		rows: data,
		csvName: "bar-values.csv",
		children: ({ labelledBy }) => /* @__PURE__ */ _jsx("svg", {
			width: "100%",
			viewBox: `0 0 ${w} ${h}`,
			role: "img",
			"aria-labelledby": labelledBy,
			children: data.map((d, i) => {
				const y = i * (bh + gap) + 4;
				const bw = Math.abs(d.value) / max * (w - lab - 60);
				return /* @__PURE__ */ _jsxs("g", { children: [
					/* @__PURE__ */ _jsx("text", {
						x: lab - 8,
						y: y + bh / 2 + 4,
						fill: "var(--fg)",
						fontSize: "12",
						textAnchor: "end",
						children: d.label
					}),
					/* @__PURE__ */ _jsx("rect", {
						x: lab,
						y,
						width: bw,
						height: bh,
						rx: "3",
						fill: color,
						stroke: "var(--fg)",
						strokeWidth: ".75",
						opacity: ".85"
					}),
					/* @__PURE__ */ _jsx("text", {
						x: lab + bw + 6,
						y: y + bh / 2 + 4,
						fill: AX,
						fontSize: "11",
						children: fmtv(d.value)
					})
				] }, i);
			})
		})
	});
}
// Gantt of span timing per node. `onPick(nid)` drills into the clicked span's node.
export function Gantt({ spans, width = 760, onPick }) {
	const flat = [];
	const walk = (arr, nid) => arr.forEach((s) => {
		flat.push({
			nid,
			name: s.name,
			start: s.start,
			dur: s.duration_s || 0,
			err: s.status === "ERROR"
		});
		if (s.children) walk(s.children, nid);
	});
	Object.entries(spans?.nodes || {}).forEach(([nid, arr]) => walk(arr, nid));
	if (!flat.length) return /* @__PURE__ */ _jsx(Empty, { children: "no spans recorded" });
	const t0 = Math.min(...flat.map((s) => s.start));
	const t1 = Math.max(...flat.map((s) => s.start + s.dur));
	const span = Math.max(1e-6, t1 - t0);
	const rowH = 28, lab = 150, w = width, h = flat.length * rowH + 24;
	const X = (t) => lab + (t - t0) / span * (w - lab - 20);
	const palette = {
		evaluate: "#2ecc71",
		implement: "#4aa3ff",
		propose: "#9a6bff",
		repair: "#ef4444",
		setup: "#f0b429",
		command: "#4aa3ff"
	};
	const columns = [
		{
			key: "nid",
			label: "Node",
			firstColumnHeader: true,
			render: (value) => onPick ? /* @__PURE__ */ _jsxs("button", {
				type: "button",
				className: "btn xs ghost",
				onClick: () => onPick(value),
				children: ["#", value]
			}) : `#${value}`
		},
		{
			key: "name",
			label: "Span"
		},
		{
			key: "start",
			label: "Started",
			numeric: true
		},
		{
			key: "dur",
			label: "Duration (s)",
			numeric: true
		},
		{
			key: "err",
			label: "Error"
		}
	];
	return /* @__PURE__ */ _jsx(ChartFrame, {
		title: "Execution span timeline",
		description: "Start time and duration for each recorded node span; failed spans also use a dashed outline.",
		columns,
		rows: flat,
		csvName: "execution-spans.csv",
		children: ({ labelledBy }) => /* @__PURE__ */ _jsxs("svg", {
			width: "100%",
			viewBox: `0 0 ${w} ${h}`,
			role: "img",
			"aria-labelledby": labelledBy,
			children: [flat.map((s, i) => {
				const y = i * rowH + 4, x = X(s.start), bw = Math.max(2, s.dur / span * (w - lab - 20));
				return /* @__PURE__ */ _jsxs("g", {
					onClick: onPick ? () => onPick(s.nid) : undefined,
					style: onPick ? { cursor: "pointer" } : undefined,
					children: [
						/* @__PURE__ */ _jsx("title", { children: `${s.nid}:${s.name} — ${fmt(s.dur, 3)}s${s.err ? " (ERROR)" : ""}` }),
						onPick && /* @__PURE__ */ _jsx("rect", {
							className: "chart-hit-area",
							x: "0",
							y: y - 2,
							width: w,
							height: rowH,
							fill: "transparent"
						}),
						/* @__PURE__ */ _jsxs("text", {
							x: lab - 6,
							y: y + 15,
							fill: "var(--fg-dim)",
							fontSize: "10",
							textAnchor: "end",
							children: [
								s.nid,
								":",
								s.name
							]
						}),
						/* @__PURE__ */ _jsx("rect", {
							x,
							y,
							width: bw,
							height: rowH - 5,
							rx: "2",
							fill: s.err ? "#ef4444" : palette[s.name] || "#4aa3ff",
							stroke: "var(--fg)",
							strokeWidth: s.err ? 1.2 : .65,
							strokeDasharray: s.err ? "3 2" : undefined,
							opacity: ".85"
						})
					]
				}, i);
			}), /* @__PURE__ */ _jsxs("text", {
				x: lab,
				y: h - 4,
				fill: AX,
				fontSize: "11",
				children: [fmt(span), "s total span"]
			})]
		})
	});
}
// Parallel coordinates of params -> metric.
export function ParallelCoords({ nodes, direction, width = 760, height = 260, onPick = null }) {
	const ev = nodes.filter((n) => (n.metric ?? null) !== null);
	if (!ev.length) return /* @__PURE__ */ _jsx(Empty, { children: "no evaluated nodes" });
	const pick = onPick || null;
	const isNum = (v) => v != null && Number.isFinite(Number(v));
	// Only numeric params can be plotted on a value axis; a string param (optimizer=adam) would give
	// NaN coordinates and blank the whole chart, so drop non-numeric axes here.
	const params = Array.from(new Set(ev.flatMap((n) => Object.keys(n.idea?.params || {})))).filter((a) => ev.some((n) => isNum(n.idea?.params?.[a])));
	const axes = [...params, "metric"];
	if (axes.length < 2) return /* @__PURE__ */ _jsx(Empty, { children: "not enough dimensions" });
	const vals = (n, a) => {
		const v = a === "metric" ? n.confirmed_mean ?? n.metric : n.idea?.params?.[a];
		return isNum(v) ? Number(v) : null;
	};
	const ranges = {};
	axes.forEach((a) => {
		const xs = ev.map((n) => vals(n, a)).filter((v) => v != null);
		ranges[a] = [Math.min(...xs), Math.max(...xs)];
	});
	const pad = 40, w = width, h = height;
	const AXX = (i) => pad + i / (axes.length - 1) * (w - 2 * pad);
	const AXY = (a, v) => {
		const [lo, hi] = ranges[a];
		return h - pad - (v - lo) / Math.max(1e-9, hi - lo) * (h - 2 * pad);
	};
	const ms = ev.map((n) => n.confirmed_mean ?? n.metric);
	const mlo = Math.min(...ms), mhi = Math.max(...ms);
	const colorOf = (m) => {
		let t = (m - mlo) / Math.max(1e-9, mhi - mlo);
		if (direction === "min") t = 1 - t;
		return `hsl(${120 * t}, 65%, 55%)`;
	};
	const rows = ev.map((node) => ({ source: node }));
	const columns = [
		{
			key: "node",
			label: "Node",
			firstColumnHeader: true,
			value: (row) => row.source.id,
			render: (value) => pick ? /* @__PURE__ */ _jsxs("button", {
				type: "button",
				className: "btn xs ghost",
				onClick: () => pick(value),
				children: ["#", value]
			}) : `#${value}`
		},
		{
			key: "operator",
			label: "Operator",
			value: (row) => row.source.operator || "—"
		},
		...axes.map((axis, index) => ({
			key: `axis-${index}`,
			label: axis,
			numeric: true,
			value: (row) => vals(row.source, axis)
		}))
	];
	return /* @__PURE__ */ _jsx(ChartFrame, {
		title: "Parameter relationships",
		description: "Parallel coordinates connect each experiment's numeric parameters to its metric; exact values are available below.",
		columns,
		rows,
		csvName: "parallel-coordinates.csv",
		children: ({ labelledBy }) => /* @__PURE__ */ _jsxs("svg", {
			width: "100%",
			viewBox: `0 0 ${w} ${h}`,
			role: "img",
			"aria-labelledby": labelledBy,
			children: [axes.map((a, i) => /* @__PURE__ */ _jsxs("g", { children: [/* @__PURE__ */ _jsx("line", {
				x1: AXX(i),
				x2: AXX(i),
				y1: pad,
				y2: h - pad,
				stroke: GRID
			}), /* @__PURE__ */ _jsx("text", {
				x: AXX(i),
				y: h - pad + 14,
				fill: AX,
				fontSize: "11",
				textAnchor: "middle",
				children: a
			})] }, a)), ev.map((n) => {
				const pts = axes.map((a, i) => {
					const v = vals(n, a);
					return v == null ? null : [AXX(i), AXY(a, v)];
				}).filter(Boolean);
				const d = pts.map((p, i) => (i ? "L" : "M") + p[0] + " " + p[1]).join(" ");
				return /* @__PURE__ */ _jsxs("g", {
					className: pick ? "pick" : undefined,
					onClick: pick ? () => pick(n.id) : undefined,
					children: [
						pick && /* @__PURE__ */ _jsx("path", {
							className: "chart-hit-area",
							d,
							fill: "none",
							stroke: "transparent",
							strokeWidth: "28"
						}),
						/* @__PURE__ */ _jsx("path", {
							d,
							fill: "none",
							stroke: "var(--fg)",
							strokeWidth: "3.5",
							opacity: ".78",
							pointerEvents: "none"
						}),
						/* @__PURE__ */ _jsx("path", {
							className: "pc-line" + (pick ? " pick" : ""),
							d,
							fill: "none",
							stroke: colorOf(n.confirmed_mean ?? n.metric),
							strokeWidth: "1.8",
							opacity: ".86",
							pointerEvents: "none"
						}),
						/* @__PURE__ */ _jsx("title", { children: `#${n.id} ${n.operator || ""} → ${fmt(n.confirmed_mean ?? n.metric)}` })
					]
				}, n.id);
			})]
		})
	});
}
// metric vs a constraint value (Pareto-ish). data: [{x,y,feasible,id}]
// `onPick(id)` (optional) drills into a point's node (points carrying an `id`).
export function Scatter({ data, xlab, ylab, width = 720, height = 260, onPick = null }) {
	if (!data || !data.length) return /* @__PURE__ */ _jsx(Empty, { children: "no constraint data" });
	const xs = data.map((d) => d.x), ys = data.map((d) => d.y);
	const pad = 40, w = width, h = height;
	// Hoist the extents out of the scale closures: recomputing Math.min/Math.max(...xs) inside X()/Y()
	// made scaling O(n²) over the point set (once per point × once per call). Compute the range once.
	const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
	const X = (v) => pad + (v - minX) / Math.max(1e-9, maxX - minX) * (w - 2 * pad);
	const Y = (v) => h - pad - (v - minY) / Math.max(1e-9, maxY - minY) * (h - 2 * pad);
	const pick = onPick || null;
	const statusOf = (value) => value === true ? "feasible" : value === false ? "infeasible" : "unknown";
	const tableRows = data.map((point) => ({
		...point,
		feasible: statusOf(point.feasible)
	}));
	const columns = [
		{
			key: "id",
			label: "Node",
			firstColumnHeader: true,
			render: (value) => value == null ? "—" : pick ? /* @__PURE__ */ _jsxs("button", {
				type: "button",
				className: "btn xs ghost",
				onClick: () => pick(value),
				children: ["#", value]
			}) : `#${value}`
		},
		{
			key: "x",
			label: xlab,
			numeric: true
		},
		{
			key: "y",
			label: ylab,
			numeric: true
		},
		{
			key: "feasible",
			label: "Feasible"
		}
	];
	return /* @__PURE__ */ _jsx(ChartFrame, {
		title: `${ylab} by ${xlab}`,
		description: "Feasible, infeasible, and unknown points use different shapes as well as colours; every point also has an exact text row.",
		columns,
		rows: tableRows,
		csvName: "scatter-data.csv",
		children: ({ labelledBy }) => /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsxs("svg", {
			width: "100%",
			viewBox: `0 0 ${w} ${h}`,
			className: pick ? "pickable" : "",
			role: "img",
			"aria-labelledby": labelledBy,
			children: [
				[
					0,
					.5,
					1
				].map((t, i) => {
					const y = pad + t * (h - 2 * pad);
					return /* @__PURE__ */ _jsx("line", {
						x1: pad,
						x2: w - pad,
						y1: y,
						y2: y,
						stroke: GRID
					}, i);
				}),
				data.map((d, i) => {
					const status = statusOf(d.feasible);
					return /* @__PURE__ */ _jsx(PointMark, {
						className: "chart-pt" + (pick && d.id != null ? " pick" : ""),
						x: X(d.x),
						y: Y(d.y),
						size: 4.5,
						shape: status === "feasible" ? "circle" : status === "infeasible" ? "diamond" : "square",
						color: status === "feasible" ? "#2ecc71" : status === "infeasible" ? "#9a6bff" : "#7f8998",
						feasibility: status,
						opacity: ".88",
						onClick: pick && d.id != null ? () => pick(d.id) : null,
						title: `${d.id != null ? `#${d.id} · ` : ""}${xlab} ${fmt(d.x)} · ${ylab} ${fmt(d.y)} · ${status === "unknown" ? "constraint status not reported" : status}`
					}, i);
				}),
				/* @__PURE__ */ _jsx("text", {
					x: w / 2,
					y: h - 6,
					fill: AX,
					fontSize: "11",
					textAnchor: "middle",
					children: xlab
				}),
				/* @__PURE__ */ _jsx("text", {
					x: 12,
					y: 14,
					fill: AX,
					fontSize: "11",
					children: ylab
				})
			]
		}), /* @__PURE__ */ _jsx(ChartLegend, { items: [
			{
				key: "feasible",
				label: "feasible",
				color: "#2ecc71",
				shape: "circle"
			},
			{
				key: "infeasible",
				label: "infeasible",
				color: "#9a6bff",
				shape: "diamond"
			},
			{
				key: "unknown",
				label: "status not reported",
				color: "#7f8998",
				shape: "square"
			}
		].filter((item) => data.some((point) => statusOf(point.feasible) === item.key)) })] })
	});
}
// Tiny sparkline of a numeric series — used by collapsed-group super-cards, sweep node cards, and
// the inspector. Returns null for <2 points (nothing meaningful to draw).
export function Spark({ series, width = 120, height = 22, label = null }) {
	if (!series || series.length < 2) return null;
	const lo = Math.min(...series), hi = Math.max(...series), span = hi - lo || 1;
	const W = width, H = height;
	const pts = series.map((v, i) => `${(i / (series.length - 1) * W).toFixed(1)},${(H - (v - lo) / span * H).toFixed(1)}`).join(" ");
	return /* @__PURE__ */ _jsx("svg", {
		className: "grp-spark",
		width: W,
		height: H,
		role: "img",
		"aria-label": label || `Trend across ${series.length} values, from ${fmt(series[0])} to ${fmt(series[series.length - 1])}`,
		children: /* @__PURE__ */ _jsx("polyline", {
			points: pts,
			fill: "none",
			stroke: "var(--accent)",
			strokeWidth: "1.5"
		})
	});
}
function Empty({ children }) {
	return /* @__PURE__ */ _jsx("div", {
		className: "muted",
		style: { padding: 20 },
		children
	});
}
// U4 · overlay several runs' running-best trajectories on ONE axis, to compare convergence at a
// glance. `runs` = [{label, run_id, series:[running-best value per evaluated node]}]. x = experiment
// index (runs have different lengths — each line just stops at its own end); y = shared metric range.
const _RUN_COLORS = [
	"#4aa3ff",
	"#2ecc71",
	"#f0b429",
	"#e0559a",
	"#8b5cf6",
	"#22d3d3",
	"#ff7a45",
	"#9aa7b5"
];
const _RUN_DASHES = [
	"",
	"7 3",
	"2 3",
	"9 3 2 3",
	"5 2",
	"1 3",
	"10 3",
	"4 3 1 3"
];
export function MultiTrajectory({ runs, width = 760, height = 240 }) {
	const withData = (runs || []).filter((r) => (r.series || []).length > 0);
	if (!withData.length) return /* @__PURE__ */ _jsx(Empty, { children: "no comparable run trajectories yet" });
	const allV = withData.flatMap((r) => r.series);
	const lo = Math.min(...allV), hi = Math.max(...allV), span = hi - lo || 1;
	const maxLen = Math.max(...withData.map((r) => r.series.length));
	const pad = 34, w = width, h = height;
	const X = (i) => pad + (maxLen <= 1 ? 0 : i / (maxLen - 1) * (w - pad - 10));
	const Y = (v) => h - pad - (v - lo) / span * (h - pad - 12);
	const rows = withData.flatMap((run) => run.series.map((metric, experiment) => ({
		run: run.label || run.run_id,
		run_id: run.run_id,
		experiment,
		metric
	})));
	const columns = [
		{
			key: "run",
			label: "Run",
			firstColumnHeader: true
		},
		{
			key: "run_id",
			label: "Run id"
		},
		{
			key: "experiment",
			label: "Experiment",
			numeric: true
		},
		{
			key: "metric",
			label: "Running best",
			numeric: true
		}
	];
	return /* @__PURE__ */ _jsx(ChartFrame, {
		title: "Cross-run trajectories",
		description: "Each run uses both a hue and a dash pattern; the table contains every exact point.",
		columns,
		rows,
		csvName: "run-trajectories.csv",
		children: ({ labelledBy }) => /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsxs("svg", {
			width: w,
			height: h,
			role: "img",
			"aria-labelledby": labelledBy,
			children: [
				/* @__PURE__ */ _jsx("line", {
					x1: pad,
					y1: h - pad,
					x2: w - 8,
					y2: h - pad,
					stroke: "var(--border)"
				}),
				/* @__PURE__ */ _jsx("line", {
					x1: pad,
					y1: 12,
					x2: pad,
					y2: h - pad,
					stroke: "var(--border)"
				}),
				/* @__PURE__ */ _jsx("text", {
					x: pad - 6,
					y: 16,
					textAnchor: "end",
					fontSize: "10",
					fill: "var(--fg-mut)",
					children: fmt(hi)
				}),
				/* @__PURE__ */ _jsx("text", {
					x: pad - 6,
					y: h - pad,
					textAnchor: "end",
					fontSize: "10",
					fill: "var(--fg-mut)",
					children: fmt(lo)
				}),
				/* @__PURE__ */ _jsx("text", {
					x: (w + pad) / 2,
					y: h - 6,
					textAnchor: "middle",
					fontSize: "10",
					fill: "var(--fg-mut)",
					children: "experiment #"
				}),
				withData.map((r, k) => {
					const c = _RUN_COLORS[k % _RUN_COLORS.length];
					const dash = _RUN_DASHES[k % _RUN_DASHES.length];
					const pts = r.series.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
					return /* @__PURE__ */ _jsxs("g", { children: [/* @__PURE__ */ _jsx("polyline", {
						points: pts,
						fill: "none",
						stroke: "var(--fg)",
						strokeDasharray: dash || undefined,
						strokeWidth: "4",
						opacity: ".78"
					}), /* @__PURE__ */ _jsx("polyline", {
						points: pts,
						fill: "none",
						stroke: c,
						strokeDasharray: dash || undefined,
						strokeWidth: "2.1",
						opacity: "0.95"
					})] }, r.run_id || k);
				})
			]
		}), /* @__PURE__ */ _jsx("div", {
			className: "row",
			style: {
				flexWrap: "wrap",
				gap: 10,
				marginTop: 4
			},
			children: withData.map((r, k) => /* @__PURE__ */ _jsxs("span", {
				className: "muted",
				style: {
					fontSize: 11,
					display: "inline-flex",
					alignItems: "center",
					gap: 4
				},
				children: [/* @__PURE__ */ _jsxs("svg", {
					width: "18",
					height: "8",
					"aria-hidden": "true",
					children: [/* @__PURE__ */ _jsx("line", {
						x1: "0",
						x2: "18",
						y1: "4",
						y2: "4",
						stroke: "var(--fg)",
						strokeDasharray: _RUN_DASHES[k % _RUN_DASHES.length] || undefined,
						strokeWidth: "4",
						opacity: ".78"
					}), /* @__PURE__ */ _jsx("line", {
						x1: "0",
						x2: "18",
						y1: "4",
						y2: "4",
						stroke: _RUN_COLORS[k % _RUN_COLORS.length],
						strokeDasharray: _RUN_DASHES[k % _RUN_DASHES.length] || undefined,
						strokeWidth: "2"
					})]
				}), r.label || r.run_id]
			}, r.run_id || k))
		})] })
	});
}
// Online training/eval curves — a small line chart per logged metric tag (loss, every recall@k, lr,
// grad norms, …) from a node's TensorBoard series {tag: [{step, value}]}. ALL metrics, not just the
// objective — the "a la TensorBoard" per-node view.
export function MetricLines({ series, cols = 2 }) {
	const tags = Object.keys(series || {}).filter((t) => (series[t] || []).length > 0).sort();
	if (!tags.length) return /* @__PURE__ */ _jsx(Empty, { children: "no metric curves logged yet — they appear once training starts writing TensorBoard events" });
	// Group by the tag prefix before the first '/' (TensorBoard convention: train/loss, val/recall@100,
	// …); a tag with no slash falls into "other". Each group is an independent COLLAPSIBLE section so a
	// run that logs dozens of scalars isn't one endless wall of charts.
	const groups = {};
	for (const t of tags) {
		const i = t.indexOf("/");
		const g = i > 0 ? t.slice(0, i) : "other";
		(groups[g] || (groups[g] = [])).push(t);
	}
	const names = Object.keys(groups).sort();
	return /* @__PURE__ */ _jsx("div", { children: names.map((g) => /* @__PURE__ */ _jsx(MetricGroup, {
		name: g,
		tags: groups[g],
		series,
		cols
	}, g)) });
}
function MetricGroup({ name, tags, series, cols }) {
	const [open, setOpen] = React.useState(false);
	const groupId = `metric-group-${React.useId().replaceAll(":", "")}`;
	return /* @__PURE__ */ _jsxs("div", {
		style: { marginBottom: 8 },
		children: [/* @__PURE__ */ _jsxs("button", {
			type: "button",
			className: "metric-group-toggle",
			"aria-expanded": open,
			"aria-controls": groupId,
			onClick: () => setOpen((o) => !o),
			children: [
				/* @__PURE__ */ _jsx("span", {
					style: {
						opacity: .6,
						fontSize: 10,
						width: 10,
						display: "inline-block"
					},
					children: open ? "▾" : "▸"
				}),
				name,
				" ",
				/* @__PURE__ */ _jsxs("span", {
					className: "muted",
					style: { fontWeight: 400 },
					children: [
						"· ",
						tags.length,
						" metric",
						tags.length === 1 ? "" : "s"
					]
				})
			]
		}), open && /* @__PURE__ */ _jsx("div", {
			id: groupId,
			className: "metric-group-grid",
			style: {
				display: "grid",
				gridTemplateColumns: `repeat(${cols}, minmax(0,1fr))`,
				gap: 10
			},
			children: tags.map((t) => /* @__PURE__ */ _jsx(MiniLine, {
				label: t,
				pts: series[t]
			}, t))
		})]
	});
}
export function MiniLine({ label, pts, width = 340, height = 130 }) {
	const [hi, setHi] = React.useState(null);
	const xs = pts.map((p) => p.step), ys = pts.map((p) => p.value);
	const minX = Math.min(...xs), maxX = Math.max(...xs);
	const minY = Math.min(...ys), maxY = Math.max(...ys);
	const pad = 30, w = width, h = height;
	const X = (v) => pad + (v - minX) / Math.max(1e-9, maxX - minX) * (w - pad - 8);
	const Y = (v) => h - pad - (v - minY) / Math.max(1e-9, maxY - minY) * (h - pad - 16);
	const d = pts.map((p, i) => (i ? "L" : "M") + X(p.step).toFixed(1) + " " + Y(p.value).toFixed(1)).join(" ");
	const last = ys[ys.length - 1];
	const nearestIdx = (px) => {
		let bi = 0, bd = 1e9;
		pts.forEach((p, i) => {
			const dd = Math.abs(X(p.step) - px);
			if (dd < bd) {
				bd = dd;
				bi = i;
			}
		});
		return bi;
	};
	const hp = hi != null ? pts[hi] : null;
	const columns = [
		{
			key: "step",
			label: "Step",
			firstColumnHeader: true,
			numeric: true
		},
		{
			key: "value",
			label: "Value",
			numeric: true
		},
		{
			key: "wall_time",
			label: "Wall time",
			numeric: true
		}
	];
	const csvName = `${String(label).replace(/[^a-z0-9._-]+/gi, "_").slice(0, 80) || "metric"}.csv`;
	return /* @__PURE__ */ _jsx("div", {
		style: {
			border: `1px solid ${GRID}`,
			borderRadius: 6,
			padding: 6,
			background: "var(--bg-1)"
		},
		children: /* @__PURE__ */ _jsx(ChartFrame, {
			title: label,
			description: `${hp ? `Step ${hp.step}: ${fmt(hp.value)}` : `Latest ${fmt(last)}`} · ${pts.length} points`,
			columns,
			rows: pts,
			csvName,
			className: "metric-mini-chart",
			children: ({ labelledBy }) => /* @__PURE__ */ _jsxs("svg", {
				width: "100%",
				viewBox: `0 0 ${w} ${h}`,
				role: "img",
				"aria-labelledby": labelledBy,
				onPointerMove: (e) => {
					const r = e.currentTarget.getBoundingClientRect();
					setHi(nearestIdx((e.clientX - r.left) / r.width * w));
				},
				onPointerLeave: () => setHi(null),
				children: [
					[
						0,
						.5,
						1
					].map((t, i) => {
						const y = pad / 2 + t * (h - pad - 16);
						return /* @__PURE__ */ _jsx("line", {
							x1: pad,
							x2: w - 8,
							y1: y,
							y2: y,
							stroke: GRID
						}, i);
					}),
					/* @__PURE__ */ _jsx("path", {
						d,
						fill: "none",
						stroke: "var(--fg)",
						strokeWidth: "3.8",
						opacity: ".78"
					}),
					/* @__PURE__ */ _jsx("path", {
						d,
						fill: "none",
						stroke: "var(--ok)",
						strokeWidth: "1.8"
					}),
					hp && /* @__PURE__ */ _jsxs(_Fragment, { children: [/* @__PURE__ */ _jsx("line", {
						x1: X(hp.step),
						x2: X(hp.step),
						y1: pad / 2,
						y2: h - pad,
						stroke: AX,
						strokeDasharray: "3 3",
						opacity: ".6"
					}), /* @__PURE__ */ _jsx("circle", {
						cx: X(hp.step),
						cy: Y(hp.value),
						r: "3.5",
						fill: "none",
						stroke: "var(--fg)",
						strokeWidth: "1.4"
					})] }),
					/* @__PURE__ */ _jsx("text", {
						x: 2,
						y: pad / 2 + 4,
						fill: AX,
						fontSize: "9",
						children: fmt(maxY)
					}),
					/* @__PURE__ */ _jsx("text", {
						x: 2,
						y: h - pad + 4,
						fill: AX,
						fontSize: "9",
						children: fmt(minY)
					}),
					/* @__PURE__ */ _jsxs("text", {
						x: pad,
						y: h - 6,
						fill: AX,
						fontSize: "9",
						children: [
							"step ",
							minX,
							"–",
							maxX
						]
					})
				]
			})
		})
	});
}
