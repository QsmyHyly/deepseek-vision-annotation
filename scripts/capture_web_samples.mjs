#!/usr/bin/env node
/**
 * scripts/capture_web_samples.mjs
 *
 * 作用：现场生成 4 张完全离线的「仿真网页」，用 Playwright 渲染成 PNG 快照，
 *       同时从 DOM 里量出每个目标元素的真实包围盒当作真值，一起写进 runs/web_samples/。
 *       产物被图片识别演示软件当作「内置测试图片」，因此真值必须与像素严格对齐，
 *       并且每次跑出的像素必须完全一致（下游靠像素比对做回归）。
 *
 * 产物：
 *   runs/web_samples/web_dashboard.png / web_shop.png / web_form.png / web_article.png
 *   runs/web_samples/manifest.json
 *
 * 关键约束（改这个脚本前请先读完，踩过的坑都写在这里）：
 *   1) 零外部资源：只允许内联 style / CSS 画的色块，不引 CDN、外链图片、外链字体。
 *      否则断网或 CI 环境下页面会渲染成空白，真值还在、图却没了。
 *   2) 完全确定性：不用 Math.random / new Date / 动画 / transition / 轮播；
 *      图表数值、色块、文案全部硬编码。同一份代码两次跑出的 PNG 必须字节一致。
 *   3) 中文可读：font-family 固定为 "Microsoft YaHei","Segoe UI",sans-serif
 *      （Windows 系统字体，不外链）。标签是给人/模型从图上读的，字体缺失会直接毁掉样本。
 *   4) 截图尺寸 = 1440 × 页面完整高度，且 client 坐标 == 图像坐标（见 captureSample 的收敛逻辑）：
 *      按 900 高渲染 → 量 scrollHeight → setViewportSize(1440, h) → 再量一次直到收敛 →
 *      window.scrollTo(0,0) → fullPage:false 截图 → 立刻再量一次 rect。
 *      不滚动 + 视口即整页 ⇒ 不需要任何滚动补偿，也不会有 letterbox 偏移。
 *      （用 fullPage:true 也可以，但那样截图与 rect 之间容易差一个滚动条宽度，故不用。）
 *   5) bbox_2d 是 0.0~1.0 相对比例（x/宽、y/高），保留 4 位小数，与 objloc 的坐标约定一致。
 *      @doc AGENTS.md#4.3-坐标约定  （该文档解释"为什么绝不能用像素坐标"）
 *   6) data-gt 属性的值 == target.label，必须是从图上肉眼可读的中文名，
 *      并且这段文字在该元素内部真实可见（模型要能把名字"读"出来）。
 *      可选 data-gt-alias="其它叫法,再一个" 给同一个元素登记**同样合理的别名**，
 *      例如搜索框：图上只有占位文字"搜索商品"，但答"搜索框"显然也是对的。
 *      别名只用来避免"答对了却判错"，绝不能拿来兜住错误答案——
 *      只登记"看着这张图的人也可能这么说"的名字，不要登记答案本身。
 *
 * 用法：node scripts/capture_web_samples.mjs
 * 退出码：0 成功；2 找不到 playwright；1 生成/校验失败（详细信息打到 stderr）。
 */
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// ---------------------------------------------------------------- 依赖加载
// 本机没装 playwright 到项目里，只能从 npx 缓存里按绝对路径 require，不要改成 import "playwright"。
const require = createRequire(import.meta.url);
const CANDIDATES = [
  "E:/Qsmy-Claude-Code/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright",
  "E:/Qsmy-Claude-Code/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright",
];
let playwright = null;
for (const c of CANDIDATES) {
  try {
    playwright = require(c);
    break;
  } catch (e) {}
}
if (!playwright) {
  console.error("找不到 playwright 模块");
  process.exit(2);
}

// 浏览器可执行文件：本机只有这几个 chromium headless shell，逐个探测；都没有就退化为默认 launch。
const EXECUTABLE_CANDIDATES = [
  "C:/Users/qsmy/AppData/Local/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe",
  "C:/Users/qsmy/AppData/Local/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-win64/chrome-headless-shell.exe",
  "C:/Users/qsmy/AppData/Local/ms-playwright/chromium_headless_shell-1200/chrome-headless-shell-win64/chrome-headless-shell.exe",
];

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const OUT_DIR = path.join(ROOT, "runs", "web_samples");
const VIEW_WIDTH = 1440;
const VIEW_HEIGHT_INITIAL = 900;
const MIN_TARGETS = 5;
const MAX_TARGETS = 9;

// ---------------------------------------------------------------- 公共样式
// 所有页面共用的重置与"顶栏 / 卡片"设计系统。刻意不写 transition / animation。
const BASE_CSS = `
*{box-sizing:border-box;margin:0;padding:0}
html{background:#eef1f7;scrollbar-width:none}
html::-webkit-scrollbar,body::-webkit-scrollbar{width:0;height:0}
body{width:1440px;background:#eef1f7;color:#1f2937;font-size:14px;line-height:1.55;
  font-family:"Microsoft YaHei","Segoe UI",sans-serif}
ul{list-style:none}
.topbar{height:64px;display:flex;align-items:center;gap:26px;padding:0 32px;background:#fff;border-bottom:1px solid #e4e9f2}
.brand{display:flex;align-items:center;gap:10px;font-size:17px;font-weight:700;color:#111827;white-space:nowrap}
.logo{width:26px;height:26px;border-radius:9px;background:linear-gradient(135deg,#4f6ef7,#8b5cf6)}
.nav{display:flex;gap:4px}
.nav span{padding:8px 14px;border-radius:9px;color:#5b6478;white-space:nowrap}
.nav span.on{background:#eef2ff;color:#3b5bdb;font-weight:600}
.searchbox{height:38px;border-radius:10px;background:#f2f5fa;border:1px solid #e4e9f2;color:#8b93a5;
  font-size:13px;display:flex;align-items:center;padding:0 14px;white-space:nowrap}
.grow{margin-left:auto}
.me{display:flex;align-items:center;gap:10px}
.me .chip{height:34px;padding:0 14px;border-radius:9px;background:#f2f5fa;color:#5b6478;display:flex;align-items:center;font-size:13px}
.me .avatar{width:36px;height:36px;border-radius:50%;background:#e8edff;color:#3b5bdb;display:flex;
  align-items:center;justify-content:center;font-size:13px;font-weight:600}
.card{background:#fff;border:1px solid #e6ebf3;border-radius:14px;box-shadow:0 1px 2px rgba(16,24,40,.05)}
`;

