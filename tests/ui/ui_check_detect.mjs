// 浏览器端真实 SSE 自检：验证"页面自己发请求、自己解析事件流、自己渲染"这条链路。
// 与同目录 smoke_detect.py 的分工：那个只看 HTTP 层有没有 annotated+accuracy，
// 这个看页面有没有把它正确落到界面（标注图、指标、按钮状态、取消、重新识别）。
import { createRequire } from "node:module";
import fs from "node:fs";

const require = createRequire(import.meta.url);
const playwright = require("E:/Qsmy-Claude-Code/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright");
const SHELL = "C:/Users/qsmy/AppData/Local/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe";
const BASE = "http://127.0.0.1:8765";
const results = [];
function check(name, ok, detail) {
  results.push(ok);
  console.log((ok ? "PASS " : "FAIL ") + name + (detail ? "  « " + detail + " »" : ""));
}

const browser = await playwright.chromium.launch({ headless: true, executablePath: SHELL });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

await page.goto(BASE, { waitUntil: "networkidle" });
await page.waitForSelector("#sampleGroups button");
await page.locator('#sampleGroups button[data-sample-id="bench_01"]').click();
await page.waitForFunction(() => {
  const im = document.getElementById("sideBefore");
  return im && im.complete && im.naturalWidth > 0;
});

// ---- 关闭思考模式后发起识别（更快，也顺带验证开关真的传到了后端）----
await page.click("#thinking");
check("关闭思考模式后强度下拉变灰", await page.isDisabled("#reasoningEffort"));
await page.click("#btnDetect");
await page.waitForFunction(() => !document.getElementById("btnCancel").disabled, null, { timeout: 15000 });
check("识别中：主按钮禁用、取消按钮可用",
  (await page.isDisabled("#btnDetect")) && !(await page.isDisabled("#btnCancel")));

await page.waitForFunction(() => {
  return ["stackAfter", "sideAfter"].some((id) => {
    const im = document.getElementById(id);
    return im && !im.hidden && im.complete && im.naturalWidth > 0 && /api\/result/.test(im.getAttribute("src") || "");
  });
}, null, { timeout: 120000 });

const metrics = (await page.textContent("#metricsBox")).replace(/\s+/g, " ");
check("SSE 识别完成后自动给出准确率指标", /检出率/.test(metrics) && /平均 IoU/.test(metrics), metrics.slice(0, 110));
const space = await page.textContent("#coordSpace");
check("坐标空间诊断已渲染", /coord_space/.test(space), space.replace(/\s+/g, " ").slice(0, 90));
// 事件顺序是 done 在前、annotated 在后（后端要等流结束才画图），
// 所以终态那一行是"已生成标注图…"；两种都算合格，只要不是空的占位文案
await page.waitForFunction(() => /已生成标注图|识别完成/.test(document.getElementById("consoleLatest").textContent), null, { timeout: 30000 })
  .catch(() => {});
const latest = await page.textContent("#consoleLatest");
check("控制台常驻显示最新关键信息", /已生成标注图/.test(latest) && /轮/.test(latest), latest);
const reasoning = await page.textContent("#logReasoning");
check("关闭思考时明确说明不会输出思维链", /已关闭思考模式/.test(reasoning), reasoning.slice(0, 40));
const toolItems = await page.locator("#logTools .tool-item").count();
const contentLen = (await page.textContent("#logContent")).length;
check("正文流式渲染出内容", contentLen > 20, contentLen + " 字");
console.log("    （本轮工具调用条目数：" + toolItems + "，取决于模型是否请求工具）");

await page.waitForFunction(() => document.getElementById("btnDetect").textContent.indexOf("重新识别") >= 0, null, { timeout: 15000 });
check("识别结束后主按钮变为「重新识别」", !(await page.isDisabled("#btnDetect")) && await page.isDisabled("#btnCancel"));
fs.mkdirSync("runs/ui", { recursive: true });
await page.screenshot({ path: "runs/ui/detect-done.png" });

// ---- 取消：开启思考后立即取消，界面必须明确反馈且不留半截结果 ----
await page.click("#thinking");
await page.click("#btnDetect");
await page.waitForTimeout(1200);
await page.click("#btnCancel");
await page.waitForTimeout(500);
const statusText = await page.textContent("#statusLine");
check("取消后有明确状态反馈", /已取消/.test(statusText), statusText);
const alertText = await page.textContent("#alertBar");
check("取消动作进入告警条", /已取消/.test(alertText), alertText.replace(/\s+/g, " ").slice(0, 70));
check("取消后按钮恢复可用", !(await page.isDisabled("#btnDetect")) && await page.isDisabled("#btnCancel"));

// 取消后紧接着重新识别：过期事件绝不能覆盖新一轮的结果（token 机制）
await page.click("#btnDetect");
await page.waitForFunction(() => !document.getElementById("btnCancel").disabled);
await page.waitForFunction(() => document.getElementById("btnCancel").disabled === true, null, { timeout: 120000 });
const metrics2 = (await page.textContent("#metricsBox")).replace(/\s+/g, " ");
check("取消后重新识别仍然产出结果（新一轮未被旧事件污染）", /检出率/.test(metrics2), metrics2.slice(0, 90));

check("全程无 JS 运行时错误", errors.length === 0, errors.slice(0, 2).join(" | "));
await browser.close();
const failed = results.filter((r) => !r).length;
console.log("\n" + (results.length - failed) + "/" + results.length + " 项通过");
console.log("截图: runs/ui/detect-done.png");
process.exit(failed ? 1 : 0);
