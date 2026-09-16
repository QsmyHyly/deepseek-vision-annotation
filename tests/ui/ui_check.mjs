// UI 自检：用真实浏览器驱动页面，验证"静态检查覆盖不到"的交互与几何。
// 为什么需要它：node --check 只能证明语法对，id 校验只能证明元素存在，
// 而"滑块是否与图片实际显示区域严格对齐""折叠是否真的让出宽度"只有跑起来才知道。
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
const CANDIDATES = [
  "E:/Qsmy-Claude-Code/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright",
  "E:/Qsmy-Claude-Code/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright",
];
let playwright = null;
for (const c of CANDIDATES) {
  try { playwright = require(c); break; } catch (e) { /* 试下一个 */ }
}
if (!playwright) { console.error("找不到 playwright 模块"); process.exit(2); }

const BASE = "http://127.0.0.1:8765";
const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log((ok ? "PASS " : "FAIL ") + name + (detail ? "  « " + detail + " »" : ""));
}
const near = (a, b, tol = 1.2) => Math.abs(a - b) <= tol;

// 本机 ms-playwright 缓存里的 headless shell 版本与 npx 缓存里的 playwright 期望版本不一致，
// 直接指定已下载的可执行文件，避免为了跑自检再去下载浏览器。
const SHELLS = [
  "C:/Users/qsmy/AppData/Local/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe",
  "C:/Users/qsmy/AppData/Local/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-win64/chrome-headless-shell.exe",
  "C:/Users/qsmy/AppData/Local/ms-playwright/chromium_headless_shell-1200/chrome-headless-shell-win64/chrome-headless-shell.exe",
];
const shell = SHELLS.find((p) => fs.existsSync(p));
const browser = await playwright.chromium.launch(shell ? { headless: true, executablePath: shell } : { headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const consoleErrors = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));

await page.goto(BASE, { waitUntil: "networkidle" });
await page.waitForSelector("#sampleGroups button", { timeout: 15000 });
const sampleCount = await page.locator("#sampleGroups button").count();
check("内置测试图目录渲染 17 张", sampleCount === 17, "实际 " + sampleCount);

// ---- 1. 载入内置测试图 ----
await page.locator('#sampleGroups button[data-sample-id="bench_01"]').click();
await page.waitForFunction(() => {
  const im = document.getElementById("sideBefore");
  return im && im.complete && im.naturalWidth > 0;
}, null, { timeout: 20000 });
const info = await page.textContent("#imageInfo");
check("载入测试图后图片信息正确", /基准场景 A/.test(info) && /900×720/.test(info), info);
check("提示词被自动填入", /识别图中的几何图形/.test(await page.inputValue("#prompt")));
check("填入真值按钮可用（带真值图）", !(await page.isDisabled("#btnFillGt")));

// ---- 2. 滑块模式在无标注图时应回退并给出说明 ----
await page.click("#tabModeSlider");
const metaFallback = await page.textContent("#viewerMeta");
check("无标注图时滑块模式回退到并排并提示", /回退/.test(metaFallback), metaFallback);
check("回退时并排可见", await page.isVisible("#modeSide"));

// ---- 3. 手动标注（离线）产出标注图 ----
await page.click("#btnManualApply");
await page.waitForFunction(() => {
  // 标注图会同时写进并排与叠加两套元素，当前模式决定哪一个可见
  return ["stackAfter", "sideAfter"].some((id) => {
    const im = document.getElementById(id);
    return im && !im.hidden && im.complete && im.naturalWidth > 0;
  });
}, null, { timeout: 20000 });
const annoVisible = await page.evaluate(() => ["sideAfter", "stackAfter"].some((id) => {
  const im = document.getElementById(id);
  return im && !im.hidden && im.complete && im.naturalWidth > 0 && im.getBoundingClientRect().width > 0;
}));
check("手动标注后标注图在对比区可见", annoVisible);
check("指标条给出结果摘要", /检出率/.test(await page.textContent("#metricsBox")));
const gtMetric = await page.textContent("#metricsBox");
check("带真值图的手动标注自动打分", /检出率/.test(gtMetric) && /平均 IoU/.test(gtMetric), gtMetric.replace(/\s+/g, " ").slice(0, 120));

