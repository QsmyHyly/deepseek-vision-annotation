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

// ---- 12. 缩放 / 平移 / 全屏：用户诉求是"图片显示得太小"，这一组锁住新增的查看能力 ----
// 关键风险是"覆盖层与图片在缩放/平移后还必须严格重合"，所以每一档缩放都单独验一次。
await page.setViewportSize({ width: 1440, height: 900 });
await page.click("#tabModeSlider");
await page.waitForTimeout(300);

const zoomState = () => page.evaluate(() => {
  const im = document.getElementById("stackBefore");
  const b = im.getBoundingClientRect();
  const o = document.getElementById("stackOverlay").getBoundingClientRect();
  const s = document.getElementById("cmpStage").getBoundingClientRect();
  return {
    b: { x: b.x, y: b.y, w: b.width, h: b.height },
    o: { x: o.x, y: o.y, w: o.width, h: o.height },
    stage: { x: s.x, y: s.y, w: s.width, h: s.height },
    label: document.getElementById("zoomLabel").textContent,
    transform: document.getElementById("zoomLayer").style.transform,
    natural: [im.naturalWidth, im.naturalHeight],
  };
});
// 覆盖层与图片的屏幕 rect 是否严格重合（同一条 1.2px 口径，见上面的 near()）
const coincide = (s) => near(s.b.x, s.o.x) && near(s.b.y, s.o.y) && near(s.b.w, s.o.w) && near(s.b.h, s.o.h);
const deviate = (s) => "Δ=(" + Math.abs(s.o.x - s.b.x).toFixed(2) + "," + Math.abs(s.o.y - s.b.y).toFixed(2) + "," +
  Math.abs(s.o.w - s.b.w).toFixed(2) + "," + Math.abs(s.o.h - s.b.h).toFixed(2) + ")";

const fitGeom = await zoomState();
check("有图时缩放工具条出现（初始为适应窗口）", await page.isVisible("#zoomBar"), "比例标签=" + fitGeom.label + " transform=" + JSON.stringify(fitGeom.transform));

// 滚轮缩放：以指针为锚点（指针下的图像点必须不动）
const anchor = { x: fitGeom.b.x + fitGeom.b.w * 0.28, y: fitGeom.b.y + fitGeom.b.h * 0.30 };
const fracBefore = (anchor.x - fitGeom.b.x) / fitGeom.b.w;
await page.mouse.move(anchor.x, anchor.y);
await page.mouse.wheel(0, -240);
await page.waitForTimeout(220);
const zoom1 = await zoomState();
check("滚轮缩放：图片显示尺寸变大、百分比标签同步更新",
  zoom1.b.w > fitGeom.b.w + 1 && zoom1.b.h > fitGeom.b.h + 1 && zoom1.label !== fitGeom.label,
  fitGeom.b.w.toFixed(0) + "×" + fitGeom.b.h.toFixed(0) + " " + fitGeom.label + " -> " + zoom1.b.w.toFixed(0) + "×" + zoom1.b.h.toFixed(0) + " " + zoom1.label);
const fracAfter = (anchor.x - zoom1.b.x) / zoom1.b.w;
check("缩放以鼠标指针为锚点（指针下的图像点保持不动）", Math.abs(fracAfter - fracBefore) < 0.02,
  "锚点处图像横向相对位置 " + fracBefore.toFixed(4) + " -> " + fracAfter.toFixed(4));
check("缩放后覆盖层仍与图片严格重合（±1.2px）", coincide(zoom1),
  "img=" + zoom1.b.w.toFixed(1) + "×" + zoom1.b.h.toFixed(1) + " @" + zoom1.b.x.toFixed(1) + "," + zoom1.b.y.toFixed(1) + " " + deviate(zoom1));

// 再放大两档，逐档验证重合
const zoomLevels = [];
for (let i = 0; i < 2; i++) {
  await page.mouse.wheel(0, -260);
  await page.waitForTimeout(200);
  zoomLevels.push(await zoomState());
}
zoomLevels.forEach((s, i) => {
  check("第 " + (i + 2) + " 档缩放（" + s.label + "）覆盖层仍与图片重合", coincide(s),
    "img=" + s.b.w.toFixed(1) + "×" + s.b.h.toFixed(1) + " " + deviate(s));
});

