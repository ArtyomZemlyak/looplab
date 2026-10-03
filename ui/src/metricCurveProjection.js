// Only the visual projection is bounded. Every retained point is a recorded point;
// first/last and each bucket's extremes survive in original order, without averaging.
export function metricCurveProjection(points) {
  const bounds = [Infinity, -Infinity, Infinity, -Infinity]
  for (const p of points) {
    bounds[0] = Math.min(bounds[0], p.step); bounds[1] = Math.max(bounds[1], p.step)
    bounds[2] = Math.min(bounds[2], p.value); bounds[3] = Math.max(bounds[3], p.value)
  }
  if (points.length <= 1024) return { points, bounds }
  const plotted = [points[0]], stride = Math.ceil((points.length - 2) / 511)
  for (let start = 1; start < points.length - 1; start += stride) {
    const end = Math.min(start + stride, points.length - 1)
    let low = start, high = start
    for (let i = start + 1; i < end; i++) {
      if (points[i].value < points[low].value) low = i
      if (points[i].value > points[high].value) high = i
    }
    plotted.push(points[Math.min(low, high)])
    if (low !== high) plotted.push(points[Math.max(low, high)])
  }
  plotted.push(points.at(-1))
  return { points: plotted, bounds }
}
