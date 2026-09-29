/* 随手笔记面板（结果页 Tab）：一视频多篇笔记，Markdown 源文 + 编辑/预览双模式。
 *
 * 组件不感知 result.js 内部：播放器能力经 ctx 注入（getCtrl / onPlayer / mountPlayer），
 * 避免 notes.js 反向依赖 result 的模块级 playerCtrl 单例。
 *
 * 关键机制：
 * - 时间戳标记：`@[03:12](t=192)` 前后端单一约定；插入时读 playerCtrl.getTime()，
 *   B站外链播放器无回读 → 回退字幕 Tab 当前高亮行的 data-start；
 * - 截图三通道：native <video>（同源流代理）前端 Canvas 抓帧；B站/YouTube 跨域
 *   iframe → 后端 /api/me/notes/frames（本地流播缓存 → 远程直连抽帧 → 封面兜底）；
 *   播放器未挂载 → 先挂载再截，永不出现死按钮；
 * - 自动保存：800ms 防抖，PATCH 携带 base_updated_at 乐观并发基线；409 时内联
 *   二选一（用我的版本覆盖 / 改用服务端版本）；beforeunload 走 fetch keepalive 兜底；
 * - 未登录渲染登录引导卡（私有资源永远要求登录）。
 */
import { $, state, escapeHtml, fmtTs, relativeTime, bus, me, notesCache, copyText } from '../core.js';
import { openAuth } from '../auth-ui.js';

const SAVE_DEBOUNCE = 800;   // 自动保存防抖（ms）
const NOTE_IMAGE_API = '/api/me/notes/images';

/* beforeunload 兜底：注册中的编辑器统一 flush（fetch keepalive，页面卸载也能送达）。
 * PATCH 无法走 sendBeacon（仅 POST），keepalive 是等价的卸载期投递机制。 */
const flushers = new Set();
window.addEventListener('beforeunload', () => {
  flushers.forEach((fn) => { try { fn(); } catch (e) { /* 尽力投递 */ } });
});

/* ---------- Markdown 子集渲染（先全文转义再白名单替换，XSS 面最小） ---------- */
const IMG_URL_RE = /^\/api\/me\/notes\/images\/[0-9a-f]+$/;
const TS_MARK_MD = /@\[([0-9:]+)\]\(t=([0-9.]+)\)/g;

const mdInline = (text) => {
  let s = escapeHtml(text);
  // 图片：仅私有图片 API 前缀可渲染为 <img>（外链图一律当文本，防 SSRF/外链追踪）
  s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) =>
    IMG_URL_RE.test(url)
      ? `<img src="${url}" alt="${alt}" class="note-img" loading="lazy" />`
      : m);
  // 链接：仅 http(s)，新窗打开
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer" class="note-link">$1</a>');
  // 时间戳标记 → 时钟图标 + 时间（点击跳播），样式对齐竞品：灰盘红针时钟、无胶囊徽标
  s = s.replace(TS_MARK_MD,
    '<button type="button" class="note-ts" data-t="$2" title="跳转到该时间点">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke-width="2"><circle cx="12" cy="12" r="9" stroke="#94A3B8"/>' +
    '<path d="M12 8v4l2.5 1.5" stroke="#EF4444" stroke-linecap="round"/></svg>$1</button>');
  // 行内代码（先于粗斜体，避免 ** 被 ` 保护后的二次处理干扰）
  s = s.replace(/`([^`]+)`/g, '<code class="note-code">$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<i>$2</i>');
  return s;
};

const renderMarkdown = (body) => {
  const lines = String(body || '').split('\n');
  const out = [];
  let listOpen = false;
  const closeList = () => { if (listOpen) { out.push('</ul>'); listOpen = false; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (/^#{1,3}\s+/.test(line)) {
      closeList();
      const level = line.match(/^#+/)[0].length;
      out.push(`<h${level} class="note-h${level}">${mdInline(line.replace(/^#+\s+/, ''))}</h${level}>`);
    } else if (/^[-*]\s+/.test(line)) {
      if (!listOpen) { out.push('<ul class="note-ul">'); listOpen = true; }
      out.push(`<li>${mdInline(line.replace(/^[-*]\s+/, ''))}</li>`);
    } else if (/^>\s?/.test(line)) {
      closeList();
      out.push(`<blockquote class="note-quote">${mdInline(line.replace(/^>\s?/, ''))}</blockquote>`);
    } else if (!line) {
      closeList();
    } else {
      closeList();
      out.push(`<p class="note-p">${mdInline(line)}</p>`);
    }
  }
  closeList();
  return out.join('');
};

