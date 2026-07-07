// KPI-tile sparkline per the stat-tile contract: ~12 points, drawn in the
// de-emphasis hue with the current (last) period marked in the series accent.
// Decorative — the tile's value + hint carry the information (aria-hidden).

function resample(values: number[], buckets = 12): number[] {
  if (values.length <= buckets) return values;
  const out: number[] = [];
  const size = values.length / buckets;
  for (let i = 0; i < buckets; i++) {
    const slice = values.slice(Math.floor(i * size), Math.max(Math.floor((i + 1) * size), Math.floor(i * size) + 1));
    out.push(slice.reduce((a, b) => a + b, 0));
  }
  return out;
}

export function Sparkline({
  values,
  width = 76,
  height = 26,
}: {
  values: number[];
  width?: number;
  height?: number;
}) {
  const data = resample(values);
  if (data.length < 2) return null;
  const max = Math.max(...data, 1);
  const pad = 3;
  const stepX = (width - pad * 2) / (data.length - 1);
  const y = (v: number) => height - pad - (v / max) * (height - pad * 2);
  const points = data.map((v, i) => `${(pad + i * stepX).toFixed(1)},${y(v).toFixed(1)}`);
  const lastX = pad + (data.length - 1) * stepX;
  const lastY = y(data[data.length - 1]);

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      aria-hidden="true"
      className="shrink-0"
    >
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke="var(--baseline)"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx={lastX} cy={lastY} r="2.5" fill="var(--series-1)" stroke="var(--surface-1)" strokeWidth="1.5" />
    </svg>
  );
}
