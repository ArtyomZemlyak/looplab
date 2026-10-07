import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "@babel/parser";
const attrs = new Set([
  "title",
  "placeholder",
  "aria-label",
  "alt",
  "label",
  "caption",
  "hint",
  "description",
  "emptyText",
  "loadingText",
  "errorText",
]);
const names =
  /^(error|err|notice|feedback|toast|catalogueSummary|statusText|reasonText|hint|label|caption|phase|status|emptyText|direction|sourceLabel|sourceHelp|comparison|headerStatus|triggerLabel|countAria|actionAria|unreadAria|unreadPhrase|loadedUnreadPhrase|actionPhrase|stillActionPhrase|uncertainActionPhrase)$/;
const members =
  /^(statusText|reasonText|waitingFor|nextCheck|budget|status|phase|label|help|technicalHelp|hint|subtitle|sub|displayTitle)$/;
const human = (s) =>
  /[a-zA-Z]{2}/.test(s) &&
  !/[\u0400-\u04ff]/.test(s) &&
  !/^(#[/]|https?:|[./].*[/]|[A-Z_]+\s*=)/.test(s);
// A shell command is copied and pasted, never translated: the Russian catalogue once rendered
// `looplab finalize <runs>/{0}` as `Завершить процесс <unes>/{0}`. Paths are opaque the same way.
const command = (s) => /^(looplab |git |python |npm )/.test(s);
const hasJSX = (n) =>
  n &&
  typeof n === "object" &&
  (["JSXElement", "JSXFragment"].includes(n.type) ||
    Object.entries(n).some(
      ([k, v]) =>
        !["loc", "extra", "comments"].includes(k) &&
        (Array.isArray(v) ? v.some(hasJSX) : hasJSX(v)),
    ));
const norm = (s) => s.replace(/\s+/g, " ").trim();
const own = new Set([
  "label",
  "title",
  "help",
  "hint",
  "description",
  "reason",
  "message",
  "note",
  "summary",
  "text",
  "placeholder",
  "sub",
  "tooltip",
]);
const proseHelpers = new Set([
  "assistantChromeModel.js",
  "assistantErrors.js",
  "assistantToolActivity.js",
  "nodeActivity.js",
  "runIndex.js",
  "trustSemantics.js",
  "extraMetrics.js",
  "stageAttribution.js",
  "scopeReportModel.js",
  "cardBoardModel.js",
  "cardBoardViewModel.js",
  "researchMemoModel.js",
  "buildingModel.js",
  "narration.js",
  "crossRunPrior.js",
  "crossRunRank.js",
  "llmHealthCopy.js",
  "resultEvidence.js",
  "traceProjection.js",
  "resourceModel.js",
  "seedReadError.js",
]);
const trusted = new Set([
  "block.title",
  "section.title",
  "recovery.message",
  "outcome.text",
  "entry.title",
  "notice.text",
  "watchView.summary",
  "gate.title",
  "fold.title",
  "directConfirm.title",
  "currentShareAckNotice.message",
  "error.title",
  "text.title",
  "text.outcome",
  "text.comparison",
  "text.caution",
  "text.next",
  "pendingDelete.message",
  "pendingIntent.text",
  "uncertainEdit.text",
  "currentRecovery.notice",
  "branch.title",
  "status.text",
  "step.title",
  "progress.next_step.title",
  "clearMessage.text",
  "pricing.title",
  "pricing.text",
  "destination.title",
  "result.headline",
  "result.nextStep",
  "overlay.text",
  "v.headline",
  "v.nextStep",
  "confirmation.message",
  "deletionNotice.text",
  "presentation.title",
  "serverCodeStale.text",
  "bindingNotice.title",
  "bindingNotice.text",
  "ambientNotice.title",
  "ambientNotice.text",
  "settingsLaunchSnapshot.reason",
  "f.warning",
  "group.title",
  "gr.title",
  "group.displayTitle",
  "f.technicalHelp",
  "controlState.notice.text",
]);
function jsxText(raw) {
  const lines = raw.split(/\r\n|\n|\r/);
  let out = "",
    last = -1;
  for (let i = 0; i < lines.length; i++) if (/[^ \t]/.test(lines[i])) last = i;
  for (let i = 0; i < lines.length; i++) {
    let l = lines[i].replace(/\t/g, " ");
    if (i !== 0) l = l.replace(/^ +/, "");
    if (i !== lines.length - 1) l = l.replace(/ +$/, "");
    if (l) out += l + (i !== last ? " " : "");
  }
  return out;
}
export function localizeSource(source, filename = "fixture.jsx") {
  const tree = parse(source, { sourceType: "module", plugins: ["jsx"] }),
    edits = [],
    messages = new Set(),
    components = new Set(),
    ownedVariables = new Set();
  const add = (s) => {
      if (human(s)) messages.add(norm(s));
    },
    raw = (n) => source.slice(n.start, n.end);
  let changed = false;
  function copy(n) {
    if (!n) return null;
    if (
      n.type === "CallExpression" &&
      ["uiText", "uiMessage"].includes(n.callee?.name)
    )
      return null;
    if (
      trusted.has(raw(n)) ||
      (filename === "ThemeSwitcher.jsx" &&
        ["cur.name", "t.name"].includes(raw(n))) ||
      (filename === "Report.jsx" && raw(n) === "c.text")
    )
      return `uiText(${raw(n)})`;
    if (n.type === "StringLiteral" && human(n.value)) {
      add(n.value);
      return `uiText(${raw(n)})`;
    }
    if (n.type === "TemplateLiteral") {
      const key = n.quasis
        .map(
          (q, i) => q.value.cooked + (i < n.expressions.length ? `{${i}}` : ""),
        )
        .join("");
      if (!human(key)) return null;
      add(key);
      return `uiMessage(${JSON.stringify(key)}, [${n.expressions.map(raw).join(", ")}])`;
    }
    if (n.type === "ConditionalExpression" && !hasJSX(n)) {
      const a = copy(n.consequent),
        b = copy(n.alternate);
      if (a || b)
        return `(${raw(n.test)} ? ${a || raw(n.consequent)} : ${b || raw(n.alternate)})`;
    }
    if (n.type === "LogicalExpression" && !hasJSX(n)) {
      const r = copy(n.right);
      if (r) return `(${raw(n.left)} ${n.operator} ${r})`;
    }
    if (n.type === "BinaryExpression" && n.operator === "+") {
      const ps = [];
      const split = (x) => {
        if (x.type === "BinaryExpression" && x.operator === "+") {
          split(x.left);
          split(x.right);
        } else ps.push(x);
      };
      split(n);
      if (!ps.some((p) => p.type === "StringLiteral" && human(p.value)))
        return null;
      if (ps.findIndex((p) => p.type === "StringLiteral") > 0)
        return `uiText(${raw(n)})`;
      let key = "",
        args = [];
      for (const p of ps)
        if (p.type === "StringLiteral") key += p.value;
        else {
          key += `{${args.length}}`;
          args.push(raw(p));
        }
      add(key);
      return `uiMessage(${JSON.stringify(key)}, [${args.join(", ")}])`;
    }
    if (n.type === "Identifier" && names.test(n.name))
      return `uiText(${raw(n)})`;
    if (
      n.type === "MemberExpression" &&
      !n.computed &&
      members.test(n.property.name)
    )
      return `uiText(${raw(n)})`;
    if (
      n.type === "CallExpression" &&
      n.callee.type === "Identifier" &&
      /(Text|Label|Notice|Reason|Feedback|Caption|Question)$/.test(
        n.callee.name,
      )
    )
      return `uiText(${raw(n)})`;
    return null;
  }
  function component(n, parents) {
    if (
      ![
        "FunctionDeclaration",
        "FunctionExpression",
        "ArrowFunctionExpression",
      ].includes(n.type)
    )
      return false;
    const p = parents.at(-1),
      name = n.id?.name || (p?.type === "VariableDeclarator" ? p.id?.name : "");
    const top = !parents.some((x) =>
      [
        "FunctionDeclaration",
        "FunctionExpression",
        "ArrowFunctionExpression",
        "ClassMethod",
      ].includes(x.type),
    );
    return (
      top &&
      (/^[A-Z]/.test(name || "") ||
        p?.type === "ExportDefaultDeclaration" ||
        (p?.type === "CallExpression" &&
          /^(memo|forwardRef)$/.test(
            p.callee?.property?.name || p.callee?.name || "",
          )))
    );
  }
  function walk(n, parents = []) {
    if (!n?.type) return;
    const p = parents.at(-1);
    if (component(n, parents)) components.add(n);
    if (
      n.type === "VariableDeclarator" &&
      n.id.type === "Identifier" &&
      n.init
    ) {
      const authored = (x) =>
        (x?.type === "StringLiteral" && /[a-zA-Z] [a-zA-Z]/.test(x.value)) ||
        (x?.type === "TemplateLiteral" &&
          x.quasis.some((q) =>
            /[a-zA-Z] [a-zA-Z]/.test(q.value.cooked || ""),
          )) ||
        (x?.type === "ConditionalExpression" &&
          (authored(x.consequent) || authored(x.alternate)));
      if (
        authored(n.init) &&
        !/(?:class|style|selector|query|command|code|path|url|body|draft|prompt|instruction)/i.test(
          n.id.name,
        )
      )
        ownedVariables.add(n.id.name);
    }
    const code = parents.some(
      (x) =>
        x.type === "JSXElement" &&
        ["code", "pre"].includes(x.openingElement?.name?.name),
    );
    if (
      code &&
      (n.type === "JSXText" ||
        (n.type === "JSXExpressionContainer" && p?.type !== "JSXAttribute"))
    )
      return;
    // LazyBoundary's canonical label is also its default reset identity. Its
    // LoadSurface translates the label; changing locale must not retry a failed chunk.
    if (
      n.type === "JSXAttribute" &&
      n.name.name === "label" &&
      p?.name?.name === "LazyBoundary"
    ) {
      if (n.value?.type === "StringLiteral") add(n.value.value);
      else if (n.value?.type === "JSXExpressionContainer" && n.value.expression?.type === "StringLiteral")
        add(n.value.expression.value);
      return;
    }
    if (
      n.type === "CallExpression" &&
      ["uiText", "uiMessage"].includes(n.callee?.name) &&
      n.arguments[0]?.type === "StringLiteral"
    ) {
      add(n.arguments[0].value);
      return;
    }
    // Translate only human prose returned by presentation helpers. Operational
    // enums and raw user/evidence returns have no authored sentence and stay intact.
    if (
      proseHelpers.has(filename) &&
      (n.type === "ReturnStatement" ||
        (n.type === "ArrowFunctionExpression" &&
          n.body?.type !== "BlockStatement"))
    ) {
      const value = n.type === "ReturnStatement" ? n.argument : n.body;
      const sentence = (x) =>
        (x?.type === "StringLiteral" &&
          /[a-zA-Z] [a-zA-Z]/.test(x.value) &&
          !command(x.value) &&
          !/^[./]/.test(x.value)) ||
        (x?.type === "TemplateLiteral" &&
          !command(x.quasis[0]?.value.cooked || "") &&
          !/^[./]/.test(x.quasis[0]?.value.cooked || "") &&
          x.quasis.some((q) =>
            /[a-zA-Z] [a-zA-Z]/.test(q.value.cooked || ""),
          )) ||
        (x?.type === "ConditionalExpression" &&
          (sentence(x.consequent) || sentence(x.alternate)));
      const replacement = sentence(value) ? copy(value) : null;
      if (replacement) {
        edits.push({ start: value.start, end: value.end, text: replacement });
        changed = true;
        return;
      }
    }
    if (n.type === "JSXText") {
      const text = jsxText(n.value);
      if (human(text)) {
        add(text);
        edits.push({
          start: n.start,
          end: n.end,
          text: `{uiText(${JSON.stringify(text)})}`,
        });
        changed = true;
      }
    }
    if (
      n.type === "JSXAttribute" &&
      attrs.has(n.name.name) &&
      n.value?.type === "StringLiteral" &&
      human(n.value.value)
    ) {
      add(n.value.value);
      edits.push({
        start: n.value.start,
        end: n.value.end,
        text: `{uiText(${JSON.stringify(n.value.value)})}`,
      });
      changed = true;
    }
    if (
      n.type === "JSXExpressionContainer" &&
      (p?.type === "JSXElement" ||
        p?.type === "JSXFragment" ||
        (p?.type === "JSXAttribute" && attrs.has(p.name.name)))
    ) {
      const r =
        n.expression.type === "Identifier" &&
        ownedVariables.has(n.expression.name)
          ? `uiText(${raw(n.expression)})`
          : copy(n.expression);
      if (r) {
        edits.push({
          start: n.expression.start,
          end: n.expression.end,
          text: r,
        });
        changed = true;
        return;
      }
    }
    // Mixed branches must translate their text without replacing the JSX branch.
    if (
      n.type === "ConditionalExpression" &&
      hasJSX(n) &&
      parents.some((x) => x.type === "JSXExpressionContainer")
    )
      for (const branch of [n.consequent, n.alternate]) {
        if (!hasJSX(branch)) {
          const r = copy(branch);
          if (r) {
            edits.push({ start: branch.start, end: branch.end, text: r });
            changed = true;
          }
        }
      }
    // Native confirmations and toast copy are authored UI too; arguments stay opaque.
    if (
      n.type === "CallExpression" &&
      ((n.callee.type === "MemberExpression" &&
        n.callee.object?.name === "window" &&
        ["confirm", "alert"].includes(n.callee.property?.name)) ||
        (n.callee.type === "Identifier" &&
          ["show", "toast"].includes(n.callee.name)))
    ) {
      const arg = n.arguments[0],
        r = copy(arg);
      if (r) {
        edits.push({ start: arg.start, end: arg.end, text: r });
        changed = true;
        return;
      }
    }
    if (
      filename === "report.js" &&
      n.type === "CallExpression" &&
      n.callee.type === "MemberExpression" &&
      n.callee.object?.name === "L" &&
      n.callee.property?.name === "push"
    )
      for (const arg of n.arguments) {
        const r = copy(arg);
        if (r) {
          edits.push({ start: arg.start, end: arg.end, text: r });
          changed = true;
        }
      }
    if (
      n.type === "StringLiteral" &&
      p?.type === "ObjectProperty" &&
      own.has(p.key?.name || p.key?.value)
    )
      add(n.value);
    if (n.type === "StringLiteral" && /^[A-Z][a-z]+$/.test(n.value))
      add(n.value);
    // Pure presentation helpers are also catalogue sources. Collecting copy never
    // changes their machine fields, and translation happens at the rendering sink.
    if (
      n.type === "StringLiteral" &&
      /[a-zA-Z] [a-zA-Z]/.test(n.value) &&
      !command(n.value) &&
      !/[\n<>]/.test(n.value) &&
      !/^(?:[.#]|\d+(?:px|vh)|[\w-]+ (?:var\(|solid |dashed ))/.test(n.value)
    )
      add(n.value);
    if (
      n.type === "ReturnStatement" &&
      n.argument?.type === "StringLiteral" &&
      / |[A-Z]/.test(n.argument.value)
    )
      add(n.argument.value);
    if (
      n.type === "TemplateLiteral" &&
      p?.type !== "TaggedTemplateExpression"
    ) {
      const key = n.quasis
        .map(
          (q, i) => q.value.cooked + (i < n.expressions.length ? `{${i}}` : ""),
        )
        .join("");
      if (
        /[a-zA-Z] [a-zA-Z]/.test(key) &&
        !command(key) &&
        !/[<>{}\n].*[<>{}\n].*[<>{}\n]/.test(key.replace(/\{\d+\}/g, ""))
      )
        add(key);
    }
    for (const [k, v] of Object.entries(n)) {
      if (["loc", "start", "end", "extra", "comments"].includes(k)) continue;
      if (Array.isArray(v)) for (const c of v) walk(c, [...parents, n]);
      else if (v?.type) walk(v, [...parents, n]);
    }
  }
  walk(tree);
  if (changed) {
    for (const fn of components) {
      if (fn.body.type === "BlockStatement") {
        if (
          !fn.body.body.some(
            (x) =>
              (x.type === "ExpressionStatement" &&
                x.expression?.callee?.name === "useUILanguage") ||
              (x.type === "VariableDeclaration" &&
                x.declarations.some(
                  (d) => d.init?.callee?.name === "useUILanguage",
                )),
          )
        )
          edits.push({
            start: fn.body.start + 1,
            end: fn.body.start + 1,
            text: "\n  useUILanguage()\n",
          });
      } else {
        edits.push({
          start: fn.body.start,
          end: fn.body.start,
          text: "{ useUILanguage(); return (",
        });
        edits.push({ start: fn.body.end, end: fn.body.end, text: ") }" });
      }
    }
    const bound = new Set(
        tree.program.body
          .filter((n) => n.type === "ImportDeclaration")
          .flatMap((n) => n.specifiers.map((s) => s.local.name)),
      ),
      missing = ["uiText", "uiMessage", "useUILanguage"].filter(
        (n) => !bound.has(n),
      );
    if (missing.length)
      edits.push({
        start: 0,
        end: 0,
        text: `import { ${missing.join(", ")} } from './uiLanguage.js'\n`,
      });
  }
  let result = source,
    edge = source.length + 1;
  for (const e of edits.sort((a, b) => b.start - a.start || b.end - a.end)) {
    if (e.end > edge && e.start !== e.end)
      throw Error(`Overlapping edits ${filename}:${e.start}`);
    result = result.slice(0, e.start) + e.text + result.slice(e.end);
    edge = e.start;
  }
  if (changed) {
    try {
      parse(result, { sourceType: "module", plugins: ["jsx"] });
    } catch (e) {
      throw new Error(filename + ": " + e.message);
    }
  }
  return { source: result, messages: [...messages], changed };
}
if (
  process.argv[1] &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  const uiRoot = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "..",
    ),
    root = path.join(uiRoot, "src"),
    catalogue = {},
    files = [];
  for (const file of fs.readdirSync(root))
    if (
      /\.(jsx|js)$/.test(file) &&
      !/^(uiLanguage|useAssistantLanguage|locale)/.test(file)
    ) {
      const full = path.join(root, file),
        r = localizeSource(fs.readFileSync(full, "utf8"), file);
      for (const key of r.messages) (catalogue[key] ??= []).push(file);
      if (r.changed) {
        files.push(file);
        if (process.argv.includes("--write")) fs.writeFileSync(full, r.source);
      }
    }
  const settings = JSON.parse(
    fs.readFileSync(
      path.resolve(uiRoot, "../looplab/serve/settings_ui_schema.json"),
      "utf8",
    ),
  );
  function scan(v, k = "") {
    if (
      typeof v === "string" &&
      [
        "label",
        "title",
        "help",
        "sub",
        "hint",
        "description",
        "shortLabel",
        "shortHelp",
        "essentialTitle",
        "essentialSub",
      ].includes(k) &&
      human(v)
    )
      (catalogue[norm(v)] ??= []).push("settings_ui_schema.json");
    else if (Array.isArray(v)) v.forEach((x) => scan(x));
    else if (v && typeof v === "object")
      Object.entries(v).forEach(([key, x]) => scan(x, key));
  }
  scan(settings);
  if (process.argv.includes("--catalogue")) {
    fs.mkdirSync(path.resolve(uiRoot, "../.tmp/doc72-global-language"), {
      recursive: true,
    });
    fs.writeFileSync(
      path.resolve(uiRoot, "../.tmp/doc72-global-language/catalogue.json"),
      JSON.stringify(
        Object.fromEntries(Object.entries(catalogue).sort()),
        null,
        2,
      ),
    );
  }
  if (process.argv.includes("--check")) {
    const page = JSON.parse(
      fs.readFileSync(path.join(root, "locales/ru.json"), "utf8"),
    );
    const missing = Object.keys(catalogue).filter(
      (key) => !Object.hasOwn(page.messages, key),
    );
    if (missing.length || files.length) {
      console.error(JSON.stringify({ unlocalizedFiles: files, missing }));
      process.exitCode = 1;
    }
  }
  console.log(
    JSON.stringify({
      messages: Object.keys(catalogue).length,
      files: files.length,
      chars: Object.keys(catalogue).join("").length,
    }),
  );
}
