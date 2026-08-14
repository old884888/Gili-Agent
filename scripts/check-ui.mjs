// check-ui.mjs —— 前端回归检查（零依赖，node ≥ 18 直接跑）
// 历史教训（开发日志 #20）：删除 HTML 元素后残留 JS 绑定 → TypeError → 整个脚本中断。
// 1) 孤儿 id：JS 引用但 index.html 不存在、且 JS 里也未动态创建 → 报错
// 2) 顶层函数重复定义（"整段追加重写"复发的哨兵）→ 报错
// 任一项失败 exit code = 1。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const html = readFileSync(path.join(root, "static/index.html"), "utf8");
const appjs = readFileSync(path.join(root, "static/app.js"), "utf8");
const uijs = readFileSync(path.join(root, "static/ui/ui.js"), "utf8");

// --- 1) 孤儿 id ---
const htmlIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
const jsCreated = new Set();
for (const m of appjs.matchAll(/\bid="([^"]+)"/g)) jsCreated.add(m[1]);
for (const m of appjs.matchAll(/\.id\s*=\s*["'`]([^"'`$]+)["'`]/g)) jsCreated.add(m[1]);

const refs = new Set();
const collect = (src) => {
  for (const m of src.matchAll(/\$\("#([A-Za-z0-9_-]+)"\)/g)) refs.add(m[1]);
  for (const m of src.matchAll(/\$\(`#([A-Za-z0-9_-]+)`\)/g)) refs.add(m[1]);
  for (const m of src.matchAll(/getElementById\("([^"]+)"\)/g)) refs.add(m[1]);
  // querySelector/closest 支持多选择器（如 "#cite-popover, .cite"），按 #id 逐个提取
  for (const m of src.matchAll(/querySelector\(\s*"([^"]+)"\s*\)/g)) {
    for (const id of m[1].matchAll(/#([A-Za-z0-9_-]+)/g)) refs.add(id[1]);
  }
  for (const m of src.matchAll(/closest\(\s*"([^"]+)"\s*\)/g)) {
    for (const id of m[1].matchAll(/#([A-Za-z0-9_-]+)/g)) refs.add(id[1]);
  }
};
collect(appjs);
collect(uijs);

const orphans = [...refs].filter((id) => !htmlIds.has(id) && !jsCreated.has(id)).sort();

// --- 2) 顶层函数重复 ---
const seen = new Map();
for (const m of appjs.matchAll(/^(?:async )?function\s+([A-Za-z_$][\w$]*)\s*\(/gm)) {
  if (!seen.has(m[1])) seen.set(m[1], []);
  seen.get(m[1]).push(appjs.slice(0, m.index).split("\n").length);
}
const dups = [...seen].filter(([, locs]) => locs.length > 1);

let fail = 0;
if (orphans.length) {
  fail = 1;
  console.log("✗ 孤儿 id（JS 引用但 HTML 不存在且 JS 未创建）:");
  for (const o of orphans) console.log("   #" + o);
} else {
  console.log("✓ 无孤儿 id");
}
if (dups.length) {
  fail = 1;
  console.log("✗ 顶层函数重复定义:");
  for (const [n, locs] of dups) console.log("   " + n + " → 行 " + locs.join(", "));
} else {
  console.log("✓ 无重复顶层函数定义");
}
console.log(fail ? "check-ui FAILED" : "check-ui PASSED");
process.exit(fail);