// ---------------------------------------------------------------- 1. 数据看板
const DASHBOARD_CSS = `
.page{padding:26px 32px 44px}
.head{display:flex;align-items:baseline;gap:14px;margin-bottom:20px}
.head h1{font-size:25px;letter-spacing:-.3px}
.head .sub{color:#8b93a5;font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:18px}
.kpi{padding:18px 20px 16px}
.kpi .kname{color:#6b7488}
.kpi .kval{font-size:29px;font-weight:700;letter-spacing:-.6px;margin:8px 0 6px;color:#111827}
.kpi .kdelta{font-size:12px;color:#8b93a5}
.kpi .kdelta b{color:#0f9d58;font-weight:600}
.kpi .kdelta.warn b{color:#e05b5b}
.spark{display:flex;align-items:flex-end;gap:4px;height:34px;margin-top:14px}
.spark i{flex:1;background:#dfe6f8;border-radius:3px}
.spark i.hi{background:#6d8bfb}
.grid2{display:grid;grid-template-columns:1fr 380px;gap:18px;margin-top:18px}
.panel{padding:20px 22px 22px}
.phead{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.phead h2{font-size:16px}
.pill{font-size:12px;color:#3b5bdb;background:#eef2ff;border-radius:999px;padding:3px 10px}
.bars{display:flex;align-items:flex-end;gap:14px;height:210px;padding-top:10px}
.bars .col{flex:1;height:100%;display:flex;flex-direction:column;justify-content:flex-end}
.bars .col i{display:block;background:linear-gradient(180deg,#7d97fb,#4f6ef7);border-radius:6px 6px 0 0}
.axis{display:flex;gap:14px;margin-top:14px;border-top:1px dashed #e6ebf3;padding-top:10px}
.axis span{flex:1;text-align:center;color:#9aa2b3;font-size:11px}
.todo li{display:flex;gap:10px;align-items:center;padding:11px 0;border-bottom:1px dashed #eef1f7;font-size:13px;color:#3f4759}
.todo li:last-child{border-bottom:0}
.todo .dot{width:8px;height:8px;border-radius:50%;background:#4f6ef7;flex:none}
.todo .dot.amber{background:#f0a02a}
.todo .dot.green{background:#20b26b}
.todo .dot.red{background:#e05b5b}
.todo .t{margin-left:auto;color:#9aa2b3;font-size:12px}
.otable{width:100%;border-collapse:collapse;font-size:13px}
.otable th{text-align:left;color:#8b93a5;font-weight:600;padding:10px 8px;border-bottom:1px solid #eef1f7}
.otable td{padding:11px 8px;border-bottom:1px dashed #eef1f7;color:#3f4759}
.otable tr:last-child td{border-bottom:0}
.tag{display:inline-block;font-size:12px;border-radius:999px;padding:2px 10px;background:#eef2ff;color:#3b5bdb}
.tag.ok{background:#e8f7ef;color:#0f9d58}
.tag.wait{background:#fff4e5;color:#b45309}
`;

