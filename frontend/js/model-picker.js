/* 「选择模型」弹窗：设置「一键 AI 分析」（总结/思维导图/问答）默认调用的模型。
 *
 * 数据来自 GET /api/ai/models（模型目录 + 各服务商可用性 + 全局默认 + 当前选择），
 * 保存走 PATCH /api/auth/me/ai-settings（provider/model 同设同清；null/null = 跟随平台默认）。
 * 需要登录时先经 ensureAuth 弹登录框，登录成功后自动续开（pendingAction 机制）。
 *
 * 依赖：core.js（$ / $$ / state / bus / escapeHtml / ai）；auth-ui.js（openAuth / ensureAuth）。
 * 本模块由 main.js 装配，auth-ui 不反向依赖本模块，规避循环引用。
 */
import { $, state, bus, escapeHtml, ai, modelDisplayName } from './core.js';
import { ensureAuth } from './auth-ui.js';

let modal = null;

const CHECK_SVG = '<svg viewBox="0 0 24 24" class="h-4 w-4 text-brand-500" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>';
const SPARK_SVG = '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="currentColor"><path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3z"/></svg>';

const close = () => {
  if (modal) { modal.remove(); modal = null; }
};


// 保存选择：同设同清；null/null 恢复跟随平台默认。成功后同步本地态并广播。
const save = async (provider, model, errEl) => {
  try {
    const out = await ai.saveSettings(provider, model);
    if (out.user) state.currentUser = { ...state.currentUser, ...out.user };
    if (state.aiModels) state.aiModels.current = provider ? { provider, model } : null;
    close();
    bus.emit('ai-settings:changed', { provider, model });
  } catch (err) {
    // 401 已由 authFetch 统一触发登录框，这里只提示其他失败
    if (errEl) {
      errEl.textContent = err.message || '保存失败，请稍后重试。';
      errEl.classList.remove('hidden');
    }
  }
};

// 渲染模型清单：搜索过滤 + 服务商分组 + 当前选中标记 + 未配 Key 置灰
const renderList = (data, cur, listEl, errEl) => {
  const providers = (data.providers || []).map((p) => ({
    ...p,
    models: (p.models || []).filter((m) =>
      !state._mpQ || m.id.toLowerCase().includes(state._mpQ)
      || (m.label || '').toLowerCase().includes(state._mpQ)),
  })).filter((p) => !state._mpQ || p.models.length);

  const def = data.default || {};
  const defLabel = def.label
    ? `平台默认（${escapeHtml(def.label)} · ${escapeHtml(def.model || '')}）`
    : '平台默认（尚未配置 AI Key）';
  const followOn = !cur || !cur.provider;
  const isOn = (key, id) => cur && cur.provider === key && cur.model === id;

  const rows = [];
  // 「跟随平台默认」行：恢复跟随全局 env 配置
  rows.push(`
    <button type="button" data-mp-default
      class="flex w-full items-center gap-2.5 rounded-xl px-3 py-2.5 text-left transition ${followOn ? 'bg-brand-50' : 'hover:bg-slate-50'}">
      <span class="grid h-9 w-9 shrink-0 place-items-center rounded-xl ${followOn ? 'bg-brand-500 text-white' : 'bg-slate-100 text-slate-500'}">${SPARK_SVG}</span>
      <span class="min-w-0 flex-1">
        <span class="block text-sm font-semibold text-slate-800">跟随平台默认</span>
        <span class="mt-0.5 block truncate text-xs text-slate-400">${defLabel}</span>
      </span>
      ${followOn ? CHECK_SVG : ''}
    </button>`);

  // 各服务商分组
  providers.forEach((p) => {
    if (!p.models.length) return;
    rows.push(`
      <div class="mt-3 flex items-center gap-2 px-3">
        <span class="text-xs font-bold text-slate-400">${escapeHtml(p.label)}</span>
        ${p.available ? '' : '<span class="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-400">未配置 Key</span>'}
      </div>`);
    p.models.forEach((m) => {
      const on = isOn(p.key, m.id);
      rows.push(`
        <button type="button" data-mp-model="${escapeHtml(p.key)}" data-mp-id="${escapeHtml(m.id)}"
          ${p.available ? '' : 'disabled'}
          class="flex w-full items-center gap-2 rounded-xl px-3 py-2.5 text-left transition ${on ? 'bg-brand-50' : p.available ? 'hover:bg-slate-50' : 'cursor-not-allowed opacity-45'}">
          <span class="min-w-0 flex-1">
            <span class="block text-sm font-semibold text-slate-800">${escapeHtml(m.label || m.id)}</span>
            <span class="mt-0.5 block truncate text-xs text-slate-400">${escapeHtml(m.id)}</span>
          </span>
          ${m.tier ? `<span class="shrink-0 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-bold text-slate-500">${escapeHtml(m.tier)}</span>` : ''}
          ${on ? CHECK_SVG : ''}
        </button>`);
    });
  });

  listEl.innerHTML = rows.join('');
  listEl.querySelector('[data-mp-default]').addEventListener('click', () => save(null, null, errEl));
  listEl.querySelectorAll('[data-mp-model]').forEach((b) =>
    b.addEventListener('click', () => save(b.dataset.mpModel, b.dataset.mpId, errEl)));
};

