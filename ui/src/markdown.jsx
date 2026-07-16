import React, { useMemo } from 'react'
import { safeMarkdownHref } from './urlSafety.js'

// A tiny dependency-free Markdown renderer — just enough for chat answers: headings, bold/italic,
// inline code, fenced code blocks, bullet/numbered lists, blockquotes, links, and paragraphs. We
// avoid a library (local-first, zero-dep UI) and we don't use dangerouslySetInnerHTML, so user/LLM
// text can never inject HTML — every node is built as React elements from parsed tokens.

// Retain the old named export for callers while the policy itself lives in the shared URL trust
// boundary. React does not neutralize javascript:/data: links for us.
export const safeHref = safeMarkdownHref

// --- inline: split a line into bold / italic / code / link spans (escapes are literal text) ---
function inline(text, keyBase) {
  const out = []
  let i = 0, k = 0
  // order matters: code first (its contents are literal), then links, then bold, then italic
  const re = /(`[^`]+`)|(\[[^\]]+\]\([^)\s]+\))|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*]+\*)|(_[^_]+_)/
  let rest = text
  while (rest.length) {
    const m = re.exec(rest)
    if (!m) { out.push(rest); break }
    if (m.index > 0) out.push(rest.slice(0, m.index))
    const tok = m[0]
    if (tok.startsWith('`')) out.push(<code key={`${keyBase}-c${k++}`}>{tok.slice(1, -1)}</code>)
    else if (tok.startsWith('[')) {
      const mm = /\[([^\]]+)\]\(([^)\s]+)\)/.exec(tok)
      const href = safeMarkdownHref(mm[2])
      out.push(href
        ? <a key={`${keyBase}-l${k++}`} href={href} target="_blank" rel="noreferrer noopener">{mm[1]}</a>
        : <span key={`${keyBase}-l${k++}`}>{mm[1]}</span>)   // unsafe scheme (javascript:/data:) → plain text
    } else if (tok.startsWith('**') || tok.startsWith('__'))
      out.push(<strong key={`${keyBase}-b${k++}`}>{tok.slice(2, -2)}</strong>)
    else out.push(<em key={`${keyBase}-i${k++}`}>{tok.slice(1, -1)}</em>)
    rest = rest.slice(m.index + tok.length)
  }
  return out
}

// --- block: fold lines into headings / code fences / lists / quotes / paragraphs ---
function parse(src) {
  const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n')
  const blocks = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    // fenced code block
    if (/^```/.test(line)) {
      const lang = line.slice(3).trim()
      const buf = []
      i++
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++ }
      i++ // closing fence
      blocks.push({ t: 'code', lang, text: buf.join('\n') })
      continue
    }
    // heading
    const h = /^(#{1,6})\s+(.*)$/.exec(line)
    if (h) { blocks.push({ t: 'h', level: h[1].length, text: h[2] }); i++; continue }
    // horizontal rule
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { blocks.push({ t: 'hr' }); i++; continue }
    // blockquote (consecutive > lines)
    if (/^\s*>\s?/.test(line)) {
      const buf = []
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^\s*>\s?/, '')); i++ }
      blocks.push({ t: 'quote', text: buf.join('\n') })
      continue
    }
    // list (bullets or ordered) — consecutive item lines
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line)
      const items = []
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, '')); i++
      }
      blocks.push({ t: 'list', ordered, items })
      continue
    }
    // blank line → separator
    if (/^\s*$/.test(line)) { i++; continue }
    // paragraph: gather until a blank line or a block starter
    const buf = []
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^```/.test(lines[i])
           && !/^(#{1,6})\s+/.test(lines[i]) && !/^\s*([-*+]|\d+\.)\s+/.test(lines[i])
           && !/^\s*>\s?/.test(lines[i])) { buf.push(lines[i]); i++ }
    blocks.push({ t: 'p', text: buf.join('\n') })
  }
  return blocks
}

export default function Markdown({ text, className }) {
  const blocks = useMemo(() => parse(text), [text])
  return (
    <div className={'md' + (className ? ' ' + className : '')}>
      {blocks.map((b, bi) => {
        if (b.t === 'code') return <pre key={bi} className="code md-code">{b.text}</pre>
        if (b.t === 'hr') return <hr key={bi} className="md-hr" />
        if (b.t === 'h') { const H = `h${Math.min(b.level + 2, 6)}`; return <H key={bi} className="md-h">{inline(b.text, bi)}</H> }
        if (b.t === 'quote') return <blockquote key={bi} className="md-quote">{inline(b.text, bi)}</blockquote>
        if (b.t === 'list') {
          const items = b.items.map((it, ii) => <li key={ii}>{inline(it, `${bi}-${ii}`)}</li>)
          return b.ordered ? <ol key={bi} className="md-list">{items}</ol> : <ul key={bi} className="md-list">{items}</ul>
        }
        // paragraph — keep single newlines as <br/> (LLMs often wrap mid-thought)
        const parts = b.text.split('\n')
        return <p key={bi} className="md-p">{parts.map((ln, li) =>
          <React.Fragment key={li}>{li > 0 && <br />}{inline(ln, `${bi}-${li}`)}</React.Fragment>)}</p>
      })}
    </div>
  )
}