const DASHBOARD_BODY = `
<header class="topbar">
  <div class="brand"><span class="logo"></span>云商铺 · 商家后台</div>
  <nav class="nav">
    <span class="on">数据看板</span><span>订单管理</span><span>商品库</span><span>客户运营</span><span>营销中心</span>
  </nav>
  <div class="searchbox grow" data-gt="搜索商品、订单、客户" data-gt-alias="搜索框,搜索栏">搜索商品、订单、客户</div>
  <div class="me"><span class="chip">消息 3</span><span class="avatar">运营</span></div>
</header>
<main class="page">
  <div class="head">
    <h1>经营概览</h1>
    <span class="sub">数据更新于 2026-09-16 08:00 · 统计口径：已支付订单</span>
  </div>
  <section class="kpis">
    <div class="card kpi" data-gt="总销售额">
      <div class="kname">总销售额</div>
      <div class="kval">¥1,286,430</div>
      <div class="kdelta">较上月 <b>+12.8%</b></div>
      <div class="spark"><i style="height:34%"></i><i style="height:52%"></i><i style="height:44%"></i><i class="hi" style="height:68%"></i><i style="height:60%"></i><i class="hi" style="height:86%"></i><i style="height:74%"></i><i class="hi" style="height:96%"></i></div>
    </div>
    <div class="card kpi" data-gt="订单量">
      <div class="kname">订单量</div>
      <div class="kval">8,642</div>
      <div class="kdelta">较上月 <b>+6.4%</b></div>
      <div class="spark"><i style="height:48%"></i><i style="height:40%"></i><i class="hi" style="height:62%"></i><i style="height:56%"></i><i style="height:70%"></i><i class="hi" style="height:78%"></i><i style="height:66%"></i><i class="hi" style="height:88%"></i></div>
    </div>
    <div class="card kpi" data-gt="退款率">
      <div class="kname">退款率</div>
      <div class="kval">1.86%</div>
      <div class="kdelta warn">较上月 <b>-0.32%</b></div>
      <div class="spark"><i style="height:78%"></i><i class="hi" style="height:66%"></i><i style="height:58%"></i><i style="height:62%"></i><i style="height:48%"></i><i class="hi" style="height:40%"></i><i style="height:36%"></i><i class="hi" style="height:28%"></i></div>
    </div>
    <div class="card kpi" data-gt="新增用户">
      <div class="kname">新增用户</div>
      <div class="kval">1,275</div>
      <div class="kdelta">较上月 <b>+9.1%</b></div>
      <div class="spark"><i style="height:30%"></i><i class="hi" style="height:46%"></i><i style="height:52%"></i><i style="height:44%"></i><i class="hi" style="height:70%"></i><i style="height:64%"></i><i class="hi" style="height:82%"></i><i style="height:90%"></i></div>
    </div>
  </section>
  <section class="grid2">
    <div class="card panel" data-gt="近30天销售趋势">
      <div class="phead"><h2>近30天销售趋势</h2><span class="pill">按日 · 单位：万元</span></div>
      <div class="bars">
        <div class="col"><i style="height:46%"></i></div>
        <div class="col"><i style="height:58%"></i></div>
        <div class="col"><i style="height:40%"></i></div>
        <div class="col"><i style="height:66%"></i></div>
        <div class="col"><i style="height:52%"></i></div>
        <div class="col"><i style="height:74%"></i></div>
        <div class="col"><i style="height:60%"></i></div>
        <div class="col"><i style="height:82%"></i></div>
        <div class="col"><i style="height:68%"></i></div>
        <div class="col"><i style="height:90%"></i></div>
        <div class="col"><i style="height:76%"></i></div>
        <div class="col"><i style="height:96%"></i></div>
      </div>
      <div class="axis"><span>09-01</span><span>09-06</span><span>09-11</span><span>09-16</span><span>09-21</span><span>09-26</span></div>
    </div>
    <div class="card panel" data-gt="待办事项">
      <div class="phead"><h2>待办事项</h2><span class="pill">4 条待处理</span></div>
      <ul class="todo">
        <li><span class="dot"></span>处理 3 笔待发货订单<span class="t">今天 10:20</span></li>
        <li><span class="dot amber"></span>审核 2 个退款申请<span class="t">今天 09:45</span></li>
        <li><span class="dot green"></span>补充「无线降噪耳机」库存<span class="t">昨天 18:02</span></li>
        <li><span class="dot red"></span>回复 5 条客户咨询<span class="t">昨天 15:31</span></li>
      </ul>
    </div>
  </section>
  <section class="card panel" style="margin-top:18px">
    <div class="phead"><h2>最近订单</h2><span class="pill">近 24 小时 · 共 326 单</span></div>
    <table class="otable">
      <thead><tr><th>订单号</th><th>客户</th><th>商品</th><th>金额</th><th>状态</th><th>下单时间</th></tr></thead>
      <tbody>
        <tr><td>SO-20260916-0421</td><td>陈嘉怡</td><td>无线降噪耳机 × 1</td><td>¥599.00</td><td><span class="tag ok">已发货</span></td><td>09-16 09:12</td></tr>
        <tr><td>SO-20260916-0420</td><td>李文博</td><td>机械键盘 × 2</td><td>¥858.00</td><td><span class="tag wait">待发货</span></td><td>09-16 09:04</td></tr>
        <tr><td>SO-20260916-0419</td><td>赵欣然</td><td>智能手表 × 1</td><td>¥1,299.00</td><td><span class="tag ok">已发货</span></td><td>09-16 08:51</td></tr>
        <tr><td>SO-20260916-0418</td><td>孙浩然</td><td>便携充电宝 × 3</td><td>¥387.00</td><td><span class="tag">已签收</span></td><td>09-16 08:33</td></tr>
      </tbody>
    </table>
  </section>
</main>
`;

// ---------------------------------------------------------------- 2. 商品列表
const SHOP_CSS = `
.shopmain{padding:24px 32px 48px}
.banner{display:flex;align-items:center;justify-content:space-between;padding:26px 34px;border-radius:16px;
  background:linear-gradient(120deg,#4338ca,#7c3aed 55%,#c026d3);color:#fff}
.btag{display:inline-block;font-size:12px;letter-spacing:2px;background:rgba(255,255,255,.22);border-radius:999px;padding:4px 12px}
.btitle{font-size:24px;font-weight:700;margin:12px 0 6px}
.bsub{font-size:13px;color:#ede9fe}
.bbtn{background:#fff;color:#4c1d95;font-size:15px;font-weight:600;padding:12px 26px;border-radius:12px;white-space:nowrap}
.toolbar{display:flex;align-items:center;gap:10px;margin:24px 0 16px}
.chip{padding:7px 14px;border-radius:999px;background:#fff;border:1px solid #e6ebf3;color:#5b6478;font-size:13px}
.chip.on{background:#eef2ff;border-color:#d6e0ff;color:#3b5bdb;font-weight:600}
.toolbar .cnt{margin-left:auto;color:#6b7488;font-size:13px}
.pgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
.pcard{overflow:hidden;display:flex;flex-direction:column}
.thumb{height:150px;position:relative;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,var(--bg1),var(--bg2))}
.thumb span{position:absolute;display:block}
.thumb .c{width:84px;height:84px;border-radius:50%;background:var(--c1);opacity:.9;left:64px}
.thumb .r{width:104px;height:32px;border-radius:10px;background:var(--c2);right:56px;bottom:34px;opacity:.9}
.pbody{padding:14px 16px 16px;display:flex;flex-direction:column;flex:1}
.pname{font-size:15px;font-weight:600;color:#111827}
.pdesc{font-size:12px;color:#8b93a5;margin:6px 0 10px}
.prow{display:flex;align-items:baseline;gap:8px;margin-bottom:12px}
.price{font-size:19px;font-weight:700;color:#e04f4f}
.old{font-size:12px;color:#a8afbd;text-decoration:line-through}
.buy{margin-top:auto;height:38px;border:0;border-radius:10px;background:#4f6ef7;color:#fff;
  font-size:14px;font-weight:600;font-family:inherit}
`;

