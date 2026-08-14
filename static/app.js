/* Gili Agent · 原型 —— 零依赖原生 JS。
 * 所有按钮已就位：真实功能走 mock 接口，未接入的功能 toast 提示。
 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// 标题等用户可控文本插入 innerHTML 前必须转义
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
// 简单哈希（卡片颜色分配用）
function hash(s) { let h = 0; for (let i = 0; i < (s || "").length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0; return Math.abs(h); }

// ---------- 基础工具 ----------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = `${path} -> ${res.status}`;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch { /* 非 JSON 响应 */ }
    throw new Error(detail);
  }
  return res.json();
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2400);
}


// ---------- 状态 ----------

let currentConv = null;
let streaming = false;

// ---------- Markdown 渲染（marked + DOMPurify + mermaid，多 CDN 兜底） ----------

const CDN_SOURCES = {
  marked: [
    "https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js",
    "https://registry.npmmirror.com/marked/12.0.2/files/marked.min.js",
  ],
  dompurify: [
    "https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js",
    "https://registry.npmmirror.com/dompurify/3.1.6/files/dist/purify.min.js",
  ],
  mermaid: [
    "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js",
    "https://registry.npmmirror.com/mermaid/10.9.1/files/dist/mermaid.min.js",
  ],
};

function loadScript(src) {
  return new Promise((res, rej) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = res;
    s.onerror = rej;
    document.head.appendChild(s);
  });
}

async function loadLibs() {
  for (const [name, urls] of Object.entries(CDN_SOURCES)) {
    if (window[name]) continue;
    for (const url of urls) {
      try {
        await loadScript(url);
        break;
      } catch { /* 换下一个 CDN */ }
    }
  }
  if (window.mermaid) {
    mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" });
  }
}

function renderMermaid(root) {
  if (!window.mermaid) return;
  const blocks = root.querySelectorAll("pre code.language-mermaid");
  if (!blocks.length) return;
  const nodes = [];
  blocks.forEach((c) => {
    const pre = c.closest("pre");
    pre.classList.add("mermaid-container");
    const div = document.createElement("div");
    div.className = "mermaid";
    div.textContent = c.textContent;
    pre.replaceWith(div);
    nodes.push(div);
  });
  mermaid.run({ nodes }).catch(() => {});
}


// ---------- 会话列表 ----------

// ---------- 会话多选删除 ----------
let multiMode = false;
let multiSelected = new Set();

$("#btn-multi-del").onclick = () => setMultiMode(!multiMode);
$("#btn-multi-cancel").onclick = () => setMultiMode(false);

/* ============================================================================
   Gili UI v4 —— 字号设置 / 学习项目顶栏绑定徽标
   ============================================================================ */

(function initFontScale() {
  const sel = $("#in-font-size");
  let scale = 1;
  try {
    const saved = parseFloat(localStorage.getItem("gili_font_scale"));
    if (Number.isFinite(saved) && saved >= 0.8 && saved <= 1.6) scale = saved;
  } catch { /* 忽略 */ }
  const apply = (v) => {
    document.documentElement.style.setProperty("--fs", String(v));
    try { localStorage.setItem("gili_font_scale", String(v)); } catch { /* 隐私模式 */ }
    if (sel && String(sel.value) !== String(v)) sel.value = String(v);
  };
  if (sel) {
    sel.value = String(scale);
    sel.onchange = () => apply(parseFloat(sel.value) || 1);
  }
  apply(scale);
})();

$("#btn-lp-badge").onclick = () => {
  if (!currentProject) return;
  sideMode = "learn";
  updateSidebarMode();
};

/* ============================================================================
   Gili UI v5 —— 知识引用 [n] / popover / 来源抽屉
   ============================================================================ */

let currentRefs = null;

function toggleMarkdown(el, btn) {
  const raw = el.dataset.raw;
  if (!raw) return;
  if (el.classList.contains("md")) {
    el.classList.remove("md");
    el.textContent = raw;
    btn.textContent = "渲染";
  } else if (renderMessageContent(el, raw)) {
    btn.textContent = "源码";
    const msg = el.closest(".msg");
    if (msg && msg._sources) renderCitations(el, msg._sources);
  }
}

function renderMessageContent(el, raw) {
  el.dataset.raw = raw;
  if (window.marked && window.DOMPurify) {
    try {
      const html = DOMPurify.sanitize(marked.parse(raw, { gfm: true, breaks: true }));
      el.classList.add("md");
      el.innerHTML = html;
      renderMermaid(el);
      enhanceCopyBlocks(el);
      return true;
    } catch { /* 降级纯文本 */ }
  }
  el.textContent = raw;
  return false;
}

let _streamRenderFrame = 0;
let _lastStreamPaint = 0;
function streamRenderMarkdown(el, raw) {
  // 流式期间只做轻量纯文本刷新，结束再一次性渲染 Markdown。
  // 每 token 都跑 marked + DOMPurify 会在长回复时把 WebView 卡死。
  el.dataset.raw = raw;
  const now = performance.now();
  if (now - _lastStreamPaint < 60) {
    if (!_streamRenderFrame) {
      _streamRenderFrame = requestAnimationFrame(() => {
        _streamRenderFrame = 0;
        _lastStreamPaint = performance.now();
        el.textContent = el.dataset.raw || "";
      });
    }
    return;
  }
  if (_streamRenderFrame) cancelAnimationFrame(_streamRenderFrame);
  _streamRenderFrame = 0;
  _lastStreamPaint = now;
  el.textContent = raw;
}

function flushStreamRender() {
  if (_streamRenderFrame) cancelAnimationFrame(_streamRenderFrame);
  _streamRenderFrame = 0;
}

function tableToTsv(table) {
  return [...table.querySelectorAll("tr")]
    .map((tr) =>
      [...tr.querySelectorAll("th, td")].map((c) => c.innerText.trim()).join("\t")
    )
    .join("\n");
}

function enhanceCopyBlocks(el) {
  if (!el) return;
  el.querySelectorAll("pre").forEach((pre) => {
    if (pre.closest(".code-block") || pre.closest(".mermaid-container")) return;
    const code = pre.querySelector("code");
    const lang = (code?.className.match(/language-([\w+-]+)/) || [])[1] || "code";
    const wrap = document.createElement("div");
    wrap.className = "code-block";
    const head = document.createElement("div");
    head.className = "code-head";
    head.innerHTML = `<span class="code-lang">${esc(lang)}</span>`;
    const btn = document.createElement("button");
    btn.className = "copy-btn";
    btn.textContent = "复制";
    btn.onclick = () => {
      copyText(pre.textContent);
      btn.textContent = "已复制";
      setTimeout(() => (btn.textContent = "复制"), 1200);
    };
    head.appendChild(btn);
    pre.parentNode.insertBefore(wrap, pre);
    wrap.appendChild(head);
    wrap.appendChild(pre);
  });
  el.querySelectorAll("table").forEach((table) => {
    if (table.closest(".table-wrap")) return;
    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    const head = document.createElement("div");
    head.className = "table-head";
    head.innerHTML = "<span>表格</span>";
    const btn = document.createElement("button");
    btn.className = "copy-btn";
    btn.textContent = "复制";
    btn.onclick = () => {
      copyText(tableToTsv(table));
      btn.textContent = "已复制";
      setTimeout(() => (btn.textContent = "复制"), 1200);
    };
    head.appendChild(btn);
    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(head);
    wrap.appendChild(table);
  });
}

function renderCitations(el, sources) {
  if (!el || !sources || !sources.length || !el.classList.contains("md")) return;
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => {
      const parent = node.parentElement;
      if (parent && parent.closest("pre, code")) return NodeFilter.FILTER_REJECT;
      return /\[\d+\]/.test(node.nodeValue || "") ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const text = node.nodeValue;
    const frag = document.createDocumentFragment();
    let last = 0;
    text.replace(/\[(\d+)\]/g, (match, num, offset) => {
      if (offset > last) frag.appendChild(document.createTextNode(text.slice(last, offset)));
      const ref = Number(num);
      const src = sources[ref - 1];
      if (src) {
        const b = document.createElement("button");
        b.className = "cite";
        b.textContent = `[${ref}]`;
        b.dataset.ref = String(ref);
        b.onclick = (e) => {
          e.stopPropagation();
          showCitePopover(src, b);
        };
        frag.appendChild(b);
      } else {
        frag.appendChild(document.createTextNode(match));
      }
      last = offset + match.length;
      return match;
    });
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  }
}

function showCitePopover(source, anchor) {
  const pop = $("#cite-popover");
  if (!pop) return;
  const typeLabel = sourceTypeLabel(source.source_type);
  pop.innerHTML = `
    <div class="cp-title">${typeLabel} ${esc(source.title || "来源")}${source.heading ? ` · ${esc(source.heading)}` : ""}</div>
    <div class="cp-excerpt">${esc(source.excerpt || "（无片段）")}</div>
    <div class="cp-actions">
      <button class="btn" data-act="open">查看原文</button>
      <button class="btn ghost" data-act="close">关闭</button>
    </div>`;
  pop.classList.remove("hidden");
  pop.querySelector('[data-act="close"]').onclick = () => pop.classList.add("hidden");
  pop.querySelector('[data-act="open"]').onclick = () => {
    pop.classList.add("hidden");
    openRefSource(source);
  };
  const rect = anchor.getBoundingClientRect();
  const popRect = pop.getBoundingClientRect();
  let left = Math.min(rect.left, window.innerWidth - popRect.width - 12);
  let top = rect.bottom + 6;
  if (top + popRect.height > window.innerHeight - 12) {
    top = Math.max(12, rect.top - popRect.height - 6);
  }
  pop.style.left = `${Math.max(12, left)}px`;
  pop.style.top = `${top}px`;
}

function sourceTypeLabel(type) {
  return {
    note: "📖 学习文档",
    memory: "🧠 记忆",
    chapter: "📚 章节",
    question: "❓ 疑问",
    kb: "📚 知识库",
  }[type] || "🔗 来源";
}

function updateRefsButton(sources) {
  const btn = $("#btn-refs");
  if (!btn) return;
  currentRefs = sources && sources.length ? sources : null;
  if (!currentRefs) {
    btn.classList.add("hidden");
    btn.innerHTML = "🔗 来源";
    return;
  }
  btn.classList.remove("hidden");
  btn.innerHTML = `🔗 来源 <span class="count">${currentRefs.length}</span>`;
}

function renderRefsDrawer() {
  const list = $("#refs-list");
  if (!list || !currentRefs) return;
  $("#refs-count").textContent = `${currentRefs.length} 个来源 · 点击跳转原文`;
  list.innerHTML = "";
  for (const s of currentRefs) {
    const item = document.createElement("div");
    item.className = "ref-item";
    item.innerHTML = `
      <div class="ref-item-head">
        <span class="idx">${s.ref}</span>
        <span class="ref-item-title">${esc(s.title || "来源")}${s.heading ? ` · ${esc(s.heading)}` : ""}</span>
        <span class="type">${sourceTypeLabel(s.source_type)}</span>
      </div>
      <div class="excerpt">${esc(s.excerpt || "")}</div>
      <div class="score">相关度 ${Math.round((s.score ?? 0) * 100)}</div>`;
    item.onclick = () => {
      $("#refs-drawer").classList.add("hidden");
      openRefSource(s);
    };
    list.appendChild(item);
  }
  if (window.GiliUI) GiliUI.stagger(list);
}

async function openRefSource(source) {
  if (!source) return;
  if (source.source_type === "memory") {
    const tab = document.querySelector('.tab[data-tab="memory"]');
    if (tab) tab.click();
    return;
  }
  if (!source.project_id) return;
  if (!currentProject || currentProject.id !== source.project_id) {
    await loadLearningProjects();
    currentProject =
      (learningProjects || []).find((p) => p.id === source.project_id) || null;
    if (!currentProject) return toast("该学习项目已不存在");
    lpDetail = null;
  }
  const title = (source.title || "").toLowerCase();
  if (source.source_type === "chapter") lpTab = "chapters";
  else if (source.source_type === "question") lpTab = "questions";
  else if (title.includes("大纲")) lpTab = "outline";
  else if (title.includes("疑问")) lpTab = "questions";
  else lpTab = "notes";
  sideMode = "learn";
  updateSidebarMode();
}

$("#btn-refs").onclick = () => {
  if (!currentRefs) return;
  renderRefsDrawer();
  $("#refs-drawer").classList.remove("hidden");
};
$("#btn-close-refs").onclick = () => $("#refs-drawer").classList.add("hidden");
document.addEventListener("click", (e) => {
  const pop = $("#cite-popover");
  if (pop && !pop.classList.contains("hidden") && !e.target.closest("#cite-popover, .cite")) {
    pop.classList.add("hidden");
  }
});

/* ---------- 记忆共享：多选连接 ---------- */

function updateMultiCount() {
  $("#multi-count").textContent = `已选 ${multiSelected.size} 项`;
  const link = $("#btn-multi-link");
  if (link) {
    link.disabled = multiSelected.size < 2;
    link.title = multiSelected.size < 2 ? "至少勾选两个对话" : "把选中对话连成共享记忆组";
  }
}

$("#btn-multi-link").onclick = async () => {
  if (!multiMode || multiSelected.size < 2) return toast("至少勾选两个对话");
  try {
    const r = await api("/api/conversations/link-memory", {
      method: "POST",
      body: JSON.stringify({ ids: [...multiSelected] }),
    });
    toast(`已把 ${r.count} 个对话连成共享记忆组`);
    setMultiMode(false);
    loadConversations();
  } catch (e) {
    toast("连接失败: " + e.message);
  }
};

$("#btn-multi-more").onclick = (e) => {
  e.stopPropagation();
  $("#multi-menu").classList.toggle("hidden");
};
document.addEventListener("click", (e) => {
  if (!e.target.closest("#multi-bar")) {
    const menu = $("#multi-menu");
    if (menu) menu.classList.add("hidden");
  }
});

/* ---------- 气泡操作菜单（复制 / 重新生成 / 编辑 / 删除） ---------- */


async function deleteMessageBubble(el) {
  if (!el.dataset.msgId) return toast("消息还没保存，无法删除");
  if (!confirm("删除这条消息（含全部版本）？后续消息会保留。")) return;
  try {
    await api(`/api/messages/${el.dataset.msgId}`, { method: "DELETE" });
    toast("消息已删除");
    await reloadCurrentConv();
  } catch (e) {
    toast("删除失败: " + e.message);
  }
}


/* ---------- 学习对话快捷命令 chips ---------- */

$$("#learn-tools .learn-chip").forEach((b) => {
  b.onclick = () => {
    const cmd = b.dataset.cmd;
    const input = $("#input");
    input.value = cmd;
    autoResizeInput();
    if (cmd === "记一下：") {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    } else {
      sendMessage();
    }
  };
});

/* ---------- 更多技能市场 ---------- */

$("#btn-more-skills").onclick = () => {
  $("#skills-market-modal").classList.remove("hidden");
  const grid = $("#market-grid");
  if (marketItems.length) {
    renderMarketCards(marketItems); // 先用缓存即时显示，不阻塞
  } else if (grid) {
    grid.innerHTML = '<div class="studio-side-empty ui-shimmer">加载技能目录…</div>';
  }
  renderSkillsMarket();
};
$("#btn-close-market").onclick = () => $("#skills-market-modal").classList.add("hidden");
$("#skills-market-modal").onclick = (e) => {
  if (e.target === e.currentTarget) e.currentTarget.classList.add("hidden");
};

