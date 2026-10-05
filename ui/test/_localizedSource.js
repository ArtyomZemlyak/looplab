import { parse } from '@babel/parser'

// Structural assertions inspect controls and gates, independent of prose wrappers.
// Live tests execute actual source. This removes only language calls/subscriptions;
// conditions, callbacks, identities, routes and command arguments are preserved.
export function canonicalUISource(source) {
  const tree = parse(source, { sourceType: 'module', plugins: ['jsx'], createParenthesizedExpressions: true })
  const raw = node => source.slice(node.start, node.end)
  function render(node, parent) {
    if (!node?.type) return ''
    if (node.type === 'ParenthesizedExpression' && ['JSXExpressionContainer', 'ParenthesizedExpression', 'ConditionalExpression'].includes(parent?.type)) {
      let expression = node.expression
      while (expression.type === 'ParenthesizedExpression') expression = expression.expression
      if (expression.type === 'ConditionalExpression') return render(node.expression, node)
    }
    if (node.type === 'ExpressionStatement' && node.expression?.callee?.name === 'useUILanguage') return ''
    if (node.type === 'JSXAttribute' && node.value?.type === 'JSXExpressionContainer') {
      const expr = node.value.expression
      if (expr.type === 'StringLiteral') return raw(node.name) + '=' + JSON.stringify(expr.value)
      if (expr.callee?.name === 'uiText' && expr.arguments[0]?.type === 'StringLiteral') return raw(node.name) + '=' + JSON.stringify(expr.arguments[0].value)
    }
    if (node.type === 'JSXExpressionContainer' && parent?.type !== 'JSXAttribute') {
      const expr = node.expression
      if (expr.callee?.name === 'uiText' && expr.arguments[0]?.type === 'StringLiteral') return expr.arguments[0].value.replace(/&/g, '&amp;')
      if (expr.callee?.name === 'uiMessage' && expr.arguments[0]?.type === 'StringLiteral' && expr.arguments[1]?.type === 'ArrayExpression') return expr.arguments[0].value.replace(/\{(\d+)\}/g, (_, index) => '{' + render(expr.arguments[1].elements[index], expr) + '}')
      if (expr.extra?.parenthesized) return '{' + render(expr, node) + '}'
    }
    if (node.type === 'CallExpression' && node.callee?.name === 'uiText') return render(node.arguments[0], node)
    if (node.type === 'CallExpression' && node.callee?.name === 'uiMessage' && node.arguments[0]?.type === 'StringLiteral' && node.arguments[1]?.type === 'ArrayExpression') return '`' + node.arguments[0].value.replace(/`/g, '\\`').replace(/\{(\d+)\}/g, (_, index) => '${' + render(node.arguments[1].elements[index], node) + '}') + '`'
    const children = Object.entries(node).filter(([key]) => !['loc', 'extra', 'comments', 'leadingComments', 'trailingComments', 'innerComments'].includes(key))
      .flatMap(([, value]) => Array.isArray(value) ? value : [value]).filter(value => value?.type && value.start >= node.start && value.end <= node.end)
    let out = raw(node)
    for (const child of children.sort((a, b) => b.start - a.start)) out = out.slice(0, child.start - node.start) + render(child, node) + out.slice(child.end - node.start)
    return out
  }
  return render(tree.program)
}