// 拖动平移
const beforePan = await zoomState();
await page.mouse.move(anchor.x, anchor.y);
await page.mouse.down();
await page.mouse.move(anchor.x + 70, anchor.y + 45, { steps: 8 });
await page.mouse.up();
await page.waitForTimeout(250);
const afterPan = await zoomState();
check("按住左键拖动可平移图片", near(afterPan.b.x - beforePan.b.x, 70, 2) && near(afterPan.b.y - beforePan.b.y, 45, 2),
  "位移=(" + (afterPan.b.x - beforePan.b.x).toFixed(1) + "," + (afterPan.b.y - beforePan.b.y).toFixed(1) + ") 期望=(70,45)");
check("平移后图片与覆盖层同步位移（两者 rect 之差不变）",
  near(afterPan.o.x - afterPan.b.x, beforePan.o.x - beforePan.b.x) &&
  near(afterPan.o.y - afterPan.b.y, beforePan.o.y - beforePan.b.y) &&
  near(afterPan.o.w - afterPan.b.w, beforePan.o.w - beforePan.b.w) && coincide(afterPan),
  "位移前 o-b=" + (beforePan.o.x - beforePan.b.x).toFixed(2) + " 位移后 o-b=" + (afterPan.o.x - afterPan.b.x).toFixed(2) + " " + deviate(afterPan));

// 适应窗口 / 1:1 / 双击复位
await page.click("#btnZoomFit");
await page.waitForTimeout(220);
const fitBack = await zoomState();
check("「适应窗口」按钮复位缩放与平移", fitBack.transform === "" && near(fitBack.b.w, fitGeom.b.w, 0.6) && fitBack.label === fitGeom.label,
  "label=" + fitBack.label + " transform=" + JSON.stringify(fitBack.transform) + " w=" + fitBack.b.w.toFixed(1));

await page.click("#btnZoom1x");
await page.waitForTimeout(220);
const oneToOne = await zoomState();
check("「1:1」按钮按原始像素显示（标签 100%）",
  oneToOne.label === "100%" && near(oneToOne.b.w, oneToOne.natural[0], 1.5) && near(oneToOne.b.h, oneToOne.natural[1], 1.5),
  "label=" + oneToOne.label + " 显示=" + oneToOne.b.w.toFixed(1) + "×" + oneToOne.b.h.toFixed(1) + " 原始=" + oneToOne.natural.join("×"));
check("1:1 时覆盖层仍与图片重合", coincide(oneToOne), deviate(oneToOne));

await page.mouse.dblclick(anchor.x, anchor.y);
await page.waitForTimeout(250);
const dblFit = await zoomState();
check("双击从 1:1 切回适应窗口", near(dblFit.b.w, fitGeom.b.w, 0.6) && dblFit.label === fitGeom.label,
  "label=" + dblFit.label + " w=" + dblFit.b.w.toFixed(1) + "（适应=" + fitGeom.b.w.toFixed(1) + "）");
await page.mouse.dblclick(anchor.x, anchor.y);
await page.waitForTimeout(250);
check("再双击切到 1:1", (await zoomState()).label === "100%", "label=" + (await zoomState()).label);
await page.click("#btnZoomFit");
await page.waitForTimeout(200);