let marketItems = [];
let marketSearch = "";
$("#in-market-search").oninput = () => {
  marketSearch = $("#in-market-search").value;
  $("#market-search").classList.toggle("has-value", !!marketSearch);
  renderMarketCards(marketItems);
};
$("#btn-market-clear").onclick = () => {
  marketSearch = "";
  $("#in-market-search").value = "";
  $("#market-search").classList.remove("has-value");
  renderMarketCards(marketItems);
};

function filteredMarketItems(items) {
  const list = Array.isArray(items) ? items : marketItems;
  const q = marketSearch.trim().toLowerCase();
  if (!q) return list;
  return list.filter((s) =>
    [s.id, s.name, s.desc, s.repo || ""].join(" ").toLowerCase().includes(q)
  );
}

async function renderSkillsMarket() {
  const grid = $("#market-grid");
  if (!grid) return;
  if (!marketItems.length) {
    grid.innerHTML = '<div class="studio-side-empty ui-shimmer">加载技能目录…</div>';
  }
  try {
    const r = await api("/api/skills/market");
    marketItems = r.items || [];
    renderMarketCards(marketItems);
  } catch (e) {
    grid.innerHTML = `<div class="studio-side-empty">加载失败：${esc(e.message)}</div>`;
  }
}

function renderMarketCards(items) {
  const grid = $("#market-grid");
  if (!grid) return;
  const visible = filteredMarketItems(items);
  if (!visible.length) {
    grid.innerHTML = `<div class="studio-side-empty">没有匹配「${esc(marketSearch)}」的技能</div>`;
    return;
  }
  grid.innerHTML = "";
  for (const s of visible) {
      const el = document.createElement("div");
      el.className = "market-card";
      el.innerHTML = `
        <div class="market-icon">${esc(s.icon || "✨")}</div>
        <div class="market-body">
          <div class="market-name">${esc(s.name)}
            <span class="badge market-badge ${s.source === "builtin" ? "builtin" : "github"}">${s.source === "builtin" ? "内置" : "GitHub"}</span>
          </div>
          <div class="market-desc">${esc(s.desc)}</div>
          ${s.repo ? `<div class="market-repo">仓库：${esc(s.repo)}</div>` : ""}
        </div>
        ${s.installed
          ? '<button class="btn small market-uninstall">卸载</button>'
          : '<button class="btn small market-install">安装</button>'}`;
      if (s.installed) {
        const ub = el.querySelector(".market-uninstall");
        ub.onclick = async () => {
          if (!ub.dataset.armed) {
            ub.dataset.armed = "1";
            ub.textContent = "再点确认";
            setTimeout(() => {
              if (ub.isConnected) {
                ub.dataset.armed = "";
                ub.textContent = "卸载";
              }
            }, 2600);
            return;
          }
          ub.dataset.armed = "";
          ub.textContent = "卸载中…";
          try {
            await api("/api/skills/uninstall", {
              method: "POST",
              body: JSON.stringify({ id: s.id }),
            });
            toast(`已卸载「${s.name}」`);
            await renderSkills();
            await renderSkillsMarket();
          } catch (e) {
            toast("卸载失败: " + e.message);
            ub.textContent = "卸载";
          }
        };
      } else {
        const btn = el.querySelector(".market-install");
        btn.onclick = async () => {
          btn.disabled = true;
          const wrap = document.createElement("div");
          wrap.className = "install-progress";
          wrap.innerHTML =
            '<div class="install-bar"><i></i></div><span class="install-text">连接镜像…</span>';
          btn.replaceWith(wrap);
          let pct = 4;
          const timer = setInterval(() => {
            const stage = pct < 30 ? "下载 SKILL.md" : pct < 65 ? "解析元数据" : "写入本地";
            pct = Math.min(88, pct + 3 + Math.random() * 6);
            wrap.querySelector(".install-text").textContent = stage;
            wrap.querySelector(".install-bar i").style.width = `${pct}%`;
          }, 260);
          try {
            const x = await api("/api/skills/install", {
              method: "POST",
              body: JSON.stringify({ id: s.id }),
            });
            clearInterval(timer);
            wrap.querySelector(".install-bar i").style.width = "100%";
            wrap.querySelector(".install-text").textContent = "完成";
            toast(`已安装「${x.name}」${x.fallback ? "（镜像失败，已用内置精简版）" : ""}`);
            await renderSkills();
            await renderSkillsMarket();
          } catch (e) {
            clearInterval(timer);
            toast("安装失败: " + e.message);
            await renderSkillsMarket();
          }
        };
      }
      grid.appendChild(el);
  }
}

/* ---------- 技能面板：缓存 + 骨架屏，避免每次点击空白重载 ---------- */

let skillsCache = null;


// 时间格式化：今天/昨天显示相对时间，今年显示 M-D HH:MM，更早显示 YY-M-D
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(String(ts).replace(" ", "T"));
  if (isNaN(d.getTime())) return ts;
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (sameDay(d, now)) return `今天 ${hm}`;
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (sameDay(d, yesterday)) return `昨天 ${hm}`;
  const md = `${d.getMonth() + 1}-${d.getDate()}`;
  if (d.getFullYear() === now.getFullYear()) return `${md} ${hm}`;
  return `${String(d.getFullYear()).slice(2)}-${md}`;
}


let currentFav = false;


// ---------- 聊天区 ----------

function updateEmptyState() {
  const empty = $("#chat-empty");
  const has = !!$("#messages").querySelector(".msg");
  if (empty) empty.style.display = has ? "none" : "flex";
  // 空态关闭滚动条（否则高 DPI 下会出现幽灵滚动条）
  $("#messages").style.overflowY = has ? "auto" : "hidden";
}

// 是否靠近底部（<100px）：流式期间只有靠近底部才自动滚动，
// 否则用户上翻看历史会被拽回去
function isNearBottom() {
  const el = $("#messages");
  return el.scrollHeight - el.scrollTop - el.clientHeight < 100;
}
function scrollToBottom(force = false) {
  if (force || isNearBottom()) {
    $("#messages").scrollTop = $("#messages").scrollHeight;
  }
}

let _scrollFrame = 0;
function scheduleScrollToBottom() {
  // 每个 token 都强制布局 + scrollTop 会很卡；合并到同一个动画帧。
  if (_scrollFrame) return;
  _scrollFrame = requestAnimationFrame(() => {
    _scrollFrame = 0;
    scrollToBottom();
  });
}

function copyText(text) {
  const done = () => toast("已复制");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}
function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); } catch { /* 忽略 */ }
  ta.remove();
  done();
}

// 思考链：折叠条 + 内容块（流式/历史加载共用）

// 版本切换条（v2/2 ◀ ▶）
function addVersionBar(el, versionNo, count) {
  if (!count || count < 2) return;
  const bar = document.createElement("div");
  bar.className = "version-bar";
  bar.innerHTML = `<button data-dir="-1">◀</button><span>v${versionNo}/${count}</span><button data-dir="1">▶</button>`;
  bar.dataset.vno = versionNo;
  const span = bar.querySelector("span");
  bar.querySelectorAll("button").forEach((b) => {
    b.onclick = async () => {
      const cur = parseInt(bar.dataset.vno);
      const next = Math.min(count, Math.max(1, cur + parseInt(b.dataset.dir)));
      if (next === cur) return;
      // 组内其他版本节点已在 DOM（刚重新生成过）→ 直接交换淡显，零请求
      const target = document.querySelector(
        `.msg[data-group="${el.dataset.group}"][data-vno="${next}"]`
      );
      if (target) {
        el.classList.add("stale");
        target.classList.remove("stale");
      } else {
        // 从库加载版本 → 渲染进当前节点
        try {
          const r = await api(`/api/messages/${el.dataset.msgId}/versions`);
          const v = r.items.find((x) => x.version_no === next);
          if (!v) return;
          renderMessageContent(el.querySelector(".content"), v.content);
          const oldBox = el.querySelector(".reasoning");
          const oldToggle = el.querySelector(".reasoning-toggle");
          if (oldBox) oldBox.remove();
          if (oldToggle) oldToggle.remove();
          if (v.reasoning) addReasoningBlock(el, v.reasoning);
          el.classList.toggle("stale", next !== parseInt(bar.dataset.vno));
        } catch (e) {
          toast("切换版本失败: " + e.message);
          return;
        }
      }
      bar.dataset.vno = next;
      span.textContent = `v${next}/${count}`;
    };
  });
  el.insertBefore(bar, el.querySelector(".content"));
}

// inline 编辑（用户消息）
function startEdit(el, contentDiv) {
  if (!el.dataset.msgId) return toast("消息还没保存，稍后再编辑");
  const raw = contentDiv.dataset.raw || contentDiv.textContent;
  contentDiv.style.display = "none";
  const box = document.createElement("div");
  box.className = "edit-box";
  box.innerHTML = `<textarea rows="4"></textarea><div class="edit-actions"><button data-act="save">保存</button><button data-act="cancel">取消</button></div>`;
  box.querySelector("textarea").value = raw;
  box.querySelector('[data-act="cancel"]').onclick = () => {
    box.remove();
    contentDiv.style.display = "";
  };
  box.querySelector('[data-act="save"]').onclick = async () => {
    const v = box.querySelector("textarea").value.trim();
    if (!v) return toast("内容不能为空");
    try {
      await api(`/api/messages/${el.dataset.msgId}`, {
        method: "PUT",
        body: JSON.stringify({ content: v }),
      });
      contentDiv.style.display = "";
      renderMessageContent(contentDiv, v);
      box.remove();
      const tg = el.querySelector(".md-toggle");
      if (tg) tg.textContent = "源码";
      toast("已保存修改（历史已变，下轮请求缓存会失效，正常）");
    } catch (e) {
      toast("保存失败: " + e.message);
    }
  };
  contentDiv.after(box);
  box.querySelector("textarea").focus();
}


function collectParams() {
  return {
    params: {
      model: $("#sel-model").value,
      thinking_enabled: $("#in-thinking").checked,
      reasoning_effort: $("#in-effort").value,
      temperature: parseFloat($("#in-temperature").value),
      top_p: parseFloat($("#in-top_p").value),
      max_tokens: parseInt($("#in-max_tokens").value),
      presence_penalty: parseFloat($("#in-presence_penalty").value),
      frequency_penalty: parseFloat($("#in-frequency_penalty").value),
      stream: $("#in-stream").checked,
    },
    memory: {
      history_token_budget: parseInt($("#in-history_budget").value),
      summary_rewrite_threshold: parseInt($("#in-summary_threshold").value),
      enable_memory_recall: $("#in-recall").checked,
      enable_kb_recall: $("#in-kb-recall").checked,
      enable_rerank: $("#in-rerank").checked,
    },
    attachments,
  };
}

// ---------- 输入工具栏：文件上传 + 记忆进度条 ----------

let attachments = [];

$("#btn-attach").onclick = () => $("#in-attach").click();
$("#in-attach").onchange = async (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  for (const f of files) {
    const fd = new FormData();
    fd.append("file", f);
    try {
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      const r = await res.json();
      if (!r.ok) {
        toast(`「${f.name}」：${r.detail || "上传失败"}`);
        continue;
      }
      attachments.push(r.file_id);
      renderAttachChips();
      toast(`已添加附件「${r.name}」（${r.tokens} tokens）`);
    } catch (err) {
      toast("上传失败: " + err.message);
    }
  }
};

function renderAttachChips() {
  const box = $("#attach-list");
  box.innerHTML = "";
  attachments.forEach((fid) => {
    const chip = document.createElement("span");
    chip.className = "attach-chip";
    chip.textContent = "📄 " + fid.slice(0, 6) + "…";
    const rm = document.createElement("button");
    rm.textContent = "✕";
    rm.title = "移除";
    rm.onclick = () => {
      attachments = attachments.filter((x) => x !== fid);
      renderAttachChips();
    };
    chip.appendChild(rm);
    box.appendChild(chip);
  });
}

async function refreshMemoryBar() {
  try {
    const budget = parseInt($("#in-history_budget").value) || 12000;
    const cid = currentConv || "";
    const u = await api(
      `/api/memory/usage?conversation_id=${encodeURIComponent(cid)}&budget=${budget}`
    );
    const pct = (x) => Math.min(100, Math.round((x / u.budget) * 100));
    $("#mem-stable").style.width = pct(u.stable) + "%";
    $("#mem-lowfreq").style.width = pct(u.lowfreq) + "%";
    $("#mem-volatile").style.width = pct(u.volatile) + "%";
    $("#mem-label").textContent = `${(u.used / 1000).toFixed(1)}k / ${(u.budget / 1000).toFixed(1)}k`;
    window._memUsage = u;
  } catch { /* 静默失败，不打断聊天 */ }
}

async function openMemUsageModal() {
  if (!window._memUsage) await refreshMemoryBar();
  if (!window._memUsage) { toast("记忆用量加载失败，请稍后重试"); return; }
  const u = window._memUsage;
  const pct = (x) => Math.min(100, Math.round((x / (u.budget || 1)) * 100));
  $("#mu-stable").style.width = pct(u.stable) + "%";
  $("#mu-lowfreq").style.width = pct(u.lowfreq) + "%";
  $("#mu-volatile").style.width = pct(u.volatile) + "%";
  $("#mu-stable-t").textContent = u.stable.toLocaleString();
  $("#mu-lowfreq-t").textContent = u.lowfreq.toLocaleString();
  $("#mu-volatile-t").textContent = u.volatile.toLocaleString();
  $("#mu-used").textContent = u.used.toLocaleString();
  $("#mu-budget").textContent = u.budget.toLocaleString();
  $("#mu-soul").textContent = u.detail.soul_tokens.toLocaleString();
  $("#mu-skills").textContent = u.detail.skill_fragments;
  $("#mu-summary").textContent = u.detail.summary_tokens.toLocaleString();
  $("#mu-facts").textContent = u.detail.fixed_facts + u.detail.retrieved_facts;
  $("#mu-hmsgs").textContent = u.detail.history_msgs;
  $("#mu-htok").textContent = u.detail.history_tokens.toLocaleString();
  $("#mu-msgs").textContent = u.store.messages.toLocaleString();
  $("#mu-items").textContent = u.store.memory_items.toLocaleString();
  $("#mu-db").textContent = u.store.db_kb;
  $("#memusage-modal").classList.remove("hidden");
}
$("#btn-mem-usage").onclick = openMemUsageModal;
$("#btn-close-memusage").onclick = () => $("#memusage-modal").classList.add("hidden");
$("#memusage-modal").onclick = (e) => {
  if (e.target === e.currentTarget) e.currentTarget.classList.add("hidden");
};

async function sendMessage() {
  const text = $("#input").value.trim();
  if (!text || streaming) return;
  $("#input").value = "";
  autoResizeInput();
  await sendChat(text, null);
}

// 统一发送管道：正常对话 / 重新生成（regenId 指定目标 AI 消息）

