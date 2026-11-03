import { readFile, stat, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const buildDirectory = resolve(projectRoot, "build");
const htmlPath = resolve(buildDirectory, "index.html");
let html = await readFile(htmlPath, "utf8");

const stylesheetMatch = html.match(/<link rel="stylesheet"[^>]*href="([^"]+)"[^>]*>/);
if (!stylesheetMatch) throw new Error("Built stylesheet reference was not found.");
const stylesheetPath = resolve(buildDirectory, stylesheetMatch[1]);
const assetBase = resolve(buildDirectory, dirname(stylesheetMatch[1]));
let stylesheet = await readFile(stylesheetPath, "utf8");

const mimeByExtension = {
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".ttf": "font/ttf",
  ".otf": "font/otf",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".svg": "image/svg+xml",
  ".webp": "image/webp",
};

const assetReferences = [...stylesheet.matchAll(/url\((['"]?)([^)'"]+)\1\)/g)];
for (const match of assetReferences) {
  const rawReference = match[2].trim();
  if (
    rawReference.startsWith("data:")
    || rawReference.startsWith("http:")
    || rawReference.startsWith("https:")
    || rawReference.startsWith("//")
  ) {
    continue;
  }
  const assetPath = resolve(assetBase, rawReference);
  const extension = assetPath.slice(assetPath.lastIndexOf(".")).toLowerCase();
  const mime = mimeByExtension[extension];
  if (!mime) continue;
  try {
    const info = await stat(assetPath);
    if (!info.isFile()) continue;
    const bytes = await readFile(assetPath);
    const dataUrl = `data:${mime};base64,${bytes.toString("base64")}`;
    stylesheet = stylesheet.replaceAll(match[0], `url("${dataUrl}")`);
  } catch {
    // Leave unresolved references alone; the build still points at build/assets.
  }
}
html = html.replace(stylesheetMatch[0], () => `<style>${stylesheet}</style>`);

const scriptMatch = html.match(/<script type="module"[^>]*src="([^"]+)"[^>]*><\/script>/);
if (!scriptMatch) throw new Error("Built module script reference was not found.");
const scriptPath = resolve(buildDirectory, scriptMatch[1]);
const script = await readFile(scriptPath, "utf8");
html = html.replace(scriptMatch[0], "");
html = html.replace("</body>", () => `<script>${script}</script>\n    </body>`);

await writeFile(htmlPath, html, "utf8");
// 双击即看的单文件原型：默认（一修）写到既有文件名；小一修构建写到带后缀的文件，
// 避免覆盖一修的既定原型。小一修的产物由 .gitignore 忽略。
const productName = (process.env.VITE_PRODUCT_NAME || "").trim();
const prototypeName = productName === "小一修"
  ? "检修窗口原型-小一修.html"
  : "检修窗口原型.html";
await writeFile(resolve(projectRoot, prototypeName), html, "utf8");
console.log(`Created standalone build/index.html and ${prototypeName}.`);
