import { createOfflineDemo, isStandaloneFileProtocol } from "../offline-demo";

describe("offline standalone preview", () => {
  it("only activates for a directly opened file", () => {
    expect(isStandaloneFileProtocol("file:")).toBe(true);
    expect(isStandaloneFileProtocol("http:")).toBe(false);
    expect(isStandaloneFileProtocol("https:")).toBe(false);
  });

  it("provides a complete visible conversation without a backend", () => {
    const demo = createOfflineDemo();
    expect(demo.conversations).toHaveLength(3);
    expect(demo.messages.some((item) => item.role === "assistant")).toBe(true);
    expect(demo.messages.some((item) => item.content.includes("未证明最优"))).toBe(true);
  });
});
