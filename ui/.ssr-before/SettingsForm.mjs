import React, { useEffect, useId, useRef, useState } from "react";
import { filterSettingsGroups, normalizeSettingsQuery } from "./settingsModel.mjs";

import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
// Renders the grouped settings form from the schema. Controlled: `form` is the editable shape
// (see settingsSchema.toForm), `onChange(key, value)` reports edits. `dirty` highlights fields that
// differ from the engine default; `unsaved` tracks edits since the last save.
//
// `only` and `hideSecret` keep compact consumers (run settings and launch dialogs) compatible.
// `mode` and `query` add progressive disclosure to the full Settings page.
function AgentPills({ f, granted, onToggleAgent, rolePills, interactionDisabled = false }) {
	if (!f.agents || !onToggleAgent) return null;
	return /* @__PURE__ */ _jsx("div", {
		className: "sf-agents",
		role: "group",
		"aria-label": `Runtime access for ${f.label}`,
		children: f.agents.map((role) => {
			const p = rolePills[role];
			const on = granted.includes(role);
			return /* @__PURE__ */ _jsx("button", {
				type: "button",
				className: "agpill" + (on ? " on" : ""),
				disabled: interactionDisabled,
				"aria-pressed": on,
				"aria-label": `${p.title}: ${on ? "allowed" : "not allowed"}`,
				title: (on ? "Allowed: " : "Not allowed: ") + p.title,
				onClick: () => onToggleAgent(f.key, role),
				children: p.short
			}, role);
		})
	});
}
// One source of truth for the two-tier change dot (unsaved wins over differs-from-default), shared by
// the per-field label and the per-tab header so they can never disagree.
function changeDot(unsaved, changed) {
	if (unsaved) return /* @__PURE__ */ _jsx("span", {
		className: "sf-dot unsaved",
		title: "unsaved — clears on Save",
		"aria-label": "unsaved",
		children: "●"
	});
	if (changed) return /* @__PURE__ */ _jsx("span", {
		className: "sf-dot fromdefault",
		title: "differs from the engine default",
		"aria-label": "customized",
		children: "●"
	});
	return null;
}
const safeId = (value) => String(value).replace(/[^a-zA-Z0-9_-]/g, "-");
const credentialSourceLabel = (source) => ({
	stored: "stored settings",
	environment: "process environment",
	dotenv: ".env file",
	none: "no source"
})[source] || "unknown source";
function Field({ idPrefix, f, value, onChange, changed, unsaved, error, granted, onToggleAgent, secretSet, credential, onClearSecret, secretActionDisabled, readOnly, rolePills, interactionDisabled = false }) {
	const set = (v) => onChange(f.key, v);
	const inputId = `${idPrefix}-setting-${safeId(f.key)}`;
	const helpId = `${inputId}-help`;
	const warningId = `${inputId}-warning`;
	const errorId = `${inputId}-error`;
	const readOnlyId = `${inputId}-readonly`;
	const hasDescription = !!f.help || f.type === "secret";
	const describedBy = [
		hasDescription ? helpId : "",
		f.warning ? warningId : "",
		error ? errorId : "",
		readOnly ? readOnlyId : ""
	].filter(Boolean).join(" ") || undefined;
	let input;
	const storedCredential = credential ? credential.stored : !!secretSet;
	const effectiveCredential = credential?.effective === true;
	const activeCredential = credential?.active === true;
	const ambientCredential = credential && (credential.source === "environment" || credential.source === "dotenv");
	const ambientEffectiveCredential = ambientCredential && effectiveCredential;
	const storedFallbackUnderAmbient = ambientCredential && storedCredential;
	const clearableCredential = credential ? credential.clearable : storedCredential;
	const incompleteStoredCredential = credential?.source === "stored" && credential.status === "incomplete";
	if (f.type === "bool") {
		input = /* @__PURE__ */ _jsxs("label", {
			className: "switch",
			title: `Toggle ${f.label}`,
			children: [/* @__PURE__ */ _jsx("input", {
				id: inputId,
				name: f.key,
				type: "checkbox",
				checked: !!value,
				"aria-describedby": describedBy,
				disabled: readOnly || interactionDisabled,
				onChange: (e) => set(e.target.checked)
			}), /* @__PURE__ */ _jsx("span", {
				className: "track",
				"aria-hidden": "true"
			})]
		});
	} else if (f.type === "enum") {
		input = /* @__PURE__ */ _jsx("select", {
			id: inputId,
			name: f.key,
			className: "text",
			value: value ?? "",
			"aria-describedby": describedBy,
			disabled: readOnly || interactionDisabled,
			onChange: (e) => set(e.target.value),
			children: f.options.map((o) => /* @__PURE__ */ _jsx("option", {
				value: o,
				children: o === "" ? "Use provider default" : o
			}, o || "__default"))
		});
	} else if (f.type === "secret") {
		// Write-only credential: the box is always blank (the value is never sent back from the server).
		input = /* @__PURE__ */ _jsxs("div", {
			className: "sf-secret",
			children: [
				/* @__PURE__ */ _jsx("input", {
					id: inputId,
					name: f.key,
					className: "text",
					type: "password",
					autoComplete: "new-password",
					value: value ?? "",
					"aria-describedby": describedBy,
					disabled: readOnly || interactionDisabled,
					placeholder: ambientCredential ? ambientEffectiveCredential ? "Ambient shared key matches base URL — enter to store a fallback" : "Ambient source has no key — enter to store a fallback" : storedCredential ? incompleteStoredCredential ? "API key missing — enter to complete the pair" : "Stored — leave blank to keep" : "Not set",
					onChange: (e) => set(e.target.value)
				}),
				storedCredential && clearableCredential && onClearSecret && /* @__PURE__ */ _jsx("button", {
					type: "button",
					className: "btn sm ghost",
					"aria-label": incompleteStoredCredential ? "Clear incomplete stored credential pair" : storedFallbackUnderAmbient ? `Clear stored fallback ${f.label} and endpoint binding` : `Clear stored ${f.label} and endpoint binding`,
					title: incompleteStoredCredential ? "Remove the orphan stored endpoint binding immediately (separate from Save)" : storedFallbackUnderAmbient ? "Remove the stored fallback key and endpoint binding; the ambient source remains untouched" : "Remove the stored key and endpoint binding immediately (separate from Save)",
					disabled: secretActionDisabled,
					onClick: () => onClearSecret(f.key),
					children: "Clear now"
				}),
				ambientCredential && /* @__PURE__ */ _jsx("span", {
					className: "sf-secret-readonly",
					title: ambientEffectiveCredential ? `The effective credential comes from the ${credentialSourceLabel(credential.source)} and cannot be changed or cleared here.${storedFallbackUnderAmbient ? " The separate stored fallback can be cleared." : ""}` : `The ${credentialSourceLabel(credential.source)} controls credential resolution but currently supplies no effective key; it cannot be changed or cleared here.${storedFallbackUnderAmbient ? " The separate stored fallback can be cleared." : ""}`,
					children: "Ambient source · read-only"
				})
			]
		});
	} else {
		const numeric = f.type === "int" || f.type === "float";
		// Keep the operator's raw transitional text (`-`, `1e`, etc.) intact. Native number inputs
		// sanitize those states to an empty string, which is our deliberate clear/null operation.
		input = /* @__PURE__ */ _jsx("input", {
			id: inputId,
			name: f.key,
			className: "text",
			type: "text",
			inputMode: f.type === "int" ? "numeric" : f.type === "float" ? "decimal" : undefined,
			value: value ?? "",
			spellCheck: numeric ? false : undefined,
			"aria-describedby": describedBy,
			"aria-invalid": error ? "true" : undefined,
			"aria-readonly": readOnly || interactionDisabled || undefined,
			readOnly: readOnly || interactionDisabled,
			placeholder: f.placeholder || "",
			onChange: (e) => set(e.target.value)
		});
	}
	const dot = changeDot(unsaved, changed);
	return /* @__PURE__ */ _jsxs("div", {
		className: "sf-field" + (unsaved ? " unsaved" : changed ? " changed" : "") + (error ? " invalid" : "") + (readOnly ? " readonly" : ""),
		children: [
			/* @__PURE__ */ _jsxs("div", {
				className: "sf-label-row",
				children: [/* @__PURE__ */ _jsxs("label", {
					className: "sf-label",
					htmlFor: inputId,
					children: [
						f.label,
						dot,
						readOnly && /* @__PURE__ */ _jsx("span", {
							className: "muted",
							title: "Fixed when this run started",
							children: " · launch-pinned"
						})
					]
				}), /* @__PURE__ */ _jsx(AgentPills, {
					f,
					granted: granted || [],
					onToggleAgent: readOnly ? undefined : onToggleAgent,
					rolePills,
					interactionDisabled
				})]
			}),
			/* @__PURE__ */ _jsx("div", {
				className: "sf-input",
				children: input
			}),
			error && /* @__PURE__ */ _jsx("div", {
				id: errorId,
				className: "sf-error",
				role: "alert",
				children: error
			}),
			readOnly && /* @__PURE__ */ _jsx("div", {
				id: readOnlyId,
				className: "sf-help",
				role: "note",
				children: "Fixed when this run started. Create a new run to use a different value; resume and replay keep this recorded value."
			}),
			hasDescription && /* @__PURE__ */ _jsxs("div", {
				id: helpId,
				className: "sf-help",
				children: [f.type === "secret" && (credential ? /* @__PURE__ */ _jsxs("span", {
					className: "sf-secret-state",
					children: [
						"Stored material: ",
						storedCredential ? "yes" : "no",
						" · Shared key: ",
						effectiveCredential ? "yes" : "no",
						" · Matches base URL: ",
						activeCredential ? "yes" : "no",
						".",
						" ",
						ambientCredential ? ambientEffectiveCredential ? `The effective key comes from the ${credentialSourceLabel(credential.source)} and is read-only here. A value entered above is stored only as a fallback pair while that override exists. ${storedCredential ? "Existing stored material may be a complete pair or only a binding; its key is never exposed. " : ""}` : `The ${credentialSourceLabel(credential.source)} controls credential resolution but supplies no effective key. A value entered above is stored only as an inactive fallback pair while that ambient source remains selected. ${storedCredential ? "Existing stored material may be a complete pair or only a binding; its key is never exposed. " : ""}` : incompleteStoredCredential ? "The stored pair is missing its API key. Enter a value to complete and rebind it to the saved endpoint, or use Clear now to remove the incomplete pair. " : storedCredential ? "Enter a value only to replace the stored key. " : "No credential is stored. ",
						storedCredential && clearableCredential ? storedFallbackUnderAmbient ? "Clear now removes only the stored fallback; the ambient source remains untouched. " : "Clear now is immediate and separate from Save. " : ""
					]
				}) : /* @__PURE__ */ _jsxs("span", {
					className: "sf-secret-state",
					children: [
						secretSet ? "A credential is stored. Enter a value only to replace it. " : "No credential is stored. ",
						"Clear now is immediate and separate from Save.",
						" "
					]
				})), f.help]
			}),
			f.warning && /* @__PURE__ */ _jsxs("div", {
				id: warningId,
				className: `sf-warning${f.warningTone === "info" ? " info" : ""}`,
				role: "note",
				children: [
					/* @__PURE__ */ _jsx("strong", { children: f.warningTitle || "High-risk experimental setting." }),
					" ",
					f.warning
				]
			})
		]
	});
}
function GroupPanel({ group, idPrefix, form, onChange, dirty, unsaved, errors, agentControl, onToggleAgent, secretState, credential, onClearSecret, secretActionDisabled, readOnlyKeys, panelId, labelledBy, searchable, rolePills, interactionDisabled }) {
	const headingId = `${idPrefix}-heading-${safeId(group.title)}`;
	return /* @__PURE__ */ _jsxs("section", {
		className: "sf-group",
		id: panelId,
		role: labelledBy ? "tabpanel" : undefined,
		"aria-labelledby": labelledBy || headingId,
		tabIndex: labelledBy ? 0 : undefined,
		children: [/* @__PURE__ */ _jsxs("div", {
			className: "sf-group-h",
			children: [searchable && /* @__PURE__ */ _jsx("h2", {
				id: headingId,
				children: group.title
			}), group.sub && /* @__PURE__ */ _jsx("span", {
				className: "muted",
				children: group.sub
			})]
		}), /* @__PURE__ */ _jsx("div", {
			className: "sf-grid",
			children: group.fields.map((f) => /* @__PURE__ */ _jsx(Field, {
				idPrefix,
				f,
				value: form[f.key],
				changed: dirty?.has(f.key),
				unsaved: unsaved?.has(f.key),
				error: errors?.[f.key],
				onChange,
				granted: agentControl?.[f.key],
				onToggleAgent,
				secretSet: secretState?.[f.key],
				credential: f.type === "secret" ? credential : null,
				onClearSecret,
				secretActionDisabled,
				readOnly: readOnlyKeys?.has(f.key),
				rolePills,
				interactionDisabled
			}, f.key))
		})]
	});
}
export default function SettingsForm({ form, onChange, dirty, unsaved, errors, only, agentControl, onToggleAgent, secretState, credential, onClearSecret, secretActionDisabled, readOnlyKeys, hideSecret, mode = "all", query = "", schema, focusKey = "", focusRequest = 0, interactionDisabled = false }) {
	const groups = filterSettingsGroups(schema.groups, {
		mode,
		query,
		only,
		hideSecret
	});
	const rolePills = schema.agentRolePills;
	// Keep the selected section by stable identity. The Essential catalogue is a sparse subset of
	// All, so retaining a numeric index silently selected a different section when modes changed.
	const [activeGroup, setActiveGroup] = useState("");
	const reactId = useId();
	const idPrefix = `sf-${safeId(reactId)}`;
	const searching = !!normalizeSettingsQuery(query);
	const selectedIndex = groups.findIndex((item) => item.title === activeGroup);
	const idx = selectedIndex >= 0 ? selectedIndex : 0;
	const group = groups[idx];
	const handledFocusRef = useRef("");
	const tablistRef = useRef(null);
	const groupUnsaved = (gr) => gr.fields.some((f) => unsaved?.has(f.key));
	const groupChanged = (gr) => gr.fields.some((f) => dirty?.has(f.key));
	useEffect(() => {
		if (!focusKey || !Object.hasOwn(schema.fieldByKey, focusKey)) return undefined;
		const focusCommand = `${focusRequest}:${focusKey}`;
		if (handledFocusRef.current === focusCommand) return undefined;
		const groupIndex = groups.findIndex((item) => item.fields.some((field) => field.key === focusKey));
		if (groupIndex < 0) return undefined;
		if (!searching && idx !== groupIndex) {
			setActiveGroup(groups[groupIndex].title);
			return undefined;
		}
		const timer = setTimeout(() => {
			const target = document.querySelector(`[data-settings-form="${idPrefix}"] [name="${focusKey}"]`);
			if (!target) return;
			target.focus();
			// A review request is a one-shot focus command. Remember it only after the target exists so
			// the tab-switch render above can finish first, but never replay it on ordinary navigation.
			handledFocusRef.current = focusCommand;
		}, 0);
		return () => clearTimeout(timer);
	}, [
		focusKey,
		focusRequest,
		mode,
		query,
		only,
		hideSecret,
		schema,
		searching,
		idx
	]);
	useEffect(() => {
		if (searching) return;
		const tablist = tablistRef.current;
		const activeTab = tablist?.querySelector("[aria-selected=\"true\"]");
		if (!tablist || !activeTab) return;
		// Keep the selected tab visible without moving the settings page vertically. scrollIntoView()
		// also scrolls ancestor containers, which made the mobile route open halfway down the page.
		const listRect = tablist.getBoundingClientRect();
		const tabRect = activeTab.getBoundingClientRect();
		if (tabRect.left < listRect.left) tablist.scrollLeft -= listRect.left - tabRect.left;
		else if (tabRect.right > listRect.right) tablist.scrollLeft += tabRect.right - listRect.right;
	}, [idx, searching]);
	if (!groups.length) return /* @__PURE__ */ _jsxs("div", {
		className: "settings-empty",
		role: "status",
		children: [/* @__PURE__ */ _jsxs("strong", { children: [
			"No settings match “",
			query.trim(),
			"”"
		] }), /* @__PURE__ */ _jsx("span", { children: "Try a field name, key, option, or a broader term." })]
	});
	if (searching) return /* @__PURE__ */ _jsx("div", {
		className: "settings-form settings-search-results",
		role: "form",
		"data-settings-form": idPrefix,
		"aria-label": "Matching settings",
		children: groups.map((gr) => /* @__PURE__ */ _jsx(GroupPanel, {
			group: gr,
			idPrefix,
			form,
			onChange,
			dirty,
			unsaved,
			errors,
			agentControl,
			onToggleAgent,
			secretState,
			credential,
			onClearSecret,
			secretActionDisabled,
			readOnlyKeys,
			searchable: true,
			rolePills,
			interactionDisabled
		}, gr.title))
	});
	const onTabKeyDown = (event, index) => {
		let next = index;
		if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % groups.length;
		else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (index - 1 + groups.length) % groups.length;
		else if (event.key === "Home") next = 0;
		else if (event.key === "End") next = groups.length - 1;
		else return;
		event.preventDefault();
		setActiveGroup(groups[next].title);
		event.currentTarget.parentElement?.querySelectorAll("[role=\"tab\"]")[next]?.focus();
	};
	const tabId = `${idPrefix}-tab-${idx}`;
	const panelId = `${idPrefix}-panel-${idx}`;
	return /* @__PURE__ */ _jsxs("div", {
		className: "settings-form tabbed",
		role: "form",
		"aria-label": "Settings fields",
		"data-settings-form": idPrefix,
		children: [/* @__PURE__ */ _jsx("div", {
			ref: tablistRef,
			className: "tabs sf-tabs",
			role: "tablist",
			"aria-label": "Settings sections",
			children: groups.map((gr, index) => /* @__PURE__ */ _jsxs("button", {
				type: "button",
				role: "tab",
				id: `${idPrefix}-tab-${index}`,
				"aria-controls": index === idx ? `${idPrefix}-panel-${index}` : undefined,
				"aria-selected": index === idx,
				tabIndex: index === idx ? 0 : -1,
				className: "tab" + (index === idx ? " active" : ""),
				onClick: () => setActiveGroup(gr.title),
				onKeyDown: (event) => onTabKeyDown(event, index),
				title: gr.sub || "",
				children: [gr.title, changeDot(groupUnsaved(gr), groupChanged(gr))]
			}, gr.title))
		}), /* @__PURE__ */ _jsx(GroupPanel, {
			group,
			idPrefix,
			form,
			onChange,
			dirty,
			unsaved,
			errors,
			agentControl,
			onToggleAgent,
			secretState,
			credential,
			onClearSecret,
			secretActionDisabled,
			readOnlyKeys,
			panelId,
			labelledBy: tabId,
			rolePills,
			interactionDisabled
		}, group.title)]
	});
}