const SHOP_BODY = `
<header class="topbar">
  <div class="brand"><span class="logo"></span>云商铺 · 商品库</div>
  <nav class="nav"><span class="on">全部商品</span><span>数码家电</span><span>办公设备</span><span>家居生活</span></nav>
  <div class="searchbox grow" data-gt="搜索商品" data-gt-alias="搜索框,搜索栏">搜索商品</div>
  <div class="me"><span class="chip">购物车 3</span><span class="avatar">我的</span></div>
</header>
<main class="shopmain">
  <section class="banner" data-gt="限时秒杀">
    <div>
      <div class="btag">限时秒杀</div>
      <div class="btitle">今晚 20:00 开抢，全场低至 5 折</div>
      <div class="bsub">前 200 名下单额外赠送一年延保服务</div>
    </div>
    <div class="bbtn">立即抢购</div>
  </section>
  <div class="toolbar">
    <span class="chip on">综合排序</span><span class="chip">销量优先</span><span class="chip">价格从低到高</span><span class="chip">仅看有货</span>
    <span class="cnt">共 128 件商品</span>
  </div>
  <section class="pgrid">
    <article class="card pcard" data-gt="无线降噪耳机">
      <div class="thumb" style="--bg1:#e0f2fe;--bg2:#bae6fd;--c1:#0284c7;--c2:#38bdf8"><span class="c"></span><span class="r"></span></div>
      <div class="pbody">
        <div class="pname">无线降噪耳机</div>
        <div class="pdesc">主动降噪 · 40 小时续航</div>
        <div class="prow"><span class="price">¥599</span><span class="old">¥799</span></div>
        <button class="buy">加入购物车</button>
      </div>
    </article>
    <article class="card pcard" data-gt="机械键盘">
      <div class="thumb" style="--bg1:#ede9fe;--bg2:#ddd6fe;--c1:#6d28d9;--c2:#a78bfa"><span class="c"></span><span class="r"></span></div>
      <div class="pbody">
        <div class="pname">机械键盘</div>
        <div class="pdesc">87 键 · 热插拔轴体</div>
        <div class="prow"><span class="price">¥429</span><span class="old">¥559</span></div>
        <button class="buy">加入购物车</button>
      </div>
    </article>
    <article class="card pcard" data-gt="智能手表">
      <div class="thumb" style="--bg1:#dcfce7;--bg2:#bbf7d0;--c1:#15803d;--c2:#4ade80"><span class="c"></span><span class="r"></span></div>
      <div class="pbody">
        <div class="pname">智能手表</div>
        <div class="pdesc">血氧监测 · 14 天续航</div>
        <div class="prow"><span class="price">¥1,299</span><span class="old">¥1,599</span></div>
        <button class="buy">加入购物车</button>
      </div>
    </article>
    <article class="card pcard" data-gt="便携充电宝">
      <div class="thumb" style="--bg1:#ffedd5;--bg2:#fed7aa;--c1:#c2410c;--c2:#fb923c"><span class="c"></span><span class="r"></span></div>
      <div class="pbody">
        <div class="pname">便携充电宝</div>
        <div class="pdesc">20000mAh · 双向快充</div>
        <div class="prow"><span class="price">¥129</span><span class="old">¥199</span></div>
        <button class="buy">加入购物车</button>
      </div>
    </article>
    <article class="card pcard" data-gt="蓝牙音箱">
      <div class="thumb" style="--bg1:#ffe4e6;--bg2:#fecdd3;--c1:#be123c;--c2:#fb7185"><span class="c"></span><span class="r"></span></div>
      <div class="pbody">
        <div class="pname">蓝牙音箱</div>
        <div class="pdesc">360 度环绕 · IPX7 防水</div>
        <div class="prow"><span class="price">¥259</span><span class="old">¥349</span></div>
        <button class="buy">加入购物车</button>
      </div>
    </article>
    <article class="card pcard" data-gt="人体工学椅">
      <div class="thumb" style="--bg1:#e2e8f0;--bg2:#cbd5e1;--c1:#334155;--c2:#64748b"><span class="c"></span><span class="r"></span></div>
      <div class="pbody">
        <div class="pname">人体工学椅</div>
        <div class="pdesc">动态腰托 · 四级扶手</div>
        <div class="prow"><span class="price">¥1,599</span><span class="old">¥1,999</span></div>
        <button class="buy">加入购物车</button>
      </div>
    </article>
  </section>
</main>
`;