const buildModal = (data) => {
  close();
  const cur = data.current || null;
  state._mpQ = '';

  modal = document.createElement('div');
  modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4 backdrop-blur-sm';
  modal.innerHTML = `
    <div class="flex max-h-[85vh] w-full max-w-md flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-card">
      <div class="flex items-start justify-between gap-3 border-b border-slate-100 px-6 py-4">
        <div>
          <h3 class="text-lg font-bold text-slate-900">默认模型</h3>
          <p class="mt-0.5 text-xs text-slate-500">一键 AI 分析（总结 / 思维导图 / 问答）调用的模型</p>
        </div>
        <button type="button" data-mp-close aria-label="关闭"
          class="-mr-1 -mt-1 grid h-8 w-8 shrink-0 place-items-center rounded-full text-slate-400 transition hover:bg-slate-100 hover:text-slate-600">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
        </button>
      </div>
      <div class="border-b border-slate-100 px-6 py-3">
        <div class="relative">
          <span class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400">
            <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg>
          </span>
          <input type="search" data-mp-search autocomplete="off" placeholder="搜索模型"
            class="w-full rounded-xl border border-slate-200 bg-slate-50 py-2 pl-9 pr-3 text-sm text-slate-800 placeholder-slate-400 outline-none transition focus:border-brand-400 focus:bg-white" />
        </div>
        <p data-mp-err class="mt-2 hidden text-xs text-rose-600"></p>
      </div>
      <div data-mp-list class="min-h-0 flex-1 overflow-y-auto px-3 py-2"></div>
      <div class="border-t border-slate-100 bg-slate-50/70 px-6 py-2.5">
        <p class="text-[11px] leading-relaxed text-slate-400">当前：${cur && cur.provider ? escapeHtml(modelDisplayName(cur.provider, cur.model) || '已自定义') : '跟随平台默认'}。切换模型后新生成的分析使用新模型，已有结果不受影响。</p>
      </div>
    </div>`;
  document.body.appendChild(modal);

  const listEl = modal.querySelector('[data-mp-list]');
  const errEl = modal.querySelector('[data-mp-err]');
  const searchEl = modal.querySelector('[data-mp-search]');

  renderList(data, cur, listEl, errEl);
  searchEl.addEventListener('input', () => {
    state._mpQ = searchEl.value.trim().toLowerCase();
    renderList(data, cur, listEl, errEl);
  });
  modal.querySelector('[data-mp-close]').addEventListener('click', close);
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
};

export const openModelPicker = () => {
  // 需要登录且未登录：弹登录框，登录成功后自动续开本弹窗
  if (!ensureAuth(() => openModelPicker())) return;
  loadAndOpen();
};

const loadAndOpen = async () => {
  try {
    const { res, data } = await ai.models();
    if (!res.ok) return;
    state.aiModels = data;
    buildModal(data);
  } catch (e) { /* 网络异常静默：不打断用户 */ }
};

export const initModelPicker = () => {
  const btn = $('#nav-model');
  if (btn) btn.addEventListener('click', openModelPicker);
  // 登录态切换时关掉弹窗（未登录不该停留在设置界面）
  bus.on('auth:changed', () => { if (modal) close(); });
};
