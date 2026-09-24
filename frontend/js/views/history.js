/* 历史视图：路由 #/history（全部历史）与 #/search?q=（搜索结果）共用。
 *
 * 数据源 /api/me/history（分页 + q 模糊 + kind 过滤）。列表项点击跳结果页
 * #/v?url=，可单条删除或清空。删除/清空后 bus.emit('library:changed') 让侧边栏同步。
 * 未登录渲染登录引导卡（私有资源永远要求登录）。
 */
import { $, $$, state, escapeHtml, relativeTime, kindBadges, bus, me, KIND_META } from '../core.js';
import { openAuth } from '../auth-ui.js';
import { addToCollection } from '../coll-picker.js';

const PAGE = 20;
// 过滤器：全部 + 五类内容（顺序与结果页 Tab 一致）
const FILTERS = [{ key: '', label: '全部' }, ...Object.keys(KIND_META).map((k) => ({ key: k, label: KIND_META[k].full }))];

let root = null;
let q = '';
let kind = '';
let offset = 0;
let items = [];
let total = 0;
let loading = false;

/* ---------- 渲染：登录引导 ---------- */
const renderLoginPrompt = () => {
  root.innerHTML = `
    <div class="mx-auto max-w-md rounded-3xl border border-dashed border-slate-200 bg-white p-10 text-center shadow-sm">
      <div class="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-brand-50 text-brand-500">
        <svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3z"/></svg>
      </div>
      <h2 class="mt-4 text-lg font-bold text-slate-900">登录后查看历史记录</h2>
      <p class="mt-2 text-sm leading-relaxed text-slate-500">解析记录、问答会话与合集将跨设备保存，随时回看。</p>
      <button id="hist-login" type="button" class="mt-5 rounded-xl bg-brand-500 px-6 py-2.5 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600">登录 / 注册</button>
    </div>`;
  $('#hist-login', root).addEventListener('click', () => openAuth('login'));
};

/* ---------- 渲染：单条历史 ---------- */
const rowHtml = (h) => `
  <div class="hist-row group relative flex items-start gap-3 rounded-2xl border border-slate-200 bg-white p-4 transition hover:border-brand-300 hover:shadow-card"
    data-key="${escapeHtml(h.content_key)}">
    <a href="#/v?url=${encodeURIComponent(h.url || '')}" class="hist-open min-w-0 flex-1">
      <div class="truncate text-sm font-semibold text-slate-800">${escapeHtml(h.title || h.url || '未命名')}</div>
      ${h.title && h.url ? `<div class="mt-0.5 truncate text-xs text-slate-400">${escapeHtml(h.url)}</div>` : ''}
      <div class="mt-2 flex flex-wrap items-center gap-2">
        <span class="flex gap-0.5">${kindBadges(h.kinds)}</span>
        <span class="text-[11px] text-slate-400">${escapeHtml(relativeTime(h.updated_at))}</span>
      </div>
    </a>
    <button type="button" class="hist-add shrink-0 rounded-lg p-1.5 text-slate-400 transition hover:bg-brand-50 hover:text-brand-500" title="加入合集"
      data-url="${escapeHtml(h.url || '')}" data-title="${escapeHtml(h.title || '')}">
      <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>
    </button>
    <button type="button" class="hist-del shrink-0 rounded-lg p-1.5 text-slate-300 opacity-0 transition hover:bg-rose-50 hover:text-rose-500 group-hover:opacity-100" title="删除该记录">
      <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>
    </button>
  </div>`;

/* ---------- 渲染：列表区（工具条 + 列表 + 加载更多） ---------- */
const renderShell = () => {
  const isSearch = q.trim().length > 0;
  root.innerHTML = `
    <div class="mx-auto w-full max-w-3xl">
      <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h1 class="text-xl font-bold text-slate-900">${isSearch ? `搜索“${escapeHtml(q)}”` : '历史记录'}</h1>
        <button id="hist-clear" type="button" class="hidden rounded-lg px-3 py-1.5 text-xs font-semibold text-rose-500 transition hover:bg-rose-50">清空全部</button>
      </div>
      <div class="mb-4 rounded-2xl border border-slate-200 bg-white p-3 shadow-sm">
        <div class="flex items-center gap-2 rounded-xl border border-slate-200 px-3 py-2 focus-within:border-brand-400">
          <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 text-slate-400" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg>
          <input id="hist-q" type="search" value="${escapeHtml(q)}" placeholder="搜索标题或链接…"
            class="w-full bg-transparent text-sm text-slate-800 placeholder-slate-400 outline-none" />
        </div>
        <div id="hist-kinds" class="mt-3 flex flex-wrap gap-2">
          ${FILTERS.map((f) => `
            <button type="button" data-kind="${escapeHtml(f.key)}"
              class="hist-kind rounded-full border px-3 py-1 text-xs font-semibold transition ${f.key === kind ? 'border-brand-400 bg-brand-50 text-brand-600' : 'border-slate-200 bg-white text-slate-500 hover:border-brand-300 hover:text-brand-600'}">${escapeHtml(f.label)}</button>`).join('')}
        </div>
      </div>
      <div id="hist-list" class="space-y-2"></div>
      <div id="hist-more" class="mt-4 text-center"></div>
    </div>`;

  // 搜索框：回车按当前 q 重新加载（就地过滤，不导航）
  const qEl = $('#hist-q', root);
  qEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); q = qEl.value; reload(); }
  });
  // kind 过滤 chip
  $$('.hist-kind', root).forEach((btn) => btn.addEventListener('click', () => {
    kind = btn.dataset.kind;
    $$('.hist-kind', root).forEach((b) => {
      const on = b.dataset.kind === kind;
      b.className = `hist-kind rounded-full border px-3 py-1 text-xs font-semibold transition ${on ? 'border-brand-400 bg-brand-50 text-brand-600' : 'border-slate-200 bg-white text-slate-500 hover:border-brand-300 hover:text-brand-600'}`;
    });
    reload();
  }));
  // 清空全部
  $('#hist-clear', root).addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    if (!window.confirm('确定清空全部历史记录？该操作不可撤销。')) return;
    btn.disabled = true;
    try { await me.clearHistory(); bus.emit('library:changed'); reload(); }
    catch (err) { btn.disabled = false; }
  });
};

