import { describe, expect, it } from "vitest";
import { getSolverPresentation } from "../plan-format";

const FALLBACK_SUMMARY = {
  scheduledTaskCount: 15,
  totalTaskCount: 28,
  solver: {
    "有解": true,
    "解状态": "FEASIBLE",
    "方案来源": "HEURISTIC_FALLBACK",
    "分层全部最优": false,
  },
};

describe("getSolverPresentation", () => {
  it("启发式兜底不能冒充求解器给出的可行方案", () => {
    const presentation = getSolverPresentation(FALLBACK_SUMMARY);

    expect(presentation.shortLabel).not.toBe("可行方案");
    expect(presentation.shortLabel).toContain("兜底");
    expect(presentation.label).toContain("启发式");
    expect(presentation.tone).toBe("warning");
    expect(presentation.description).toContain("15/28");
  });

  it("求解器找到的可行解仍标为可行方案", () => {
    const presentation = getSolverPresentation({
      scheduledTaskCount: 25,
      totalTaskCount: 25,
      solver: { "解状态": "FEASIBLE", "方案来源": "CP_SAT", "分层全部最优": false },
    });

    expect(presentation.shortLabel).toBe("可行方案");
    expect(presentation.tone).toBe("info");
  });

  it("已证明最优保持不变", () => {
    const presentation = getSolverPresentation({
      scheduledTaskCount: 25,
      totalTaskCount: 25,
      solver: { "解状态": "OPTIMAL", "方案来源": "CP_SAT", "分层全部最优": true },
    });

    expect(presentation.shortLabel).toBe("已证明最优");
    expect(presentation.tone).toBe("success");
  });
});
