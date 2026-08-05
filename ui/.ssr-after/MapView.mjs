import React, { useEffect, useMemo, useRef } from "react";
import { ReactFlow, Background, Controls, Handle, MiniMap, Panel, Position, useReactFlow } from "@xyflow/react";

import { fmt } from "./util.mjs";
import { regionGeometry, groupColor } from "./grouping.mjs";
import { RegionShell, SuperShell } from "./groupnodes.mjs";
import { OpIcon } from "./icons.mjs";
import { packRunGrid, UNASSIGNED_CLUSTER } from "./runMapModel.mjs";
import { effectiveRunStatus, indexProjects, projectAncestorCollapsed, projectDepth } from "./runIndex.mjs";
import { followClientRoute } from "./accessibility.mjs";
import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
// Cross-run map: projects are regions and runs are readable cards inside them. Large clusters are
// represented by a super-node until expanded; expanded runs use bounded grid packing rather than an
// unbounded horizontal row (55 unassigned runs previously produced an ~11.7k px line).
const RUN_W = 190, RUN_H = 80, RUN_DX = 214, ROW_DY = 122, INDENT = 64;
function RunNode({ data }) {
	const run = data.run;
	const themes = Object.entries(run.themes || {});
	const status = effectiveRunStatus(run);
	const stalled = status === "stalled";
	const open = () => data.onOpen(run.run_id);
	return /* @__PURE__ */ _jsxs("a", {
		className: "run-node nodrag nopan",
		"data-run-open-id": run.run_id,
		href: `#/run/${encodeURIComponent(run.run_id)}`,
		onClick: (event) => followClientRoute(event, open),
		"aria-label": `Open ${run.label || run.run_id}, ${status}, ${run.task_id || "unknown task"}`,
		title: run.goal,
		children: [
			/* @__PURE__ */ _jsx(Handle, {
				type: "source",
				position: Position.Right,
				style: { opacity: 0 }
			}),
			/* @__PURE__ */ _jsx(Handle, {
				type: "target",
				position: Position.Left,
				style: { opacity: 0 }
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "row",
				children: [/* @__PURE__ */ _jsx("span", {
					className: "pill phase " + status,
					children: status
				}), /* @__PURE__ */ _jsx("b", { children: run.label || run.run_id })]
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "muted",
				children: [
					run.label ? `${run.run_id} · ` : "",
					run.task_id,
					" · best ",
					fmt(run.best_confirmed ?? run.best_metric),
					" ",
					run.direction || ""
				]
			}),
			themes.length > 0 && /* @__PURE__ */ _jsx("div", {
				className: "chips",
				children: themes.slice(0, 4).map(([theme, info]) => /* @__PURE__ */ _jsxs("span", {
					className: "chip sm",
					title: `best ${fmt(info.best_metric)}`,
					children: [
						theme,
						" ",
						/* @__PURE__ */ _jsx("b", { children: info.count })
					]
				}, theme))
			})
		]
	});
}
function ProjRegion({ data }) {
	const toggle = () => data.onToggle(data.id);
	const tab = /* @__PURE__ */ _jsxs("button", {
		type: "button",
		className: "grp-tab nodrag nopan",
		onClick: (event) => {
			event.stopPropagation();
			toggle();
		},
		title: `Collapse ${data.name}`,
		children: [
			/* @__PURE__ */ _jsx("span", {
				className: "grp-chev",
				children: "▾"
			}),
			/* @__PURE__ */ _jsx(OpIcon, {
				name: "folder",
				className: "t-ic"
			}),
			" ",
			data.name,
			/* @__PURE__ */ _jsx("span", {
				className: "grp-n",
				children: data.count
			})
		]
	});
	return /* @__PURE__ */ _jsx(RegionShell, {
		w: data.w,
		h: data.h,
		path: data.path,
		tint: data.tint,
		tab
	});
}
function ProjSuper({ data }) {
	return /* @__PURE__ */ _jsxs(SuperShell, {
		tint: data.tint,
		onClick: () => data.onToggle(data.id),
		title: `Expand ${data.name}`,
		children: [/* @__PURE__ */ _jsxs("div", {
			className: "row",
			children: [
				/* @__PURE__ */ _jsx("span", {
					className: "grp-chev btn-chev",
					children: "▸"
				}),
				/* @__PURE__ */ _jsxs("b", {
					className: "grp-name",
					children: [
						/* @__PURE__ */ _jsx(OpIcon, {
							name: "folder",
							className: "t-ic"
						}),
						" ",
						data.name
					]
				}),
				/* @__PURE__ */ _jsx("span", {
					className: "spacer",
					style: { flex: 1 }
				}),
				/* @__PURE__ */ _jsx("span", {
					className: "grp-n",
					children: data.count
				})
			]
		}), /* @__PURE__ */ _jsxs("div", {
			className: "muted",
			style: { marginTop: 3 },
			children: [
				data.runs,
				" run",
				data.runs !== 1 ? "s" : "",
				" · expand to inspect"
			]
		})]
	});
}
const nodeTypes = {
	run: RunNode,
	projRegion: ProjRegion,
	projSuper: ProjSuper
};
export function buildGraph(projects, runs, collapsed, onOpen, onToggle) {
	// Reuse the List view's index instead of re-deriving it: this copy sorted with a bare
	// `a.name.localeCompare(b.name)`, so ONE project with a null name crashed the whole Map view while
	// the list — which coerces via String(a.name || '') — carried on. `indexProjects` also returns the
	// cycle-guarded `subtree` this file used to reimplement without the guard. Roots are keyed `null`
	// there, not 'root'.
	const { byParent: childrenOf, byId, subtree: rawSubtree } = indexProjects(projects);
	const runsByProject = {};
	runs.forEach((run) => {
		const projectId = run.project_id && byId[run.project_id] ? run.project_id : UNASSIGNED_CLUSTER;
		(runsByProject[projectId] ||= []).push(run);
	});
	// Memoize the shared subtree walk: this view asks for the same regions on every render pass.
	const subtreeCache = new Map();
	const subtree = (id) => {
		if (!subtreeCache.has(id)) subtreeCache.set(id, rawSubtree(id));
		return subtreeCache.get(id);
	};
	// The ancestor walks are shared too, and cycle-guarded for the same reason: `subtree`'s guard is
	// per-descent and says nothing about a parent chain that loops back on itself.
	const depthOf = (id) => projectDepth(byId, id);
	const ancestorCollapsed = (id) => projectAncestorCollapsed(byId, id, collapsed);
	const visibleCount = (ids) => runs.reduce((count, run) => count + (ids.has(run.project_id) ? 1 : 0), 0);
	const maxDepth = projects.length ? Math.max(...projects.map((project) => depthOf(project.id))) : 0;
	const graphNodes = [], runPositions = {};
	let y = 0;
	const placeRuns = (items, x) => {
		const packed = packRunGrid(items, {
			x,
			y,
			dx: RUN_DX,
			dy: ROW_DY
		});
		items.forEach((run) => {
			const position = packed.positions.get(run.run_id);
			runPositions[run.run_id] = position;
			graphNodes.push({
				id: `run:${run.run_id}`,
				type: "run",
				position,
				zIndex: 5,
				width: RUN_W,
				height: RUN_H,
				data: {
					run,
					onOpen
				}
			});
		});
		if (items.length) y += packed.height;
	};
	const visit = (project) => {
		if (ancestorCollapsed(project.id)) return;
		const depth = depthOf(project.id);
		const count = visibleCount(subtree(project.id));
		if (collapsed.has(project.id) && count > 0) {
			graphNodes.push({
				id: `ps:${project.id}`,
				type: "projSuper",
				position: {
					x: depth * INDENT,
					y
				},
				zIndex: 5,
				data: {
					id: project.id,
					name: project.name,
					count,
					runs: count,
					tint: groupColor(project.id),
					onToggle
				}
			});
			y += ROW_DY;
			return;
		}
		placeRuns(runsByProject[project.id] || [], depth * INDENT);
		(childrenOf[project.id] || []).forEach(visit);
	};
	(childrenOf[null] || []).forEach(visit);
	const unassigned = runsByProject[UNASSIGNED_CLUSTER] || [];
	if (unassigned.length) {
		if (collapsed.has(UNASSIGNED_CLUSTER)) {
			graphNodes.push({
				id: `ps:${UNASSIGNED_CLUSTER}`,
				type: "projSuper",
				position: {
					x: 0,
					y
				},
				zIndex: 5,
				data: {
					id: UNASSIGNED_CLUSTER,
					name: "Unassigned",
					count: unassigned.length,
					runs: unassigned.length,
					tint: groupColor(UNASSIGNED_CLUSTER),
					onToggle
				}
			});
			y += ROW_DY;
		} else {
			placeRuns(unassigned, 0);
		}
	}
	const regions = [];
	projects.forEach((project) => {
		if (collapsed.has(project.id) || ancestorCollapsed(project.id)) return;
		const ids = subtree(project.id);
		const rects = runs.filter((run) => ids.has(run.project_id) && runPositions[run.run_id]).map((run) => ({
			...runPositions[run.run_id],
			w: RUN_W,
			h: RUN_H
		}));
		if (!rects.length) return;
		const depth = depthOf(project.id);
		const geometry = regionGeometry(rects, 18 + (maxDepth - depth) * 16);
		regions.push({
			id: `pr:${project.id}`,
			type: "projRegion",
			position: {
				x: geometry.x,
				y: geometry.y
			},
			zIndex: depth,
			selectable: false,
			draggable: false,
			focusable: false,
			data: {
				id: project.id,
				name: project.name,
				count: rects.length,
				w: geometry.w,
				h: geometry.h,
				path: geometry.path,
				tint: groupColor(project.id),
				onToggle
			}
		});
	});
	if (unassigned.length && !collapsed.has(UNASSIGNED_CLUSTER)) {
		const rects = unassigned.map((run) => ({
			...runPositions[run.run_id],
			w: RUN_W,
			h: RUN_H
		}));
		const geometry = regionGeometry(rects, 24);
		regions.push({
			id: `pr:${UNASSIGNED_CLUSTER}`,
			type: "projRegion",
			position: {
				x: geometry.x,
				y: geometry.y
			},
			zIndex: 0,
			selectable: false,
			draggable: false,
			focusable: false,
			data: {
				id: UNASSIGNED_CLUSTER,
				name: "Unassigned",
				count: rects.length,
				w: geometry.w,
				h: geometry.h,
				path: geometry.path,
				tint: groupColor(UNASSIGNED_CLUSTER),
				onToggle
			}
		});
	}
	const edges = [];
	runs.forEach((run) => (run.seeded_from || []).forEach((source) => {
		if (runPositions[run.run_id] && runPositions[source]) edges.push({
			id: `seed:${source}->${run.run_id}`,
			source: `run:${source}`,
			target: `run:${run.run_id}`,
			className: "seed-edge"
		});
	}));
	return {
		nodes: [...regions, ...graphNodes],
		edges
	};
}
function FitVisible({ signature, initialViewport }) {
	const { fitView, setViewport } = useReactFlow();
	const initialViewportRef = useRef(initialViewport);
	useEffect(() => {
		const frame = requestAnimationFrame(() => {
			if (initialViewportRef.current) {
				const saved = initialViewportRef.current;
				initialViewportRef.current = null;
				setViewport(saved);
			} else {
				fitView({
					padding: .16,
					maxZoom: 1
				});
			}
		});
		return () => cancelAnimationFrame(frame);
		// fitView changes the React Flow store; keying only on the visible node signature prevents a
		// fit→render→fit feedback loop while the run-list polling refreshes object identities.
	}, [
		signature,
		fitView,
		setViewport
	]);
	return null;
}
export default function MapView({ onOpen, runs = [], projects = [], collapsed = new Set(), onToggle, scopeLabel = "All runs", initialViewport = null, onViewportChange = null }) {
	const { nodes, edges } = useMemo(() => buildGraph(projects, runs, collapsed, onOpen, onToggle), [
		projects,
		runs,
		collapsed,
		onOpen,
		onToggle
	]);
	// Reframe when the filtered run scope changes, but preserve zoom/pan when a user expands a cluster.
	// Auto-fitting all 57 newly revealed cards would immediately shrink their text back to ~40%.
	const signature = runs.map((run) => run.run_id).sort().join("|");
	const runNodeCount = nodes.filter((node) => node.type === "run").length;
	const collapsedIds = nodes.filter((node) => node.type === "projSuper").map((node) => node.data.id);
	return /* @__PURE__ */ _jsx("div", {
		className: "mapwrap",
		children: /* @__PURE__ */ _jsxs(ReactFlow, {
			nodes,
			edges,
			nodeTypes,
			minZoom: .15,
			maxZoom: 1.6,
			proOptions: { hideAttribution: true },
			nodesDraggable: false,
			nodesFocusable: false,
			onlyRenderVisibleElements: true,
			onMoveEnd: (_, viewport) => onViewportChange?.(viewport),
			children: [
				/* @__PURE__ */ _jsx(FitVisible, {
					signature,
					initialViewport
				}),
				/* @__PURE__ */ _jsx(Background, {
					color: "var(--line)",
					gap: 22
				}),
				/* @__PURE__ */ _jsx(Controls, { showInteractive: false }),
				/* @__PURE__ */ _jsx(MiniMap, {
					pannable: true,
					zoomable: true,
					className: "run-minimap",
					nodeColor: (node) => node.type === "run" ? "var(--accent)" : "var(--line-2)"
				}),
				/* @__PURE__ */ _jsxs(Panel, {
					position: "top-left",
					className: "map-summary",
					children: [
						/* @__PURE__ */ _jsxs("b", { children: [runs.length, " runs"] }),
						/* @__PURE__ */ _jsx("span", { children: scopeLabel }),
						/* @__PURE__ */ _jsxs("span", { children: [
							runNodeCount,
							" visible · ",
							collapsedIds.length,
							" collapsed cluster",
							collapsedIds.length === 1 ? "" : "s"
						] }),
						collapsedIds.length > 0 && /* @__PURE__ */ _jsx("button", {
							className: "btn sm",
							onClick: () => collapsedIds.forEach(onToggle),
							children: "Expand clusters"
						})
					]
				})
			]
		})
	});
}