// ---- 4. 滑块几何：覆盖层必须与图片显示矩形严格重合 ----
await page.click("#tabModeSlider");
await page.waitForTimeout(300);
const geom = await page.evaluate(() => {
  const b = document.getElementById("stackBefore").getBoundingClientRect();
  const o = document.getElementById("stackOverlay").getBoundingClientRect();
  const img = document.getElementById("stackBefore");
  const style = getComputedStyle(document.getElementById("stackAfter"));
  return {
    b: { x: b.x, y: b.y, w: b.width, h: b.height },
    o: { x: o.x, y: o.y, w: o.width, h: o.height },
    natural: [img.naturalWidth, img.naturalHeight],
    clip: style.clipPath,
  };
});
const aligned = near(geom.b.x, geom.o.x) && near(geom.b.y, geom.o.y) && near(geom.b.w, geom.o.w) && near(geom.b.h, geom.o.h);
check("滑块覆盖层与图片显示矩形重合（±1.2px）", aligned,
  "img=" + JSON.stringify(geom.b) + " overlay=" + JSON.stringify(geom.o));
const aspectOk = near(geom.b.w / geom.b.h, geom.natural[0] / geom.natural[1], 0.01);
check("图片按原始宽高比 contain 显示（无拉伸）", aspectOk,
  "显示比=" + (geom.b.w / geom.b.h).toFixed(4) + " 原图比=" + (geom.natural[0] / geom.natural[1]).toFixed(4));

// ---- 5. 拖动分隔线：位置换算必须是"图片的百分比" ----
const targetX = geom.b.x + geom.b.w * 0.25;
await page.mouse.move(geom.b.x + geom.b.w * 0.5, geom.b.y + geom.b.h * 0.5);
await page.mouse.down();
await page.mouse.move(targetX, geom.b.y + geom.b.h * 0.5, { steps: 6 });
await page.mouse.up();
const after = await page.evaluate(() => ({
  now: document.getElementById("sliderHandle").getAttribute("aria-valuenow"),
  line: document.getElementById("sliderLine").style.left,
  clip: document.getElementById("stackAfter").style.clipPath,
}));
check("拖动到 25% 时分隔线落在预期位置", Math.abs(Number(after.now) - 25) <= 1.5,
  "aria-valuenow=" + after.now + " line=" + after.line + " clip=" + after.clip);

// 三种模式的左右顺序必须一致：先切掉左侧 pct%，左边露出的是底层原图、右边是标注图。
// 若哪天有人把切分方向改回去（从右侧切），这条会立刻红——它锁的是"和并排模式同向"这个约定。
const clipLeft = Number((after.clip.match(/inset\([^)]*?([\d.]+)%\)/) || [])[1]);
check("滑块左侧是原图、右侧是标注图（与并排模式同向）", Math.abs(clipLeft - 25) <= 1.5,
  "clip=" + after.clip + " → 标注层左侧被切掉 " + clipLeft + "%，左边露出原图");

// ---- 6. 键盘可达：方向键微调分隔线 ----
await page.focus("#sliderHandle");
const beforeKey = Number(await page.getAttribute("#sliderHandle", "aria-valuenow"));
await page.keyboard.press("ArrowRight");
await page.keyboard.press("ArrowRight");
const afterKey = Number(await page.getAttribute("#sliderHandle", "aria-valuenow"));
check("分隔线可用左右方向键调整（键盘可达）", afterKey > beforeKey, beforeKey + " -> " + afterKey);

// ---- 7. 叠加模式 ----
await page.click("#tabModeOverlay");
check("叠加模式显示不透明度控件", await page.isVisible("#opacityCtl"));
await page.evaluate(() => {
  const el = document.getElementById("opacity");
  el.value = "40";
  el.dispatchEvent(new Event("input", { bubbles: true }));
});
const opacity = await page.evaluate(() => ({
  img: getComputedStyle(document.getElementById("stackAfter")).opacity,
  label: document.getElementById("opacityVal").textContent,
  lineHidden: getComputedStyle(document.getElementById("sliderLine")).display === "none",
}));
check("叠加模式应用不透明度且隐藏分隔线", opacity.img === "0.4" && opacity.lineHidden && opacity.label === "40%",
  JSON.stringify(opacity));