// ---------------------------------------------------------------- 3. 登录表单
const FORM_CSS = `
.fwrap{display:flex;gap:32px;padding:72px 96px 40px;align-items:stretch}
.ffooter{text-align:center;color:#9aa2b3;font-size:12px;padding:0 0 30px}
.intro{width:520px;border-radius:20px;padding:44px 42px;color:#fff;
  background:linear-gradient(150deg,#1e293b,#312e81 58%,#4c1d95)}
.intro .ibrand{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:600;color:#c7d2fe}
.intro .idot{width:24px;height:24px;border-radius:8px;background:linear-gradient(135deg,#818cf8,#c084fc)}
.intro h2{font-size:30px;line-height:1.4;margin:28px 0 16px;letter-spacing:-.4px}
.intro p{color:#c7d2fe;font-size:14px;line-height:1.8}
.feats{margin-top:32px}
.feats li{display:flex;align-items:center;gap:12px;padding:13px 0;font-size:14px;color:#e0e7ff;border-top:1px solid rgba(255,255,255,.16)}
.feats li .fi{width:22px;height:22px;border-radius:50%;background:rgba(255,255,255,.2);flex:none}
.formcard{flex:1;padding:44px 48px;background:#fff;border-radius:20px;border:1px solid #e6ebf3;
  box-shadow:0 12px 32px rgba(16,24,40,.06)}
.formcard h1{font-size:26px}
.formcard .sub2{color:#8b93a5;margin:8px 0 26px;font-size:13px}
.field{margin-bottom:18px}
.field label{display:block;font-size:13px;font-weight:600;color:#3f4759;margin-bottom:8px}
.input{height:46px;border:1px solid #dfe5ef;border-radius:11px;display:flex;align-items:center;
  padding:0 14px;font-size:14px;background:#fbfcfe}
.input.ph{color:#a8afbd;background:#fff}
.select{height:46px;border:1px solid #dfe5ef;border-radius:11px;display:flex;align-items:center;
  padding:0 14px;font-size:14px;background:#fff}
.select .arrow{margin-left:auto;width:0;height:0;border-left:5px solid transparent;
  border-right:5px solid transparent;border-top:6px solid #98a1b3}
.checks{display:flex;gap:10px}
.ck{flex:1;height:42px;border:1px solid #dfe5ef;border-radius:10px;display:flex;align-items:center;
  justify-content:center;font-size:13px;color:#5b6478}
.ck.on{border-color:#b9c8ff;background:#eef2ff;color:#3b5bdb;font-weight:600}
.rowline{display:flex;align-items:center;gap:10px;font-size:13px;color:#5b6478;margin:6px 0 22px}
.rowline .box{position:relative;width:18px;height:18px;border-radius:5px;background:#4f6ef7;flex:none}
.rowline .box:after{content:"";position:absolute;left:5px;top:2px;width:5px;height:9px;
  border-right:2px solid #fff;border-bottom:2px solid #fff;transform:rotate(40deg)}
.primary{width:100%;height:48px;border:0;border-radius:12px;color:#fff;font-size:16px;font-weight:600;
  font-family:inherit;background:linear-gradient(90deg,#4f6ef7,#7c5cf6)}
.foot{display:flex;align-items:center;justify-content:space-between;margin-top:20px;font-size:13px;color:#6b7488}
.foot .link{color:#3b5bdb}
`;

const FORM_BODY = `
<div class="fwrap">
  <aside class="intro">
    <div class="ibrand"><span class="idot"></span>启明 · 企业协作平台</div>
    <h2>让团队的每一次协作<br>都有迹可循</h2>
    <p>任务、文档、审批流统一在一处，权限随组织架构自动同步，管理员可随时审计全部操作记录。</p>
    <ul class="feats">
      <li><span class="fi"></span>企业邮箱单点登录</li>
      <li><span class="fi"></span>任务与文档实时协同</li>
      <li><span class="fi"></span>全链路操作审计</li>
      <li><span class="fi"></span>国密算法数据加密</li>
    </ul>
  </aside>
  <section class="formcard">
    <!-- 块级标题的 CSS 框会横跨整张卡片，比可见文字宽得多；
         data-gt 挂在行内 span 上，真值框才贴着文字本身。 -->
    <h1><span data-gt="账号登录">账号登录</span></h1>
    <p class="sub2">请使用企业邮箱登录，账号由管理员统一开通</p>
    <!-- ⚠️ data-gt 打在**控件本体**上，不是外面那层带 label 的 .field 容器。
         踩过：原先打在 .field 上，真值框把上方那行 label 一起圈了进去，
         而模型自然只框输入框 → IoU 掉到 0.62，看着像模型定位不准，
         其实是在考"框画得多紧"。让人来标也是框控件本体，所以以控件为准。 -->
    <div class="field">
      <label>账号邮箱</label>
      <div class="input" data-gt="账号邮箱">zhangwei@company.com</div>
    </div>
    <div class="field">
      <label>登录密码</label>
      <div class="input ph" data-gt="登录密码">请输入 8 位以上密码</div>
    </div>
    <div class="field">
      <label>所属团队</label>
      <div class="select" data-gt="所属团队">产品研发中心<span class="arrow"></span></div>
    </div>
    <!-- 通知方式做成单个下拉而不是一排复选框：多控件拼成一组时，
         真值只能框整组，模型却会分别框每一块，IoU 必然对不上。 -->
    <div class="field">
      <label>通知方式</label>
      <div class="select" data-gt="通知方式">邮件通知、短信通知<span class="arrow"></span></div>
    </div>
    <!-- 下面是**故意留下的干扰项**：可见但不在真值里 -->
    <div class="rowline">
      <span class="box"></span>记住登录状态（7 天内免登录）
    </div>
    <button class="primary" data-gt="立即登录">立即登录</button>
    <div class="foot"><span class="link">忘记密码</span><span>首次登录请先绑定手机号</span></div>
  </section>
</div>
<div class="ffooter">© 2026 启明科技 · 服务条款 · 隐私政策 · 沪ICP备 2026001234 号</div>
`;

