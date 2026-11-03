import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

describe("vite build configuration", () => {
  it("uses relative asset paths so build/index.html works over file protocol", () => {
    const source = readFileSync(resolve(process.cwd(), "vite.config.ts"), "utf8");
    expect(source).toMatch(/base:\s*['"]\.\/['"]/);
  });

  it("post-processes the production build into one standalone HTML file", () => {
    const packageJson = JSON.parse(
      readFileSync(resolve(process.cwd(), "package.json"), "utf8"),
    );
    expect(packageJson.scripts.build).toContain("make-standalone.mjs");
    const standaloneScript = readFileSync(
      resolve(process.cwd(), "scripts/make-standalone.mjs"),
      "utf8",
    );
    expect(standaloneScript).toContain('html.replace("</body>"');
  });
});
