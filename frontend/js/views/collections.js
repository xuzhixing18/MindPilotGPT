/* 合集视图：两个路由共用一个模块，导出两个视图对象。
 *
 *   collectionsView      路由 #/collections —— 合集管理列表（新建 / 重命名 / 删除）
 *   collectionDetailView 路由 #/c/:id       —— 合集详情（条目列表 / 移除条目 / 重命名 / 删除）
 *
 * 数据源 /api/me/collections*。写操作成功后 bus.emit('library:changed') 让侧边栏同步。
 * ?new=1 进入管理页时自动弹出新建框（侧边栏「+ 新建」入口）。未登录渲染登录引导卡。
 */
import { $, state, escapeHtml, relativeTime, bus, me } from '../core.js';
import { navigate } from '../router.js';
import { openAuth } from '../auth-ui.js';

/* ---------- 共用：登录引导卡 ---------- */
const loginPromptHtml = (title) => `
  <div class="mx-auto max-w-md rounded-3xl border border-dashed border-slate-200 bg-white p-10 text-center shadow-sm">
    <div class="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-brand-50 text-brand-500">
      <svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7h18M3 12h18M3 17h18"/></svg>
    </div>
    <h2 class="mt-4 text-lg font-bold text-slate-900">${escapeHtml(title)}</h2>
    <p class="mt-2 text-sm leading-relaxed text-slate-500">登录后可创建合集，把视频与解析内容归类收藏。</p>
    <button type="button" class="coll-login mt-5 rounded-xl bg-brand-500 px-6 py-2.5 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600">登录 / 注册</button>
  </div>`;

const bindLoginPrompt = (root) => {
  const btn = $('.coll-login', root);
  if (btn) btn.addEventListener('click', () => openAuth('login'));
};

