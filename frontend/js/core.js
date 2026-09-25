/* MindPilot 前端核心：全局状态、工具函数、请求封装、缓存与常量。
 *
 * 所有视图/模块共享此单例状态，避免跨模块重复拉取。原生 ES Modules，无构建工具。
 * 循环依赖规避：401 的统一处理（弹登录框 / 重渲染）由 auth-ui 通过
 * setUnauthorizedHandler 注册回调，core 只负责触发，不反向依赖 auth-ui。
 */

/* ---------- DOM / 文本工具 ---------- */
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export const escapeHtml = (str = '') =>
  String(str).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export const fmtDuration = (sec) => {
  if (!sec && sec !== 0) return '';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
};

export const fmtSize = (bytes) => {
  if (!bytes) return '';
  const mb = bytes / 1024 / 1024;
  if (mb >= 1024) return `${(mb / 1024).toFixed(2)} GB`;
  return `${mb.toFixed(1)} MB`;
};

// 播放量等数字的中文简写：894000 -> 89.4万
export const fmtCount = (n) => {
  if (!n && n !== 0) return '';
  if (n >= 100000000) return `${(n / 100000000).toFixed(1)}亿`;
  if (n >= 10000) return `${(n / 10000).toFixed(1)}万`;
  return String(n);
};

// 字幕时间戳：支持超过 1 小时（HH:MM:SS）
export const fmtTs = (sec) => {
  if (!sec && sec !== 0) return '';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  const mm = h > 0 ? String(m).padStart(2, '0') : m;
  const ss = String(s).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
};

// 评论时间戳（unix 秒）→ YYYY-MM-DD；无值返回 ''
export const fmtDate = (ts) => {
  const n = Number(ts);
  if (!n) return '';
  const d = new Date(n * 1000);
  if (Number.isNaN(d.getTime())) return '';
  const p = (v) => String(v).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
};

// ISO 时间串 → 「刚刚 / N 分钟前 / N 小时前 / 昨天 / N 天前 / YYYY-MM-DD」，用于历史与侧边栏
export const relativeTime = (iso) => {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const diff = Date.now() - d.getTime();
  const min = Math.floor(diff / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.floor(hr / 24);
  if (day === 1) return '昨天';
  if (day < 30) return `${day} 天前`;
  return fmtDate(Math.floor(d.getTime() / 1000));
};

/* ---------- 全局状态（单例，模块间共享） ---------- */
export const state = {
  aiAvailable: false,   // 由 /api/health 告知：AI 总结按钮可用性提示
  ffmpeg: true,         // 由 /api/health 告知：缺 ffmpeg 时解析首页给出提示
  authEnabled: false,   // 是否提供登录能力（决定登录入口显隐）
  authRequired: false,  // 业务端点是否强制登录（与后端门禁同一判据）
  currentUser: null,    // 已登录用户完整资料（/api/auth/me 回传）或 null
  aiModels: null,      // GET /api/ai/models 回传的目录与当前选择（model-picker 缓存）
};

// 会话级缓存与并发去重：同一 url 重复点击秒回、并发点击只发一次请求
export const txCache = new Map();       // url -> 转写结果
export const sumCache = new Map();      // url -> 总结响应
export const mindmapCache = new Map();  // url -> 思维导图响应
export const commentsCache = new Map(); // url -> 高赞评论响应
export const qaCache = new Map();       // url -> { messages, sessionId } 问答会话（多轮，切 Tab 保留）
const inflight = new Map();             // `${type}:${url}` -> Promise

/* ---------- 轻量事件总线：解耦 401 / 登录态变更 / 侧边栏刷新 ---------- */
const listeners = new Map();   // event -> Set<fn>
export const bus = {
  on(event, fn) {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(fn);
    return () => bus.off(event, fn);
  },
  off(event, fn) { const s = listeners.get(event); if (s) s.delete(fn); },
  emit(event, payload) { const s = listeners.get(event); if (s) s.forEach((fn) => fn(payload)); },
};

// 登录态一旦变化（登出 / 换号 / 会话失效）即清空「按用户私有」的问答会话缓存：
// qaCache 里存着上一账号的对话与 session_id，不清会串到下一个账号。
// （转写/总结/导图/评论缓存是全局内容缓存，与用户无关，故保留。）
bus.on('auth:changed', () => { qaCache.clear(); });

// 401 统一处理回调（auth-ui 注册）：清态 → 弹登录框
let unauthorizedHandler = null;
export const setUnauthorizedHandler = (fn) => { unauthorizedHandler = fn; };
export const markUnauthorized = () => {
  state.currentUser = null;
  bus.emit('auth:changed', null);
  if (unauthorizedHandler) unauthorizedHandler();
};

/* ---------- 请求封装 ---------- */
// 触发浏览器原生下载（GET，流式，对大文件/手机友好）
export const triggerDownload = (videoUrl, formatId) => {
  const params = new URLSearchParams({ url: videoUrl });
  if (formatId) params.set('format_id', formatId);
  const a = document.createElement('a');
  a.href = `/api/download?${params.toString()}`;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
};

// POST JSON：返回 {res, data}；401 时统一触发登录框（后端门禁为最终裁决）
export const postJson = async (path, body) => {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) markUnauthorized();
  return { res, data };
};