// ⚠️「适应」与「1:1」是"救援按钮"：任何缩放/平移历史下都必须能把画面恢复成可用状态。
// 先把图拖到视口外（每个方向拖三次），再分别点这两个按钮，看画面能不能回来。
const inViewArea = (s) => {
  const w = Math.max(0, Math.min(s.b.x + s.b.w, s.stage.x + s.stage.w) - Math.max(s.b.x, s.stage.x));
  const h = Math.max(0, Math.min(s.b.y + s.b.h, s.stage.y + s.stage.h) - Math.max(s.b.y, s.stage.y));
  return w * h;
};
const dragAway = async () => {
  for (let i = 0; i < 3; i++) {
    await page.mouse.move(anchor.x, anchor.y);
    await page.mouse.down();
    await page.mouse.move(anchor.x + 1500, anchor.y + 1000, { steps: 4 });
    await page.mouse.up();
    await page.waitForTimeout(120);
  }
};
await page.mouse.move(anchor.x, anchor.y);
await page.mouse.wheel(0, -300);
await page.waitForTimeout(200);
await dragAway();
const draggedAway = await zoomState();
await page.click("#btnZoomFit");
await page.waitForTimeout(250);
const refitted = await zoomState();
check("拖到视口外后「适应」仍能把整图恢复回来",
  inViewArea(refitted) > refitted.b.w * refitted.b.h * 0.98 && coincide(refitted),
  "拖走后可见=" + Math.round(inViewArea(draggedAway)) + "px² → 适应后 " + Math.round(inViewArea(refitted)) +
  "px²（整图 " + Math.round(refitted.b.w * refitted.b.h) + "px²）");
await page.mouse.move(anchor.x, anchor.y);
await page.mouse.wheel(0, -300);
await page.waitForTimeout(200);
await dragAway();
await page.click("#btnZoom1x");
await page.waitForTimeout(250);
const oneToOneAfter = await zoomState();
check("拖到视口外后「1:1」会把画面摆回视口正中",
  oneToOneAfter.label === "100%" &&
  near(oneToOneAfter.b.x + oneToOneAfter.b.w / 2, oneToOneAfter.stage.x + oneToOneAfter.stage.w / 2, 1.5) &&
  near(oneToOneAfter.b.y + oneToOneAfter.b.h / 2, oneToOneAfter.stage.y + oneToOneAfter.stage.h / 2, 1.5) &&
  coincide(oneToOneAfter),
  "label=" + oneToOneAfter.label + " 中心偏移=(" +
  (oneToOneAfter.b.x + oneToOneAfter.b.w / 2 - oneToOneAfter.stage.x - oneToOneAfter.stage.w / 2).toFixed(1) + "," +
  (oneToOneAfter.b.y + oneToOneAfter.b.h / 2 - oneToOneAfter.stage.y - oneToOneAfter.stage.h / 2).toFixed(1) + ") " + deviate(oneToOneAfter));
await page.click("#btnZoomFit");
await page.waitForTimeout(200);

// 手柄与平移必须分工：拖手柄只改分割位置，不能把图片一起拖走
const handleDrag = await page.evaluate(() => {
  const r = document.getElementById("sliderHandle").getBoundingClientRect();
  return { x: r.x + r.width / 2, y: r.y + r.height / 2, now: Number(document.getElementById("sliderHandle").getAttribute("aria-valuenow")) };
});
const beforeHandleDrag = await zoomState();
await page.mouse.move(handleDrag.x, handleDrag.y);
await page.mouse.down();
await page.mouse.move(handleDrag.x - 60, handleDrag.y, { steps: 5 });
await page.mouse.up();
await page.waitForTimeout(200);
const afterHandleDrag = await zoomState();
const handleNow = Number(await page.getAttribute("#sliderHandle", "aria-valuenow"));
check("拖分隔线手柄只改分割位置、不会平移图片",
  near(afterHandleDrag.b.x, beforeHandleDrag.b.x) && near(afterHandleDrag.b.y, beforeHandleDrag.b.y) &&
  Math.abs(handleNow - handleDrag.now) > 3,
  "图片位移=" + (afterHandleDrag.b.x - beforeHandleDrag.b.x).toFixed(1) + "px 分割 " + handleDrag.now + "% -> " + handleNow + "%");

// 按钮无障碍：Tab 可达 + aria-label
const zoomA11y = await page.evaluate(() => ["btnZoomOut", "btnZoomIn", "btnZoomFit", "btnZoom1x", "btnZoomFull"].map((id) => {
  const el = document.getElementById(id);
  return { id: id, label: el && el.getAttribute("aria-label"), focusable: !!el && el.tabIndex >= 0 };
}));
check("缩放按钮都有 aria-label 且可 Tab 聚焦", zoomA11y.every((x) => x.label && x.focusable), JSON.stringify(zoomA11y));

