import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import { P01Dashboard } from "../P01Dashboard";

describe("P01Dashboard", () => {
  it("leads with time, total cost and two independent baseline comparisons", () => {
    render(<P01Dashboard onSelectPlan={vi.fn()} onOpenAttention={vi.fn()} />);

    const firstPlan = screen.getByText("首选").closest("article");
    expect(firstPlan).not.toBeNull();
    const card = within(firstPlan!);
    expect(card.getByText("建议出海时间")).toBeInTheDocument();
    expect(card.getByText("预计总经济成本")).toBeInTheDocument();
    expect(card.getByText("¥282.1万")).toBeInTheDocument();
    expect(card.getByText("较严重性优先基准")).toBeInTheDocument();
    expect(card.getByText("较低风速优先基准")).toBeInTheDocument();
    expect(card.queryByText("检修人员")).not.toBeInTheDocument();
    expect(card.queryByText("备件准备")).not.toBeInTheDocument();
  });

  it("switches to the high-risk recommendation without exposing task detail", () => {
    render(<P01Dashboard onSelectPlan={vi.fn()} onOpenAttention={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /高风险优先/ }));

    expect(screen.getByText("10月24日 07:30 — 10月27日 17:00")).toBeInTheDocument();
    expect(screen.getByText("多支出 ¥12.1万")).toBeInTheDocument();
    expect(screen.queryByText("主轴温升异常")).not.toBeInTheDocument();
  });

  it("orders actionable attention from critical to informational", () => {
    render(<P01Dashboard onSelectPlan={vi.fn()} onOpenAttention={vi.fn()} />);
    const items = screen.getAllByRole("button").filter((button) => button.classList.contains("attention-row"));
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining("新增 1 项高风险告警"),
      expect.stringContaining("明日出海物资尚未备齐"),
      expect.stringContaining("2 张检修单待审核"),
    ]);
  });
});