/* ---------- 面板组件 ---------- */
export const renderNotesPanel = (panel, ctx) => {
  const { contentKey, url, title, getCtrl, onPlayer, mountPlayer } = ctx;
  if (panel._notesOff) { try { panel._notesOff(); } catch (e) { /* 旧订阅 */ } panel._notesOff = null; }

  /* 未登录：登录引导卡（私有资源永远要求登录，与 history.js 同风格） */
  const renderLoginPrompt = () => {
    panel.innerHTML = `
      <div class="mx-auto flex max-w-md flex-1 flex-col items-center justify-center rounded-3xl border border-dashed border-slate-200 bg-white p-10 text-center shadow-sm">
        <div class="grid h-12 w-12 place-items-center rounded-2xl bg-cyan-50 text-cyan-500">
          <svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
        </div>
        <h2 class="mt-4 text-lg font-bold text-slate-900">登录后开始记笔记</h2>
        <p class="mt-2 text-sm leading-relaxed text-slate-500">随手笔记仅自己可见，跨设备同步保存；支持时间戳跳播与视频截图。</p>
        <button class="notes-login mt-5 rounded-xl bg-brand-500 px-6 py-2.5 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600" type="button">登录 / 注册</button>
      </div>`;
    panel.querySelector('.notes-login').addEventListener('click', () => openAuth('login'));
  };

  if (!state.currentUser) { renderLoginPrompt(); return; }

  /* 状态：列表 + 当前编辑器（body/title 为工作副本，base 为并发基线） */
  const st = {
    notes: [],
    activeId: null,
    edit: null,          // { id, body, title, base, dirty, saving, conflict }
    preview: false,
    timer: null,
    toast: '',           // 轻提示（截断 / 截图失败等）
  };
  const byId = (id) => st.notes.find((n) => n.id === id);

  /* ---------- 面板骨架 ---------- */
  panel.innerHTML = `
    <div class="fade-in flex h-full min-h-0 flex-col">
      <div class="flex items-center justify-between gap-2">
        <h4 class="inline-flex items-center gap-2 text-base font-bold text-slate-900">
          <svg viewBox="0 0 24 24" class="h-5 w-5 text-cyan-500" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
          随手笔记
          <span class="notes-count rounded-full bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-500"></span>
        </h4>
        <button type="button" class="notes-new inline-flex items-center gap-1 rounded-xl bg-cyan-500 px-3 py-1.5 text-sm font-bold text-white shadow-sm transition hover:bg-cyan-600 active:scale-95">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>新建笔记
        </button>
      </div>
      <div class="notes-toast mt-1 text-xs text-amber-600"></div>
      <div class="notes-list mt-2 max-h-44 shrink-0 space-y-2 overflow-y-auto pr-1"></div>
      <div class="notes-editor mt-3 flex min-h-0 flex-1 flex-col"></div>
    </div>`;

  const listEl = $('.notes-list', panel);
  const editorEl = $('.notes-editor', panel);
  const countEl = $('.notes-count', panel);
  const toastEl = $('.notes-toast', panel);

  const setToast = (msg) => { st.toast = msg; toastEl.textContent = msg; if (msg) setTimeout(() => { if (st.toast === msg) setToast(''); }, 4000); };

  /* ---------- 列表渲染 ---------- */
  const noteTitle = (n) => (n.title || '').trim() || (n.body || '').split('\n').map((l) => l.replace(TS_MARK_MD, '$1').trim()).filter(Boolean)[0] || '无标题笔记';

  const paintList = () => {
    countEl.textContent = st.notes.length ? `${st.notes.length} 篇` : '';
    if (!st.notes.length) {
      listEl.innerHTML = `
        <div class="rounded-2xl border border-dashed border-slate-200 bg-slate-50 p-6 text-center text-sm text-slate-400">
          还没有笔记。看视频时随手记下时间点、截图与想法。
        </div>`;
      return;
    }
    listEl.innerHTML = st.notes.map((n) => `
      <div class="notes-item group flex items-center gap-2 rounded-xl border px-3 py-2 transition ${n.id === st.activeId ? 'note-item-on' : ''}" data-id="${n.id}" title="点击切换到这篇笔记">
        <button type="button" class="notes-pick min-w-0 flex-1 text-left">
          <span class="block truncate text-sm font-semibold ${n.id === st.activeId ? 'text-cyan-700' : 'text-slate-700'}">${escapeHtml(noteTitle(n))}</span>
          <span class="mt-0.5 block truncate text-xs text-slate-400">
            ${(n.marks || []).length ? `<span class="mr-1 inline-flex items-center gap-0.5"><svg viewBox="0 0 24 24" class="h-3 w-3" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>${n.marks.length} 个时间点</span>` : ''}
            ${n.image_count ? `<span class="mr-1">· ${n.image_count} 张图</span>` : ''}
            · ${escapeHtml(relativeTime(n.updated_at))}
          </span>
        </button>
        <button type="button" class="notes-del shrink-0 rounded-lg p-1.5 text-slate-300 opacity-0 transition hover:bg-rose-50 hover:text-rose-500 group-hover:opacity-100" title="删除这篇笔记" data-id="${n.id}">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>
        </button>
      </div>`).join('');
  };

  listEl.addEventListener('click', async (e) => {
    const del = e.target.closest('.notes-del');
    if (del) {
      e.stopPropagation();
      const n = byId(del.dataset.id);
      if (!n || !window.confirm('删除这篇笔记？其图片会一并删除，不可恢复。')) return;
      await flush();   // 先保存正在编辑的内容，避免删除竞态
      try {
        await me.deleteNote(del.dataset.id);
        st.notes = st.notes.filter((x) => x.id !== del.dataset.id);
        notesCache.set(contentKey, st.notes);
        if (st.activeId === del.dataset.id) { st.activeId = null; st.edit = null; }
        paintList();
        paintEditor();
      } catch (err) { setToast(err.message || '删除失败'); }
      return;
    }
    const item = e.target.closest('.notes-pick');
    if (!item) return;
    if (item.closest('.notes-item').dataset.id === st.activeId) return;
    await flush();
    st.activeId = item.closest('.notes-item').dataset.id;
    st.preview = false;
    paintList();
    paintEditor();
  });

  /* ---------- 时间戳 / 截图：与左栏播放器桥接 ---------- */
  const currentTime = () => {
    const ctrl = getCtrl && getCtrl();
    if (ctrl) {
      const t = ctrl.getTime();
      if (t != null) return t;
    }
    // B站外链播放器无进度回读：回退到字幕 Tab 当前高亮行的时间
    const row = document.querySelector('.sub-row.sub-active .sub-ts');
    if (row && row.dataset.t !== '') return Number(row.dataset.t);
    return null;
  };

  const seekTo = (sec) => {
    const ctrl = getCtrl && getCtrl();
    if (ctrl) { ctrl.seek(sec); return; }
    if (mountPlayer) mountPlayer(sec);
  };

  const insertAtCursor = (text) => {
    const ta = editorEl.querySelector('.notes-ta');
    if (!ta) return;
    const a = ta.selectionStart, b = ta.selectionEnd;
    ta.setRangeText(text, a, b, 'end');
    ta.focus();
    // setRangeText 不触发 input 事件：必须手动同步工作副本 e.body，
    // 否则自动保存与预览都拿的是旧正文（插入的时间戳/截图会丢失）
    if (st.edit) st.edit.body = ta.value;
    markDirty();
  };

  const insertTs = () => {
    const t = currentTime();
    if (t == null) { setToast('暂无法读取播放进度：请先在左栏播放视频，或到字幕 Tab 定位一行。'); return; }
    insertAtCursor(`@[${fmtTs(t)}](t=${Math.floor(t)})`);
  };

  const copyTs = async () => {
    const t = currentTime();
    if (t == null) { setToast('暂无法读取播放进度。'); return; }
    const ok = await copyText(fmtTs(t));
    setToast(ok ? `已复制 ${fmtTs(t)}` : '复制失败');
  };

  const insertImageMd = (image, t) => {
    const label = t != null ? `截图 ${fmtTs(t)}` : '贴图';
    insertAtCursor(`\n![${label}](${image.url})\n`);
  };

  const uploadBlob = async (blob, t) => {
    const fd = new FormData();
    fd.append('file', blob, 'shot.png');
    fd.append('content_key', contentKey);
    if (t != null) fd.append('t', String(Math.floor(t)));
    if (st.activeId) fd.append('note_id', st.activeId);
    const res = await fetch(NOTE_IMAGE_API, { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `上传失败 (HTTP ${res.status})`);
    return data.image;
  };

  const capture = async () => {
    const t = currentTime();
    const ctrl = getCtrl && getCtrl();
    const video = ctrl && ctrl.el && ctrl.el.tagName === 'VIDEO' ? ctrl.el : null;
    setToast('正在截取画面…');
    try {
      if (video && video.readyState >= 2) {
        // 抖音等原生 <video>（后端流代理，同源）：前端 Canvas 抓帧，零服务端开销
        const c = document.createElement('canvas');
        c.width = video.videoWidth; c.height = video.videoHeight;
        c.getContext('2d').drawImage(video, 0, 0);
        const blob = await new Promise((r) => c.toBlob(r, 'image/png'));
        if (!blob) throw new Error('画面抓取失败');
        insertImageMd(await uploadBlob(blob, t), t);
      } else {
        // B站/YouTube 跨域 iframe：后端抽帧（本地流播缓存 → 远程直连只拉 t 附近片段
        // 抽帧 → 封面兜底；不触发整片下载，也不挂载原生播放器——挂载外链播放器
        // 也不会填充后端流缓存，抖音未挂载时先挂载反而触发整片下载）
        const data = await me.captureFrame(url, t || 0);
        insertImageMd(data.image, data.image.t);
        if (data.frame_type === 'poster') setToast('远程抽帧失败（平台限流或超时），已插入视频封面；稍后再试或换个时间点。');
      }
      setToast('');
    } catch (err) {
      setToast(err.message || '截图失败，请重试');
    }
  };

  /* ---------- 自动保存（防抖 + 乐观并发 + 409 二选一） ---------- */
  const paintStatus = () => {
    const el = editorEl.querySelector('.notes-status');
    if (!el) return;
    const e = st.edit;
    el.textContent = !e ? '' : (e.conflict ? '版本冲突待处理'
      : e.saving ? '保存中…' : e.dirty ? '未保存' : '已保存');
    el.className = `notes-status text-xs ${e && (e.dirty || e.conflict) ? 'text-amber-600' : 'text-slate-400'}`;
  };

  const patchNow = async (extra = {}) => {
    const e = st.edit;
    if (!e || !e.id || e.saving) return;
    const payload = { body: e.body, title: e.title, base_updated_at: e.base, ...extra };
    e.saving = true; e.dirty = false; paintStatus();
    try {
      const data = await me.patchNote(e.id, payload);
      const note = data.note || {};
      e.base = note.updated_at || e.base;
      if (note.truncated) setToast('内容已达上限（2 万字），超出部分被截断。');
      const n = byId(e.id);
      if (n) { Object.assign(n, { body: e.body, title: e.title, marks: note.marks || n.marks, image_count: note.image_count != null ? note.image_count : n.image_count, updated_at: note.updated_at || n.updated_at }); paintList(); }
    } catch (err) {
      e.dirty = true;
      if (err.status === 409 && err.data && err.data.server) {
        e.conflict = err.data.server;   // 服务端当前版本（含完整正文）
      } else {
        setToast(err.message || '保存失败，稍后重试');
      }
    } finally {
      e.saving = false;
      paintStatus();
      paintConflict();
    }
  };

  const markDirty = () => {
    const e = st.edit;
    if (!e) return;
    e.dirty = true;
    if (st.timer) { clearTimeout(st.timer); st.timer = null; }
    // 冲突未解决期间暂停自动保存（否则旧基线反复 409），等用户二选一后再恢复
    if (e.conflict) { paintStatus(); return; }
    paintStatus();
    st.timer = setTimeout(() => { st.timer = null; patchNow(); }, SAVE_DEBOUNCE);
  };

  const flush = () => {
    if (st.timer) { clearTimeout(st.timer); st.timer = null; }
    if (st.edit && st.edit.dirty && !st.edit.conflict) return patchNow();
    return Promise.resolve();
  };

  const paintConflict = () => {
    const bar = editorEl.querySelector('.notes-conflict');
    if (!bar) return;
    const e = st.edit;
    if (!e || !e.conflict) { bar.classList.add('hidden'); bar.innerHTML = ''; return; }
    const server = e.conflict;
    bar.classList.remove('hidden');
    bar.innerHTML = `
      <div class="flex flex-wrap items-center justify-between gap-2">
        <span class="text-xs font-semibold text-amber-700">该笔记已在其他窗口被修改，请选择版本：</span>
        <span class="flex gap-2">
          <button type="button" class="notes-keep-mine rounded-lg bg-white px-3 py-1 text-xs font-semibold text-amber-700 ring-1 ring-amber-300 transition hover:bg-amber-50">用我的版本覆盖</button>
          <button type="button" class="notes-take-server rounded-lg bg-white px-3 py-1 text-xs font-semibold text-slate-600 ring-1 ring-slate-300 transition hover:bg-slate-50">改用服务端版本</button>
        </span>
      </div>`;
    bar.querySelector('.notes-keep-mine').addEventListener('click', async () => {
      const base = e.conflict.updated_at;   // 以服务端版本为基线覆盖写入
      e.conflict = null;
      await patchNow({ base_updated_at: base });
    });
    bar.querySelector('.notes-take-server').addEventListener('click', () => {
      const server2 = e.conflict;
      e.conflict = null;
      e.body = server2.body || '';
      e.title = server2.title || '';
      e.base = server2.updated_at || e.base;
      e.dirty = false;
      const ta = editorEl.querySelector('.notes-ta');
      if (ta) ta.value = e.body;
      const titleEl = editorEl.querySelector('.notes-title');
      if (titleEl) titleEl.value = e.title;
      const n = byId(e.id);
      if (n) { Object.assign(n, { body: e.body, title: e.title, updated_at: e.base }); paintList(); }
      paintStatus();
      paintConflict();
    });
  };

  /* ---------- 编辑器渲染（只在切换笔记时重建，输入不重绘以保光标） ---------- */
  const paintEditor = () => {
    if (!st.activeId || !st.edit) {
      editorEl.innerHTML = `
        <div class="flex flex-1 items-center justify-center rounded-2xl border border-dashed border-slate-200 bg-slate-50 p-8 text-center text-sm text-slate-400">
          <div>
            <p>从上方选择一篇笔记，或点击「新建笔记」开始记录。</p>
            <p class="mt-1 text-xs text-slate-400">快捷键：Ctrl+Shift+T 插入时间戳 · Ctrl+Alt+T 复制时间戳 · Ctrl+Shift+S 插入截图</p>
          </div>
        </div>`;
      return;
    }
    const e = st.edit;
    editorEl.innerHTML = `
      <div class="flex min-h-0 flex-1 flex-col rounded-2xl border border-slate-200 bg-white p-3 shadow-sm">
        <div class="flex items-center gap-2">
          <input type="text" class="notes-title min-w-0 flex-1 rounded-lg border-0 bg-transparent px-1 py-1 text-sm font-bold text-slate-800 outline-none placeholder:text-slate-300 focus:bg-slate-50" placeholder="笔记标题（可留空）" value="${escapeHtml(e.title)}" />
          <span class="notes-status text-xs text-slate-400"></span>
          <button type="button" class="notes-mode shrink-0 rounded-lg px-2 py-1 text-xs font-semibold text-slate-500 transition hover:bg-slate-100 hover:text-slate-700">${st.preview ? '编辑' : '预览'}</button>
          <button type="button" class="notes-del2 shrink-0 rounded-lg px-2 py-1 text-xs font-semibold text-slate-400 transition hover:bg-rose-50 hover:text-rose-500" title="删除这篇笔记">删除</button>
        </div>
        <div class="notes-conflict mt-2 hidden rounded-xl border border-amber-200 bg-amber-50 px-3 py-2"></div>
        <div class="mt-2 flex flex-wrap items-center gap-1">
          <button type="button" class="notes-md notes-md-b rounded-lg px-2 py-1 text-xs font-bold text-slate-600 transition hover:bg-slate-100" title="粗体">B</button>
          <button type="button" class="notes-md notes-md-i rounded-lg px-2 py-1 text-xs font-semibold italic text-slate-600 transition hover:bg-slate-100" title="斜体">I</button>
          <button type="button" class="notes-md notes-md-ul rounded-lg px-2 py-1 text-xs text-slate-600 transition hover:bg-slate-100" title="无序列表">• 列表</button>
          <button type="button" class="notes-md notes-md-q rounded-lg px-2 py-1 text-xs text-slate-600 transition hover:bg-slate-100" title="引用">&rdquo; 引用</button>
          <span class="mx-1 h-4 w-px bg-slate-200"></span>
          <button type="button" class="notes-ts-btn inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs font-semibold text-sky-600 transition hover:bg-sky-50" title="在光标处插入当前播放时间戳（Ctrl+Shift+T）">
            <svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>插入时间戳
          </button>
          <button type="button" class="notes-cp-ts rounded-lg px-2 py-1 text-xs font-semibold text-sky-600 transition hover:bg-sky-50" title="复制当前播放时间戳（Ctrl+Alt+T）">复制时间戳</button>
          <button type="button" class="notes-shot inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs font-semibold text-cyan-600 transition hover:bg-cyan-50" title="截取当前画面插入笔记（Ctrl+Shift+S）">
            <svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 8h3l2-3h6l2 3h3v12H4z"/><circle cx="12" cy="13" r="3.5"/></svg>截图
          </button>
        </div>
        <textarea class="notes-ta mt-2 min-h-[9rem] w-full flex-1 resize-none rounded-xl border border-slate-200 p-3 font-mono text-sm leading-relaxed text-slate-700 outline-none focus:border-cyan-400" placeholder="记录你的想法…&#10;&#10;支持 Markdown（**粗体** / 列表 / 引用 / # 标题）；Ctrl+Shift+T 插入时间戳，Ctrl+Shift+S 插入截图；粘贴图片直接上传。"></textarea>
        <div class="notes-pv note-md mt-2 hidden min-h-[9rem] flex-1 overflow-y-auto rounded-xl border border-slate-200 bg-slate-50/60 p-4"></div>
      </div>`;

    const ta = editorEl.querySelector('.notes-ta');
    const pv = editorEl.querySelector('.notes-pv');
    const titleEl = editorEl.querySelector('.notes-title');
    ta.value = e.body;

    titleEl.addEventListener('input', () => { e.title = titleEl.value; markDirty(); });
    ta.addEventListener('input', () => { e.body = ta.value; markDirty(); });

    /* Markdown 快捷键与截图/时间戳快捷键（尽力拦截，浏览器保留手势时仍可用工具栏按钮） */
    ta.addEventListener('keydown', (ev) => {
      const k = ev.key.toLowerCase();
      if (ev.ctrlKey && ev.shiftKey && k === 't') { ev.preventDefault(); insertTs(); return; }
      if (ev.ctrlKey && ev.altKey && k === 't') { ev.preventDefault(); copyTs(); return; }
      if (ev.ctrlKey && ev.shiftKey && k === 's') { ev.preventDefault(); capture(); return; }
      if (ev.ctrlKey && k === 'b') { ev.preventDefault(); wrapSel('**', '**', '粗体'); return; }
      if (ev.ctrlKey && k === 'i') { ev.preventDefault(); wrapSel('*', '*', '斜体'); }
    });

    /* 粘贴图片：直接上传并插入（截图/网页图片二进制走同一通道） */
    ta.addEventListener('paste', async (ev) => {
      const file = Array.from((ev.clipboardData && ev.clipboardData.files) || []).find((f) => f.type.startsWith('image/'));
      if (!file) return;
      ev.preventDefault();
      setToast('正在上传图片…');
      try {
        insertImageMd(await uploadBlob(file, currentTime()), null);
        setToast('');
      } catch (err) { setToast(err.message || '图片上传失败'); }
    });

    /* 工具栏：选区包裹 / 行首前缀（setRangeText 不触发 input，均需手动同步 e.body） */
    const wrapSel = (before, after, placeholder) => {
      const a = ta.selectionStart, b = ta.selectionEnd;
      const sel = ta.value.slice(a, b) || placeholder;
      ta.setRangeText(before + sel + after, a, b, 'end');
      ta.focus();
      e.body = ta.value;
      markDirty();
    };
    const prefixLines = (prefix) => {
      const a = ta.selectionStart, b = ta.selectionEnd;
      const start = ta.value.lastIndexOf('\n', a - 1) + 1;
      const endRaw = ta.value.indexOf('\n', b);
      const end = endRaw === -1 ? ta.value.length : endRaw;
      const block = ta.value.slice(start, end);
      const lines = block.split('\n').map((l) => (l.startsWith(prefix) ? l.slice(prefix.length) : prefix + l));
      ta.setRangeText(lines.join('\n'), start, end, 'end');
      ta.focus();
      e.body = ta.value;
      markDirty();
    };
    editorEl.querySelector('.notes-md-b').addEventListener('click', () => wrapSel('**', '**', '粗体'));
    editorEl.querySelector('.notes-md-i').addEventListener('click', () => wrapSel('*', '*', '斜体'));
    editorEl.querySelector('.notes-md-ul').addEventListener('click', () => prefixLines('- '));
    editorEl.querySelector('.notes-md-q').addEventListener('click', () => prefixLines('> '));
    editorEl.querySelector('.notes-ts-btn').addEventListener('click', insertTs);
    editorEl.querySelector('.notes-cp-ts').addEventListener('click', copyTs);
    editorEl.querySelector('.notes-shot').addEventListener('click', capture);

    editorEl.querySelector('.notes-del2').addEventListener('click', async () => {
      if (!st.activeId || !window.confirm('删除这篇笔记？其图片会一并删除，不可恢复。')) return;
      try {
        await me.deleteNote(st.activeId);
        st.notes = st.notes.filter((x) => x.id !== st.activeId);
        notesCache.set(contentKey, st.notes);
        st.activeId = null;
        st.edit = null;
        paintList();
        paintEditor();
      } catch (err) { setToast(err.message || '删除失败'); }
    });

    /* 编辑/预览切换 */
    const paintMode = () => {
      editorEl.querySelector('.notes-mode').textContent = st.preview ? '编辑' : '预览';
      ta.classList.toggle('hidden', st.preview);
      pv.classList.toggle('hidden', !st.preview);
      if (st.preview) pv.innerHTML = renderMarkdown(e.body) || '<p class="note-p text-slate-400">（空）</p>';
    };
    editorEl.querySelector('.notes-mode').addEventListener('click', () => { st.preview = !st.preview; paintMode(); });
    paintMode();

    /* 预览内时间戳点击 → 跳播（未挂载则先在左栏挂载） */
    pv.addEventListener('click', (ev) => {
      const ts = ev.target.closest('.note-ts');
      if (ts) seekTo(Number(ts.dataset.t));
    });

    paintStatus();
    paintConflict();
  };

  /* ---------- 新建笔记 ---------- */
  panel.querySelector('.notes-new').addEventListener('click', async () => {
    await flush();
    try {
      const data = await me.createNote({ content_key: contentKey, url, title });
      const note = data.note;
      st.notes = [note, ...st.notes];
      notesCache.set(contentKey, st.notes);
      st.activeId = note.id;
      st.preview = false;
      st.edit = { id: note.id, body: note.body || '', title: note.title || '', base: note.updated_at, dirty: false, saving: false, conflict: null };
      paintList();
      paintEditor();
      const ta = editorEl.querySelector('.notes-ta');
      if (ta) ta.focus();
    } catch (err) {
      setToast(err.message || '新建失败');
      if (err.status === 401) renderLoginPrompt();
    }
  });

  /* ---------- 数据加载（notesCache 会话级命中，换卡秒回） ---------- */
  const load = async () => {
    const cached = notesCache.get(contentKey);
    if (cached) { st.notes = cached; }
    paintList();
    if (!st.activeId && st.notes.length) {
      const first = st.notes[0];
      st.activeId = first.id;
      st.edit = { id: first.id, body: first.body || '', title: first.title || '', base: first.updated_at, dirty: false, saving: false, conflict: null };
    }
    paintEditor();
    if (cached) return;
    const { res, data } = await me.notes({ content_key: contentKey, limit: 100 });
    if (!panel.isConnected) return;
    if (res.status === 401) { renderLoginPrompt(); return; }   // core 已触发登录框
    if (!res.ok) { setToast((data && data.detail) || '笔记加载失败'); return; }
    st.notes = data.items || [];
    notesCache.set(contentKey, st.notes);
    if (!st.activeId && st.notes.length) {
      const first = st.notes[0];
      st.activeId = first.id;
      st.edit = { id: first.id, body: first.body || '', title: first.title || '', base: first.updated_at, dirty: false, saving: false, conflict: null };
    }
    paintList();
    paintEditor();
  };

  /* 登录态变化（登出/换号）：notesCache 已由 core 清空，整面板重渲染 */
  const offAuth = bus.on('auth:changed', () => {
    if (!panel.isConnected) { offAuth(); flushers.delete(flush); return; }
    if (st.timer) { clearTimeout(st.timer); st.timer = null; }
    flushers.delete(flush);
    renderNotesPanel(panel, ctx);
  });
  panel._notesOff = () => { try { offAuth(); } catch (e) { /* 已解绑 */ } flushers.delete(flush); };

  /* 播放器挂载事件订阅：时间戳按需读取即可，无需响应挂载事件；保留接口注释供后续联动 */
  if (onPlayer) onPlayer(() => { if (!panel.isConnected) return; });

  flushers.add(flush);
  load();
};

export default { renderNotesPanel };
