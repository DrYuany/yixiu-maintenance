import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ConversationCanvas } from "../ConversationCanvas";
import type { StageEvent } from "../../../store/types";

const stages: StageEvent[] = [
  {
    id: "think.0",
    label: "理解这句话，决定下一步",
    status: "done",
    detail: "决定调用 读取告警清单",
  },
  {
    id: "tool.0.0",
    label: "调用工具 · 读取告警清单",
    status: "done",
    tool: "read_alarms",
    detail: "已读到数据 · 共 15 条 · 12 ms",
  },
  {
    id: "think.1",
    label: "结合读到的事实继续推理",
    status: "running",
    detail: "模型在判断要不要查数据、查哪份数据",
  },
];

describe("agent 过程面板", () => {
  it("直接说出现在到哪个环节、调用了哪个工具、拿回了什么", () => {
    const { container } = render(
      <ConversationCanvas
        messages={[]}
        sending
        stages={stages}
        onSend={async () => undefined}
      />,
    );

    expect(screen.getByText(/当前环节：结合读到的事实继续推理/)).toBeTruthy();
    expect(screen.getByText("read_alarms")).toBeTruthy();
    expect(screen.getByText(/共 15 条/)).toBeTruthy();
    expect(screen.getByText("第 2 步")).toBeTruthy();
    expect(screen.getAllByText("已完成").length).toBeGreaterThan(0);
    expect(screen.getByText("进行中")).toBeTruthy();
    expect(screen.getByText(/2\/3 步完成/)).toBeTruthy();

    // 类名冲突回归：旧原型的 .process-steps 是六列网格（窄屏三列），
    // 会把中文挤成一字一行。过程面板必须只用独立类名。
    expect(container.querySelector(".agent-process-list")).not.toBeNull();
    expect(container.querySelector(".process-steps")).toBeNull();
  });

  it("没有 running 步骤时说明已整理出回答，而不是留一个空转的圈", () => {
    render(
      <ConversationCanvas
        messages={[]}
        sending
        stages={stages.map((stage) => ({ ...stage, status: "done" as const }))}
        onSend={async () => undefined}
      />,
    );
    expect(screen.getByText(/当前环节：已整理出回答/)).toBeTruthy();
    expect(screen.getByText(/3\/3 步完成/)).toBeTruthy();
  });
});
