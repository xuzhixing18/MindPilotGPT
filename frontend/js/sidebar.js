/* 侧边栏：品牌 + 新解析 + 搜索联想 + 合集列表 + 历史列表 + 登录引导卡 + 折叠。
 *
 * 数据来源 /api/me/sidebar（首屏聚合，免请求瀑布）。登录态变更 / 私有资源变更 /
 * 路由变更分别通过 core.bus 订阅刷新。未登录时私有区渲染登录引导卡。
 */
import { $, state, escapeHtml, relativeTime, kindBadges, bus, me } from './core.js';
import { navigate, currentRoute } from './router.js';
import { openAuth } from './auth-ui.js';
import { addToCollection } from './coll-picker.js';

const COLLAPSE_KEY = 'mp_sidebar_collapsed';

let sidebarEl, bodyEl, searchEl, suggestEl, newParseBtn, toggleBtn;
let recentHistory = [];      // 最近一次 /api/me/sidebar 的历史，供搜索联想本地过滤
let searchTimer = null;

/* ---------- 折叠 ---------- */
const applyCollapsed = (collapsed) => {
  sidebarEl.classList.toggle('hidden', collapsed);
  try { localStorage.setItem(COLLAPSE_KEY, collapsed ? '1' : '0'); } catch (e) { /* 隐私模式忽略 */ }
};

/* ---------- 搜索联想 ---------- */
const hideSuggest = () => suggestEl.classList.add('hidden');

const renderSuggest = (q) => {
  const kw = q.trim().toLowerCase();
  if (!kw) { hideSuggest(); return; }
  const hits = recentHistory
    .filter((h) => (h.title || '').toLowerCase().includes(kw) || (h.url || '').toLowerCase().includes(kw))
    .slice(0, 8);
  if (!hits.length) { hideSuggest(); return; }
  suggestEl.innerHTML = hits.map((h) => `
    <button type="button" class="sb-suggest-item flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-slate-600 transition hover:bg-brand-50"
      data-url="${escapeHtml(h.url || '')}">
      <span class="min-w-0 flex-1 truncate">${escapeHtml(h.title || h.url || '未命名')}</span>
      <span class="flex shrink-0 gap-0.5">${kindBadges(h.kinds)}</span>
    </button>`).join('');
  suggestEl.classList.remove('hidden');
  suggestEl.querySelectorAll('.sb-suggest-item').forEach((btn) =>
    btn.addEventListener('click', () => {
      const url = btn.dataset.url;
      hideSuggest();
      searchEl.value = '';
      if (url) navigate(`/v?url=${encodeURIComponent(url)}`);
    }));
};

const goSearch = () => {
  const q = searchEl.value.trim();
  hideSuggest();
  if (q) navigate(`/search?q=${encodeURIComponent(q)}`);
  searchEl.value = '';   // 跳结果页后清空输入，避免旧词（如手机号）残留在框内
};

/* ---------- 渲染：合集 + 历史（登录态） ---------- */
const renderPrivate = (data) => {
  const colls = data.collections || [];
  const hist = data.recent_history || [];
  recentHistory = hist;

  const collHtml = colls.length
    ? colls.map((c) => `
        <a href="#/c/${encodeURIComponent(c.id)}" class="sb-coll group flex items-center gap-2 rounded-lg px-2.5 py-2 text-sm text-slate-600 transition hover:bg-slate-100"
          data-coll="${escapeHtml(c.id)}">
          <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 text-slate-400" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7h18M3 12h18M3 17h18"/></svg>
          <span class="min-w-0 flex-1 truncate">${escapeHtml(c.name)}</span>
          <span class="shrink-0 text-xs text-slate-400">${c.count || 0}</span>
        </a>`).join('')
    : '<p class="px-2.5 py-1.5 text-xs text-slate-400">还没有合集</p>';

  const histHtml = hist.length
    ? hist.map((h) => `
        <div class="sb-hist-row group relative flex items-center gap-1 rounded-lg pr-1 transition hover:bg-slate-100"
          data-key="${escapeHtml(h.content_key)}" data-url="${escapeHtml(h.url || '')}" data-title="${escapeHtml(h.title || '')}">
          <a href="#/v?url=${encodeURIComponent(h.url || '')}" class="sb-hist min-w-0 flex-1 px-2.5 py-2">
            <span class="block truncate text-sm text-slate-700">${escapeHtml(h.title || h.url || '未命名')}</span>
            <span class="mt-0.5 flex items-center gap-1.5">
              <span class="flex gap-0.5">${kindBadges(h.kinds)}</span>
              <span class="text-[11px] text-slate-400">${escapeHtml(relativeTime(h.updated_at))}</span>
            </span>
          </a>
          <button type="button" class="sb-hist-add shrink-0 rounded p-1.5 text-slate-400 transition hover:bg-brand-50 hover:text-brand-500" title="加入合集">
            <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>
          </button>
        </div>`).join('')
    : '<p class="px-2.5 py-1.5 text-xs text-slate-400">解析视频后，记录会出现在这里</p>';

  bodyEl.innerHTML = `
    <div class="px-3 py-2">
      <div class="mb-1 flex items-center justify-between px-1">
        <span class="text-xs font-semibold text-slate-400">合集</span>
        <button id="sb-new-coll" type="button" class="rounded px-1.5 text-xs font-semibold text-brand-500 transition hover:text-brand-600">+ 新建</button>
      </div>
      <div class="space-y-0.5">${collHtml}</div>
    </div>
    <div class="mt-2 border-t border-slate-100 px-3 py-2">
      <div class="mb-1 flex items-center justify-between px-1">
        <span class="text-xs font-semibold text-slate-400">历史记录</span>
        <a href="#/history" class="rounded px-1.5 text-xs font-semibold text-brand-500 transition hover:text-brand-600">查看全部</a>
      </div>
      <div class="space-y-0.5">${histHtml}</div>
    </div>`;

  // 新建合集：跳合集管理页（创建 UI 在那里）
  const nc = $('#sb-new-coll', bodyEl);
  if (nc) nc.addEventListener('click', () => navigate('/collections?new=1'));

  // 历史行「加入合集」：悬停显示的 + 按钮
  bodyEl.querySelectorAll('.sb-hist-add').forEach((btn) => btn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const row = btn.closest('.sb-hist-row');
    addToCollection({ content_key: row.dataset.key, url: row.dataset.url, title: row.dataset.title });
  }));
  highlightActive();
};