// 缩放范围钳制在 10%~800%
await page.mouse.move(anchor.x, anchor.y);
for (let i = 0; i < 20; i++) await page.mouse.wheel(0, -400);
await page.waitForTimeout(250);
const zoomMax = await zoomState();
for (let i = 0; i < 45; i++) await page.mouse.wheel(0, 400);
await page.waitForTimeout(250);
const zoomMin = await zoomState();
check("缩放范围被钳制在 10%~800%（两端仍精确重合）",
  Number(zoomMax.label.replace("%", "")) <= 800 && Number(zoomMin.label.replace("%", "")) >= 10 && coincide(zoomMax) && coincide(zoomMin),
  "上限=" + zoomMax.label + " " + deviate(zoomMax) + " 下限=" + zoomMin.label + " " + deviate(zoomMin));

// 并排模式：缩放作用于**每一格自己的视口**（.pane-zoom），两格同倍率同位置对照
await page.click("#btnZoomFit");
await page.click("#tabModeSide");
await page.waitForTimeout(300);
const paneRect = () => page.evaluate(() => {
  const a = document.getElementById("sideBefore").getBoundingClientRect();
  const b = document.getElementById("sideAfter").getBoundingClientRect();
  return { a: [a.width, a.height], b: [b.width, b.height] };
});
const sideBeforeZoom = await paneRect();
const stageCenter = await page.evaluate(() => {
  const r = document.getElementById("cmpStage").getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
});
await page.mouse.move(stageCenter.x, stageCenter.y);
await page.mouse.wheel(0, -300);
await page.waitForTimeout(250);
const sideAfterZoom = await paneRect();
const kLeft = sideAfterZoom.a[0] / sideBeforeZoom.a[0], kRight = sideAfterZoom.b[0] / sideBeforeZoom.b[0];
check("并排模式下两格同步同倍率放大", kLeft > 1.05 && Math.abs(kLeft - kRight) < 0.02,
  "左格×" + kLeft.toFixed(3) + " 右格×" + kRight.toFixed(3));

// ⚠️ 上面那条只比了"倍率比值"，量不出"两格看到的是不是同一块图"——pane 与 img 的 rect 本来就同比，
// 那是恒真断言（曾经因此漏掉一个真 bug：两格被一起推出对比区，一格露左半、一格露右半）。
// 这里改成量**看得见的窗口**换算成原图归一化坐标后的区间。注意 getBoundingClientRect() 不受
// overflow 裁剪影响，所以必须三重相交：图片 ∩ 窗格(.pane 的 overflow:hidden) ∩ 对比区(#cmpStage)。
const sideWindow = () => page.evaluate(() => {
  const sr = document.getElementById("cmpStage").getBoundingClientRect();
  return [...document.querySelectorAll("#modeSide .pane")].map((pane) => {
    const img = pane.querySelector("img");
    const pr = pane.getBoundingClientRect(), ir = img.getBoundingClientRect();
    const l = Math.max(pr.left, sr.left), r = Math.min(pr.right, sr.right);
    const t = Math.max(pr.top, sr.top), b = Math.min(pr.bottom, sr.bottom);
    const vl = Math.max(ir.left, l), vr = Math.min(ir.right, r);
    const vt = Math.max(ir.top, t), vb = Math.min(ir.bottom, b);
    return {
      paneW: pr.width, winW: Math.max(0, r - l), visW: Math.max(0, vr - vl),
      nx: [(vl - ir.left) / ir.width, (vr - ir.left) / ir.width],
      ny: [(vt - ir.top) / ir.height, (vb - ir.top) / ir.height],
    };
  });
});
// 再放大两档（共 3 档，与复现脚本 runs/probe_side_zoom.mjs 的条件一致）
for (let i = 0; i < 2; i++) { await page.mouse.wheel(0, -120); await page.waitForTimeout(200); }
const sw = await sideWindow();
const [s0, s1] = sw;
const fmtRange = (s) => "x=[" + s.nx[0].toFixed(4) + "," + s.nx[1].toFixed(4) + "] y=[" + s.ny[0].toFixed(4) + "," + s.ny[1].toFixed(4) + "]";
check("并排两格看到的是同一块归一化区域（逐项 < 0.01）",
  Math.abs(s0.nx[0] - s1.nx[0]) < 0.01 && Math.abs(s0.nx[1] - s1.nx[1]) < 0.01 &&
  Math.abs(s0.ny[0] - s1.ny[0]) < 0.01 && Math.abs(s0.ny[1] - s1.ny[1]) < 0.01,
  "左格 " + fmtRange(s0) + " 右格 " + fmtRange(s1));