// ---- 8. 控制台折叠：应把宽度让给对比区 ----
const wideBefore = await page.evaluate(() => document.getElementById("cmpStage").getBoundingClientRect().width);
await page.click("#btnToggleConsole");
await page.waitForTimeout(300);
const collapsed = await page.evaluate(() => ({
  cls: document.getElementById("layout").classList.contains("console-collapsed"),
  bodyHidden: !document.getElementById("consoleBody").offsetParent,
  rail: getComputedStyle(document.getElementById("consoleRail")).display,
  stage: document.getElementById("cmpStage").getBoundingClientRect().width,
  overlay: document.getElementById("stackOverlay").getBoundingClientRect(),
  img: document.getElementById("stackBefore").getBoundingClientRect(),
}));
check("折叠后控制台正文隐藏、竖条出现", collapsed.cls && collapsed.bodyHidden && collapsed.rail === "flex",
  JSON.stringify({ cls: collapsed.cls, bodyHidden: collapsed.bodyHidden, rail: collapsed.rail }));
check("折叠后对比区变宽", collapsed.stage > wideBefore, wideBefore.toFixed(0) + " -> " + collapsed.stage.toFixed(0));
check("折叠后覆盖层仍与图片重合", near(collapsed.overlay.x, collapsed.img.x) && near(collapsed.overlay.width, collapsed.img.width),
  "img.x=" + collapsed.img.x.toFixed(1) + " overlay.x=" + collapsed.overlay.x.toFixed(1));
await page.click("#btnToggleConsole");

// ---- 9. 流式日志三段可折叠 ----
await page.click("#btnToggleReasoning");
check("思考过程可折叠", !(await page.isVisible("#logReasoning")));
await page.click("#btnToggleReasoning");
check("思考过程可再展开", await page.isVisible("#logReasoning"));

// ---- 10. 错误提示必须出现在顶部告警条（不是只写日志） ----
// 先把已有告警清空：否则"告警条可见"可能是上一条留下的，测不出本次失败
while (await page.locator(".alert-close").count()) {
  await page.locator(".alert-close").first().click();
}
await page.waitForFunction(() => document.getElementById("alertBar").hidden);
await page.click("#tabUrl");
await page.fill("#urlInput", "https://example.invalid/nope.png");
await page.click("#btnLoadUrl");
await page.waitForFunction(() => !document.getElementById("alertBar").hidden, null, { timeout: 60000 });
const alertText = await page.textContent("#alertBar");
check("载入失败时顶部告警条可见并说明原因", /失败/.test(alertText), alertText.replace(/\s+/g, " ").slice(0, 110));
await page.click(".alert-close");
check("告警可关闭", await page.isHidden("#alertBar"));

// ---- 11. 窄屏退化 ----
await page.setViewportSize({ width: 900, height: 820 });
await page.waitForTimeout(300);
const narrow = await page.evaluate(() => {
  const cols = getComputedStyle(document.getElementById("layout")).gridTemplateColumns.split(" ").length;
  const cmp = document.getElementById("cmpStage").getBoundingClientRect();
  return { cols, cmpW: cmp.width, cmpH: cmp.height, bodyW: document.body.clientWidth };
});
check("窄屏退化为单列布局", narrow.cols === 1, JSON.stringify(narrow));
check("窄屏对比区仍占满宽度且有高度", narrow.cmpW > narrow.bodyW * 0.8 && narrow.cmpH > 240, JSON.stringify(narrow));

// 截图存档（宽屏 + 窄屏各一张，窄屏截图前先切回叠加模式看效果）
await page.setViewportSize({ width: 1440, height: 900 });
await page.click("#tabModeSlider");
await page.waitForTimeout(400);
fs.mkdirSync("runs/ui", { recursive: true });
await page.screenshot({ path: "runs/ui/wide-slider.png" });
await page.click("#btnToggleConsole");
await page.waitForTimeout(300);
await page.screenshot({ path: "runs/ui/wide-collapsed.png" });
await page.click("#btnToggleConsole");
await page.setViewportSize({ width: 900, height: 900 });
await page.waitForTimeout(400);
await page.screenshot({ path: "runs/ui/narrow.png", fullPage: false });

// 上面第 10 步是故意请求一个不可达 URL 来验证告警条，浏览器必然记录一条
// "Failed to load resource: 400"，那是被测行为本身，不算页面缺陷。
const unexpected = consoleErrors.filter((t) => !/Failed to load resource/.test(t));
check("无 JS 运行时错误（console/pageerror）", unexpected.length === 0, unexpected.slice(0, 3).join(" | "));

await browser.close();
const failed = results.filter((r) => !r.ok);
console.log("\n" + (results.length - failed.length) + "/" + results.length + " 项通过");
console.log("截图: runs/ui/wide-slider.png, runs/ui/wide-collapsed.png, runs/ui/narrow.png");
process.exit(failed.length ? 1 : 0);
