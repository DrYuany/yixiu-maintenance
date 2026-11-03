import { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

type RangeDays = 3 | 7 | 14 | 30;

interface HourlyPoint {
  time: string;
  wind: number | null;
  price: number | null;
  powerMw: number | null;
}

interface DailyPoint {
  date: string;
  windMean: number | null;
  priceMean: number | null;
  seaState: string | null;
  usableHours: number | null;
}

export interface EnvironmentChartSpec {
  schemaVersion: "environment-chart.v1";
  title: string;
  dataNature: string | null;
  source?: string | null;
  inputHash?: string | null;
  asOf?: string | null;
  timezone?: string;
  defaultRange: number;
  availableRanges: number[];
  units?: {
    wind?: string;
    price?: string;
    power?: string;
    usableHours?: string;
  };
  hourly: HourlyPoint[];
  daily: DailyPoint[];
  annotations: {
    planningBoundary: string | null;
    targetWindow: {
      id: string | null;
      start: string;
      end: string;
      approval?: string | null;
    } | null;
  };
}

const RANGE_OPTIONS: RangeDays[] = [3, 7, 14, 30];

export function isEnvironmentChartSpec(value: unknown): value is EnvironmentChartSpec {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<EnvironmentChartSpec>;
  return item.schemaVersion === "environment-chart.v1"
    && Array.isArray(item.availableRanges)
    && Array.isArray(item.hourly)
    && Array.isArray(item.daily)
    && Boolean(item.annotations && typeof item.annotations === "object");
}

function shortTime(value: string, hourly: boolean) {
  if (hourly) return `${value.slice(5, 10)} ${value.slice(11, 13)}时`;
  return value.slice(5, 10);
}

function markerValue(value: string | null | undefined, hourly: boolean) {
  if (!value) return undefined;
  return hourly ? value : value.slice(0, 10);
}

export function EnvironmentChart({ spec }: { spec: EnvironmentChartSpec }) {
  const available = useMemo(
    () => RANGE_OPTIONS.filter((range) => spec.availableRanges.includes(range)),
    [spec.availableRanges],
  );
  const initialRange = available.includes(spec.defaultRange as RangeDays)
    ? spec.defaultRange as RangeDays
    : available[0] ?? 7;
  const [range, setRange] = useState<RangeDays>(initialRange);

  useEffect(() => {
    setRange(initialRange);
  }, [initialRange, spec.inputHash]);

  const hourly = range <= 7;
  const data = useMemo(() => {
    if (hourly) {
      return spec.hourly.slice(0, range * 24).map((item) => ({
        x: item.time,
        wind: item.wind,
        price: item.price,
        powerMw: item.powerMw,
        seaState: null,
        usableHours: null,
      }));
    }
    return spec.daily.slice(0, range).map((item) => ({
      x: item.date,
      wind: item.windMean,
      price: item.priceMean,
      powerMw: null,
      seaState: item.seaState,
      usableHours: item.usableHours,
    }));
  }, [hourly, range, spec.daily, spec.hourly]);

  const boundary = markerValue(spec.annotations.planningBoundary, hourly);
  const targetStart = markerValue(spec.annotations.targetWindow?.start, hourly);
  const targetEnd = markerValue(spec.annotations.targetWindow?.end, hourly);
  const target = spec.annotations.targetWindow;
  const natureLabel = String(spec.dataNature || "").includes("合成")
    ? "合成测试数据"
    : spec.dataNature || "数据性质未标注";

  return (
    <section className="environment-chart-card" aria-label="环境数据可视化">
      <div className="environment-chart-head">
        <div>
          <span className="environment-chart-eyebrow">检修环境</span>
          <h3>{spec.title || "风速与电价"}</h3>
        </div>
        <span className={`environment-data-badge${natureLabel === "合成测试数据" ? " synthetic" : ""}`}>
          {natureLabel}
        </span>
      </div>

      <div className="environment-range-switch" aria-label="图表时间范围">
        {RANGE_OPTIONS.map((option) => {
          const enabled = available.includes(option);
          return (
            <button
              type="button"
              key={option}
              disabled={!enabled}
              aria-pressed={range === option}
              title={enabled ? `查看未来 ${option} 天` : `当前快照不足 ${option} 天`}
              onClick={() => enabled && setRange(option)}
            >
              {option} 天
            </button>
          );
        })}
      </div>

      <div className="environment-chart-legend" aria-hidden="true">
        <span><i className="wind" />风速 m/s</span>
        <span><i className="price" />电价 元/MWh</span>
        <small>{hourly ? "逐时" : "逐日"} · {data.length} 个数据点</small>
      </div>

      <div
        className="environment-chart-plot"
        role="img"
        aria-label={`未来 ${range} 天风速与电价，${hourly ? "逐时" : "逐日"}，共 ${data.length} 个数据点`}
      >
        <ResponsiveContainer width="100%" height={270}>
          <LineChart data={data} margin={{ top: 12, right: 8, left: 0, bottom: 4 }}>
            <CartesianGrid strokeDasharray="3 5" vertical={false} />
            <XAxis
              dataKey="x"
              minTickGap={28}
              tickFormatter={(value) => shortTime(String(value), hourly)}
              tick={{ fontSize: 10 }}
            />
            <YAxis
              yAxisId="wind"
              width={38}
              tick={{ fontSize: 10 }}
              label={{ value: "m/s", angle: -90, position: "insideLeft", fontSize: 10 }}
            />
            <YAxis
              yAxisId="price"
              orientation="right"
              width={48}
              tick={{ fontSize: 10 }}
              label={{ value: "元/MWh", angle: 90, position: "insideRight", fontSize: 10 }}
            />
            <Tooltip
              labelFormatter={(value) => shortTime(String(value), hourly)}
              formatter={(value, name) => [
                value == null ? "缺测" : Number(value).toLocaleString(),
                name === "wind" ? "风速 m/s" : "电价 元/MWh",
              ]}
            />
            {targetStart && targetEnd ? (
              <ReferenceArea
                x1={targetStart}
                x2={targetEnd}
                yAxisId="wind"
                fill="#d8f0e6"
                fillOpacity={0.65}
                strokeOpacity={0}
              />
            ) : null}
            {boundary ? (
              <ReferenceLine
                x={boundary}
                yAxisId="wind"
                stroke="#8b5cf6"
                strokeDasharray="5 4"
              />
            ) : null}
            <Line
              yAxisId="wind"
              type="monotone"
              dataKey="wind"
              stroke="#1677ff"
              strokeWidth={2}
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="price"
              stroke="#f59e0b"
              strokeWidth={2}
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>

      <div className="environment-chart-annotations">
        <span><i className="boundary" />规划边界：{spec.annotations.planningBoundary?.slice(0, 16).replace("T", " ") || "未记录"}</span>
        <span><i className="window" />{target ? `${target.id || "目标窗口"}：${target.start.slice(5, 16).replace("T", " ")} 至 ${target.end.slice(5, 16).replace("T", " ")}` : "当前没有可标注的目标窗口"}</span>
      </div>
      <p className="environment-chart-note">
        海况与可用小时在逐日视图中随数据点提供；缺测值保持断线，不做插值。
        {spec.source ? ` 来源：${spec.source}。` : ""}
      </p>
    </section>
  );
}