function setSendState() {
  $("#btn-send").disabled = streaming;
  $("#btn-send").textContent = streaming ? "生成中…" : "发送";
}

const input = $("#input");
function autoResizeInput() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 140) + "px";
}
input.addEventListener("input", autoResizeInput);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
$("#btn-send").onclick = sendMessage;

$("#btn-fav").onclick = async () => {
  if (!currentConv) return toast("先打开或新建一个对话");
  try {
    const r = await api(`/api/conversations/${currentConv}/favorite`, {
      method: "POST",
      body: JSON.stringify({ favorite: !currentFav }),
    });
    currentFav = r.favorite;
    $("#btn-fav").textContent = currentFav ? "★ 已收藏" : "☆ 收藏";
    toast(currentFav ? "已收藏，随时可在列表里找到 ⭐" : "已取消收藏");
    loadConversations();
  } catch (e) {
    toast("操作失败: " + e.message);
  }
};

$("#btn-export").onclick = async () => {
  if (!currentConv) return toast("先打开或新建一个对话");
  try {
    const r = await api(`/api/conversations/${currentConv}/export`, { method: "POST" });
    toast(r.ok ? `已导出：${r.name}（exports/ 目录）` : r.detail || "导出失败");
  } catch (e) {
    toast("导出失败: " + e.message);
  }
};

$("#btn-clear").onclick = () => {
  $("#messages").innerHTML = "";
  updateEmptyState();
  toast("已清空当前视图（历史消息仍在数据库，重开会话可恢复）");
};

// ---------- 设置 & 技能 ----------

let settingsCache = null;

async function loadSettings() {
  try {
    settingsCache = await api("/api/settings");
    updateMode();
  } catch (e) {
    toast("设置加载失败: " + e.message);
  }
}

function updateMode() {
  const hasKey = settingsCache && settingsCache.api_key;
  const dot = $("#status-dot"), txt = $("#status-text");
  if (hasKey) {
    dot.className = "dot ok";
    txt.textContent = "真实模式 · DeepSeek";
  } else {
    dot.className = "dot";
    txt.textContent = "原型模式 · 未配置 Key";
  }
}

$("#btn-settings").onclick = () => {
  const s = settingsCache || {};
  // 已保存的 key 显示为卡片；未保存空输入框
  if (s.api_key) {
    $("#key-card").classList.remove("hidden");
    $("#key-card .key-mask").textContent = s.api_key;
    $("#in-key").value = "";
  } else {
    $("#key-card").classList.add("hidden");
    $("#in-key").value = "";
  }
  $("#in-baseurl").value = s.base_url || "";
  $("#in-defmodel").value = s.default_model || "deepseek-v4-flash";
  $("#in-soul").value = s.soul || "";
  $("#in-search-provider").value = s.search_provider || "duckduckgo";
  $("#in-tavily-key").value = s.tavily_api_key || "";
  $("#test-result").textContent = "";
  $("#settings-modal").classList.remove("hidden");
};

// Key 输入框：不清空（保留上次输入），用户手动覆盖即可

let clearApiKey = false;
$("#btn-apply-key").onclick = async () => {
  try {
    const r = await api("/api/settings/reveal-key", {
      method: "POST",
      body: JSON.stringify({ field: "api_key" }),
    });
    $("#in-key").value = r.value || "";
    clearApiKey = false;
    toast("已解密并填入，可编辑或测试连接");
  } catch (e) {
    toast("应用失败: " + e.message);
  }
};
$("#btn-clear-key").onclick = () => {
  clearApiKey = true;
  $("#in-key").value = "";
  $("#key-card").classList.add("hidden");
};

$("#btn-close-settings").onclick = () => $("#settings-modal").classList.add("hidden");
$("#learning-center-modal").onclick = (e) => {
  if (e.target === e.currentTarget) e.currentTarget.classList.add("hidden");
};

// ---------- 参数说明书 ----------

$("#btn-param-help").onclick = () => {
  const val = (id) => $(id).value;
  $("#help-thinking").textContent = $("#in-thinking").checked ? "开" : "关";
  $("#help-effort").textContent = val("#in-effort");
  $("#help-temperature").textContent = Number(val("#in-temperature")).toFixed(1);
  $("#help-top_p").textContent = Number(val("#in-top_p")).toFixed(2);
  $("#help-max_tokens").textContent = Number(val("#in-max_tokens")).toLocaleString();
  $("#help-presence").textContent = Number(val("#in-presence_penalty")).toFixed(1);
  $("#help-frequency").textContent = Number(val("#in-frequency_penalty")).toFixed(1);
  $("#help-stream").textContent = $("#in-stream").checked ? "开" : "关";
  const preset = $("#in-context-preset");
  $("#help-preset").textContent = preset.value
    ? preset.options[preset.selectedIndex].text.split(" · ")[0]
    : "自定义";
  $("#help-budget").textContent = Number(val("#in-history_budget")).toLocaleString();
  $("#help-threshold").textContent = Number(val("#in-summary_threshold")).toLocaleString();
  $("#help-recall").textContent = $("#in-recall").checked ? "开" : "关";
  $("#help-modal").classList.remove("hidden");
};

$("#btn-close-help").onclick = () => $("#help-modal").classList.add("hidden");
$("#help-modal").onclick = (e) => {
  if (e.target === e.currentTarget) e.currentTarget.classList.add("hidden");
};
$("#settings-modal").onclick = (e) => {
  if (e.target === e.currentTarget) e.currentTarget.classList.add("hidden");
};

$("#btn-save-settings").onclick = async () => {
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        api_key: clearApiKey ? "__clear__" : $("#in-key").value.trim(),
        base_url: $("#in-baseurl").value.trim(),
        default_model: $("#in-defmodel").value,
        soul: $("#in-soul").value,
        search_provider: $("#in-search-provider").value,
        tavily_api_key: $("#in-tavily-key").value.trim(),
      }),
    });
    toast("设置已保存");
    $("#settings-modal").classList.add("hidden");
    await loadSettings();
    await renderSkills();
  } catch (e) {
    toast("保存失败: " + e.message);
  }
};

$("#btn-test").onclick = async () => {
  const box = $("#test-result");
  box.textContent = "测试中…";
  try {
    const r = await api("/api/settings/test", {
      method: "POST",
      body: JSON.stringify({
        api_key: $("#in-key").value.trim(),
        base_url: $("#in-baseurl").value.trim(),
      }),
    });
    box.textContent = r.ok
      ? "连接成功 ✓ 可用模型: " + r.models.join(", ")
      : "连接失败: " + r.error;
  } catch (e) {
    box.textContent = "请求失败: " + e.message;
  }
};

async function renderSkills() {
  try {
    const cfg = await api("/api/config");
    const on = (settingsCache && settingsCache.skills) || {};
    const list = $("#skill-list");
    list.innerHTML = "";
    for (const sk of cfg.skills) {
      const el = document.createElement("div");
      el.className = "skill-item" + (sk.future ? " future" : "");
      el.innerHTML = `
        <div class="skill-head">
          <span class="skill-name">${sk.name}</span>
          <label class="switch"><input type="checkbox" data-skill="${sk.id}"
            ${on[sk.id] ? "checked" : ""} ${sk.future ? "disabled" : ""}>
            <span class="slider"></span></label>
        </div>
        <div class="skill-desc">${sk.desc}</div>`;
      el.querySelector("input").onchange = async (e) => {
        const state = { ...((settingsCache && settingsCache.skills) || {}), [sk.id]: e.target.checked };
        try {
          await api("/api/settings", { method: "PUT", body: JSON.stringify({ skills: state }) });
          toast(`${sk.name} 已${e.target.checked ? "开启" : "关闭"}（稳定区已变化，缓存将失效）`);
          settingsCache.skills = state;
        } catch (err) {
          toast("保存失败: " + err.message);
        }
      };
      list.appendChild(el);
    }
  } catch (e) {
    toast("技能列表加载失败: " + e.message);
  }
}

// ---------- 参数面板 ----------

const PARAMS_STORAGE_KEY = "gili-prototype-params";
const DEFAULT_PARAMS = {
  params: {
    model: "deepseek-v4-flash",
    thinking_enabled: true,
    reasoning_effort: "high",
    temperature: 1.0,
    top_p: 1.0,
    max_tokens: 4096,
    presence_penalty: 0,
    frequency_penalty: 0,
    stream: true,
  },
  memory: {
    history_token_budget: 12000,
    summary_rewrite_threshold: 16000,
    enable_memory_recall: true,
    enable_kb_recall: true,
    enable_rerank: false,
  },
};

function bindRange(id, formatter = (v) => v) {
  const el = $(`#in-${id}`);
  const val = $(`#val-${id}`);
  if (!el || !val) return;
  const update = () => (val.textContent = formatter(el.value));
  el.addEventListener("input", update);
  update();
}

function setParamsStatus(state, text) {
  const box = $("#params-status");
  const label = $("#params-status-text");
  if (box) box.dataset.state = state;
  if (label) label.textContent = text;
}

function syncThinkingDependent() {
  const panel = $("#tab-params");
  const locked = panel && panel.classList.contains("locked");
  const effort = $("#in-effort");
  const thinking = $("#in-thinking");
  if (effort) effort.disabled = !!locked || !(thinking && thinking.checked);
}

function setParamsLocked(locked) {
  const panel = $("#tab-params");
  if (!panel) return;
  panel.classList.toggle("locked", locked);
  panel.querySelectorAll("input, select").forEach((el) => {
    el.disabled = locked;
  });

  const saveBtn = $("#btn-save-cfg");
  const editBtn = $("#btn-edit-cfg");
  if (saveBtn) {
    saveBtn.disabled = locked;
    saveBtn.textContent = locked ? "已保存 ✓" : "保存配置";
  }
  if (editBtn) editBtn.classList.toggle("hidden", !locked);
  syncThinkingDependent();
}

function applyParamsConfig(cfg, dispatch = true) {
  const params = { ...DEFAULT_PARAMS.params, ...(cfg?.params || {}) };
  const memory = { ...DEFAULT_PARAMS.memory, ...(cfg?.memory || {}) };

  const setSelect = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = [...el.options].some((o) => o.value === value)
      ? value
      : el.options[0]?.value || "";
    if (dispatch) el.dispatchEvent(new Event("change"));
  };
  const setValue = (id, value, evt = "input") => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = value;
    if (dispatch) el.dispatchEvent(new Event(evt));
  };
  const setChecked = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.checked = !!value;
    if (dispatch) el.dispatchEvent(new Event("change"));
  };

  setSelect("sel-model", params.model);
  setChecked("in-thinking", params.thinking_enabled);
  setSelect("in-effort", params.reasoning_effort);
  setValue("in-temperature", params.temperature);
  setValue("in-top_p", params.top_p);
  setValue("in-max_tokens", params.max_tokens);
  setValue("in-presence_penalty", params.presence_penalty);
  setValue("in-frequency_penalty", params.frequency_penalty);
  setChecked("in-stream", params.stream);
  setValue("in-history_budget", memory.history_token_budget);
  setValue("in-summary_threshold", memory.summary_rewrite_threshold);
  setChecked("in-recall", memory.enable_memory_recall);
  setChecked("in-kb-recall", memory.enable_kb_recall);
  setChecked("in-rerank", memory.enable_rerank);

  matchPreset();
  syncThinkingDependent();
}

function restoreParams() {
  let cfg = null;
  try {
    const raw = localStorage.getItem(PARAMS_STORAGE_KEY);
    if (raw) cfg = JSON.parse(raw);
  } catch {
    cfg = null;
  }

  if (!cfg || typeof cfg !== "object" || (!cfg.params && !cfg.memory)) {
    applyParamsConfig(DEFAULT_PARAMS, false);
    setParamsLocked(false);
    setParamsStatus("dirty", "未保存");
    return;
  }

  applyParamsConfig(cfg, false);
  setParamsLocked(true);
  setParamsStatus("saved", "已保存 · 只读");
}

function saveParamsConfig() {
  const cfg = collectParams();
  const saved = {
    params: cfg.params,
    memory: cfg.memory,
    savedAt: new Date().toISOString(),
  };
  try {
    localStorage.setItem(PARAMS_STORAGE_KEY, JSON.stringify(saved));
  } catch (err) {
    toast(`保存失败: ${err.message}`);
    return;
  }
  setParamsLocked(true);
  setParamsStatus("saved", "已保存 · 只读");
  toast("配置已保存，参数面板已锁定");
}

function resetParamsConfig() {
  applyParamsConfig(DEFAULT_PARAMS);
  try {
    localStorage.removeItem(PARAMS_STORAGE_KEY);
  } catch { /* 隐私模式下忽略 */ }
  setParamsLocked(false);
  setParamsStatus("dirty", "未保存");
  toast("参数已重置为默认值");
}

$("#in-thinking").addEventListener("change", (e) => {
  syncThinkingDependent();
});
bindRange("temperature", (v) => Number(v).toFixed(1));
bindRange("top_p", (v) => Number(v).toFixed(2));
bindRange("presence_penalty", (v) => Number(v).toFixed(1));
bindRange("frequency_penalty", (v) => Number(v).toFixed(1));
syncThinkingDependent();

// 未锁定时，任意参数变化都显示“未保存”，避免误以为修改已生效到下次启动
$("#tab-params").addEventListener("input", (e) => {
  if (!e.target.matches("input, select")) return;
  if ($("#tab-params").classList.contains("locked")) return;
  setParamsStatus("dirty", "未保存");
});
$("#tab-params").addEventListener("change", (e) => {
  if (!e.target.matches("input, select")) return;
  if ($("#tab-params").classList.contains("locked")) return;
  setParamsStatus("dirty", "未保存");
});

// 上下文档位预设：一键设定 历史预算 + 摘要阈值
const CONTEXT_PRESETS = {
  light: { history: 8000, summary: 10000 },
  standard: { history: 12000, summary: 16000 },
  deep: { history: 32000, summary: 40000 },
  extreme: { history: 100000, summary: 110000 },
};
function matchPreset() {
  const h = parseInt($("#in-history_budget").value);
  const s = parseInt($("#in-summary_threshold").value);
  for (const [k, v] of Object.entries(CONTEXT_PRESETS)) {
    if (v.history === h && v.summary === s) {
      $("#in-context-preset").value = k;
      return;
    }
  }
  $("#in-context-preset").value = ""; // 手动微调过 → 显示"自定义"
}
["in-history_budget", "in-summary_threshold"].forEach((id) => {
  document.getElementById(id).addEventListener("input", matchPreset);
});
$("#in-context-preset").onchange = (e) => {
  const p = CONTEXT_PRESETS[e.target.value];
  if (!p) return;
  $("#in-history_budget").value = p.history;
  $("#in-summary_threshold").value = p.summary;
  $("#in-history_budget").dispatchEvent(new Event("input"));
  $("#in-summary_threshold").dispatchEvent(new Event("input"));
  toast(`已切换「${e.target.options[e.target.selectedIndex].text}」档位`);
};

$("#btn-save-cfg").onclick = saveParamsConfig;
$("#btn-edit-cfg").onclick = () => {
  setParamsLocked(false);
  setParamsStatus("dirty", "未保存");
  toast("参数面板已解锁，修改后记得保存");
};
$("#btn-reset-cfg").onclick = resetParamsConfig;

