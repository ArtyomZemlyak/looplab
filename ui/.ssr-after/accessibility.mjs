import React, { useEffect, useId, useRef, useState } from "react";
import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
const textValue = (value) => {
	if (value == null) return "";
	if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
		return String(value);
	}
	try {
		return JSON.stringify(value);
	} catch {
		return String(value);
	}
};
const spreadsheetSafeText = (value) => {
	const text = textValue(value);
	if (typeof value !== "string") return text;
	// Spreadsheet applications can execute CSV cells as formulas. Preserve real numeric values, but
	// force untrusted text with a formula/control prefix to remain literal when the export is opened.
	return /^[\t\r]/.test(text) || /^[\u0000-\u0020]*[=+\-@]/.test(text) ? `'${text}` : text;
};
const csvCell = (value) => `"${spreadsheetSafeText(value).replaceAll("\"", "\"\"")}"`;
export function tableCsv(columns, rows) {
	const header = columns.map((column) => csvCell(column.label || column.key)).join(",");
	const body = rows.map((row) => columns.map((column) => {
		const value = column.value ? column.value(row) : row?.[column.key];
		return csvCell(value);
	}).join(","));
	return [header, ...body].join("\r\n");
}
export function downloadBlob(filename, parts, type) {
	if (typeof document === "undefined" || typeof URL === "undefined") return false;
	const href = URL.createObjectURL(new Blob(parts, { type }));
	const anchor = document.createElement("a");
	anchor.href = href;
	anchor.download = filename;
	// Firefox does not fire a download for a synthetic click on a DETACHED anchor, and revoking the
	// blob URL on the same tick can abort the download before the stream starts. Attach, click, then
	// detach and revoke on the next tick.
	anchor.style.display = "none";
	document.body.appendChild(anchor);
	anchor.click();
	document.body.removeChild(anchor);
	setTimeout(() => URL.revokeObjectURL(href), 0);
	return true;
}
export function downloadTableCsv(filename, columns, rows) {
	return downloadBlob(filename || "data.csv", [`\ufeff${tableCsv(columns, rows)}`], "text/csv;charset=utf-8");
}
function TableElement({ caption, columns, rows, rowKey, empty, card }) {
	return /* @__PURE__ */ _jsxs("table", {
		className: "tbl data-table" + (card ? " cardable" : ""),
		children: [
			/* @__PURE__ */ _jsx("caption", {
				className: "sr-only",
				children: caption
			}),
			/* @__PURE__ */ _jsx("thead", { children: /* @__PURE__ */ _jsx("tr", { children: columns.map((column) => /* @__PURE__ */ _jsx("th", {
				scope: "col",
				className: column.numeric ? "numeric" : undefined,
				children: column.label || column.key
			}, column.key)) }) }),
			/* @__PURE__ */ _jsx("tbody", { children: rows.length === 0 ? /* @__PURE__ */ _jsx("tr", { children: /* @__PURE__ */ _jsx("td", {
				colSpan: columns.length,
				className: "data-table-empty",
				children: empty
			}) }) : rows.map((row, index) => /* @__PURE__ */ _jsx("tr", { children: columns.map((column, columnIndex) => {
				const raw = column.value ? column.value(row) : row?.[column.key];
				const rendered = column.render ? column.render(raw, row, index) : textValue(raw);
				const Cell = column.rowHeader || columnIndex === 0 && column.firstColumnHeader ? "th" : "td";
				return /* @__PURE__ */ _jsx(Cell, {
					scope: Cell === "th" ? "row" : undefined,
					"data-label": column.label || column.key,
					className: column.numeric ? "numeric" : undefined,
					children: rendered
				}, column.key);
			}) }, rowKey ? rowKey(row, index) : index)) })
		]
	});
}
export function DataTable({ caption, columns = null, rows = [], rowKey = null, empty = "No rows", csvName = null, card = true, children = null, className = "" }) {
	const generated = useId().replaceAll(":", "");
	const headingId = `data-table-${generated}`;
	let table = children;
	if (columns) {
		table = /* @__PURE__ */ _jsx(TableElement, {
			caption,
			columns,
			rows,
			rowKey,
			empty,
			card
		});
	} else if (React.isValidElement(children)) {
		table = React.cloneElement(children, {
			className: `${children.props.className || "tbl"} data-table${card ? " cardable" : ""}`,
			children: [/* @__PURE__ */ _jsx("caption", {
				className: "sr-only",
				children: caption
			}, "caption"), ...React.Children.toArray(children.props.children)]
		});
	}
	return /* @__PURE__ */ _jsxs("section", {
		className: `data-table-region ${className}`.trim(),
		children: [/* @__PURE__ */ _jsxs("div", {
			className: "data-table-heading",
			children: [/* @__PURE__ */ _jsx("span", {
				id: headingId,
				children: caption
			}), csvName && columns && rows.length > 0 && /* @__PURE__ */ _jsx("button", {
				type: "button",
				className: "btn xs ghost",
				"aria-label": `Export ${caption} as CSV`,
				onClick: () => downloadTableCsv(csvName, columns, rows),
				children: "Export CSV"
			})]
		}), /* @__PURE__ */ _jsx("div", {
			className: "data-table-scroll",
			role: "region",
			"aria-labelledby": headingId,
			tabIndex: 0,
			children: table
		})]
	});
}
export function ChartFrame({ title, description, columns = [], rows = [], csvName, children, className = "" }) {
	const generated = useId().replaceAll(":", "");
	const titleId = `chart-title-${generated}`;
	const descriptionId = `chart-description-${generated}`;
	const [showData, setShowData] = useState(false);
	const labelledBy = `${titleId} ${descriptionId}`;
	return /* @__PURE__ */ _jsxs("figure", {
		className: `accessible-chart ${className}`.trim(),
		children: [
			/* @__PURE__ */ _jsxs("figcaption", { children: [/* @__PURE__ */ _jsx("span", {
				id: titleId,
				className: "accessible-chart-title",
				children: title
			}), /* @__PURE__ */ _jsx("span", {
				id: descriptionId,
				className: "accessible-chart-description",
				children: description
			})] }),
			/* @__PURE__ */ _jsx("div", {
				className: "accessible-chart-visual",
				role: "region",
				"aria-labelledby": titleId,
				"aria-describedby": descriptionId,
				tabIndex: 0,
				children: typeof children === "function" ? children({
					labelledBy,
					titleId,
					descriptionId
				}) : children
			}),
			/* @__PURE__ */ _jsxs("div", {
				className: "accessible-chart-actions",
				children: [/* @__PURE__ */ _jsx("button", {
					type: "button",
					className: "btn xs ghost",
					"aria-expanded": showData,
					"aria-label": `${showData ? "Hide" : "View"} ${title} data`,
					"aria-controls": showData ? `chart-data-${generated}` : undefined,
					onClick: () => setShowData((value) => !value),
					children: showData ? "Hide data" : "View data"
				}), csvName && rows.length > 0 && /* @__PURE__ */ _jsx("button", {
					type: "button",
					className: "btn xs ghost",
					"aria-label": `Export ${title} data as CSV`,
					onClick: () => downloadTableCsv(csvName, columns, rows),
					children: "Export CSV"
				})]
			}),
			showData && /* @__PURE__ */ _jsx("div", {
				id: `chart-data-${generated}`,
				children: /* @__PURE__ */ _jsx(DataTable, {
					caption: `${title} data`,
					columns,
					rows,
					card: true,
					csvName: null
				})
			})
		]
	});
}
// Keep native-link affordances (open in a new tab, save link, etc.) while using the SPA router for
// an ordinary primary click. A link that always calls preventDefault() is only native in appearance.
export function followClientRoute(event, navigate) {
	if (!event || event.defaultPrevented || event.button != null && event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
	event.preventDefault?.();
	navigate?.();
	return true;
}
export function nextRovingIndex(key, index, length) {
	if (!Number.isInteger(index) || !Number.isInteger(length) || length < 1) return null;
	if (key === "ArrowRight" || key === "ArrowDown") return (index + 1) % length;
	if (key === "ArrowLeft" || key === "ArrowUp") return (index - 1 + length) % length;
	if (key === "Home") return 0;
	if (key === "End") return length - 1;
	return null;
}
// Shared keyboard/focus contract for the compact theme and energy radio menus.
export function useRovingRadioMenu(open, setOpen) {
	const triggerRef = useRef(null);
	const menuRef = useRef(null);
	const close = (restore = false) => {
		setOpen(false);
		if (restore) requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }));
	};
	useEffect(() => {
		if (!open) return;
		requestAnimationFrame(() => (menuRef.current?.querySelector("[aria-checked=\"true\"]") || menuRef.current?.querySelector("[role=\"menuitemradio\"]"))?.focus());
	}, [open]);
	const onKeyDown = (event) => {
		const items = [...menuRef.current?.querySelectorAll("[role=\"menuitemradio\"]") || []];
		if (event.key === "Escape") {
			event.preventDefault();
			close(true);
			return;
		}
		const next = nextRovingIndex(event.key, Math.max(0, items.indexOf(document.activeElement)), items.length);
		if (next == null) return;
		event.preventDefault();
		items[next]?.focus();
	};
	return {
		triggerRef,
		menuRef,
		close,
		onKeyDown
	};
}
