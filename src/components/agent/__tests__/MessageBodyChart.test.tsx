import { fireEvent, render, screen } from "@testing-library/react";
import { MessageBody } from "../MessageBody";

const validSpec = {
  schemaVersion: "environment-chart.v1",
  title: "风速与电价",
  dataNature: "合成",
  source: "测试基座",
  inputHash: "abc123",
  asOf: "2026-10-23T18:00:00+08:00",
  timezone: "Asia/Shanghai",
  defaultRange: 3,
  availableRanges: [3],
  units: { wind: "m/s", price: "元/MWh", power: "MW", usableHours: "小时" },
  hourly: Array.from({ length: 72 }, (_, index) => ({
    time: `2026-10-24T${String(index % 24).padStart(2, "0")}:00:00+08:00`,
    wind: 9,
    price: 430,
    powerMw: 5,
  })),
  daily: [],
  annotations: { planningBoundary: null, targetWindow: null },
};

describe("MessageBody chart blocks", () => {
  it("renders a supported chart block between normal markdown sections", () => {
    const content = `前言\n\n\`\`\`chart\n${JSON.stringify(validSpec)}\n\`\`\`\n\n结论`;
    render(<MessageBody content={content} />);

    expect(screen.getByText("前言")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "3 天" })).toBeInTheDocument();
    expect(screen.getByText("结论")).toBeInTheDocument();
  });

  it("falls back to a normal code block for invalid or unknown chart specs", () => {
    const unknown = { ...validSpec, schemaVersion: "environment-chart.v99" };
    const content = `\`\`\`chart\nnot-json\n\`\`\`\n\n\`\`\`chart\n${JSON.stringify(unknown)}\n\`\`\``;
    const { container } = render(<MessageBody content={content} />);

    expect(container.querySelectorAll("pre code")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "3 天" })).not.toBeInTheDocument();
  });

  it("hydrates a saved chart message and keeps it interactive", () => {
    const content = `历史回答\n\n\`\`\`chart\n${JSON.stringify(validSpec)}\n\`\`\``;
    const { rerender } = render(<MessageBody content={content} />);

    rerender(<MessageBody content={content} />);
    fireEvent.click(screen.getByRole("button", { name: "3 天" }));

    expect(screen.getByRole("button", { name: "3 天" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(/逐时 · 72 个数据点/)).toBeInTheDocument();
  });
});
