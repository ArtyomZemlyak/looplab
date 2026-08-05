import React, { useEffect, useState } from "react";
import { jsxs as _jsxs } from "react/jsx-runtime";
const KEY = "ll.density";
const read = () => {
	try {
		return localStorage.getItem(KEY) === "comfortable";
	} catch {
		return false;
	}
};
function apply(comfortable, persist = true) {
	document.documentElement.toggleAttribute("data-comfortable", comfortable);
	if (persist) {
		try {
			localStorage.setItem(KEY, comfortable ? "comfortable" : "compact");
		} catch {}
	}
}
export function initDensity() {
	const comfortable = read();
	apply(comfortable, false);
	return comfortable;
}
export default function DensityToggle() {
	const [comfortable, setComfortable] = useState(read);
	useEffect(() => {
		apply(comfortable);
	}, [comfortable]);
	useEffect(() => {
		const sync = (event) => {
			if (event.key && event.key !== KEY) return;
			const next = read();
			setComfortable(next);
			apply(next, false);
		};
		window.addEventListener("storage", sync);
		return () => window.removeEventListener("storage", sync);
	}, []);
	return /* @__PURE__ */ _jsxs("button", {
		type: "button",
		className: "btn sm ghost density-toggle",
		"aria-pressed": comfortable,
		title: "Switch between compact and comfortable information density",
		onClick: () => setComfortable((value) => !value),
		children: ["Aa · ", comfortable ? "Comfortable" : "Compact"]
	});
}
