import { describe, expect, it } from "vitest";
import {
  formatBaselineDelta,
  formatCurrency,
  getStrategyLabel,
  prototypePlans,
} from "../prototype-data";

describe("maintenance prototype data", () => {
  it("keeps the three decision strategies explicit", () => {
    expect(getStrategyLabel("economic")).toBe("经济性优先");
    expect(getStrategyLabel("risk")).toBe("高风险优先");
    expect(getStrategyLabel("weighted")).toBe("加权最优");
  });

  it("formats total economic cost for fast scanning", () => {
    expect(formatCurrency(2821043.19)).toBe("¥282.1万");
  });

  it("shows each baseline difference independently", () => {
    const economicPlan = prototypePlans.economic;
    expect(formatBaselineDelta(economicPlan.severityBaselineDelta)).toBe(
      "少支出 ¥18.6万",
    );
    expect(formatBaselineDelta(economicPlan.lowWindBaselineDelta)).toBe(
      "少支出 ¥9.8万",
    );
  });
});