/* ---------- 共用：新建 / 重命名合集弹框 ---------- */
// 返回一个覆盖层，onSubmit(name, description) 由调用方处理（成功则关闭）
const openCollModal = ({ title, name = '', description = '', submitLabel, onSubmit }) => {
  const overlay = document.createElement('div');
  overlay.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4';
  overlay.innerHTML = `
    <div class="w-full max-w-md rounded-3xl bg-white p-6 shadow-xl">
      <h3 class="text-lg font-bold text-slate-900">${escapeHtml(title)}</h3>
      <label class="mt-4 block text-xs font-semibold text-slate-500">合集名称</label>
      <input type="text" class="coll-name mt-1 w-full rounded-xl border border-slate-200 px-3.5 py-2.5 text-sm outline-none focus:border-brand-400" value="${escapeHtml(name)}" maxlength="60" placeholder="如：AI 学习清单" />
      <label class="mt-3 block text-xs font-semibold text-slate-500">描述（选填）</label>
      <textarea class="coll-desc mt-1 w-full rounded-xl border border-slate-200 px-3.5 py-2.5 text-sm outline-none focus:border-brand-400" rows="2" maxlength="200" placeholder="一句话描述这个合集">${escapeHtml(description)}</textarea>
      <p class="coll-err mt-2 hidden text-xs text-rose-500"></p>
      <div class="mt-5 flex justify-end gap-2">
        <button type="button" class="coll-cancel rounded-xl border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-600 transition hover:bg-slate-50">取消</button>
        <button type="button" class="coll-ok rounded-xl bg-brand-500 px-5 py-2 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600">${escapeHtml(submitLabel)}</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const nameEl = $('.coll-name', overlay);
  const descEl = $('.coll-desc', overlay);
  const errEl = $('.coll-err', overlay);
  const okBtn = $('.coll-ok', overlay);
  const close = () => overlay.remove();

  const showError = (msg) => { errEl.textContent = msg; errEl.classList.toggle('hidden', !msg); };

  const submit = async () => {
    const n = nameEl.value.trim();
    if (!n) { showError('请输入合集名称。'); nameEl.focus(); return; }
    okBtn.disabled = true;
    try {
      await onSubmit(n, descEl.value.trim());
      close();
    } catch (e) {
      showError(e.message || '操作失败，请重试。');
      okBtn.disabled = false;
    }
  };

  $('.coll-cancel', overlay).addEventListener('click', close);
  okBtn.addEventListener('click', submit);
  nameEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  setTimeout(() => nameEl.focus(), 60);
  return close;
};

/* ========================================================================== */
/* 合集管理列表 #/collections                                                  */
/* ========================================================================== */
let listRoot = null;

const paintCollections = (colls) => {
  const grid = $('#coll-grid', listRoot);
  if (!grid) return;
  if (!colls.length) {
    grid.innerHTML = '<div class="col-span-full rounded-2xl border border-dashed border-slate-200 bg-white p-10 text-center text-sm text-slate-400">还没有合集，点击右上角「新建合集」开始整理。</div>';
    return;
  }
  grid.innerHTML = colls.map((c) => `
    <div class="coll-card group relative rounded-2xl border border-slate-200 bg-white p-5 transition hover:border-brand-300 hover:shadow-card" data-id="${escapeHtml(c.id)}">
      <a href="#/c/${encodeURIComponent(c.id)}" class="block">
        <div class="flex items-center gap-2">
          <span class="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-brand-50 text-brand-500">
            <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7h18M3 12h18M3 17h18"/></svg>
          </span>
          <span class="min-w-0 flex-1 truncate text-sm font-bold text-slate-800">${escapeHtml(c.name)}</span>
        </div>
        ${c.description ? `<p class="mt-2 line-clamp-2 text-xs leading-relaxed text-slate-400">${escapeHtml(c.description)}</p>` : ''}
        <div class="mt-3 flex items-center gap-3 text-[11px] text-slate-400">
          <span>${c.count || 0} 个视频</span>
          <span>· ${escapeHtml(relativeTime(c.updated_at))}</span>
        </div>
      </a>
      <div class="absolute right-3 top-3 flex gap-1 opacity-0 transition group-hover:opacity-100">
        <button type="button" class="coll-edit rounded-lg p-1.5 text-slate-400 transition hover:bg-brand-50 hover:text-brand-600" title="重命名">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg>
        </button>
        <button type="button" class="coll-del rounded-lg p-1.5 text-slate-400 transition hover:bg-rose-50 hover:text-rose-500" title="删除">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>
        </button>
      </div>
    </div>`).join('');

  grid.querySelectorAll('.coll-card').forEach((card) => {
    const id = card.dataset.id;
    const coll = colls.find((c) => c.id === id) || {};
    card.querySelector('.coll-edit').addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation();
      openCollModal({
        title: '重命名合集', name: coll.name || '', description: coll.description || '', submitLabel: '保存',
        onSubmit: async (n, d) => { await me.updateCollection(id, { name: n, description: d }); bus.emit('library:changed'); loadCollections(); },
      });
    });
    card.querySelector('.coll-del').addEventListener('click', async (e) => {
      e.preventDefault(); e.stopPropagation();
      if (!window.confirm(`删除合集「${coll.name || ''}」？合集内条目会一并移除（不影响历史记录）。`)) return;
      try { await me.deleteCollection(id); bus.emit('library:changed'); loadCollections(); } catch (err) { /* 401 已处理 */ }
    });
  });
};

const loadCollections = async () => {
  const grid = $('#coll-grid', listRoot);
  if (grid) grid.innerHTML = '<div class="col-span-full p-8 text-center text-sm text-slate-400"><span class="spinner"></span> 加载中…</div>';
  try {
    const { res, data } = await me.collections();
    if (!res.ok) { if (res.status === 401) { listRoot.innerHTML = loginPromptHtml('登录后查看合集'); bindLoginPrompt(listRoot); } return; }
    paintCollections(data.collections || []);
  } catch (e) {
    if (grid) grid.innerHTML = `<div class="col-span-full rounded-2xl border border-rose-200 bg-rose-50 p-6 text-sm text-rose-600">${escapeHtml(e.message || '网络错误')}</div>`;
  }
};

export const collectionsView = {
  title: '合集',
  mount(container, ctx) {
    listRoot = container;
    container.classList.add('px-4', 'py-6', 'sm:px-6');
    if (!state.currentUser) { container.innerHTML = loginPromptHtml('登录后查看合集'); bindLoginPrompt(container); return; }

    container.innerHTML = `
      <div class="mx-auto w-full max-w-4xl">
        <div class="mb-5 flex items-center justify-between gap-3">
          <h1 class="text-xl font-bold text-slate-900">我的合集</h1>
          <button id="coll-new" type="button" class="inline-flex items-center gap-1.5 rounded-xl bg-brand-500 px-4 py-2 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600">
            <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>新建合集
          </button>
        </div>
        <div id="coll-grid" class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3"></div>
      </div>`;

    const openCreate = () => openCollModal({
      title: '新建合集', submitLabel: '创建',
      onSubmit: async (n, d) => {
        const data = await me.createCollection(n, d);
        bus.emit('library:changed');
        const id = data && data.collection ? data.collection.id : null;
        if (id) navigate(`/c/${encodeURIComponent(id)}`);
        else loadCollections();
      },
    });

    $('#coll-new', container).addEventListener('click', openCreate);
    loadCollections();
    if (ctx.query.get('new') === '1') openCreate();
  },
  unmount() { listRoot = null; },
};

/* ========================================================================== */
/* 合集详情 #/c/:id                                                            */
/* ========================================================================== */
let detailRoot = null;
let detailId = null;

const paintDetail = (coll) => {
  const itemsWrap = $('#coll-items', detailRoot);
  if (!itemsWrap) return;
  $('#coll-title', detailRoot).textContent = coll.name || '合集';
  const descEl = $('#coll-desc', detailRoot);
  descEl.textContent = coll.description || '';
  descEl.classList.toggle('hidden', !coll.description);
  $('#coll-count', detailRoot).textContent = `${(coll.items || []).length} 个视频`;

  const items = coll.items || [];
  if (!items.length) {
    itemsWrap.innerHTML = '<div class="rounded-2xl border border-dashed border-slate-200 bg-white p-10 text-center text-sm text-slate-400">这个合集还是空的。在结果页解析视频后，可将其加入合集。</div>';
    return;
  }
  itemsWrap.innerHTML = items.map((it) => `
    <div class="coll-item group flex items-start gap-3 rounded-2xl border border-slate-200 bg-white p-4 transition hover:border-brand-300 hover:shadow-card" data-key="${escapeHtml(it.content_key)}">
      <a href="#/v?url=${encodeURIComponent(it.url || '')}" class="min-w-0 flex-1">
        <div class="truncate text-sm font-semibold text-slate-800">${escapeHtml(it.title || it.url || '未命名')}</div>
        ${it.title && it.url ? `<div class="mt-0.5 truncate text-xs text-slate-400">${escapeHtml(it.url)}</div>` : ''}
        <div class="mt-1.5 text-[11px] text-slate-400">加入于 ${escapeHtml(relativeTime(it.added_at))}</div>
      </a>
      <button type="button" class="coll-item-del shrink-0 rounded-lg p-1.5 text-slate-300 opacity-0 transition hover:bg-rose-50 hover:text-rose-500 group-hover:opacity-100" title="从合集移除">
        <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
      </button>
    </div>`).join('');

  itemsWrap.querySelectorAll('.coll-item').forEach((row) => {
    const key = row.dataset.key;
    row.querySelector('.coll-item-del').addEventListener('click', async (e) => {
      e.preventDefault(); e.stopPropagation();
      try { await me.removeItem(detailId, key); bus.emit('library:changed'); loadDetail(); } catch (err) { /* 401 已处理 */ }
    });
  });
};

const loadDetail = async () => {
  const itemsWrap = $('#coll-items', detailRoot);
  if (itemsWrap) itemsWrap.innerHTML = '<div class="p-8 text-center text-sm text-slate-400"><span class="spinner"></span> 加载中…</div>';
  try {
    const { res, data } = await me.collection(detailId);
    if (!res.ok) {
      if (res.status === 401) { detailRoot.innerHTML = loginPromptHtml('登录后查看合集'); bindLoginPrompt(detailRoot); return; }
      detailRoot.innerHTML = `
        <div class="mx-auto max-w-md rounded-3xl border border-slate-200 bg-white p-10 text-center shadow-sm">
          <p class="text-sm text-slate-500">${escapeHtml(data.detail || '合集不存在或已被删除。')}</p>
          <a href="#/collections" class="mt-4 inline-block rounded-xl bg-brand-500 px-5 py-2 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600">返回合集列表</a>
        </div>`;
      return;
    }
    paintDetail(data);
  } catch (e) {
    if (itemsWrap) itemsWrap.innerHTML = `<div class="rounded-2xl border border-rose-200 bg-rose-50 p-6 text-sm text-rose-600">${escapeHtml(e.message || '网络错误')}</div>`;
  }
};

export const collectionDetailView = {
  title: '合集详情',
  mount(container, ctx) {
    detailRoot = container;
    detailId = ctx.params.id;
    container.classList.add('px-4', 'py-6', 'sm:px-6');
    if (!state.currentUser) { container.innerHTML = loginPromptHtml('登录后查看合集'); bindLoginPrompt(container); return; }

    container.innerHTML = `
      <div class="mx-auto w-full max-w-3xl">
        <a href="#/collections" class="mb-3 inline-flex items-center gap-1 text-sm text-slate-500 transition hover:text-brand-600">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 18l-6-6 6-6"/></svg>全部合集
        </a>
        <div class="mb-5 flex flex-wrap items-start justify-between gap-3">
          <div class="min-w-0">
            <h1 id="coll-title" class="truncate text-xl font-bold text-slate-900">合集</h1>
            <p id="coll-desc" class="mt-1 hidden text-sm text-slate-500"></p>
            <p id="coll-count" class="mt-1 text-xs text-slate-400">0 个视频</p>
          </div>
          <div class="flex shrink-0 gap-2">
            <button id="coll-rename" type="button" class="rounded-xl border border-slate-200 bg-white px-4 py-2 text-sm font-semibold text-slate-600 transition hover:border-brand-300 hover:text-brand-600">重命名</button>
            <button id="coll-delete" type="button" class="rounded-xl border border-rose-200 bg-white px-4 py-2 text-sm font-semibold text-rose-500 transition hover:bg-rose-50">删除合集</button>
          </div>
        </div>
        <div id="coll-items" class="space-y-2"></div>
      </div>`;

    // 重命名：先拉取当前值预填弹框
    $('#coll-rename', container).addEventListener('click', async () => {
      try {
        const { res, data } = await me.collection(detailId);
        if (!res.ok) return;
        openCollModal({
          title: '重命名合集', name: data.name || '', description: data.description || '', submitLabel: '保存',
          onSubmit: async (n, d) => { await me.updateCollection(detailId, { name: n, description: d }); bus.emit('library:changed'); loadDetail(); },
        });
      } catch (e) { /* 忽略 */ }
    });
    $('#coll-delete', container).addEventListener('click', async () => {
      if (!window.confirm('删除该合集？合集内条目会一并移除（不影响历史记录）。')) return;
      try { await me.deleteCollection(detailId); bus.emit('library:changed'); navigate('/collections'); } catch (e) { /* 401 已处理 */ }
    });

    loadDetail();
  },
  unmount() { detailRoot = null; detailId = null; },
};