// ---------- Tab 切换 ----------

$$(".tab").forEach((tab) => {
  tab.onclick = () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".tab-body").forEach((b) => b.classList.remove("active"));
    tab.classList.add("active");
    const body = $(`#tab-${tab.dataset.tab}`);
    if (body) body.classList.add("active");
    if (tab.dataset.tab === "data") refreshMetrics();
  };
});

// ---------- 记忆面板 ----------

const LAYER_NAMES = { core: "核心", working: "工作", longterm: "长期" };

function renderMemoryItem(box, it) {
  const isSummary = it.kind === "summary";
  const el = document.createElement("div");
  el.className = "mem-item" + (isSummary ? " summary" : "");
  const imp = it.importance || 1;
  const stars = "★".repeat(Math.min(imp, 3));
  el.innerHTML = `
    <div class="mem-item-text">${esc(it.content)}</div>
    <div class="mem-item-meta">
      <span class="mem-kind">${isSummary ? "📄 摘要" : "📌 事实"}</span>
      ${stars ? `<span class="mem-stars" title="重要性 ${imp}/3">${stars}</span>` : ""}
      <span class="mem-tokens">${it.tokens} tok</span>
      <span class="mem-time">${(it.created_at || "").slice(5, 16) || "—"}</span>
    </div>
    <div class="mem-item-ops">
      <button data-op="edit" title="编辑">✏️</button>
      <button data-op="del" title="删除">🗑</button>
    </div>`;
  el.querySelector('[data-op="edit"]').onclick = () => {
    const textEl = el.querySelector(".mem-item-text");
    const ops = el.querySelector(".mem-item-ops");
    const ta = document.createElement("textarea");
    ta.className = "mem-edit";
    ta.value = it.content;
    const saveBtn = document.createElement("button");
    saveBtn.textContent = "保存";
    saveBtn.title = "保存修改";
    const cancelBtn = document.createElement("button");
    cancelBtn.textContent = "取消";
    cancelBtn.title = "取消编辑";
    const done = () => {
      textEl.style.display = "";
      ta.remove();
      saveBtn.remove();
      cancelBtn.remove();
    };
    const save = async () => {
      const content = ta.value.trim();
      if (!content) { ta.focus(); return; }
      if (content === it.content) { done(); return; }
      try {
        await api(`/api/memory/${it.id}`, { method: "PUT", body: JSON.stringify({ content }) });
        toast("已保存（低频区变化，下次请求缓存失效）");
        loadMemory();
      } catch (e) {
        toast("保存失败: " + e.message);
        done();
      }
    };
    saveBtn.onclick = save;
    cancelBtn.onclick = done;
    ta.onkeydown = (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save(); }
      else if (e.key === "Escape") done();
    };
    textEl.style.display = "none";
    textEl.after(ta);
    ops.appendChild(saveBtn);
    ops.appendChild(cancelBtn);
    ta.focus();
  };
  el.querySelector('[data-op="del"]').onclick = async () => {
    try {
      await api(`/api/memory/${it.id}`, { method: "DELETE" });
      toast("已删除");
      loadMemory();
    } catch (e) {
      toast("删除失败: " + e.message);
    }
  };
  box.appendChild(el);
}

async function loadMemory() {
  try {
    const { items } = await api("/api/memory");
    $$(".mem-items").forEach((box) => (box.innerHTML = ""));
    const counts = { core: 0, working: 0, longterm: 0 };
    for (const it of items) {
      const box = $(`.mem-items[data-layer="${it.layer}"]`);
      if (!box) continue;
      renderMemoryItem(box, it);
      counts[it.layer] = (counts[it.layer] || 0) + 1;
    }
    for (const [layer, n] of Object.entries(counts)) {
      const el = $(`.mem-layer-count[data-count="${layer}"]`);
      if (el) el.textContent = `${n} 条`;
    }
    $$(".mem-items").forEach((b) => {
      if (!b.children.length) b.innerHTML = '<div class="mem-empty">（暂无内容）</div>';
    });
  } catch (e) {
    toast("记忆加载失败: " + e.message);
  }
}

// 记忆搜索（L1 关键词检索，/api/memory/search）
async function searchMemory() {
  const q = $("#in-mem-search").value.trim();
  if (!q) return loadMemory();
  try {
    const { items } = await api("/api/memory/search?q=" + encodeURIComponent(q));
    $$(".mem-items").forEach((box) => (box.innerHTML = ""));
    const box = $(`.mem-items[data-layer="longterm"]`);
    if (!items.length) {
      box.innerHTML = '<div class="mem-empty">无匹配记忆 —— 试试换关键词</div>';
      return;
    }
    for (const it of items) renderMemoryItem(box, it);
  } catch (e) {
    toast("搜索失败: " + e.message);
  }
}
$("#btn-mem-search").onclick = searchMemory;
$("#in-mem-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") searchMemory();
});

// 添加记忆：按钮展开输入区
$("#btn-add-mem").onclick = () => {
  $("#mem-add-box").classList.toggle("hidden");
  if (!$("#mem-add-box").classList.contains("hidden")) $("#in-mem-content").focus();
};
$("#btn-cancel-mem").onclick = () => {
  $("#mem-add-box").classList.add("hidden");
  $("#in-mem-content").value = "";
};
$("#btn-save-mem").onclick = async () => {
  const content = $("#in-mem-content").value.trim();
  if (!content) return toast("先写点内容再添加");
  try {
    await api("/api/memory", {
      method: "POST",
      body: JSON.stringify({
        layer: $("#sel-mem-layer").value,
        content,
        importance: parseInt($("#sel-mem-importance").value || "2", 10),
      }),
    });
    $("#in-mem-content").value = "";
    $("#mem-add-box").classList.add("hidden");
    toast("已添加到长期记忆");
    loadMemory();
  } catch (e) {
    toast("添加失败: " + e.message);
  }
};

$("#btn-summarize").onclick = async () => {
  try {
    const r = await api("/api/memory/summarize", { method: "POST" });
    toast(r.detail || "压缩完成");
  } catch (e) {
    toast("压缩失败: " + e.message);
  }
};

// ---------- 数据面板 ----------

function drawLineChart(canvas, series) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 300;
  const h = 180;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  if (!series || !series.length) {
    ctx.fillStyle = "#8b93a3";
    ctx.font = "12px sans-serif";
    ctx.fillText("暂无数据 —— 发几条消息开始统计", w / 2 - 110, h / 2);
    return;
  }

  const pad = { l: 34, r: 10, t: 12, b: 22 };
  const iw = w - pad.l - pad.r;
  const ih = h - pad.t - pad.b;
  const max = 100;
  const min = 0;

  // 网格与 Y 轴
  ctx.strokeStyle = "#2a3040";
  ctx.fillStyle = "#8b93a3";
  ctx.font = "10px sans-serif";
  ctx.lineWidth = 1;
  for (let v = 0; v <= 100; v += 25) {
    const y = pad.t + ih - (v - min) / (max - min) * ih;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(pad.l + iw, y);
    ctx.stroke();
    ctx.fillText(v + "%", 2, y + 3);
  }

  // 折线
  const pts = series.map((s, i) => ({
    x: pad.l + (series.length === 1 ? iw / 2 : (i / (series.length - 1)) * iw),
    y: pad.t + ih - (s.hit_rate * 100) / 100 * ih,
  }));

  ctx.beginPath();
  pts.forEach((p, i) => (i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)));
  ctx.strokeStyle = "#4f8cff";
  ctx.lineWidth = 2;
  ctx.stroke();

  // 填充
  ctx.lineTo(pts[pts.length - 1].x, pad.t + ih);
  ctx.lineTo(pts[0].x, pad.t + ih);
  ctx.closePath();
  ctx.fillStyle = "rgba(79,140,255,0.12)";
  ctx.fill();

  // 数据点 + X 轴标签
  pts.forEach((p, i) => {
    ctx.beginPath();
    ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
    ctx.fillStyle = "#4f8cff";
    ctx.fill();
    if (i % 2 === 0 || i === pts.length - 1) {
      ctx.fillStyle = "#8b93a3";
      ctx.fillText(series[i].ts, p.x - 14, h - 6);
    }
  });
}