// 平移同样必须是两格共享的一个量：在**右格**里拖，两格仍要看到同一块归一化区域
await page.mouse.move(stageCenter.x + 150, stageCenter.y);
await page.mouse.down();
await page.mouse.move(stageCenter.x + 90, stageCenter.y - 40, { steps: 3 });
await page.mouse.up();
await page.waitForTimeout(200);
const swPanned = await sideWindow();
check("并排在右格内拖动平移后，两格仍看到同一块归一化区域",
  Math.abs(swPanned[0].nx[0] - swPanned[1].nx[0]) < 0.01 && Math.abs(swPanned[0].nx[1] - swPanned[1].nx[1]) < 0.01 &&
  Math.abs(swPanned[0].ny[0] - swPanned[1].ny[0]) < 0.01 && Math.abs(swPanned[0].ny[1] - swPanned[1].ny[1]) < 0.01,
  "左格 " + fmtRange(swPanned[0]) + " 右格 " + fmtRange(swPanned[1]));
check("并排两格都没被推出对比区、且可见窗口宽度相等",
  near(s0.winW, s0.paneW, 0.6) && near(s1.winW, s1.paneW, 0.6) &&
  near(s0.visW, s1.visW, 0.6) && s0.visW > 0,
  "左格窗格内 " + s0.winW.toFixed(1) + "/" + s0.paneW.toFixed(1) + " 可见 " + s0.visW.toFixed(1) +
  "；右格窗格内 " + s1.winW.toFixed(1) + "/" + s1.paneW.toFixed(1) + " 可见 " + s1.visW.toFixed(1));
await page.screenshot({ path: "runs/ui/zoomed.png" });
await page.click("#tabModeSlider");
await page.click("#btnZoomFit");
await page.waitForTimeout(250);

// 左栏折叠：把宽度让给对比区（与右栏控制台折叠同一诉求）
const stageWide = (await zoomState()).stage.w;
await page.click("#btnToggleLeft");
await page.waitForTimeout(350);
const leftCollapsed = await page.evaluate(() => {
  const s = document.getElementById("cmpStage").getBoundingClientRect();
  const b = document.getElementById("stackBefore").getBoundingClientRect();
  const o = document.getElementById("stackOverlay").getBoundingClientRect();
  return {
    cls: document.getElementById("layout").classList.contains("left-collapsed"),
    stackHidden: !document.getElementById("leftStack").offsetParent,
    stage: s.width,
    dx: Math.abs(o.x - b.x), dy: Math.abs(o.y - b.y), dw: Math.abs(o.width - b.width), dh: Math.abs(o.height - b.height),
  };
});
check("折叠左栏后对比区变宽", leftCollapsed.cls && leftCollapsed.stackHidden && leftCollapsed.stage > stageWide + 20,
  stageWide.toFixed(0) + " -> " + leftCollapsed.stage.toFixed(0));
check("折叠左栏后覆盖层仍与图片重合",
  leftCollapsed.dx <= 1.2 && leftCollapsed.dy <= 1.2 && leftCollapsed.dw <= 1.2 && leftCollapsed.dh <= 1.2,
  "Δ=(" + leftCollapsed.dx.toFixed(2) + "," + leftCollapsed.dy.toFixed(2) + "," + leftCollapsed.dw.toFixed(2) + "," + leftCollapsed.dh.toFixed(2) + ")");

