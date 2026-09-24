/* 合集选择器：把视频加入已有合集；用户从未建过合集时自动创建并加入「默认合集」。
 *
 * 侧边栏历史行 / 历史列表行 / 结果页共用。纯前端编排，复用 /api/me/collections
 * 现有接口（list / create / items），不新增后端端点。成功后 bus.emit('library:changed')
 * 让侧边栏合集计数同步刷新。
 */
import { $, escapeHtml, bus, me } from './core.js';

const DEFAULT_COLL_NAME = '默认合集';

/* ---------- 轻量 toast（全局单例） ---------- */
let toastTimer = null;
export const toast = (msg, kind = 'ok') => {
  let el = $('#mp-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'mp-toast';
    el.className = 'fixed left-1/2 top-4 z-50 -translate-x-1/2 rounded-full px-4 py-2 text-sm font-semibold text-white shadow-card transition';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.remove('hidden', 'bg-rose-500', 'bg-slate-800');
  el.classList.add(kind === 'err' ? 'bg-rose-500' : 'bg-slate-800');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 2200);
};

const closePicker = (overlay) => overlay.remove();

/* ---------- 选择器弹窗 ---------- */
const openPicker = (colls, target) => {
  const overlay = document.createElement('div');
  overlay.className = 'fixed inset-0 z-50 grid place-items-center bg-black/40 p-4';
  overlay.innerHTML = `
    <div class="w-full max-w-sm rounded-2xl border border-slate-200 bg-white p-4 shadow-card">
      <div class="flex items-center justify-between">
        <h3 class="text-sm font-bold text-slate-900">加入合集</h3>
        <button type="button" class="cp-close rounded p-1 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600" title="关闭">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
      </div>
      <p class="mt-1 truncate text-xs text-slate-400">${escapeHtml(target.title || target.url || '')}</p>
      <div class="mt-3 max-h-64 space-y-1 overflow-y-auto">
        ${colls.map((c) => `
          <button type="button" data-coll="${escapeHtml(c.id)}"
            class="cp-item flex w-full items-center justify-between gap-2 rounded-xl border border-slate-200 px-3 py-2 text-left text-sm text-slate-700 transition hover:border-brand-300 hover:bg-brand-50">
            <span class="min-w-0 flex-1 truncate">${escapeHtml(c.name)}</span>
            <span class="shrink-0 text-xs text-slate-400">${c.count || 0}</span>
          </button>`).join('')}
      </div>
      <div class="mt-3 flex items-center gap-2 border-t border-slate-100 pt-3">
        <input class="cp-name w-full rounded-xl border border-slate-200 px-3 py-2 text-sm outline-none focus:border-brand-400" placeholder="新建合集名称…" />
        <button type="button" class="cp-create shrink-0 rounded-xl bg-brand-500 px-3 py-2 text-sm font-bold text-white transition hover:bg-brand-600">新建并加入</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const done = () => { bus.emit('library:changed'); closePicker(overlay); };
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closePicker(overlay); });
  overlay.querySelector('.cp-close').addEventListener('click', () => closePicker(overlay));

  overlay.querySelectorAll('.cp-item').forEach((btn) => btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      await me.addItem(btn.dataset.coll, target);
      toast('已加入合集');
      done();
    } catch (err) {
      btn.disabled = false;
      toast(err.status === 400 ? '该视频已在此合集中' : (err.message || '加入失败'), 'err');
    }
  }));

  overlay.querySelector('.cp-create').addEventListener('click', async () => {
    const name = overlay.querySelector('.cp-name').value.trim();
    if (!name) { toast('请输入合集名称', 'err'); return; }
    try {
      const created = await me.createCollection(name);
      await me.addItem(created.collection.id, target);
      toast('已新建并加入合集');
      done();
    } catch (err) {
      toast(err.message || '创建失败', 'err');
    }
  });
};

/* ---------- 入口：无合集 → 自动建「默认合集」并加入；有合集 → 弹选择器 ----------
 * target: { content_key, url, title }（与后端 ItemBody 同构）
 */
export const addToCollection = async (target) => {
  if (!target || !target.content_key) { toast('缺少视频标识，无法加入合集', 'err'); return; }
  try {
    const { res, data } = await me.collections();
    if (!res.ok) { toast('加载合集失败', 'err'); return; }
    const colls = data.collections || [];
    if (!colls.length) {
      const created = await me.createCollection(DEFAULT_COLL_NAME, '系统默认合集');
      await me.addItem(created.collection.id, target);
      bus.emit('library:changed');
      toast(`已加入${DEFAULT_COLL_NAME}`);
      return;
    }
    openPicker(colls, target);
  } catch (err) {
    toast(err.message || '加入合集失败', 'err');
  }
};