/* ---------- 渲染：列表内容 ---------- */
const paintList = () => {
  const listEl = $('#hist-list', root);
  const moreEl = $('#hist-more', root);
  const clearBtn = $('#hist-clear', root);
  if (!listEl) return;

  clearBtn.classList.toggle('hidden', total === 0);

  if (!items.length) {
    listEl.innerHTML = `
      <div class="rounded-2xl border border-dashed border-slate-200 bg-white p-10 text-center text-sm text-slate-400">
        ${q.trim() ? '没有匹配的历史记录。' : '还没有历史记录，解析视频后会出现在这里。'}
      </div>`;
    moreEl.innerHTML = '';
    return;
  }

  listEl.innerHTML = items.map(rowHtml).join('');
  listEl.querySelectorAll('.hist-row').forEach((row) => {
    const key = row.dataset.key;
    row.querySelector('.hist-add').addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const btn = e.currentTarget;
      addToCollection({ content_key: key, url: btn.dataset.url, title: btn.dataset.title });
    });
    row.querySelector('.hist-del').addEventListener('click', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (!window.confirm('删除这条历史记录？相关问答会话也会一并删除。')) return;
      try {
        await me.deleteHistory(key);
        bus.emit('library:changed');
        reload();
      } catch (err) { /* 401 已由 core 处理 */ }
    });
  });

  // 加载更多
  if (items.length < total) {
    moreEl.innerHTML = `<button id="hist-load-more" type="button" class="rounded-xl border border-slate-200 bg-white px-5 py-2 text-sm font-semibold text-slate-600 transition hover:border-brand-300 hover:text-brand-600">加载更多（剩余 ${total - items.length}）</button>`;
    $('#hist-load-more', moreEl).addEventListener('click', () => loadPage(false));
  } else {
    moreEl.innerHTML = items.length ? '<p class="text-xs text-slate-400">已经到底了</p>' : '';
  }
};

/* ---------- 数据加载 ---------- */
const loadPage = async (reset) => {
  if (loading) return;
  loading = true;
  const listEl = $('#hist-list', root);
  if (reset && listEl) listEl.innerHTML = '<div class="p-8 text-center text-sm text-slate-400"><span class="spinner"></span> 加载中…</div>';
  try {
    const nextOffset = reset ? 0 : offset;
    const { res, data } = await me.history({ q, kind, limit: PAGE, offset: nextOffset });
    if (!res.ok) {
      if (res.status === 401) { renderLoginPrompt(); return; }   // core 已触发登录框
      if (listEl) listEl.innerHTML = `<div class="rounded-2xl border border-rose-200 bg-rose-50 p-6 text-sm text-rose-600">${escapeHtml(data.detail || '加载失败')}</div>`;
      return;
    }
    offset = nextOffset + (data.items || []).length;
    items = reset ? (data.items || []) : items.concat(data.items || []);
    total = data.total || 0;
    paintList();
  } catch (e) {
    if (listEl) listEl.innerHTML = `<div class="rounded-2xl border border-rose-200 bg-rose-50 p-6 text-sm text-rose-600">${escapeHtml(e.message || '网络错误')}</div>`;
  } finally {
    loading = false;
  }
};

const reload = () => loadPage(true);

/* ---------- 视图对象 ---------- */
export default {
  title: '历史记录',
  mount(container, ctx) {
    root = container;
    container.classList.add('px-4', 'py-6', 'sm:px-6');
    if (!state.currentUser) { renderLoginPrompt(); return; }
    // /search?q= 用查询词初始化；/history 用可选的 q/kind
    q = ctx.query.get('q') || '';
    kind = ctx.query.get('kind') || '';
    offset = 0;
    items = [];
    total = 0;
    renderShell();
    reload();
  },
  unmount() { root = null; },
};
