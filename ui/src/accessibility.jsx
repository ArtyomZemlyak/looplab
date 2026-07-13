import React, { useId, useState } from 'react'

const textValue = value => {
  if (value == null) return ''
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  try { return JSON.stringify(value) } catch { return String(value) }
}

const spreadsheetSafeText = value => {
  const text = textValue(value)
  if (typeof value !== 'string') return text
  // Spreadsheet applications can execute CSV cells as formulas. Preserve real numeric values, but
  // force untrusted text with a formula/control prefix to remain literal when the export is opened.
  return /^[\t\r]/.test(text) || /^[\u0000-\u0020]*[=+\-@]/.test(text) ? `'${text}` : text
}

const csvCell = value => `"${spreadsheetSafeText(value).replaceAll('"', '""')}"`

export function tableCsv(columns, rows) {
  const header = columns.map(column => csvCell(column.label || column.key)).join(',')
  const body = rows.map(row => columns.map(column => {
    const value = column.value ? column.value(row) : row?.[column.key]
    return csvCell(value)
  }).join(','))
  return [header, ...body].join('\r\n')
}

export function downloadTableCsv(filename, columns, rows) {
  if (typeof document === 'undefined' || typeof URL === 'undefined') return false
  const blob = new Blob([`\ufeff${tableCsv(columns, rows)}`], { type: 'text/csv;charset=utf-8' })
  const href = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = href
  anchor.download = filename || 'data.csv'
  anchor.click()
  URL.revokeObjectURL(href)
  return true
}

function TableElement({ caption, columns, rows, rowKey, empty, card }) {
  return <table className={'tbl data-table' + (card ? ' cardable' : '')}>
    <caption className="sr-only">{caption}</caption>
    <thead><tr>{columns.map(column => <th key={column.key} scope="col"
      className={column.numeric ? 'numeric' : undefined}>{column.label || column.key}</th>)}</tr></thead>
    <tbody>{rows.length === 0
      ? <tr><td colSpan={columns.length} className="data-table-empty">{empty}</td></tr>
      : rows.map((row, index) => <tr key={rowKey ? rowKey(row, index) : index}>
        {columns.map((column, columnIndex) => {
          const raw = column.value ? column.value(row) : row?.[column.key]
          const rendered = column.render ? column.render(raw, row, index) : textValue(raw)
          const Cell = column.rowHeader || columnIndex === 0 && column.firstColumnHeader ? 'th' : 'td'
          return <Cell key={column.key} scope={Cell === 'th' ? 'row' : undefined}
            data-label={column.label || column.key} className={column.numeric ? 'numeric' : undefined}>
            {rendered}
          </Cell>
        })}
      </tr>)}</tbody>
  </table>
}

export function DataTable({ caption, columns = null, rows = [], rowKey = null,
  empty = 'No rows', csvName = null, card = true, children = null, className = '' }) {
  const generated = useId().replaceAll(':', '')
  const headingId = `data-table-${generated}`
  let table = children
  if (columns) {
    table = <TableElement caption={caption} columns={columns} rows={rows} rowKey={rowKey}
      empty={empty} card={card} />
  } else if (React.isValidElement(children)) {
    table = React.cloneElement(children, {
      className: `${children.props.className || 'tbl'} data-table${card ? ' cardable' : ''}`,
      children: [<caption className="sr-only" key="caption">{caption}</caption>, children.props.children],
    })
  }
  return <section className={`data-table-region ${className}`.trim()}>
    <div className="data-table-heading">
      <span id={headingId}>{caption}</span>
      {csvName && columns && rows.length > 0 && <button type="button" className="btn xs ghost"
        aria-label={`Export ${caption} as CSV`}
        onClick={() => downloadTableCsv(csvName, columns, rows)}>Export CSV</button>}
    </div>
    <div className="data-table-scroll" role="region" aria-labelledby={headingId} tabIndex={0}>{table}</div>
  </section>
}

export function ChartFrame({ title, description, columns = [], rows = [], csvName,
  children, className = '' }) {
  const generated = useId().replaceAll(':', '')
  const titleId = `chart-title-${generated}`
  const descriptionId = `chart-description-${generated}`
  const [showData, setShowData] = useState(false)
  const labelledBy = `${titleId} ${descriptionId}`
  return <figure className={`accessible-chart ${className}`.trim()}>
    <figcaption>
      <span id={titleId} className="accessible-chart-title">{title}</span>
      <span id={descriptionId} className="accessible-chart-description">{description}</span>
    </figcaption>
    <div className="accessible-chart-visual" role="region" aria-labelledby={titleId}
      aria-describedby={descriptionId} tabIndex={0}>{typeof children === 'function'
      ? children({ labelledBy, titleId, descriptionId }) : children}</div>
    <div className="accessible-chart-actions">
      <button type="button" className="btn xs ghost" aria-expanded={showData}
        aria-label={`${showData ? 'Hide' : 'View'} ${title} data`}
        aria-controls={`chart-data-${generated}`} onClick={() => setShowData(value => !value)}>
        {showData ? 'Hide data' : 'View data'}
      </button>
      {csvName && rows.length > 0 && <button type="button" className="btn xs ghost"
        aria-label={`Export ${title} data as CSV`}
        onClick={() => downloadTableCsv(csvName, columns, rows)}>Export CSV</button>}
    </div>
    {showData && <div id={`chart-data-${generated}`}>
      <DataTable caption={`${title} data`} columns={columns} rows={rows} card csvName={null} />
    </div>}
  </figure>
}

// Keep native-link affordances (open in a new tab, save link, etc.) while using the SPA router for
// an ordinary primary click. A link that always calls preventDefault() is only native in appearance.
export function followClientRoute(event, navigate) {
  if (!event || event.defaultPrevented || (event.button != null && event.button !== 0)
      || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false
  event.preventDefault?.()
  navigate?.()
  return true
}

export function nextRovingIndex(key, index, length) {
  if (!Number.isInteger(index) || !Number.isInteger(length) || length < 1) return null
  if (key === 'ArrowRight' || key === 'ArrowDown') return (index + 1) % length
  if (key === 'ArrowLeft' || key === 'ArrowUp') return (index - 1 + length) % length
  if (key === 'Home') return 0
  if (key === 'End') return length - 1
  return null
}