// ---------------------------------------------------------------- 4. 文章详情
const ARTICLE_CSS = `
.acont{display:grid;grid-template-columns:1fr 340px;gap:28px;padding:30px 96px 56px;align-items:start}
.amain{background:#fff;border:1px solid #e6ebf3;border-radius:16px;padding:32px 40px 36px}
.crumb{color:#9aa2b3;font-size:12px;margin-bottom:14px}
.atitle{font-size:32px;line-height:1.38;letter-spacing:-.5px;color:#111827}
.ameta{display:flex;gap:18px;color:#8b93a5;font-size:13px;margin:14px 0 20px;padding-bottom:18px;border-bottom:1px solid #eef1f7}
.cover{position:relative;height:300px;border-radius:14px;overflow:hidden;
  background:linear-gradient(120deg,#0f172a,#1d4ed8 56%,#0891b2)}
.cover .cs{position:absolute;border-radius:50%;background:rgba(255,255,255,.16)}
.cover .cs.a{width:190px;height:190px;right:60px;top:-46px}
.cover .cs.b{width:120px;height:120px;right:216px;bottom:-34px;background:rgba(255,255,255,.10)}
.cover .cs.c{width:56px;height:56px;left:96px;top:74px;background:rgba(255,255,255,.22)}
.cover .ctag{position:absolute;left:18px;bottom:16px;background:rgba(15,23,42,.55);color:#e2e8f0;
  font-size:13px;padding:6px 12px;border-radius:8px}
.amain p{font-size:15px;color:#3f4759;line-height:1.9;margin-bottom:16px}
.amain p.lead{font-size:16px;color:#2b3445;background:#f7f9fd;border-left:4px solid #4f6ef7;
  border-radius:0 10px 10px 0;padding:16px 20px;margin:24px 0 18px}
.lead strong{color:#3b5bdb}
.h2{font-size:19px;color:#111827;margin:26px 0 14px;padding-left:12px;border-left:4px solid #4f6ef7}
.aside{background:#fff;border:1px solid #e6ebf3;border-radius:16px;padding:22px 22px 12px}
.rhead{font-size:16px;padding-bottom:14px;border-bottom:1px solid #eef1f7;margin-bottom:4px}
.rcard{display:flex;gap:12px;padding:14px 0;border-bottom:1px dashed #eef1f7;align-items:center}
.rcard:last-child{border-bottom:0}
.rthumb{width:64px;height:64px;border-radius:10px;flex:none}
.r1{background:linear-gradient(135deg,#1e3a8a,#3b82f6)}
.r2{background:linear-gradient(135deg,#065f46,#34d399)}
.r3{background:linear-gradient(135deg,#7c2d12,#fb923c)}
.rtitle{font-size:14px;font-weight:600;color:#1f2937;line-height:1.45}
.rmeta{font-size:12px;color:#9aa2b3;margin-top:5px}
`;

const ARTICLE_BODY = `
<header class="topbar">
  <div class="brand"><span class="logo"></span>智造观察 · 资讯</div>
  <nav class="nav"><span class="on">首页</span><span>智能制造</span><span>新能源</span><span>人工智能</span><span>产业观察</span></nav>
  <div class="me grow"><span class="chip">订阅</span><span class="avatar">登录</span></div>
</header>
<div class="acont">
  <main class="amain">
    <div class="crumb">首页 / 智能制造 / 正文</div>
    <h1 class="atitle" data-gt="国产人形机器人进入汽车总装线">国产人形机器人进入汽车总装线</h1>
    <div class="ameta"><span>记者 林安</span><span>2026-09-16 09:20</span><span>阅读 1.2 万</span><span>评论 36</span></div>
    <!-- 别名叫法是给人看的：这块区域叫"封面图""文章封面"都合理，答对不该判错 -->
 <figure class="cover" data-gt="封面图 · 人形机器人总装线" data-gt-alias="封面图,封面图区域,文章封面">
      <span class="cs a"></span><span class="cs b"></span><span class="cs c"></span>
      <span class="ctag">封面图 · 人形机器人总装线</span>
    </figure>
    <p class="lead" data-gt="导语"><strong>导语：</strong>首批 6 台人形机器人已在华南某汽车总装车间连续作业 30 天，承担物料搬运与螺栓预紧两道工序，日均节拍稳定在 52 秒。</p>
    <p>据产线负责人介绍，这批机器人并没有替代原有工位，而是被安排在夜班与换线间隙，负责把零件从缓存区送到线边，并把预紧数据实时回传到制造执行系统。</p>
    <p>与传统机械臂不同，人形机器人依靠双足与双臂完成搬运，不需要为每一种车型重新改造夹具，这让它在多车型混线生产中的改造成本大幅下降。</p>
    <h2 class="h2">从演示到上岗的三道门槛</h2>
    <p>车间环境对稳定性极为苛刻：地面油污、光照变化、人员穿行都会影响视觉定位。厂方为此在工位周边加装了反光标识，并把单次任务时长压缩到 3 分钟以内。</p>
    <p>一位参与部署的工程师表示，目前机器人单班有效作业时间约为 6.5 小时，剩余时间用于换电与自检，距离真正意义上的无人化还有明显距离。</p>
  </main>
  <aside class="aside">
    <!-- 同 form 的大标题：块级 h2 的框横跨整个侧栏，真值挂在行内 span 上才贴文字 -->
 <h2 class="rhead"><span data-gt="相关推荐">相关推荐</span></h2>
    <div class="rcard" data-gt="固态电池量产提速">
      <div class="rthumb r1"></div>
      <div><div class="rtitle">固态电池量产提速</div><div class="rmeta">新能源 · 2 小时前</div></div>
    </div>
    <div class="rcard" data-gt="城市低空物流试点扩容">
      <div class="rthumb r2"></div>
      <div><div class="rtitle">城市低空物流试点扩容</div><div class="rmeta">产业观察 · 5 小时前</div></div>
    </div>
    <div class="rcard" data-gt="开源大模型推理成本再降">
      <div class="rthumb r3"></div>
      <div><div class="rtitle">开源大模型推理成本再降</div><div class="rmeta">人工智能 · 昨天</div></div>
    </div>
  </aside>
</div>
`;

