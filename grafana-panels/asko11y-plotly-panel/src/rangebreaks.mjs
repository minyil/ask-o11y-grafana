// Plotly's WebGL traces do not support axis rangebreaks: combined, the axis
// collapses and no line is drawn. Such figures are drawn with the equivalent
// SVG trace; only the renderer changes, never the data.
const SVG_TRACE_FOR = { scattergl: "scatter" };

function hasRangebreaks(layout) {
  return Object.entries(layout ?? {}).some(
    ([key, axis]) =>
      /^[xy]axis\d*$/.test(key) &&
      Array.isArray(axis?.rangebreaks) &&
      axis.rangebreaks.some((item) => item && item.enabled !== false),
  );
}

export function withRangebreakSafeTraces(data, layout) {
  if (!hasRangebreaks(layout)) {
    return data;
  }
  return data.map((trace) =>
    trace && SVG_TRACE_FOR[trace.type]
      ? { ...trace, type: SVG_TRACE_FOR[trace.type] }
      : trace,
  );
}
