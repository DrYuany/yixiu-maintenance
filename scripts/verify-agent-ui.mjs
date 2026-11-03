import { chromium } from "playwright-core";

const chromePath =
  process.env.CHROME_PATH ||
  "C:/Program Files/Google/Chrome/Application/chrome.exe";

const browser = await chromium.launch({
  executablePath: chromePath,
  headless: true,
});
const urlArgument = process.argv.find((item) => item.startsWith("--url="));
const targetUrl = urlArgument ? urlArgument.slice("--url=".length) : "http://127.0.0.1:3000/";
const page = await browser.newPage({
  viewport: { width: 1600, height: 1000 },
});
const errors = [];
page.on("console", (message) => {
  if (message.type() === "error") errors.push(message.text());
});
page.on("pageerror", (error) => errors.push(String(error)));

await page.goto(targetUrl, { waitUntil: "networkidle" });
await page.waitForSelector(".agent-workspace", { timeout: 20000 });
await page.waitForTimeout(2500);

const beforeMessages = await page.locator(".thread-message").count();
const katexBefore = await page.locator(".katex").count();
await page.screenshot({
  path: ".runlogs/verify-agent-math.png",
  fullPage: false,
});

if (process.argv.includes("--compare-shot")) {
  const compareMessage = page
    .locator(".thread-message.assistant")
    .filter({ hasText: "关键差异" })
    .last();
  if (await compareMessage.count()) {
    await compareMessage.scrollIntoViewIfNeeded();
    await page.waitForTimeout(400);
    await page.screenshot({
      path: ".runlogs/verify-agent-compare.png",
      fullPage: false,
    });
  }
}

let streaming = 0;
if (process.env.SKIP_SEND !== "1" && !process.argv.includes("--skip-send")) {
  await page.fill(".agent-composer textarea", "这版方案哪些任务没排，为什么？");
  await page.click(".send-agent-message");
  await page.waitForTimeout(1500);
  streaming = await page.locator(".message-markdown.is-streaming").count();
  await page.waitForTimeout(12000);
}
const afterMessages = await page.locator(".thread-message").count();
const lastAssistant = await page
  .locator(".thread-message.assistant .message-markdown")
  .last()
  .innerText();
const lastBubble = await page
  .locator(".thread-message.assistant .message-content")
  .last()
  .boundingBox();
const viewport = page.viewportSize();
await page.screenshot({
  path: ".runlogs/verify-agent-answer.png",
  fullPage: false,
});

console.log(JSON.stringify({
  beforeMessages,
  afterMessages,
  katexBefore,
  streamingCaretSeen: streaming > 0,
  lastAnswerChars: lastAssistant.trim().length,
  lastBubbleBottom: lastBubble ? Math.round(lastBubble.y + lastBubble.height) : null,
  viewportHeight: viewport?.height ?? null,
  errors,
}, null, 2));

await browser.close();