async function refreshMetrics() {
  const cid = currentConv || "";
  // 未选对话 → 空态
  if (!cid) {
    $("#data-source").textContent = "未选择对话";
    $("#data-source").className = "badge demo";
    $("#stat-invalid").textContent = "打开一个对话，这里显示它的命中率 / 成本 / 延迟";
    $("#stat-hitrate").textContent = "--";
    $("#stat-tokens").textContent = "--";
    $("#stat-cost").textContent = "--";
    $("#stat-latency").textContent = "--";
    $("#log-body").innerHTML = "";
    return;
  }
  try {
    const q = `?conversation_id=${encodeURIComponent(cid)}`;
    const s = await api("/api/metrics/summary" + q);
    $("#data-source").textContent = s.count ? "真实数据" : "暂无数据";
    $("#data-source").className = "badge" + (s.count ? "" : " demo");
    $("#stat-invalid").textContent = s.count
      ? `缓存失效事件 ${s.cache_invalidated ?? 0} 次（技能切换 / 摘要重写 / 记忆编辑 / 历史裁剪触发）`
      : "还没有请求记录 —— 发几条消息，命中率统计从这里开始";
    $("#stat-hitrate").textContent = (s.hit_rate * 100).toFixed(1) + "%";
    $("#stat-tokens").textContent = s.total_prompt_tokens.toLocaleString();
    $("#stat-cost").textContent = s.cost_cny.toFixed(6);
    $("#stat-latency").textContent = s.avg_latency_ms;
    drawLineChart($("#chart-hitrate"), s.series);

    const { items } = await api("/api/metrics/requests" + q);
    const body = $("#log-body");
    body.innerHTML = "";
    if (!items.length) {
      body.innerHTML = '<tr><td colspan="8" style="color:var(--dim)">（暂无请求）</td></tr>';
      return;
    }
    for (const r of items) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${r.ts}</td><td>${r.prompt_tokens}</td><td>${r.hit_tokens}</td>
        <td>${r.miss_tokens}</td><td>${(r.hit_rate * 100).toFixed(1)}%</td>
        <td>${r.reasoning_tokens ?? 0}</td><td>${r.latency_ms}</td>
        <td>${r.cache_invalidated ? "⚠" : ""}</td>`;
      body.appendChild(tr);
    }
  } catch (e) {
    toast("指标加载失败: " + e.message);
  }
}

// ---------- 学习模式 ----------

const SUBJECT_EMOJIS = {
  "python": "🐍", "java": "☕", "javascript": "🟨", "js": "🟨", "ts": "🔷",
  "c语言": "⚙️", "c++": "🛠️", "rust": "🦀", "go": "🔹", "编程": "💻",
  "英语": "🇬🇧", "日语": "🇯🇵", "韩语": "🇰🇷", "法语": "🇫🇷", "德语": "🇩🇪",
  "写作": "✍️", "摄影": "📷", "音乐": "🎵", "绘画": "🎨", "设计": "🎯",
  "数学": "📐", "物理": "⚛️", "化学": "🧪", "生物": "🧬", "历史": "📜",
  "心理学": "🧠", "经济学": "📊", "医学": "🩺", "法律": "⚖️",
};
const CARD_GRADIENTS = [
  "linear-gradient(135deg, #4f8cff20, #7c5cff10)",
  "linear-gradient(135deg, #34d39920, #0ea5e910)",
  "linear-gradient(135deg, #fbbf2420, #f9731610)",
  "linear-gradient(135deg, #ec489920, #8b5cf610)",
];

function emojiFor(subject) {
  const s = (subject || "").toLowerCase();
  for (const [k, v] of Object.entries(SUBJECT_EMOJIS)) {
    if (s.includes(k)) return v;
  }
  return "📚";
}

// ---------- 学习模式（对话式引导：AI 在对话里问，点选项卡或打字回答） ----------

async function reloadCurrentConv() {
  if (!currentConv) return;
  await openConversation(currentConv, $("#chat-title").textContent, currentFav);
}

async function enterLearningMode() {
  try {
    if (!currentConv) {
      const conv = await api("/api/conversations", { method: "POST" });
      currentConv = conv.id;
      await loadConversations();
    }
    const r = await api("/api/learning/mode", {
      method: "POST",
      body: JSON.stringify({ conversation_id: currentConv }),
    });
    if (r.status === "has_project") {
      toast(`此对话已是学习项目「${r.project.subject}」，文档在 📖 学习文档`);
      return;
    }
    await reloadCurrentConv();
    scrollToBottom(true);
  } catch (e) {
    toast("进入学习模式失败: " + e.message);
  }
}

// 点击选项卡 → 作为回答推进状态机（AI 引导消息落库后刷新显示）
async function answerLearningOption(val) {
  if (!currentConv) return;
  try {
    const r = await api("/api/learning/mode", {
      method: "POST",
      body: JSON.stringify({ conversation_id: currentConv, answer: val }),
    });
    if (r.status === "created") toast("🎉 学习项目创建成功！");
    else if (r.status === "error") toast("创建失败：" + (r.detail || ""));
    else if (r.status === "ended") toast("学习引导已结束");
    await reloadCurrentConv();
  } catch (e) {
    toast("引导已结束或已失效，重新点 🎓 学习模式");
    await reloadCurrentConv();
  }
}

// 确认摘要卡片：字段可点改（选择类点击切换 / 文本类内联编辑）
function renderPlanEdit(box, qo) {
  const plan = qo.plan || {};
  const assumed = qo.assumed || [];
  const FIELDS = [
    { key: "subject", label: "📚 想学", type: "text" },
    { key: "goal", label: "🎯 真实目标", type: "text" },
    { key: "level_from", label: "🎚 当前水平", type: "choice", opts: ["零基础", "入门", "进阶"] },
    { key: "level_to", label: "🎯 目标水平", type: "choice", opts: ["入门", "进阶", "精通"] },
    { key: "style", label: "🧭 计划风格", type: "choice", opts: ["详细（章节拆细）", "精炼（结构清晰）"] },
  ];
  for (const f of FIELDS) {
    const row = document.createElement("div");
    row.className = "plan-field";
    const label = document.createElement("span");
    label.className = "plan-label";
    label.textContent = f.label;
    const val = document.createElement("span");
    val.className = "plan-val" + (assumed.includes(f.key) ? " assumed" : "");
    if (f.type === "choice") {
      const chip = document.createElement("button");
      chip.className = "plan-chip";
      chip.textContent = plan[f.key] || "（未指定）";
      chip.title = "点击切换";
      chip.onclick = async () => {
        chip.disabled = true;
        const cur = plan[f.key];
        const next = f.opts[(f.opts.indexOf(cur) + 1) % f.opts.length];
        await editPlanField(f.key, next);
      };
      val.appendChild(chip);
    } else {
      val.textContent = plan[f.key] || "（未填写）";
      const btn = document.createElement("button");
      btn.className = "plan-edit-btn";
      btn.textContent = "✏️";
      btn.title = "点击修改";
      btn.onclick = () => {
        const input = document.createElement("input");
        input.type = "text";
        input.className = "plan-input";
        input.value = plan[f.key] || "";
        input.placeholder = f.key === "subject" ? "想学什么？" : "想达到什么效果？例如：两个月内能独立爬网站";
        val.innerHTML = "";
        val.appendChild(input);
        input.focus();
        let busy = false;
        const commit = async () => {
          if (busy) return;
          const v = input.value.trim();
          if (!v) {
            val.textContent = plan[f.key] || "（未填写）";
            val.appendChild(btn);
            return;
          }
          busy = true;
          await editPlanField(f.key, v);
        };
        input.onkeydown = (e) => {
          if (e.key === "Enter") { e.preventDefault(); commit(); }
          else if (e.key === "Escape") {
            val.textContent = plan[f.key] || "（未填写）";
            val.appendChild(btn);
          }
        };
        input.onblur = () => { if (document.contains(input)) commit(); };
      };
      val.appendChild(btn);
    }
    row.appendChild(label);
    row.appendChild(val);
    box.appendChild(row);
  }
}

async function editPlanField(field, value) {
  try {
    const r = await api("/api/learning/mode", {
      method: "POST",
      body: JSON.stringify({ conversation_id: currentConv, action: "edit-plan", field, value }),
    });
    if (r.status === "error") { toast("修改失败：" + (r.detail || "")); return; }
    await reloadCurrentConv();
    scrollToBottom(true);
  } catch (e) {
    toast("修改失败: " + e.message);
  }
}

$("#btn-learning").onclick = enterLearningMode;
$("#btn-learning-docs").onclick = () => { loadLearningProjects().then(openLearningCenter); };

// 学习中心
let learningProjects = [];


// 项目详情


// modal 关闭

// ---------- 启动 ----------

let _booted = false;

async function init() {
  if (_booted) return;
  _booted = true;

  // 立刻更新状态（不等 API）
  $("#status-dot").className = "dot";
  $("#status-text").textContent = "加载中…";

  await loadLibs();
  updateEmptyState();
  setSendState();
  restoreParams();

  // 会话列表优先级最高
  await loadConversations();
  // 兜底：确保首次一定显示
  if (!$("#conv-list").children.length) {
    setTimeout(async () => { await loadConversations(); }, 800);
    setTimeout(async () => { await loadConversations(); }, 2500);
  }

  // 健康检查
  api("/api/health").then((h) => {
    if (h) { $("#status-dot").className = "dot ok"; $("#status-text").textContent = "服务在线 v" + h.version; }
  }).catch(() => { $("#status-dot").className = "dot err"; $("#status-text").textContent = "服务未连接"; });

  await loadSettings();
  await renderSkills();
  loadLearningProjects();
  loadMemory().catch(() => {});
  refreshMetrics().catch(() => {});
  refreshMemoryBar();
}

init();

/* ============================================================================
   Gili UI v2 —— 交互层覆盖
   同名 function 声明会提升并覆盖旧实现；接口与数据流不变，只重构 DOM 与动效
   ============================================================================ */

// ---------- 会话记录卡片 ----------

let convRenderedIds = new Set();
let convListSignature = "";

async function loadConversations() {
  const list = $("#conv-list");
  if (!list) return;
  if (!list.children.length) {
    list.innerHTML = '<div class="conv-loading"><span class="spin"></span> 加载会话…</div>';
  }
  try {
    const { items } = await api("/api/conversations");
    // 数据未变：只更新选中态，避免每次刷新整列重绘（也是卡片慢/闪的原因之一）
    const signature = JSON.stringify({
      mode: multiMode,
      items: (items || []).map((c) => [
        c.id, c.title, c.updated_at, c.msg_count, c.favorite,
        c.project_id, c.project_subject, c.memory_group, c.memory_group_size,
      ]),
    });
    if (signature === convListSignature && list.children.length) {
      $$(".conv-item").forEach((x) =>
        x.classList.toggle("active", x.dataset.id === currentConv)
      );
      return;
    }
    convListSignature = signature;
    list.innerHTML = "";
    const frag = document.createDocumentFragment();
    const nextIds = new Set((items || []).map((c) => c.id));
    let delay = 0;

    for (const c of items || []) {
      const isLearning = !!c.project_id;
      const isSel = multiSelected.has(c.id);
      const isNew = !convRenderedIds.has(c.id);
      const el = document.createElement("div");
      el.className =
        "conv-item" +
        (c.id === currentConv ? " active" : "") +
        (isLearning ? " learning" : "") +
        (isSel ? " selected" : "") +
        (isNew ? " conv-in" : "");
      el.style.animationDelay = `${Math.min(delay, 180)}ms`;
      if (isNew) delay += 14;
      el.dataset.id = c.id;

      const grad = CARD_GRADIENTS[Math.abs(hash(c.project_subject || c.title)) % CARD_GRADIENTS.length];
      el.innerHTML = `
        ${multiMode ? `<input type="checkbox" class="conv-check" ${isSel ? "checked" : ""}>` : ""}
        <div class="conv-avatar"${isLearning ? ` style="background:${grad};border-color:rgba(255,255,255,.16)"` : ""}>${isLearning ? emojiFor(c.project_subject) : "💬"}</div>
        <div class="conv-body">
          <div class="conv-t"><span class="t-text">${esc(c.title)}</span>${c.favorite ? '<span class="fav-star">★</span>' : ""}</div>
          <div class="conv-m">
            ${isLearning ? '<span class="badge lc-badge">🎓 学习</span>' : ""}
            ${c.memory_group ? `<span class="badge share-badge">🔗 共享组 ${c.memory_group_size || 0}</span>` : ""}
            <span>${fmtTime(c.updated_at)} · ${c.msg_count} 条</span>
          </div>
        </div>
        ${multiMode ? "" : `<div class="conv-ops">
          <button data-op="rename" title="重命名">✏️</button>
          <button data-op="ai" title="AI 起标题">✨</button>
          ${c.memory_group ? '<button data-op="unlink" title="取消记忆共享">🔗</button>' : ""}
          <button data-op="del" title="删除">🗑</button>
        </div>`}`;

      const clickCard = () => {
        if (multiMode) {
          const cb = el.querySelector(".conv-check");
          cb.checked = !cb.checked;
          if (cb.checked) multiSelected.add(c.id);
          else multiSelected.delete(c.id);
          el.classList.toggle("selected", cb.checked);
          updateMultiCount();
        } else {
          openConversation(c.id, c.title, c.favorite);
        }
      };
      el.onclick = clickCard;
      if (multiMode) {
        el.querySelector(".conv-check").onclick = (e) => {
          e.stopPropagation();
          clickCard();
        };
      } else {
        el.querySelector('[data-op="rename"]').onclick = async (e) => {
          e.stopPropagation();
          const t = prompt("输入新标题：", c.title);
          if (!t || !t.trim()) return;
          try {
            const r = await api(`/api/conversations/${c.id}`, { method: "PUT", body: JSON.stringify({ title: t.trim() }) });
            if (r.status === "ok") {
              toast(`已重命名为「${r.title}」`);
              if (c.id === currentConv) $("#chat-title").textContent = r.title;
              loadConversations();
            } else {
              toast(r.detail || "重命名失败");
            }
          } catch (err) {
            toast("重命名失败: " + err.message);
          }
        };
        el.querySelector('[data-op="ai"]').onclick = async (e) => {
          e.stopPropagation();
          try {
            const r = await api(`/api/conversations/${c.id}/auto-title`, { method: "POST" });
            if (r.ok) {
              toast(`AI 标题：「${r.title}」`);
              if (c.id === currentConv) $("#chat-title").textContent = r.title;
              loadConversations();
            } else {
              toast(r.detail || "起标题失败");
            }
          } catch (err) {
            toast("起标题失败: " + err.message);
          }
        };
        const unlinkBtn = el.querySelector('[data-op="unlink"]');
        if (unlinkBtn) {
          unlinkBtn.onclick = async (e) => {
            e.stopPropagation();
            if (!confirm(`把「${c.title}」移出共享记忆组？\n之后它只使用自己的记忆。`)) return;
            try {
              await api(`/api/conversations/${c.id}/unlink-memory`, { method: "POST" });
              toast("已取消记忆共享");
              loadConversations();
            } catch (err) {
              toast("取消失败: " + err.message);
            }
          };
        }
        el.querySelector('[data-op="del"]').onclick = async (e) => {
          e.stopPropagation();
          if (!confirm(`删除对话「${c.title}」？\n消息和摘要将被永久删除，此操作不可恢复。`)) return;
          try {
            await api(`/api/conversations/${c.id}`, { method: "DELETE" });
            toast("对话已删除");
            if (currentConv === c.id) {
              currentConv = null;
              $("#chat-title").textContent = "未命名对话";
              $("#messages").innerHTML = "";
              updateEmptyState();
              refreshCurrentProject();
            }
            loadConversations();
          } catch (err) {
            toast("删除失败: " + err.message);
          }
        };
      }
      frag.appendChild(el);
    }

    convRenderedIds = nextIds;
    list.appendChild(frag);
  } catch (e) {
    list.innerHTML = '<div class="conv-loading err">加载失败，点击重试</div>';
    const retry = list.querySelector(".conv-loading");
    if (retry) retry.onclick = loadConversations;
    toast("会话列表加载失败: " + e.message);
  }
}

// ---------- 思考链：脉冲灯 + 折叠面板 ----------

let _reasoningPaintFrame = 0;
let _lastReasoningPaint = 0;

function streamRenderReasoning(box, raw) {
  // 思考链默认折叠时完全不碰 DOM；用户展开时才用节流刷新，
  // 避免 1 万字思考链每 token 都重建一次 textContent。
  box.dataset.raw = raw;
  if (box.classList.contains("hidden")) return;

  const now = performance.now();
  if (now - _lastReasoningPaint < 80) {
    if (!_reasoningPaintFrame) {
      _reasoningPaintFrame = requestAnimationFrame(() => {
        _reasoningPaintFrame = 0;
        _lastReasoningPaint = performance.now();
        box.textContent = box.dataset.raw || "";
      });
    }
    return;
  }
  if (_reasoningPaintFrame) cancelAnimationFrame(_reasoningPaintFrame);
  _reasoningPaintFrame = 0;
  _lastReasoningPaint = now;
  box.textContent = raw;
}

function flushReasoningRender() {
  if (_reasoningPaintFrame) cancelAnimationFrame(_reasoningPaintFrame);
  _reasoningPaintFrame = 0;
}

function setReasoningState(wrap, state, chars = 0) {
  const btn = wrap.querySelector(".reasoning-toggle");
  const dot = wrap.querySelector(".pulse-dot");
  const label = wrap.querySelector(".rt-label");
  const count = wrap.querySelector(".rt-count");
  const box = wrap.querySelector(".reasoning");
  if (!btn) return;
  if (state === "live") {
    dot.classList.remove("idle");
    label.textContent = "💭 思考中…";
    count.textContent = "";
    box.classList.add("live");
  } else {
    dot.classList.add("idle");
    label.textContent = "💭 思考链";
    count.textContent = chars ? `${chars} 字` : "";
    box.classList.remove("live");
  }
}

function ensureReasoningBlock(el, live = false) {
  let wrap = el.querySelector(".reasoning-wrap");
  if (!wrap) {
    wrap = document.createElement("div");
    wrap.className = "reasoning-wrap";
    wrap.innerHTML =
      '<button type="button" class="reasoning-toggle">' +
      '<span class="pulse-dot"></span><span class="rt-label">💭 思考中…</span>' +
      '<span class="rt-count"></span><span class="rt-chevron">▾</span></button>' +
      '<div class="reasoning hidden"></div>';
    const btn = wrap.querySelector(".reasoning-toggle");
    const box = wrap.querySelector(".reasoning");
    btn.onclick = () => {
      box.classList.toggle("hidden");
      btn.classList.toggle("open", !box.classList.contains("hidden"));
      if (!box.classList.contains("hidden")) box.textContent = box.dataset.raw || "";
    };
    el.insertBefore(wrap, el.querySelector(".content"));
  }
  return wrap;
}

function addReasoningBlock(el, reasoning, live = false) {
  const wrap = ensureReasoningBlock(el, live);
  wrap.querySelector(".reasoning").textContent = reasoning || "";
  setReasoningState(wrap, live ? "live" : "done", live ? 0 : (reasoning || "").length);
}

// ---------- 聊天气泡 ----------


// ---------- 流式发送（逐块淡入 + 思考脉冲 + 生成呼吸光） ----------

async function sendChat(text, regenId) {
  if (streaming) return;
  if (!regenId) appendMessage("user", text);
  const msgEl = appendMessage("assistant", "", true);
  const contentDiv = msgEl.querySelector(".content");
  let rawContent = "";
  let rawReasoning = "";
  let proposalReady = false;
  flushStreamRender();
  flushReasoningRender();
  _lastStreamPaint = 0;
  _lastReasoningPaint = 0;
  streaming = true;
  setSendState();

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        conversation_id: currentConv,
        message: text,
        regenerate_message_id: regenId,
        ...collectParams(),
      }),
    });
    if (!res.ok) throw new Error(`/api/chat -> ${res.status}`);

    // 学习模式拦截：普通 JSON（引导消息已落库）→ 刷新对话显示
    if ((res.headers.get("content-type") || "").includes("application/json")) {
      const j = await res.json();
      msgEl.remove();
      if (j.mode !== "learning" && j.mode !== "learning_update" && j.mode !== "learning_command") {
        throw new Error("chat 响应异常");
      }
      if (j.status === "created") toast("🎉 学习项目创建成功！");
      streaming = false;
      setSendState();
      await reloadCurrentConv();
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        if (!part.startsWith("data:")) continue;
        let evt;
        try {
          evt = JSON.parse(part.slice(5).trim());
        } catch {
          continue;
        }
        if (evt.event === "start") {
          currentConv = evt.conversation_id || currentConv;
        } else if (evt.event === "tool") {
          const tip = document.createElement("div");
          tip.className = "tool-tip";
          tip.textContent = `🔎 已联网搜索「${evt.query}」（${evt.results} 条结果）`;
          msgEl.insertBefore(tip, contentDiv);
        } else if (evt.event === "delta") {
          if (evt.kind === "reasoning") {
            rawReasoning += evt.text;
            const wrap = ensureReasoningBlock(msgEl, true);
            streamRenderReasoning(wrap.querySelector(".reasoning"), rawReasoning);
            setReasoningState(wrap, "live");
          } else {
            if (!contentDiv.classList.contains("streaming")) {
              contentDiv.classList.add("streaming");
            }
            rawContent += evt.text;
            contentDiv.dataset.raw = rawContent;
            streamRenderMarkdown(contentDiv, rawContent);
          }
          scheduleScrollToBottom();
        } else if (evt.event === "done") {
          msgEl.dataset.msgId = evt.message_id || "";
          msgEl.dataset.group = evt.version_group || msgEl.dataset.msgId;
          msgEl.dataset.vno = evt.version_no || 1;
          if (regenId) {
            const oldNode = document.querySelector(`.msg[data-msg-id="${regenId}"]`);
            if (oldNode) {
              oldNode.classList.add("stale");
              oldNode.dataset.group = evt.version_group || oldNode.dataset.group;
              oldNode.dataset.vno = "1";
            }
            addVersionBar(msgEl, evt.version_no || 2, evt.versions_count || 2);
            toast(`已重新生成 v${evt.version_no || 2}（旧版保留，可切换查看）`);
          }
          const truncated = evt.usage && evt.usage.finish_reason === "length";
          toast(
            `usage：命中 ${evt.usage.prompt_cache_hit_tokens ?? 0} / 未命中 ${evt.usage.prompt_cache_miss_tokens ?? 0}` +
            (evt.cache_invalidated ? " ⚠ 前缀已变，本回合缓存失效" : "") +
            (truncated ? " ⚠ 被 max_tokens 截断，调大 max_tokens 或降思考强度" : "")
          );
          refreshMetrics();
        } else if (evt.event === "proposal") {
          proposalReady = true;
        } else if (evt.event === "error") {
          if (evt.message_id) msgEl.dataset.msgId = evt.message_id;
          throw new Error(evt.detail || "请求失败");
        }
      }
    }
  } catch (e) {
    rawContent += "\n[错误] " + e.message;
    contentDiv.textContent = rawContent;
    contentDiv.dataset.raw = rawContent;
  }

  // 结束态：停动画、打标、一次性渲染 Markdown
  flushStreamRender();
  flushReasoningRender();
  msgEl.classList.remove("typing", "streaming");
  contentDiv.classList.remove("streaming");
  if (contentDiv.querySelector(".typing-dots")) contentDiv.textContent = "";
  const wrap = msgEl.querySelector(".reasoning-wrap");
  if (wrap) {
    wrap.querySelector(".reasoning").textContent = rawReasoning;
    setReasoningState(wrap, "done", rawReasoning.length);
  }
  if (!rawContent && !rawReasoning) contentDiv.textContent = "（回复为空）";
  if (rawContent) {
    if (renderMessageContent(contentDiv, rawContent)) {
      msgEl.querySelector(".md-toggle").textContent = "源码";
    }
  }
  if (msgEl._sources) {
    renderCitations(contentDiv, msgEl._sources);
    updateRefsButton(msgEl._sources);
  }
  scrollToBottom(true);
  if (proposalReady) await reloadCurrentConv();
  streaming = false;
  setSendState();
  loadConversations();
  refreshMemoryBar();
}

// ---------- 学习工作室：单窗口工作台 ----------

const STUDY_GRADIENTS = [
  "linear-gradient(135deg, rgba(91,140,255,.34), rgba(139,108,255,.2))",
  "linear-gradient(135deg, rgba(52,211,153,.3), rgba(14,165,233,.18))",
  "linear-gradient(135deg, rgba(251,191,36,.28), rgba(249,115,22,.16))",
  "linear-gradient(135deg, rgba(236,72,153,.3), rgba(139,92,246,.18))",
];

let studioProjects = [];
let studioSelected = null;
let studioDetail = null;
let studioTab = "overview";
let studioSearch = "";

async function loadLearningProjects() {
  try {
    const r = await api("/api/learning/projects");
    studioProjects = r.items || [];
    learningProjects = studioProjects;
  } catch {
    /* 静默 */
  }
}

async function openLearningCenter() {
  await loadLearningProjects();
  studioDetailCache = {};
  renderStudio();
  const modal = $("#learning-center-modal");
  modal.classList.add("studio-modal");
  modal.classList.remove("hidden");
}

function renderStudio() {
  const modal = $("#learning-center-modal");
  modal.innerHTML = `
    <div class="studio-box">
      <header class="studio-head">
        <div class="studio-brand">
          <span class="studio-logo">🎓</span>
          <div>
            <div class="studio-title">学习工作室</div>
            <div class="studio-sub">把「想学」变成一份可执行、可追踪的计划</div>
          </div>
        </div>
        <div class="studio-stats">
          <div class="studio-stat"><b id="st-total">0</b><span>项目</span></div>
          <div class="studio-stat"><b id="st-avg">0%</b><span>平均进度</span></div>
          <div class="studio-stat"><b id="st-done">0</b><span>完成章节</span></div>
        </div>
        <button id="btn-studio-close" class="help-close" title="关闭">✕</button>
      </header>
      <div class="studio-body">
        <aside class="studio-side">
          <div class="studio-tools">
            <div class="studio-search" id="studio-search">
              <span>🔎</span>
              <input type="text" id="in-studio-search" placeholder="搜索项目 / 学科 / 水平">
              <button class="clear" id="btn-studio-clear" title="清空">✕</button>
            </div>
            <button id="btn-studio-new" class="btn primary">＋ 新项目</button>
          </div>
          <div id="studio-list" class="studio-list"></div>
        </aside>
        <section id="studio-detail" class="studio-detail"></section>
      </div>
    </div>`;

  $("#btn-studio-close").onclick = () => modal.classList.add("hidden");
  $("#btn-studio-new").onclick = studioNewProject;
  const searchInput = $("#in-studio-search");
  searchInput.value = studioSearch;
  searchInput.oninput = () => {
    studioSearch = searchInput.value.trim().toLowerCase();
    $("#studio-search").classList.toggle("has-value", !!searchInput.value);
    renderStudioList();
  };
  $("#btn-studio-clear").onclick = () => {
    studioSearch = "";
    searchInput.value = "";
    $("#studio-search").classList.remove("has-value");
    renderStudioList();
  };
  if (studioSearch) $("#studio-search").classList.add("has-value");

  renderStudioStats();
  renderStudioList();
  renderStudioDetail();
  if (window.GiliUI) {
    const steps = $(".studio-steps");
    if (steps) GiliUI.stagger(steps);
  }
}

function renderStudioStats() {
  const items = studioProjects || [];
  const pcts = items.map((p) => {
    const total = p.chapters_total || 0;
    return total ? Math.round(((p.chapters_done || 0) / total) * 100) : 0;
  });
  const avg = pcts.length ? Math.round(pcts.reduce((a, b) => a + b, 0) / pcts.length) : 0;
  const done = items.reduce((a, p) => a + (p.chapters_done || 0), 0);
  $("#st-total").textContent = items.length;
  $("#st-avg").textContent = `${avg}%`;
  $("#st-done").textContent = done;
}

function filteredProjects() {
  if (!studioSearch) return studioProjects || [];
  return (studioProjects || []).filter((p) =>
    [p.subject, p.level_from, p.level_to, p.style]
      .join(" ")
      .toLowerCase()
      .includes(studioSearch)
  );
}

let studioListSignature = "";
let studioDetailCache = {};

function renderStudioList() {
  const list = $("#studio-list");
  if (!list) return;
  const items = filteredProjects();
  if (!items.length) {
    list.innerHTML = studioProjects.length
      ? '<div class="studio-side-empty">没有匹配「' + esc(studioSearch) + '」的项目</div>'
      : '<div class="studio-side-empty">还没有项目<br>点「＋ 新项目」创建第一个</div>';
    return;
  }
  const signature = JSON.stringify(
    items.map((p) => [p.id, p.subject, p.chapters_done, p.chapters_total, p.status])
  );
  if (signature === studioListSignature) {
    // 数据没变：只切换选中态，避免整列重绘造成闪烁
    $$(".study-card").forEach((el) =>
      el.classList.toggle("active", el.dataset.id === studioSelected)
    );
    return;
  }
  studioListSignature = signature;
  list.innerHTML = "";
  items.forEach((p) => {
    const total = p.chapters_total || 1;
    const pct = Math.round(((p.chapters_done || 0) / total) * 100);
    const done = p.status === "done";
    const grad = STUDY_GRADIENTS[Math.abs(hash(p.subject || p.id)) % STUDY_GRADIENTS.length];
    const el = document.createElement("div");
    el.className = "study-card" + (p.id === studioSelected ? " active" : "");
    el.dataset.id = p.id;
    el.innerHTML = `
      <div class="study-card-head">
        <div class="study-card-avatar" style="background:${grad}">${emojiFor(p.subject)}</div>
        <div class="study-card-id">
          <div class="study-card-title">
            <span class="t-text">${esc(p.subject)}</span>
            <span class="study-status ${done ? "done" : ""}">${done ? "已完成" : "进行中"}</span>
          </div>
          <div class="study-card-meta">${esc(p.level_from || "?")} → ${esc(p.level_to || "?")} · ${esc(p.style || "计划")}</div>
        </div>
      </div>
      <div class="study-card-foot">
        <div class="study-track"><div class="study-fill" style="width:${pct}%"></div></div>
        <span class="study-pct">${pct}%</span>
        <button class="btn small study-go" title="继续学习">继续</button>
        <button class="btn small study-doc" title="查看学习文档">文档</button>
        <button class="btn small study-del" title="删除学习记录">🗑</button>
      </div>`;
    el.onclick = () => openStudioDetail(p.id);
    el.querySelector(".study-go").onclick = async (e) => {
      e.stopPropagation();
      await studioContinue(p);
    };
    el.querySelector(".study-doc").onclick = async (e) => {
      e.stopPropagation();
      studioTab = "outline";
      await openStudioDetail(p.id);
    };
    el.querySelector(".study-del").onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`删除学习记录「${p.subject}」？\n大纲 / 进度 / 笔记 / 疑问将一并删除，原对话会保留。`)) return;
      try {
        await api(`/api/learning/projects/${p.id}`, { method: "DELETE" });
        toast("学习记录已删除");
        studioProjects = studioProjects.filter((x) => x.id !== p.id);
        learningProjects = studioProjects;
        delete studioDetailCache[p.id];
        if (studioSelected === p.id) {
          studioSelected = null;
          studioDetail = null;
        }
        studioListSignature = "";
        renderStudioList();
        renderStudioDetail();
        loadConversations();
      } catch (err) {
        toast("删除失败: " + err.message);
      }
    };
    list.appendChild(el);
  });
  if (window.GiliUI) GiliUI.stagger(list);
}

function renderStudioDetail() {
  const pane = $("#studio-detail");
  if (!pane) return;
  if (!studioSelected) {
    pane.innerHTML = `
      <div class="studio-empty">
        <div class="studio-empty-orb">🎓</div>
        <h2>还没有学习项目</h2>
        <p>四步把「想学」变成「在学」，全程 AI 陪你拆解</p>
        <div class="studio-steps">
          <div class="studio-step"><b>1</b><span>新建项目<br>告诉我你想学什么</span></div>
          <div class="studio-step"><b>2</b><span>回答 4 个问题<br>确定水平与目标</span></div>
          <div class="studio-step"><b>3</b><span>AI 生成大纲<br>章节拆到可执行</span></div>
          <div class="studio-step"><b>4</b><span>边学边记<br>笔记 / 疑问自动归档</span></div>
        </div>
        <div class="studio-subjects">
          ${["Python", "英语", "写作", "摄影", "数据分析", "心理学"].map(
            (s) => `<button class="chip" data-subject="${esc(s)}">${esc(s)}</button>`
          ).join("")}
        </div>
        <button id="btn-studio-first" class="btn primary">开始第一个项目</button>
      </div>`;
    pane.querySelector("#btn-studio-first").onclick = studioNewProject;
    pane.querySelectorAll(".studio-subjects .chip").forEach((b) => {
      b.onclick = () => studioNewProject(b.dataset.subject);
    });
    return;
  }
  if (!studioDetail) {
    pane.innerHTML = '<div class="studio-side-empty ui-shimmer">加载项目详情…</div>';
    return;
  }

  const p = studioDetail.project;
  const done = studioDetail.progress.done;
  const total = studioDetail.progress.total;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const isDone = p.status === "done";
  const grad = STUDY_GRADIENTS[Math.abs(hash(p.subject || p.id)) % STUDY_GRADIENTS.length];
  const RING_C = 326.73;
  const offset = RING_C * (1 - pct / 100);
  const TABS = [
    ["overview", "概览"],
    ["outline", "大纲"],
    ["progress", "进度"],
    ["notes", "笔记"],
    ["organized", "整理"],
    ["questions", "疑问"],
  ];

  pane.innerHTML = `
    <div class="studio-detail-head">
      <div class="sd-avatar" style="background:${grad}">${emojiFor(p.subject)}</div>
      <div class="sd-id">
        <div class="sd-title">${esc(p.subject)}<span class="sd-status ${isDone ? "done" : ""}">${isDone ? "已完成" : "进行中"}</span></div>
        <div class="sd-meta">创建于 ${fmtTime(p.created_at)} · ${esc(p.style || "个性化计划")}</div>
        <div class="sd-level"><b>${esc(p.level_from || "零基础")}</b><span class="sd-arrow">→</span><b>${esc(p.level_to || "入门")}</b></div>
        <div class="sd-actions">
          <button class="btn primary sd-continue">💬 继续对话</button>
          <button class="btn sd-outline">📄 查看大纲</button>
        </div>
      </div>
      <div class="sd-ring">
        <svg viewBox="0 0 120 120">
          <defs>
            <linearGradient id="studyGrad" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="#5b8cff"/>
              <stop offset="100%" stop-color="#8b6cff"/>
            </linearGradient>
          </defs>
          <circle class="ring-track" cx="60" cy="60" r="52"></circle>
          <circle class="ring-fill" cx="60" cy="60" r="52"
            transform="rotate(-90 60 60)"
            style="stroke-dasharray:${RING_C};stroke-dashoffset:${offset}"></circle>
          <text class="ring-pct" x="60" y="57" text-anchor="middle">${pct}%</text>
          <text class="ring-label" x="60" y="76" text-anchor="middle">进度</text>
        </svg>
      </div>
    </div>
    <div class="sd-stats">
      <div class="sd-stat"><b>${done}/${total}</b><span>章节完成</span></div>
      <div class="sd-stat"><b>${(studioDetail.notes || []).length}</b><span>学习文档</span></div>
      <div class="sd-stat"><b>${(studioDetail.questions || []).length}</b><span>挂起疑问</span></div>
    </div>
    <div class="studio-tabs">
      ${TABS.map(
        ([key, label]) =>
          `<button class="studio-tab ${key === studioTab ? "active" : ""}" data-tab="${key}">${label}</button>`
      ).join("")}
    </div>
    <div id="studio-pane" class="studio-pane"></div>`;

  pane.querySelector(".sd-continue").onclick = () => studioContinue(p);
  pane.querySelector(".sd-outline").onclick = () => {
    studioTab = "outline";
    renderStudioDetail();
  };
  pane.querySelectorAll(".studio-tab").forEach((b) => {
    b.onclick = () => {
      studioTab = b.dataset.tab;
      renderStudioDetail();
    };
  });
  renderStudioPane();
}

async function refreshStudioDetail() {
  const pid = studioSelected;
  if (!pid) return;
  studioDetailCache[pid] = null;
  studioDetail = null;
  await openStudioDetail(pid);
}

function editChapterRow(item) {
  const ch = (studioDetail.chapters || []).find((c) => c.id === item.dataset.cid);
  if (!ch || item.querySelector(".chapter-edit-form")) return;
  const form = document.createElement("div");
  form.className = "chapter-edit-form";
  form.innerHTML =
    '<input class="ch-inp" data-f="title" placeholder="章节标题">' +
    '<input class="ch-inp" data-f="objective" placeholder="学习目标（可选）">' +
    '<button class="btn small primary" data-act="save">保存</button>' +
    '<button class="btn small" data-act="cancel">取消</button>';
  form.querySelector('[data-f="title"]').value = ch.title || "";
  form.querySelector('[data-f="objective"]').value = ch.objective || "";
  item.appendChild(form);
  form.querySelector('[data-act="save"]').onclick = async () => {
    const title = form.querySelector('[data-f="title"]').value.trim();
    if (!title) { toast("标题不能为空"); return; }
    try {
      await api(`/api/learning/chapters/${ch.id}`, {
        method: "PUT",
        body: JSON.stringify({
          title,
          objective: form.querySelector('[data-f="objective"]').value.trim(),
        }),
      });
      await refreshStudioDetail();
    } catch (e) {
      toast("保存失败: " + e.message);
    }
  };
  form.querySelector('[data-act="cancel"]').onclick = () => form.remove();
}

function renderStudioPane() {
  const host = $("#studio-pane");
  if (!host || !studioDetail) return;
  const d = studioDetail;

  if (studioTab === "overview") {
    const chapters = d.chapters || [];
    if (!chapters.length) {
      host.innerHTML = '<div class="studio-hint">还没有生成章节</div>';
      return;
    }
    host.innerHTML =
      '<div class="chapter-list">' +
      chapters
        .map((ch, i) => {
          const done = ch.status === "done";
          return (
            `<div class="chapter-item ${done ? "done" : "pending"}" data-cid="${ch.id}">` +
            `<button class="ch-check" data-act="toggle" title="${done ? "取消完成" : "标记完成"}">${done ? "✓" : "○"}</button>` +
            `<span class="ch-num">${String(i + 1).padStart(2, "0")}</span>` +
            `<div class="ch-main"><div class="ch-title">${esc(ch.title)}` +
            `<span class="ch-status ${done ? "done" : ""}">${done ? "已完成" : "待学习"}</span></div>` +
            (ch.objective ? `<div class="ch-obj">${esc(ch.objective)}</div>` : "") +
            '</div>' +
            '<div class="ch-ops">' +
            '<button class="ch-op" data-act="move" data-dir="up" title="上移">⬆</button>' +
            '<button class="ch-op" data-act="move" data-dir="down" title="下移">⬇</button>' +
            '<button class="ch-op" data-act="edit" title="编辑标题/目标">✏️</button>' +
            '<button class="ch-op danger" data-act="del" title="删除章节">🗑</button>' +
            '<button class="btn small ch-start" data-act="start" title="在对话中开始本章">开始</button>' +
            '</div></div>'
          );
        })
        .join("") +
      "</div>" +
      '<div class="chapter-add-row"><button id="btn-chapter-add" class="btn small">＋ 添加章节</button></div>';
    host.querySelectorAll('[data-act="toggle"]').forEach((btn) => {
      btn.onclick = async () => {
        const item = btn.closest(".chapter-item");
        const cid = item.dataset.cid;
        const ch = (studioDetail.chapters || []).find((c) => c.id === cid);
        if (!ch) return;
        const next = ch.status === "done" ? "pending" : "done";
        btn.disabled = true;
        try {
          await api(`/api/learning/chapters/${cid}`, {
            method: "PUT",
            body: JSON.stringify({ status: next }),
          });
          ch.status = next;
          studioDetail.progress.done = studioDetail.chapters.filter((c) => c.status === "done").length;
          const p = studioProjects.find((x) => x.id === studioDetail.project.id);
          if (p) p.chapters_done = studioDetail.progress.done;
          studioListSignature = "";
          renderStudioDetail();
          renderStudioList();
        } catch (e) {
          toast("更新失败: " + e.message);
          btn.disabled = false;
        }
      };
    });
    host.querySelectorAll('[data-act="start"]').forEach((btn) => {
      btn.onclick = async () => {
        const cid = btn.closest(".chapter-item").dataset.cid;
        try {
          const r = await api(`/api/learning/chapters/${cid}/start`, { method: "POST" });
          toast(`已在对话中开始：${r.title}`);
          if (currentProject && currentProject.id === studioDetail.project.id) {
            sideMode = "conv";
            updateSidebarMode();
          }
          loadConversations();
        } catch (e) {
          toast("开始失败: " + e.message);
        }
      };
    });
    host.querySelectorAll('[data-act="move"]').forEach((btn) => {
      btn.onclick = async () => {
        const cid = btn.closest(".chapter-item").dataset.cid;
        btn.disabled = true;
        try {
          await api(`/api/learning/chapters/${cid}/move`, {
            method: "POST",
            body: JSON.stringify({ direction: btn.dataset.dir }),
          });
          await refreshStudioDetail();
        } catch (e) {
          toast("移动失败: " + e.message);
          btn.disabled = false;
        }
      };
    });
    host.querySelectorAll('[data-act="edit"]').forEach((btn) => {
      btn.onclick = () => editChapterRow(btn.closest(".chapter-item"));
    });
    host.querySelectorAll('[data-act="del"]').forEach((btn) => {
      btn.onclick = async () => {
        const item = btn.closest(".chapter-item");
        const ch = (studioDetail.chapters || []).find((c) => c.id === item.dataset.cid);
        if (!ch) return;
        if (!confirm(`删除章节「${ch.title}」？进度与文档会同步更新。`)) return;
        btn.disabled = true;
        try {
          await api(`/api/learning/chapters/${item.dataset.cid}`, { method: "DELETE" });
          toast("已删除");
          await refreshStudioDetail();
        } catch (e) {
          toast("删除失败: " + e.message);
          btn.disabled = false;
        }
      };
    });
    const addBtn = $("#btn-chapter-add");
    if (addBtn) {
      addBtn.onclick = () => {
        if (host.querySelector(".chapter-add-form")) return;
        const form = document.createElement("div");
        form.className = "chapter-add-form";
        form.innerHTML =
          '<input class="ch-inp" data-f="title" placeholder="章节标题">' +
          '<input class="ch-inp" data-f="objective" placeholder="学习目标（可选）">' +
          '<button class="btn small primary" data-act="save-add">保存</button>' +
          '<button class="btn small" data-act="cancel-add">取消</button>';
        addBtn.after(form);
        addBtn.style.display = "none";
        const titleEl = form.querySelector('[data-f="title"]');
        titleEl.focus();
        form.querySelector('[data-act="save-add"]').onclick = async () => {
          const title = titleEl.value.trim();
          if (!title) { toast("标题不能为空"); return; }
          try {
            await api(`/api/learning/projects/${studioDetail.project.id}/chapters`, {
              method: "POST",
              body: JSON.stringify({
                title,
                objective: form.querySelector('[data-f="objective"]').value.trim(),
              }),
            });
            await refreshStudioDetail();
          } catch (e) {
            toast("添加失败: " + e.message);
          }
        };
        form.querySelector('[data-act="cancel-add"]').onclick = () => {
          form.remove();
          addBtn.style.display = "";
        };
      };
    }
    return;
  }

  if (studioTab === "questions") {
    const qs = d.questions || [];
    if (!qs.length) {
      host.innerHTML = '<div class="studio-hint">学习中产生的问题会挂在这里，不打断主线</div>';
      return;
    }
    host.innerHTML = qs
      .map(
        (q) =>
          `<div class="question-item">${esc(q.question)}` +
          `<div class="q-meta">${esc(q.concept || "概念")} · ${esc(q.source_ref || "出处")} · ${fmtTime(q.created_at)}</div></div>`
      )
      .join("");
    return;
  }

  if (studioTab === "organized") {
    const filename = "笔记整理.md";
    const note = (d.notes || []).find((n) => n.filename === filename);
    const content = note ? note.content : "";
    const bar = document.createElement("div");
    bar.className = "studio-doc-bar";
    bar.innerHTML =
      '<button class="btn small" id="btn-organize" title="让 AI 把零散笔记整理成结构化版本">🤖 重新整理</button>' +
      '<button class="btn small" id="btn-organize-rollback" title="回退到上一版整理稿">⏪ 回退</button>' +
      (note && note.version_no ? `<span class="doc-ver">v${note.version_no}</span>` : "");
    host.appendChild(bar);
    bar.querySelector("#btn-organize").onclick = async () => {
      const btn = bar.querySelector("#btn-organize");
      btn.disabled = true;
      btn.textContent = "🤖 整理中…（10-30 秒）";
      try {
        const r = await api(`/api/learning/projects/${studioDetail.project.id}/organize`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        toast(r.content || "整理完成");
        await refreshStudioDetail();
      } catch (e) {
        toast("整理失败: " + e.message);
        btn.disabled = false;
        btn.textContent = "🤖 重新整理";
      }
    };
    bar.querySelector("#btn-organize-rollback").onclick = async () => {
      if (!confirm("回退到上一版整理稿？（当前稿会保留在历史里）")) return;
      try {
        const r = await api(`/api/learning/projects/${studioDetail.project.id}/organize/rollback`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        toast(r.content || "已回退");
        await refreshStudioDetail();
      } catch (e) {
        toast("回退失败: " + e.message);
      }
    };
    const doc = document.createElement("div");
    doc.className = "studio-doc";
    if (content) {
      renderMessageContent(doc, content);
    } else {
      doc.innerHTML =
        '<div class="studio-hint">还没有整理稿。在对话里说「整理笔记」，或点上方 🤖 重新整理（需要先有一些笔记）。</div>';
    }
    host.appendChild(doc);
    return;
  }

  const FILE_MAP = { outline: "大纲.md", progress: "进度表.md", notes: "笔记.md" };
  const filename = FILE_MAP[studioTab];
  const note = (d.notes || []).find((n) => n.filename === filename);
  const content = note ? note.content : "";
  if (!content) {
    host.innerHTML = '<div class="studio-hint">这份文档还没有内容</div>';
    return;
  }
  host.innerHTML = '<div class="studio-doc"></div>';
  renderMessageContent(host.querySelector(".studio-doc"), content);
}

async function openStudioDetail(pid) {
  if (
    studioSelected === pid &&
    studioDetail &&
    studioDetail.project &&
    studioDetail.project.id === pid
  ) {
    return; // 重复点击当前卡片：不动 DOM，避免闪一下
  }
  studioSelected = pid;
  studioTab = "overview";
  if (studioDetailCache[pid]) {
    studioDetail = studioDetailCache[pid];
    renderStudioList();
    renderStudioDetail();
    return;
  }
  studioDetail = null;
  renderStudioList();
  renderStudioDetail();
  try {
    const detail = await api(`/api/learning/projects/${pid}`);
    studioDetailCache[pid] = detail;
    if (studioSelected !== pid) return;
    studioDetail = detail;
    renderStudioDetail();
  } catch (e) {
    studioDetail = null;
    const pane = $("#studio-detail");
    if (pane && studioSelected === pid) {
      pane.innerHTML = `
        <div class="studio-empty">
          <div class="studio-empty-orb">⚠️</div>
          <h2>项目详情加载失败</h2>
          <p>${esc(e.message)}</p>
          <button id="btn-studio-retry" class="btn primary">重试</button>
        </div>`;
      pane.querySelector("#btn-studio-retry").onclick = () => openStudioDetail(pid);
    }
    toast("打开项目失败: " + e.message);
  }
}

async function studioContinue(p) {
  if (!p.has_conversation) {
    toast("该项目的对话已被删除，只能查看文档");
    studioTab = "outline";
    await openStudioDetail(p.id);
    return;
  }
  $("#learning-center-modal").classList.add("hidden");
  openConversation(p.conversation_id, p.conversation_title || `${p.subject}学习笔记`, false);
}

async function studioNewProject() {
  $("#learning-center-modal").classList.add("hidden");
  try {
    const conv = await api("/api/conversations", { method: "POST" });
    await openConversation(conv.id, conv.title);
    await loadConversations();
    await enterLearningMode();
  } catch (e) {
    toast("新建学习对话失败: " + e.message);
  }
}

// ---------- 聊天空状态装饰 ----------

(function enhanceEmptyState() {
  const empty = $("#chat-empty");
  if (empty && !empty.querySelector(".orb")) {
    const orb = document.createElement("div");
    orb.className = "orb";
    orb.textContent = "✨";
    empty.prepend(orb);
  }
})();

/* ============================================================================
   Gili UI v3 —— 多选全选 / 只动新气泡 / 学习侧栏 / 大设置窗 / 三栏拖拽
   ============================================================================ */

// ---------- 多选全选 ----------

function setMultiMode(on) {
  multiMode = on;
  multiSelected.clear();
  $("#multi-bar").classList.toggle("hidden", !on);
  const menu = $("#multi-menu");
  if (menu) menu.classList.add("hidden");
  $("#btn-multi-del").textContent = on ? "✕ 退出" : "🗑 多选";
  const all = $("#btn-multi-all");
  if (all) all.textContent = "全选";
  updateMultiCount();
  loadConversations();
}

$("#btn-multi-all").onclick = () => {
  const nodes = [...$$(".conv-item")];
  const checks = nodes.map((el) => el.querySelector(".conv-check")).filter(Boolean);
  if (!checks.length) return;
  const allChecked = checks.every((cb) => cb.checked);
  multiSelected = new Set();
  checks.forEach((cb, i) => {
    cb.checked = !allChecked;
    nodes[i].classList.toggle("selected", cb.checked);
    if (cb.checked) multiSelected.add(nodes[i].dataset.id);
  });
  $("#btn-multi-all").textContent = allChecked ? "全选" : "全不选";
  updateMultiCount();
};

// ---------- 气泡：只给新消息加动画，历史加载不再整体重播 ----------

function appendMessage(role, text, typing = false, extra = {}) {
  const animate = extra.animate !== false;
  const el = document.createElement("div");
  el.className = `msg ${role}${typing ? " typing" : ""}${animate ? " anim" : ""}`;
  el.dataset.msgId = extra.msgId || "";
  el.dataset.group = extra.group || "";
  el.dataset.vno = extra.versionNo || 1;

  if (role === "assistant") {
    el.innerHTML =
      '<div class="who"><span class="who-name"><i class="who-dot"></i>Gili</span>' +
      '<span class="msg-ops"><button data-op="copy" title="复制">📋</button>' +
      '<button data-op="regen" title="重新生成">🔄</button>' +
      '<button data-op="del" title="删除">🗑️</button>' +
      '<button class="md-toggle" title="Markdown 源码/渲染切换">MD</button></span></div>' +
      '<div class="content"></div>';
    const content = el.querySelector(".content");
    if (typing) {
      content.innerHTML = '<span class="typing-dots"><i></i><i></i><i></i></span>';
    } else {
      content.textContent = text || "";
    }
    const mdToggle = el.querySelector(".md-toggle");
    mdToggle.onclick = () => toggleMarkdown(content, mdToggle);
    el.querySelector('[data-op="copy"]').onclick = () =>
      copyText(content.dataset.raw || content.textContent);
    el.querySelector('[data-op="regen"]').onclick = () => {
      if (!el.dataset.msgId) return toast("消息还没保存，稍后再重新生成");
      sendChat("", el.dataset.msgId);
    };
    el.querySelector('[data-op="del"]').onclick = () => deleteMessageBubble(el);
    if (!typing && text) {
      if (renderMessageContent(content, text)) mdToggle.textContent = "源码";
    }
    if (Array.isArray(extra.sources) && extra.sources.length) {
      el._sources = extra.sources;
      renderCitations(content, extra.sources);
    }
    if (extra.reasoning) addReasoningBlock(el, extra.reasoning);
    addVersionBar(el, extra.versionNo || 1, extra.versions || 0);

    // quick_options 兼容旧数组 + 新对象（opts/input/placeholder）
    const qo = Array.isArray(extra.quickOptions)
      ? { opts: extra.quickOptions, input: false, placeholder: "" }
      : Object.assign({ opts: [], input: false, placeholder: "" }, extra.quickOptions || {});
    const opts = Array.isArray(qo.opts) ? qo.opts : [];
    if (qo.kind === "plan_edit") {
      // 确认摘要卡片：字段可点改（选择类点击切换 / 文本类内联编辑）
      const box = document.createElement("div");
      box.className = "msg-options plan-card";
      renderPlanEdit(box, qo);
      for (const o of opts) {
        const b = document.createElement("button");
        b.className = "opt-chip confirm-chip";
        b.textContent = o;
        b.onclick = async () => {
          b.disabled = true;
          await answerLearningOption(o);
        };
        box.appendChild(b);
      }
      el.appendChild(box);
    } else if (opts.length) {
      const box = document.createElement("div");
      box.className = "msg-options";
      for (const o of opts) {
        const b = document.createElement("button");
        b.className = "opt-chip";
        b.textContent = o;
        b.onclick = async () => {
          b.disabled = true;
          await answerLearningOption(o);
        };
        box.appendChild(b);
      }
      el.appendChild(box);
    }
    if (qo.input) {
      const box = document.createElement("div");
      box.className = "msg-input";
      const input = document.createElement("input");
      input.type = "text";
      input.className = "guide-input";
      input.placeholder = qo.placeholder || "输入你的回答…";
      const btn = document.createElement("button");
      btn.className = "opt-chip";
      btn.textContent = "发送";
      const submit = async () => {
        const val = input.value.trim();
        if (!val) return;
        input.disabled = true;
        btn.disabled = true;
        await answerLearningOption(val);
      };
      btn.onclick = submit;
      input.onkeydown = (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          submit();
        }
      };
      box.appendChild(input);
      box.appendChild(btn);
      el.appendChild(box);
    }
  } else {
    el.innerHTML =
      '<div class="msg-ops"><button data-op="copy" title="复制">📋</button>' +
      '<button data-op="edit" title="编辑">✏️</button>' +
      '<button data-op="del" title="删除">🗑️</button></div>' +
      '<div class="content"></div>';
    const content = el.querySelector(".content");
    content.textContent = text || "";
    if (text && window.marked) renderMessageContent(content, text);
    el.querySelector('[data-op="copy"]').onclick = () =>
      copyText(content.dataset.raw || content.textContent);
    el.querySelector('[data-op="edit"]').onclick = () => startEdit(el, content);
    el.querySelector('[data-op="del"]').onclick = () => deleteMessageBubble(el);
  }

  $("#messages").appendChild(el);
  updateEmptyState();
  scrollToBottom();
  return el;
}

// ---------- 学习模式侧栏（当前会话的项目资料） ----------

let sideMode = "conv";
let currentProject = null;
let lpTab = "chapters";
let lpDetail = null;

async function openConversation(cid, title, favorite = false) {
  currentConv = cid;
  currentFav = !!favorite;
  $("#btn-fav").textContent = currentFav ? "★ 已收藏" : "☆ 收藏";
  $$(".conv-item").forEach((x) => x.classList.remove("active"));
  const el = document.querySelector(`.conv-item[data-id="${cid}"]`);
  if (el) el.classList.add("active");
  $("#chat-title").textContent = title;
  $("#messages").innerHTML = "";
  updateEmptyState();

  refreshCurrentProject(); // 不阻塞历史消息加载，项目侧栏就绪后自行更新
  try {
    const { items } = await api(`/api/conversations/${cid}/messages`);
    for (const m of items) {
      let quickOptions = null;
      if (m.quick_options) {
        try { quickOptions = JSON.parse(m.quick_options); } catch { /* 旧格式忽略 */ }
      }
      let sources = null;
      if (m.sources) {
        try { sources = JSON.parse(m.sources); } catch { /* 旧格式忽略 */ }
      }
      appendMessage(m.role === "assistant" ? "assistant" : "user", m.content, false, {
        reasoning: m.reasoning || "",
        versions: m.versions_count || 0,
        versionNo: m.version_no || 1,
        group: m.version_group || "",
        msgId: m.id,
        quickOptions,
        sources,
        animate: false,
      });
    }
    if (!items.length) appendMessage("assistant", "这是段新对话，说点什么吧。");
    const lastAi = [...$$("#messages .msg.assistant")].filter((m) => m._sources).pop();
    updateRefsButton(lastAi ? lastAi._sources : null);
    scrollToBottom(true);
    refreshMemoryBar();
    refreshMetrics();
  } catch (e) {
    toast("消息加载失败: " + e.message);
  }
}

async function refreshCurrentProject() {
  if (!currentConv) {
    currentProject = null;
    sideMode = "conv";
    updateSidebarMode();
    return;
  }
  await loadLearningProjects();
  const found =
    (learningProjects || []).find((p) => p.conversation_id === currentConv) || null;
  currentProject = found;
  if (found) {
    lpDetail = null;
    lpTab = "chapters";
    sideMode = "learn";
  } else {
    sideMode = "conv";
  }
  const badge = $("#btn-lp-badge");
  if (badge) {
    if (found) {
      badge.textContent = `📚 ${found.subject}`;
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  }
  updateSidebarMode();
}

function updateSidebarMode() {
  const modeEl = $("#side-mode");
  const list = $("#conv-list");
  const panel = $("#learning-panel");
  const tools = $("#learn-tools");
  if (!modeEl) return;
  const has = !!currentProject;
  modeEl.classList.toggle("hidden", !has);
  if (tools) tools.classList.toggle("hidden", !has);
  $$(".side-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === (has ? sideMode : "conv"))
  );
  if (!has) {
    sideMode = "conv";
    list.classList.remove("hidden");
    panel.classList.add("hidden");
    return;
  }
  if (sideMode === "learn") {
    list.classList.add("hidden");
    panel.classList.remove("hidden");
    renderLearningPanel();
  } else {
    list.classList.remove("hidden");
    panel.classList.add("hidden");
  }
}

$$(".side-tab").forEach((b) => {
  b.onclick = () => {
    sideMode = b.dataset.mode;
    updateSidebarMode();
  };
});

function renderLearningPanel() {
  const panel = $("#learning-panel");
  if (!panel || !currentProject) return;

  if (!lpDetail) {
    panel.innerHTML = '<div class="lp-card lp-empty">正在加载学习资料…</div>';
    api(`/api/learning/projects/${currentProject.id}`)
      .then((d) => {
        if (currentProject && d.project && currentProject.id === d.project.id) {
          lpDetail = d;
          renderLearningPanel();
        }
      })
      .catch((e) => {
        lpDetail = { error: e.message };
        renderLearningPanel();
      });
    return;
  }

  if (lpDetail.error) {
    panel.innerHTML =
      `<div class="lp-card lp-empty">加载失败：${esc(lpDetail.error)}` +
      '<br><button class="btn small lp-open" id="lp-retry">重试</button></div>';
    panel.querySelector("#lp-retry").onclick = () => {
      lpDetail = null;
      renderLearningPanel();
    };
    return;
  }

  const d = lpDetail;
  const p = d.project;
  const done = d.progress.done;
  const total = d.progress.total;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const grad = STUDY_GRADIENTS[Math.abs(hash(p.subject || p.id)) % STUDY_GRADIENTS.length];
  const tabs = [
    ["chapters", "章节"],
    ["outline", "大纲"],
    ["notes", "笔记"],
    ["questions", "疑问"],
  ];
  panel.innerHTML = `
    <div class="lp-card">
      <div class="lp-head">
        <div class="lp-emoji" style="background:${grad}">${emojiFor(p.subject)}</div>
        <div class="lp-id">
          <div class="lp-title">${esc(p.subject)}</div>
          <div class="lp-meta">${esc(p.level_from || "零基础")} → ${esc(p.level_to || "入门")}</div>
        </div>
      </div>
      <div class="lp-track"><div class="lp-fill" style="width:${pct}%"></div></div>
      <div class="lp-pct">${done}/${total} 章 · ${pct}%</div>
    </div>
    <div class="lp-card">
      <div class="lp-tabs">
        ${tabs.map(
          ([key, label]) =>
            `<button class="lp-tab ${key === lpTab ? "active" : ""}" data-tab="${key}">${label}</button>`
        ).join("")}
      </div>
      <div class="lp-body" id="lp-body"></div>
    </div>
    <button class="btn lp-open" id="lp-open-studio">🎓 打开学习工作室</button>`;

  panel.querySelectorAll(".lp-tab").forEach((b) => {
    b.onclick = () => {
      lpTab = b.dataset.tab;
      renderLearningPanel();
    };
  });
  panel.querySelector("#lp-open-studio").onclick = () => openLearningCenter();
  renderLpBody();
}

function renderLpBody() {
  const body = $("#lp-body");
  if (!body || !lpDetail || !lpDetail.project) return;
  const d = lpDetail;

  if (lpTab === "chapters") {
    const chs = d.chapters || [];
    if (!chs.length) {
      body.innerHTML = '<div class="lp-empty">还没有生成章节</div>';
      return;
    }
    body.innerHTML = chs
      .map((ch, i) => {
        const done = ch.status === "done";
        return (
          `<div class="lp-chapter ${done ? "done" : ""}" data-cid="${ch.id}">` +
          '<button class="lp-ch-dot" data-act="toggle" title="' + (done ? "取消完成" : "标记完成") + '"></button>' +
          `<div class="lp-ch-txt"><div class="lp-ch-title">${i + 1}. ${esc(ch.title)}</div>` +
          (ch.objective ? `<div class="lp-ch-obj">${esc(ch.objective)}</div>` : "") +
          `</div><button class="btn small lp-ch-start" data-act="start" title="在对话中开始本章">开始</button></div>`
        );
      })
      .join("");
    body.querySelectorAll('[data-act="toggle"]').forEach((btn) => {
      btn.onclick = async () => {
        const item = btn.closest(".lp-chapter");
        const ch = (lpDetail.chapters || []).find((c) => c.id === item.dataset.cid);
        if (!ch) return;
        const next = ch.status === "done" ? "pending" : "done";
        btn.disabled = true;
        try {
          await api(`/api/learning/chapters/${ch.id}`, {
            method: "PUT",
            body: JSON.stringify({ status: next }),
          });
          ch.status = next;
          lpDetail.progress.done = lpDetail.chapters.filter((c) => c.status === "done").length;
          renderLearningPanel();
          loadLearningProjects();
        } catch (e) {
          toast("更新失败: " + e.message);
          btn.disabled = false;
        }
      };
    });
    body.querySelectorAll('[data-act="start"]').forEach((btn) => {
      btn.onclick = async () => {
        const cid = btn.closest(".lp-chapter").dataset.cid;
        try {
          const r = await api(`/api/learning/chapters/${cid}/start`, { method: "POST" });
          toast(`已在对话中开始：${r.title}`);
          sideMode = "conv";
          updateSidebarMode();
          loadConversations();
        } catch (e) {
          toast("开始失败: " + e.message);
        }
      };
    });
    return;
  }

  if (lpTab === "questions") {
    const qs = d.questions || [];
    if (!qs.length) {
      body.innerHTML = '<div class="lp-empty">还没有挂起的疑问</div>';
      return;
    }
    body.innerHTML = qs
      .map(
        (q) =>
          `<div class="lp-question">${esc(q.question)}` +
          `<div class="q-meta">${esc(q.concept || "概念")} · ${esc(q.source_ref || "出处")}</div></div>`
      )
      .join("");
    return;
  }

  const filename = lpTab === "outline" ? "大纲.md" : "笔记.md";
  const note = (d.notes || []).find((n) => n.filename === filename);
  if (!note || !note.content) {
    body.innerHTML = '<div class="lp-empty">这份文档还没有内容</div>';
    return;
  }
  body.innerHTML = '<div class="lp-doc"></div>';
  renderMessageContent(body.querySelector(".lp-doc"), note.content);
}

// ---------- 三栏拖动（带最小/最大限制 + 记忆宽度） ----------

(function initColumnResize() {
  const app = $("#app");
  if (!app) return;
  const MIN = { left: 190, right: 260 };
  const MAX = { left: 430, right: 560 };
  const CHAT_MIN = 380;
  let cols = { left: 232, right: 344 };
  try {
    const saved = JSON.parse(localStorage.getItem("gili_cols") || "null");
    if (saved && typeof saved.left === "number" && typeof saved.right === "number") {
      cols = saved;
    }
  } catch { /* 忽略损坏的本地记录 */ }

  const apply = () => {
    app.style.setProperty("--col-left", `${cols.left}px`);
    app.style.setProperty("--col-right", `${cols.right}px`);
    try { localStorage.setItem("gili_cols", JSON.stringify(cols)); } catch { /* 隐私模式 */ }
  };
  apply();

  $$(".col-divider").forEach((div) => {
    div.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const side = div.dataset.side;
      const startX = e.clientX;
      const startW = side === "left" ? cols.left : cols.right;
      const win = window.innerWidth;
      document.body.classList.add("col-resizing");
      div.classList.add("dragging");

      const move = (ev) => {
        const delta = side === "left" ? ev.clientX - startX : startX - ev.clientX;
        let w = startW + delta;
        if (side === "left") {
          w = Math.max(MIN.left, Math.min(MAX.left, w, win - cols.right - CHAT_MIN));
        } else {
          w = Math.max(MIN.right, Math.min(MAX.right, w, win - cols.left - CHAT_MIN));
        }
        if (side === "left") cols.left = Math.round(w);
        else cols.right = Math.round(w);
        apply();
      };
      const up = () => {
        document.body.classList.remove("col-resizing");
        div.classList.remove("dragging");
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
  });
})();

// ---------- 新建对话 / 批量删除：同步侧栏状态 ----------

$("#btn-new-chat").onclick = async () => {
  try {
    const conv = await api("/api/conversations", { method: "POST" });
    currentConv = conv.id;
    $("#chat-title").textContent = conv.title;
    $("#messages").innerHTML = "";
    updateEmptyState();
    await refreshCurrentProject();
    appendMessage("assistant", "新对话已创建。聊天内容会自动保存在本地，随时可以回来继续。");
    await loadConversations();
  } catch (e) {
    toast("新建失败: " + e.message);
  }
};

$("#btn-multi-confirm").onclick = async () => {
  if (!multiSelected.size) return toast("先勾选要删除的对话");
  if (!confirm(`删除选中的 ${multiSelected.size} 个对话？消息和摘要将永久删除，不可恢复。`)) return;
  let ok = 0;
  for (const cid of multiSelected) {
    try {
      await api(`/api/conversations/${cid}`, { method: "DELETE" });
      ok++;
    } catch { /* 跳过 */ }
  }
  if (currentConv && multiSelected.has(currentConv)) {
    currentConv = null;
    $("#chat-title").textContent = "未命名对话";
    $("#messages").innerHTML = "";
    updateEmptyState();
    refreshCurrentProject();
  }
  toast(`已删除 ${ok} 个对话`);
  setMultiMode(false);
  loadConversations();
};