// 折叠状态下再缩放一次：宽度变了，覆盖层必须仍然贴住
const collapsedBase = await zoomState();
await page.mouse.move(collapsedBase.b.x + collapsedBase.b.w * 0.4, collapsedBase.b.y + collapsedBase.b.h * 0.4);
await page.mouse.wheel(0, -260);
await page.waitForTimeout(300);
const collapsedZoom = await zoomState();
check("左栏折叠状态下缩放，覆盖层仍与图片重合", coincide(collapsedZoom),
  "img=" + collapsedZoom.b.w.toFixed(1) + "×" + collapsedZoom.b.h.toFixed(1) + " " + deviate(collapsedZoom));

// 窄屏：单列里不存在"左右让宽度"，必须无视折叠状态（否则用户会以为左栏丢了）
await page.setViewportSize({ width: 900, height: 820 });
await page.waitForTimeout(350);
const narrowLeft = await page.evaluate(() => ({
  cols: getComputedStyle(document.getElementById("layout")).gridTemplateColumns.split(" ").length,
  stackVisible: !!document.getElementById("leftStack").offsetParent,
  toggle: getComputedStyle(document.getElementById("btnToggleLeft")).display,
  w: document.getElementById("cmpStage").getBoundingClientRect().width,
  bodyW: document.body.clientWidth,
}));
check("窄屏 900px 仍是单列，且折叠状态被忽略（左栏始终展开）",
  narrowLeft.cols === 1 && narrowLeft.stackVisible && narrowLeft.toggle === "none" && narrowLeft.w > narrowLeft.bodyW * 0.8,
  JSON.stringify(narrowLeft));
await page.setViewportSize({ width: 1440, height: 900 });
await page.waitForTimeout(300);
await page.click("#btnToggleLeft");
await page.waitForTimeout(300);
check("展开左栏后对比区宽度回到原值", near((await zoomState()).stage.w, stageWide, 1.5),
  (await zoomState()).stage.w.toFixed(0) + " vs " + stageWide.toFixed(0));

// 全屏：无头浏览器如果拿不到全屏权限，也只断言"按钮可用 + 点击不报错"，不假装验证通过
const fullBtn = await page.evaluate(() => {
  const el = document.getElementById("btnZoomFull");
  return { exists: !!el, label: el && el.getAttribute("aria-label"), text: el && el.textContent.trim() };
});
const errBeforeFull = consoleErrors.length;
await page.mouse.move(anchor.x, anchor.y);
await page.mouse.wheel(0, -300);            // 顺带验证"进出全屏要保持缩放状态"
await page.waitForTimeout(200);
const beforeFull = await zoomState();
await page.click("#btnZoomFull");
await page.waitForTimeout(500);
const fullInfo = await page.evaluate(() => {
  const s = document.getElementById("cmpStage").getBoundingClientRect();
  const b = document.getElementById("stackBefore").getBoundingClientRect();
  const o = document.getElementById("stackOverlay").getBoundingClientRect();
  return {
    active: !!document.fullscreenElement,
    stage: [s.width, s.height], vw: window.innerWidth, vh: window.innerHeight,
    dx: Math.abs(o.x - b.x), dy: Math.abs(o.y - b.y), dw: Math.abs(o.width - b.width), dh: Math.abs(o.height - b.height),
    label: document.getElementById("zoomLabel").textContent,
    transform: document.getElementById("zoomLayer").style.transform,
  };
});
check("全屏按钮存在、有 aria-label、点击不报错",
  fullBtn.exists && !!fullBtn.label && consoleErrors.length === errBeforeFull,
  "按钮=「" + fullBtn.text + "」aria-label=" + fullBtn.label + " 新增错误=" + (consoleErrors.length - errBeforeFull) +
  " fullscreenElement=" + fullInfo.active);