// ---------------------------------------------------------------- 样本定义
// title 用函数：高度要等渲染完才知道，manifest 里写的是实际值。
const SAMPLES = [
  {
    id: "web_dashboard",
    title: (h) => "电商数据看板（1440×" + h + "）",
    purpose: "测看板类布局的文字目标定位：4 张 KPI 卡片、趋势图区、待办区与导航搜索框，标签都是区块内的中文名，考验“名字与框”的对应关系。",
    css: DASHBOARD_CSS,
    body: DASHBOARD_BODY,
  },
  {
    id: "web_shop",
    title: (h) => "商品列表页（1440×" + h + "）",
    purpose: "测商品网格定位：6 张商品卡以商品名为标签，卡内还有价格、描述与“加入购物车”等干扰文本，另加秒杀横幅与搜索框。",
    css: SHOP_CSS,
    body: SHOP_BODY,
  },
  {
    id: "web_form",
    title: (h) => "登录设置表单页（1440×" + h + "）",
    purpose: "测表单控件定位：中文字段名在控件上方，需要把标签正确绑定到输入框、下拉、复选组与按钮上，placeholder 文字也算可见文本。",
    css: FORM_CSS,
    body: FORM_BODY,
  },
  {
    id: "web_article",
    title: (h) => "新闻文章详情页（1440×" + h + "）",
    purpose: "测图文混排长页面：大标题、封面图区、导语段与右侧 3 张推荐卡片，考验长文本区块的边界判断与中文标题识别。",
    css: ARTICLE_CSS,
    body: ARTICLE_BODY,
  },
];

// ---------------------------------------------------------------- 工具函数
function buildDocument(sample) {
  return (
    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">' +
    "<title>" + sample.id + "</title><style>" +
    BASE_CSS + sample.css +
    "</style></head><body>" + sample.body + "</body></html>"
  );
}

const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);
const round4 = (v) => Math.round(v * 10000) / 10000;
const floor4 = (v) => Math.floor(v * 10000) / 10000;
const ceil4 = (v) => Math.ceil(v * 10000) / 10000;

/** 像素矩形 -> 归一化 bbox_2d。先四舍五入到 4 位；万一舍入把框压扁了，改用向外取整兜底。 */
function toUnitBBox(rect, width, height) {
  const nx1 = clamp01(rect[0] / width);
  const ny1 = clamp01(rect[1] / height);
  const nx2 = clamp01(rect[2] / width);
  const ny2 = clamp01(rect[3] / height);
  let x1 = round4(nx1);
  let y1 = round4(ny1);
  let x2 = round4(nx2);
  let y2 = round4(ny2);
  if (!(x1 < x2)) {
    x1 = floor4(nx1);
    x2 = ceil4(nx2);
  }
  if (!(y1 < y2)) {
    y1 = floor4(ny1);
    y2 = ceil4(ny2);
  }
  return [x1, y1, x2, y2];
}

/** 读 PNG 头里的 IHDR，拿到真实像素尺寸——manifest 的 width/height 以它为准，不信内存里的推算。 */
function readPngSize(file) {
  const buf = fs.readFileSync(file);
  const signature = [137, 80, 78, 71, 13, 10, 26, 10];
  if (buf.length < 24) throw new Error("PNG 文件过短：" + file);
  for (let i = 0; i < 8; i++) {
    if (buf[i] !== signature[i]) throw new Error("不是合法 PNG：" + file);
  }
  const chunkType = buf.toString("ascii", 12, 16);
  if (chunkType !== "IHDR") throw new Error("PNG 首个块不是 IHDR：" + file);
  return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

/** 本地时间 + 时区偏移，形如 2026-09-16T12:00:00+08:00。 */
function localIso(date) {
  const pad = (n) => String(n).padStart(2, "0");
  const offsetMin = -date.getTimezoneOffset();
  const sign = offsetMin >= 0 ? "+" : "-";
  const oh = pad(Math.floor(Math.abs(offsetMin) / 60));
  const om = pad(Math.abs(offsetMin) % 60);
  return (
    date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) +
    "T" + pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds()) +
    sign + oh + ":" + om
  );
}

/** 等两帧，确保布局/字体已经落地（不用 sleep，保持确定性）。 */
async function settle(page) {
  await page.evaluate(
    () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
  );
}

