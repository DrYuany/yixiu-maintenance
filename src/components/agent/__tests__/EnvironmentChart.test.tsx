import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { EnvironmentChart } from "../EnvironmentChart";

const hourly = Array.from({ length: 168 }, (_, index) => ({
  time: `2026-10-${String(23 + Math.floor(index / 24)).padStart(2, "0")}T${String(index % 24).padStart(2, "0")}:00:00+08:00`,
  wind: 8 + (index % 5),
  price: 420 + (index % 7),
  powerMw: 6,
}));

const daily = Array.from({ length: 30 }, (_, index) => ({
  date: `2026-11-${String(index + 1).padStart(2, "0")}`,
  windMean: 9 + (index % 4),
  priceMean: 450 + (index % 6),
  seaState: index % 2 ? "良好" : "一般",
  usableHours: 8,
}));

const spec = {
  schemaVersion: "environment-chart.v1" as const,
  title: "风速与电价",
  dataNature: "合成",
  source: "测试基座快照",
  defaultRange: 7,
  availableRanges: [3, 7, 14, 30],
  hourly,
  daily,
  annotations: {
    planningBoundary: "2026-10-30T18:00:00+08:00",
    targetWindow: {
      id: "窗口02",
      start: "2026-11-02T00:00:00+08:00",
      end: "2026-11-12T00:00:00+08:00",
    },
  },
};

describe("EnvironmentChart", () => {
  it("switches between hourly and daily ranges without requesting new data", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(<EnvironmentChart spec={spec} />);

    expect(screen.getByRole("button", { name: "7 天" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(/逐时 · 168 个数据点/)).toBeInTheDocument();
    expect(screen.getByText("合成测试数据")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "14 天" }));

    expect(screen.getByRole("button", { name: "14 天" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(/逐日 · 14 个数据点/)).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it("shows units, provenance, planning markers and accessible range controls", () => {
    render(<EnvironmentChart spec={spec} />);

    expect(screen.getByText("风速 m/s")).toBeInTheDocument();
    expect(screen.getByText("电价 元/MWh")).toBeInTheDocument();
    expect(screen.getByText(/规划边界/)).toBeInTheDocument();
    expect(screen.getByText(/窗口02/)).toBeInTheDocument();
    expect(screen.getByText(/海况与可用小时/)).toBeInTheDocument();
    expect(screen.getByText(/缺测值保持断线/)).toBeInTheDocument();
    expect(screen.getByText(/来源：测试基座快照/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /未来 7 天风速与电价/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "3 天" }));
    expect(screen.getByText(/逐时 · 72 个数据点/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "30 天" }));
    expect(screen.getByText(/逐日 · 30 个数据点/)).toBeInTheDocument();
  });

  it("disables unavailable ranges and explains a missing target window", () => {
    render(
      <EnvironmentChart
        spec={{
          ...spec,
          availableRanges: [3],
          defaultRange: 3,
          annotations: { planningBoundary: null, targetWindow: null },
        }}
      />,
    );

    expect(screen.getByRole("button", { name: "7 天" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "7 天" })).toHaveAttribute("title", "当前快照不足 7 天");
    expect(screen.getByText("当前没有可标注的目标窗口")).toBeInTheDocument();
  });
});
