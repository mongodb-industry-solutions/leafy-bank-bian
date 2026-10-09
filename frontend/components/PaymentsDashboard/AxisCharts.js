"use client";

// Recharts-backed axis charts. Loaded with next/dynamic (ssr: false) from the dashboard so
// the KPI strip and tables paint before the chart bundle arrives.
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

const AXIS = { fontSize: 11, fill: "#5c6c75" };

function tickFormatter(bucket) {
  return (iso) => {
    const d = new Date(iso);
    return bucket === "hour"
      ? d.toLocaleTimeString([], { hour: "numeric" }).toLowerCase()
      : d.toLocaleDateString([], { month: "short", day: "numeric" });
  };
}

export function VolumeChart({ series, types, colors, labels, bucket }) {
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={series} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
        <CartesianGrid stroke="#e8edeb" vertical={false} />
        <XAxis dataKey="t" tick={AXIS} tickFormatter={tickFormatter(bucket)} tickLine={false} minTickGap={24} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} allowDecimals={false} />
        <Tooltip
          labelFormatter={tickFormatter(bucket)}
          formatter={(value, name) => [value, labels[name] ?? name]}
          cursor={{ fill: "rgba(0,0,0,0.04)" }}
        />
        {types.map((t) => (
          <Bar key={t} dataKey={t} stackId="v" fill={colors[t]} isAnimationActive={false} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

export function TrendChart({ series, bucket }) {
  return (
    <ResponsiveContainer width="100%" height={200}>
      <AreaChart data={series} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
        <CartesianGrid stroke="#e8edeb" vertical={false} />
        <XAxis dataKey="t" tick={AXIS} tickFormatter={tickFormatter(bucket)} tickLine={false} minTickGap={24} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} allowDecimals={false} />
        <Tooltip labelFormatter={tickFormatter(bucket)} formatter={(v) => [v, "Exceptions"]} />
        <Area
          type="monotone"
          dataKey="count"
          stroke="#cf4a4a"
          fill="#f7c9c9"
          fillOpacity={0.6}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