// 只有浏览器真的进了全屏才断言"铺满 + 重合 + 保持缩放"，否则如实跳过
if (fullInfo.active) {
  check("全屏下对比区真正铺满屏幕", near(fullInfo.stage[0], fullInfo.vw, 2) && near(fullInfo.stage[1], fullInfo.vh, 2),
    fullInfo.stage.map((v) => v.toFixed(0)).join("×") + " vs 视口 " + fullInfo.vw + "×" + fullInfo.vh);
  // 全屏后容器变大：显示比例（标签）与平移必须原样保留，而变换层的 k 会被重算以满足这个比例
  const samePan = (a, b) => a.replace(/scale\([^)]*\)/, "") === b.replace(/scale\([^)]*\)/, "");
  check("全屏下覆盖层仍与图片重合、且显示比例与平移原样保留",
    fullInfo.label === beforeFull.label && samePan(beforeFull.transform, fullInfo.transform) &&
    fullInfo.dx <= 1.2 && fullInfo.dy <= 1.2 && fullInfo.dw <= 1.2 && fullInfo.dh <= 1.2,
    "比例 " + beforeFull.label + " -> " + fullInfo.label + "；transform " + JSON.stringify(beforeFull.transform) +
    " -> " + JSON.stringify(fullInfo.transform) +
    " Δ=(" + fullInfo.dx.toFixed(2) + "," + fullInfo.dy.toFixed(2) + "," + fullInfo.dw.toFixed(2) + "," + fullInfo.dh.toFixed(2) + ")");
}
await page.screenshot({ path: "runs/ui/fullscreen.png" });
// 退出全屏：Esc 由浏览器处理（无头 shell 可能不响应），这里用"再点一次按钮"这条应用自身的路径
if (fullInfo.active) {
  await page.click("#btnZoomFull");
  await page.waitForTimeout(400);
}
if (await page.evaluate(() => !!document.fullscreenElement)) {
  await page.evaluate(() => { const exit = document.exitFullscreen || document.webkitExitFullscreen; if (exit) exit.call(document); });
  await page.waitForTimeout(400);
}
check("再点一次全屏按钮能退出全屏", !(await page.evaluate(() => !!document.fullscreenElement)));
await page.click("#btnZoomFit");
await page.waitForTimeout(200);

// 叠加模式是整图重合叠放：不能沿用滑块留下的裁剪（否则标注图会缺一块）
await page.click("#tabModeSlider");
await page.waitForTimeout(200);
const splitGeom = await zoomState();
const handleNow2 = await page.evaluate(() => {
  const r = document.getElementById("sliderHandle").getBoundingClientRect();
  return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
});
await page.mouse.move(handleNow2.x, handleNow2.y);
await page.mouse.down();
await page.mouse.move(splitGeom.b.x + splitGeom.b.w * 0.25, splitGeom.b.y + splitGeom.b.h * 0.5, { steps: 5 });
await page.mouse.up();
await page.waitForTimeout(200);
const clipInSlider = await page.evaluate(() => document.getElementById("stackAfter").style.clipPath);
await page.click("#tabModeOverlay");
await page.waitForTimeout(250);
const clipInOverlay = await page.evaluate(() => document.getElementById("stackAfter").style.clipPath);
check("切到叠加模式会清掉滑块留下的裁剪", clipInOverlay === "none", "滑块时=" + clipInSlider + " 叠加时=" + clipInOverlay);
await page.click("#tabModeSlider");
await page.waitForTimeout(250);
const clipRestored = await page.evaluate(() => document.getElementById("stackAfter").style.clipPath);
check("切回滑块模式恢复原来的分割位置（25%）", Math.abs(Number((clipRestored.match(/inset\([^)]*?([\d.]+)%\)/) || [])[1]) - 25) <= 1.5, clipRestored);
await page.click("#btnZoomFit");
await page.waitForTimeout(200);
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
console.log("截图: runs/ui/wide-slider.png, runs/ui/wide-collapsed.png, runs/ui/narrow.png, runs/ui/zoomed.png, runs/ui/fullscreen.png");
process.exit(failed.length ? 1 : 0);