// ---------------------------------------------------------------- 核心：单张样本
async function captureSample(browser, sample) {
  const context = await browser.newContext({
    viewport: { width: VIEW_WIDTH, height: VIEW_HEIGHT_INITIAL },
    deviceScaleFactor: 1, // 固定 1：PNG 像素 == CSS 像素，坐标换算不用再乘缩放比
  });
  const page = await context.newPage();
  await page.setContent(buildDocument(sample), { waitUntil: "load" });
  await page.evaluate(async () => {
    if (document.fonts && document.fonts.ready) {
      await document.fonts.ready;
    }
  });
  await settle(page);

  // 收敛页面高度：viewport 高度会反过来影响 vh 布局，迭代几次直到 scrollHeight 不再变化。
  let height = VIEW_HEIGHT_INITIAL;
  for (let i = 0; i < 6; i++) {
    const measured = await page.evaluate(() => {
      const d = document.documentElement;
      const b = document.body;
      return Math.max(d.scrollHeight, b ? b.scrollHeight : 0, d.offsetHeight);
    });
    if (measured === height) break;
    height = measured;
    await page.setViewportSize({ width: VIEW_WIDTH, height: height });
    await settle(page);
  }

  // 视口 == 整页，且不滚动 ⇒ client 坐标就是图像坐标。
  await page.evaluate(() => window.scrollTo(0, 0));
  const file = path.join(OUT_DIR, sample.id + ".png");
  await page.screenshot({ path: file, fullPage: false });

  const rawRects = await page.evaluate(() => {
    const out = [];
    const nodes = document.querySelectorAll("[data-gt]");
    for (const el of nodes) {
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue; // 隐藏元素直接跳过
      const alias = el.getAttribute("data-gt-alias") || "";
      out.push({
        label: el.getAttribute("data-gt"),
        aliases: alias.split(/[,，|]/).map((s) => s.trim()).filter(Boolean),
        rect: [r.left, r.top, r.right, r.bottom],
      });
    }
    return out;
  });
  await context.close();

  const size = readPngSize(file);
  if (size.width !== VIEW_WIDTH) {
    throw new Error(sample.id + "：PNG 宽度 " + size.width + " != " + VIEW_WIDTH);
  }

  const targets = rawRects.map((item) => ({
    label: item.label,
    bbox_2d: toUnitBBox(item.rect, size.width, size.height),
    label_mode: "text",
    expect_shape: false,
    // 空数组不写进 manifest，避免下游多一个恒空的字段
    ...(item.aliases.length ? { aliases: item.aliases } : {}),
  }));

  return {
    id: sample.id,
    title: sample.title(size.height),
    purpose: sample.purpose,
    file: sample.id + ".png",
    width: size.width,
    height: size.height,
    targets,
  };
}

// ---------------------------------------------------------------- 自检
function validate(manifest) {
  const problems = [];
  const ids = new Set();
  for (const s of manifest.samples) {
    if (!/^web_[a-z0-9_]+$/.test(s.id)) problems.push(s.id + "：id 不合法");
    if (ids.has(s.id)) problems.push(s.id + "：id 重复");
    ids.add(s.id);
    if (!Number.isInteger(s.width) || !Number.isInteger(s.height)) problems.push(s.id + "：宽高不是整数");
    if (s.targets.length < MIN_TARGETS || s.targets.length > MAX_TARGETS) {
      problems.push(s.id + "：targets 数量 " + s.targets.length + " 不在 " + MIN_TARGETS + "~" + MAX_TARGETS);
    }
    const labels = new Set();
    for (const t of s.targets) {
      if (labels.has(t.label)) problems.push(s.id + "：label 重复 -> " + t.label);
      labels.add(t.label);
      if (t.label_mode !== "text" || t.expect_shape !== false) problems.push(s.id + "：label_mode/expect_shape 不合规");
      const b = t.bbox_2d;
      if (!Array.isArray(b) || b.length !== 4) {
        problems.push(s.id + "：" + t.label + " bbox 不是 4 元组");
        continue;
      }
      for (const v of b) {
        if (typeof v !== "number" || !(v >= 0 && v <= 1)) problems.push(s.id + "：" + t.label + " 越界 " + v);
        if (Math.abs(v * 10000 - Math.round(v * 10000)) > 1e-6) problems.push(s.id + "：" + t.label + " 小数位超过 4");
      }
      if (!(b[0] < b[2])) problems.push(s.id + "：" + t.label + " x1>=x2");
      if (!(b[1] < b[3])) problems.push(s.id + "：" + t.label + " y1>=y2");
    }
  }
  return problems;
}

// ---------------------------------------------------------------- 主流程
async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });

  const executablePath = EXECUTABLE_CANDIDATES.find((p) => fs.existsSync(p)) || null;
  const launchOptions = {
    args: [
      "--hide-scrollbars", // 滚动条会吃掉 15px 布局宽度，直接把"坐标 == 像素"这件事搞歪
      "--force-color-profile=srgb",
      "--disable-lcd-text",
      "--font-render-hinting=none",
    ],
  };
  if (executablePath) launchOptions.executablePath = executablePath;
  const browser = await playwright.chromium.launch(launchOptions);

  const samples = [];
  try {
    for (const sample of SAMPLES) {
      const result = await captureSample(browser, sample);
      samples.push(result);
      console.log(
        "[OK] " + result.id + "  " + result.width + "x" + result.height +
        "  targets=" + result.targets.length
      );
    }
  } finally {
    await browser.close();
  }

  const manifest = {
    generator: "scripts/capture_web_samples.mjs",
    generated_at: localIso(new Date()), // 页面内容确定性；这个时间戳只标记生成时刻
    viewport: { width: VIEW_WIDTH, height: VIEW_HEIGHT_INITIAL },
    samples,
  };

  const problems = validate(manifest);
  if (problems.length > 0) {
    console.error("manifest 自检失败：");
    for (const p of problems) console.error("  - " + p);
    process.exit(1);
  }

  const manifestPath = path.join(OUT_DIR, "manifest.json");
  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + String.fromCharCode(10), "utf8");

  console.log("输出目录：" + OUT_DIR);
  console.log("清单：" + manifestPath);
  for (const s of samples) {
    console.log("  " + s.id + " -> " + s.targets.map((t) => t.label).join(" / "));
  }
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