// GET JSON：返回 {res, data}；401 时统一触发登录框
export const getJson = async (path) => {
  const res = await fetch(path);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) markUnauthorized();
  return { res, data };
};

// 认证接口调用：解包 {"detail": ...} 信封，失败抛出带 status 的 Error
export const authFetch = async (url, options = {}) => {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401) markUnauthorized();
    const err = new Error(data.detail || `请求失败 (HTTP ${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
};

// 客户端 single-flight：相同 key 的并发请求复用同一 Promise
export const dedup = (key, factory) => {
  if (inflight.has(key)) return inflight.get(key);
  const p = factory().finally(() => inflight.delete(key));
  inflight.set(key, p);
  return p;
};

/* ---------- /api/me/* 私有资源接口（历史 / 合集 / 问答会话 / 侧边栏） ----------
 * 一律需登录；未登录/跨用户由后端返回 401/404，这里透传 {res, data} 由调用方处理。
 * 语义：GET 用 getJson，写操作用 meFetch（返回 data 或抛带 status 的 Error）。
 */
export const me = {
  sidebar: () => getJson('/api/me/sidebar'),
  history: (params = {}) => {
    const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '' && v != null));
    return getJson(`/api/me/history?${qs.toString()}`);
  },
  historyDetail: (contentKey) => getJson(`/api/me/history/${encodeURIComponent(contentKey)}`),
  deleteHistory: (contentKey) => meFetch(`/api/me/history/${encodeURIComponent(contentKey)}`, { method: 'DELETE' }),
  clearHistory: () => meFetch('/api/me/history', { method: 'DELETE' }),
  collections: () => getJson('/api/me/collections'),
  createCollection: (name, description = '') =>
    meFetch('/api/me/collections', { method: 'POST', body: { name, description } }),
  collection: (id) => getJson(`/api/me/collections/${encodeURIComponent(id)}`),
  updateCollection: (id, patch) =>
    meFetch(`/api/me/collections/${encodeURIComponent(id)}`, { method: 'PATCH', body: patch }),
  deleteCollection: (id) => meFetch(`/api/me/collections/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  addItem: (id, item) => meFetch(`/api/me/collections/${encodeURIComponent(id)}/items`, { method: 'POST', body: item }),
  removeItem: (id, contentKey) =>
    meFetch(`/api/me/collections/${encodeURIComponent(id)}/items/${encodeURIComponent(contentKey)}`, { method: 'DELETE' }),
  sessions: (contentKey = '') => getJson(`/api/me/qa/sessions?content_key=${encodeURIComponent(contentKey)}`),
  createSession: (contentKey, title = '') =>
    meFetch('/api/me/qa/sessions', { method: 'POST', body: { content_key: contentKey, title } }),
  session: (id) => getJson(`/api/me/qa/sessions/${encodeURIComponent(id)}`),
  renameSession: (id, title) => meFetch(`/api/me/qa/sessions/${encodeURIComponent(id)}`, { method: 'PATCH', body: { title } }),
  deleteSession: (id) => meFetch(`/api/me/qa/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }),
};

// 私有资源写操作：成功返回 data，失败抛带 status 的 Error（401 已触发登录框）
async function meFetch(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) markUnauthorized();
  if (!res.ok) {
    const err = new Error(data.detail || `请求失败 (HTTP ${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ---------- AI 模型选择（一键分析默认模型） ----------
 * 目录拉取开放访问（无密钥信息）；保存走 PATCH /api/auth/me/ai-settings（需登录），
 * provider/model 同设同清，null/null = 恢复跟随平台默认。失败抛带 status 的 Error。
 */
export const ai = {
  models: () => getJson('/api/ai/models'),
  saveSettings: (provider, model) => authFetch('/api/auth/me/ai-settings', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider, model }),
  }),
};

// 把 (provider, model) 翻译为展示名「服务商 · 模型」：优先读 /api/ai/models
// 目录的 label（后端已在未指定时回退模型 ID 的大写）；目录未加载时同样
// 回退大写 ID，保证任何路径下未指定展示名的模型都以大写呈现。
export const modelDisplayName = (provider, model) => {
  const data = state.aiModels;
  if (data) {
    for (const p of data.providers || []) {
      if (p.key !== provider) continue;
      if (!model) return `${p.label} · 默认`;
      const m = (p.models || []).find((x) => x.id === model);
      return m ? `${p.label} · ${m.label}` : `${p.label} · ${String(model).toUpperCase()}`;
    }
  }
  return provider ? `${provider} · ${(model || '默认').toUpperCase()}` : '';
};

/* ---------- 字幕导出（纯前端 Blob 下载 SRT / TXT） ---------- */
const srtTime = (sec) => {
  const t = Math.max(0, sec || 0);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = Math.floor(t % 60);
  const ms = Math.round((t - Math.floor(t)) * 1000);
  const p = (v, l = 2) => String(v).padStart(l, '0');
  return `${p(h)}:${p(m)}:${p(s)},${p(ms, 3)}`;
};
export const buildSubtitleText = (segs, kind) => segs
  .map((seg, i) => {
    if (kind !== 'srt') return seg.text;
    const end = seg.end != null ? seg.end : (segs[i + 1] ? seg[i + 1].start : seg.start);
    return `${i + 1}\n${srtTime(seg.start)} --> ${srtTime(end)}\n${seg.text}\n`;
  })
  .join('\n');
const downloadBlob = (filename, blob) => {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
};
export const downloadText = (filename, text, mime) => downloadBlob(filename, new Blob([text], { type: mime }));

/* ---------- 思维导图导出（Markdown 大纲 / Canvas PNG，无第三方库） ---------- */
export const buildMindmapMarkdown = (mm) => {
  const lines = [`# ${mm.title || '思维导图'}`];
  const walk = (n, depth) => (n.children || []).forEach((k) => {
    lines.push(`${'  '.repeat(depth)}- ${k.title || ''}`);
    walk(k, depth + 1);
  });
  walk(mm, 0);
  return lines.join('\n');
};

export const exportMindmapPng = (mm, baseName) => {
  // 先序展开为行 {title, depth}，并记录每个节点的直接子节点行号用于画连接线
  const rows = [];
  const directKids = new Map();   // row -> [childRow...]
  const walk = (n, depth) => {
    const row = rows.length;
    rows.push({ title: n.title || '', depth });
    const direct = (n.children || []).map((k) => walk(k, depth + 1));
    if (direct.length) directKids.set(row, direct);
    return row;
  };
  walk(mm, 0);

  const ROW_H = 40, RECT_H = 30, INDENT = 30, PAD_X = 12, MARGIN = 24;
  const dpr = window.devicePixelRatio || 1;
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  const font = (bold) => `${bold ? '600 ' : ''}14px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif`;
  const xOf = (d) => MARGIN + d * INDENT;
  const yOf = (r) => MARGIN + r * ROW_H;

  // 先按 1x 测量文本宽度以确定画布尺寸
  const widths = rows.map((r, i) => {
    ctx.font = font(i === 0);
    return ctx.measureText(r.title).width + PAD_X * 2;
  });
  const W = Math.max(...rows.map((r, i) => xOf(r.depth) + widths[i])) + MARGIN;
  const H = MARGIN * 2 + (rows.length - 1) * ROW_H + RECT_H;
  canvas.width = Math.ceil(W * dpr);
  canvas.height = Math.ceil(H * dpr);
  ctx.scale(dpr, dpr);

  ctx.fillStyle = '#FFFFFF';
  ctx.fillRect(0, 0, W, H);

  const roundRect = (x, y, w, h, r) => {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  };

  // 连接线：父节点下引竖脊 + 到各直接子节点的横档
  ctx.strokeStyle = '#CBD5E1';
  ctx.lineWidth = 1.5;
  directKids.forEach((kids, row) => {
    const spineX = xOf(rows[row].depth) + 12;
    ctx.beginPath();
    ctx.moveTo(spineX, yOf(row) + RECT_H);
    ctx.lineTo(spineX, yOf(kids[kids.length - 1]) + RECT_H / 2);
    ctx.stroke();
    kids.forEach((c) => {
      ctx.beginPath();
      ctx.moveTo(spineX, yOf(c) + RECT_H / 2);
      ctx.lineTo(xOf(rows[c].depth), yOf(c) + RECT_H / 2);
      ctx.stroke();
    });
  });

  // 节点：根为品牌蓝实心，其余浅灰底
  rows.forEach((r, i) => {
    const x = xOf(r.depth), y = yOf(i), w = widths[i];
    ctx.fillStyle = i === 0 ? '#1677FF' : '#F1F5F9';
    roundRect(x, y, w, RECT_H, 8);
    ctx.fill();
    ctx.font = font(i === 0);
    ctx.fillStyle = i === 0 ? '#FFFFFF' : '#334155';
    ctx.textBaseline = 'middle';
    ctx.fillText(r.title, x + PAD_X, y + RECT_H / 2);
  });

  canvas.toBlob((blob) => { if (blob) downloadBlob(`${baseName}.png`, blob); }, 'image/png');
};

/* ---------- 复用小图标 ---------- */
export const ICON = {
  person: '<svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>',
  eye: '<svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>',
  film: '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 5v14M17 5v14M3 10h4M3 14h4M17 10h4M17 14h4"/></svg>',
  audio: '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18V6l8-2v12"/><circle cx="7" cy="18" r="2"/><circle cx="15" cy="16" r="2"/></svg>',
  slider: '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></svg>',
  dl: '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 21h16"/></svg>',
  spark: '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3z"/></svg>',
  caret: '<svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>',
  copy: '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 012-2h10"/></svg>',
  check: '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>',
  thumb: '<svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 9V5a3 3 0 00-6 0v4H5a2 2 0 00-2 2l1 8a2 2 0 002 2h11a2 2 0 002-1.6l1.2-7A2 2 0 0018.2 9H14z"/><path d="M8 9v12"/></svg>',
};

export const AI_CONFIG_HINT = '请复制 .env.example 为 .env 并填入 API Key 后重启服务。';

/* ---------- 历史 kinds 徽标（侧边栏 / 历史列表 / 合集详情共用） ----------
 * kinds 取值与后端 library.service.KINDS 一致：transcribe/summary/mindmap/comments/qa。
 */
export const KIND_META = {
  summary: { short: '总', full: '总结摘要', cls: 'bg-brand-50 text-brand-600' },
  transcribe: { short: '字', full: '字幕文本', cls: 'bg-emerald-50 text-emerald-600' },
  mindmap: { short: '导', full: '思维导图', cls: 'bg-indigo-50 text-indigo-600' },
  comments: { short: '评', full: '高赞评论', cls: 'bg-amber-50 text-amber-600' },
  qa: { short: '问', full: 'AI 问答', cls: 'bg-rose-50 text-rose-600' },
};
// kinds 数组 → 一排小徽标 HTML（未知 kind 忽略；空则返回 ''）
export const kindBadges = (kinds) => (kinds || [])
  .filter((k) => KIND_META[k])
  .map((k) => `<span class="inline-grid h-4 w-4 place-items-center rounded text-[10px] font-bold ${KIND_META[k].cls}" title="${KIND_META[k].full}">${KIND_META[k].short}</span>`)
  .join('');

/* ---------- 健康检查：拉 /api/health 写入 state（ffmpeg 提示由解析首页读 state 渲染） ---------- */
export const loadHealth = async () => {
  try {
    const res = await fetch('/api/health');
    const data = await res.json();
    state.aiAvailable = !!data.ai;
    state.ffmpeg = data.ffmpeg !== false;
    state.authEnabled = !!data.auth;
    state.authRequired = !!data.auth_required;
  } catch (e) { /* 忽略：按默认（不可用/不禁用）处理 */ }
  return state;
};