/* ---------- 渲染：登录引导卡（未登录） ---------- */
const renderLoginPrompt = () => {
  recentHistory = [];
  bodyEl.innerHTML = `
    <div class="px-3 py-2">
      <div class="rounded-2xl border border-dashed border-slate-200 bg-white p-4 text-center">
        <div class="mx-auto grid h-10 w-10 place-items-center rounded-xl bg-brand-50 text-brand-500">
          <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3z"/></svg>
        </div>
        <p class="mt-2.5 text-sm font-semibold text-slate-700">登录后同步历史与合集</p>
        <p class="mt-1 text-xs leading-relaxed text-slate-400">解析记录、问答会话与合集将跨设备保存，随时回看。</p>
        <button id="sb-login-btn" type="button"
          class="mt-3 w-full rounded-xl bg-brand-500 py-2 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600">
          登录 / 注册
        </button>
      </div>
    </div>`;
  const btn = $('#sb-login-btn', bodyEl);
  if (btn) btn.addEventListener('click', () => openAuth('login'));
};

/* ---------- 当前路由高亮 ---------- */
const highlightActive = () => {
  const route = currentRoute();
  const path = route ? route.path : '/';
  const query = route ? route.query : null;
  const activeUrl = path === '/v' && query ? query.get('url') : null;
  const collId = path.startsWith('/c/') ? decodeURIComponent(path.slice(3)) : null;

  bodyEl.querySelectorAll('.sb-hist').forEach((a) => {
    const on = activeUrl && a.getAttribute('href') === `#/v?url=${encodeURIComponent(activeUrl)}`;
    a.classList.toggle('bg-brand-50', !!on);
    a.classList.toggle('text-brand-700', !!on);
  });
  bodyEl.querySelectorAll('.sb-coll').forEach((a) => {
    const on = collId && a.dataset.coll === collId;
    a.classList.toggle('bg-brand-50', !!on);
  });
};

/* ---------- 加载 ---------- */
export const reloadSidebar = async () => {
  if (!bodyEl) return;
  if (!state.currentUser) { renderLoginPrompt(); return; }
  try {
    const { res, data } = await me.sidebar();
    if (res.ok) renderPrivate(data);
    else renderLoginPrompt();
  } catch (e) {
    renderLoginPrompt();
  }
};

/* ---------- 初始化 ---------- */
export const initSidebar = () => {
  sidebarEl = $('#sidebar');
  bodyEl = $('#sb-body');
  searchEl = $('#sb-search');
  if (searchEl) searchEl.value = '';   // 清掉浏览器恢复/上次残留的输入，让 placeholder 正常显示
  suggestEl = $('#sb-suggest');
  newParseBtn = $('#sb-new-parse');
  toggleBtn = $('#sb-toggle');

  // 折叠态记忆
  let collapsed = false;
  try { collapsed = localStorage.getItem(COLLAPSE_KEY) === '1'; } catch (e) { /* 忽略 */ }
  applyCollapsed(collapsed);
  if (toggleBtn) toggleBtn.addEventListener('click', () => applyCollapsed(!sidebarEl.classList.contains('hidden')));

  if (newParseBtn) newParseBtn.addEventListener('click', () => navigate('/'));

  // 搜索：300ms 防抖联想 + 回车跳搜索结果
  if (searchEl) {
    searchEl.addEventListener('input', () => {
      clearTimeout(searchTimer);
      const v = searchEl.value;
      searchTimer = setTimeout(() => renderSuggest(v), 300);
    });
    searchEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); goSearch(); } });
    searchEl.addEventListener('focus', () => renderSuggest(searchEl.value));
    // 点击别处收起联想
    document.addEventListener('click', (e) => {
      if (!suggestEl.contains(e.target) && e.target !== searchEl) hideSuggest();
    });
  }

  // 订阅：登录态变更 / 私有资源变更 → 重载；路由变更 → 刷新高亮
  bus.on('auth:changed', () => reloadSidebar());
  bus.on('library:changed', () => reloadSidebar());
  bus.on('route:changed', () => highlightActive());

  reloadSidebar();
};
