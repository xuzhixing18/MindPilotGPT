/* 结果视图（路由 #/v?url=...）：视频信息卡 + 五 Tab 分析面板（总结/字幕/思维导图/评论/问答）。
 *
 * 由 app.js 的结果渲染逻辑迁入。单条 url 走完整卡（右栏 Tab），多条 url（批量）走
 * compact 卡列表 + 全部下载。QA 在登录态下携带 session_id，使多轮问答落到同一服务端
 * 会话（避免每轮产生游离会话）；匿名态行为不变。
 */
import {
  state, escapeHtml, fmtDuration, fmtSize, fmtCount, fmtTs, fmtDate,
  postJson, dedup, markUnauthorized,
  txCache, sumCache, mindmapCache, commentsCache, qaCache,
  triggerDownload, buildSubtitleText, downloadText, buildMindmapMarkdown, exportMindmapPng,
  ICON, AI_CONFIG_HINT,
} from '../core.js';
import { addToCollection } from '../coll-picker.js';
import { createPlayer, detectPlayer } from '../player.js';

/* ---------- 左栏封面播放器：模块级共享控制器（字幕 Tab 联动） ----------
 * 播放器不再内嵌在字幕 Tab 内部，而是在左栏封面区原位挂载：封面默认展示
 * 海报 + 播放按钮，点击后 createPlayer 原位替换为 iframe；字幕时间戳 / 章节
 * 点击时若播放器尚未挂载，则先按对应时间点挂载（自动播放）。控制器模块级
 * 唯一，换卡时销毁旧实例；字幕面板通过 onPlayer 订阅保持同步。
 */
let playerCtrl = null;
let playerSlot = null;   // { el, target, posterHtml }：当前卡左栏封面槽位
const playerSubs = new Set();
const onPlayer = (fn) => { playerSubs.add(fn); return () => playerSubs.delete(fn); };
const setPlayerCtrl = (ctrl) => {
  if (playerCtrl && playerCtrl !== ctrl) { try { playerCtrl.destroy(); } catch (e) { /* 已销毁 */ } }
  playerCtrl = ctrl;
  playerSubs.forEach((fn) => fn(ctrl));
};
/* 在左栏封面槽挂载播放器（sec=起播秒）；已挂载则直接 seek；失败恢复封面可重试。
 * spinner 以 absolute 覆盖层展示：createPlayer 是 appendChild 追加 iframe，
 * 若提前写入普通流的 spinner 会占满容器把 iframe 挤出可视区（aspect-video
 * 固定高 + overflow-hidden），导致播放器被永远挡住。 */
const mountLeftPlayer = async (sec) => {
  if (playerCtrl) { playerCtrl.seek(sec); return playerCtrl; }
  const slot = playerSlot;
  if (!slot || !slot.el.isConnected) return null;
  const spin = document.createElement('div');
  spin.className = 'absolute inset-0 z-10 grid place-items-center bg-slate-100/95 text-xs text-slate-400';
  spin.innerHTML = '<span class="inline-flex items-center gap-2"><span class="spinner"></span> 播放器加载中…</span>';
  slot.el.innerHTML = '';   // 清掉海报/时长角标/播放按钮：它们都是 h-full，不清会把 iframe 挤出容器
  slot.el.appendChild(spin);
  const ctrl = await createPlayer(slot.el, slot.target, { sec, autoplay: true });
  spin.remove();
  if (!slot.el.isConnected) { if (ctrl) ctrl.destroy(); return null; }
  if (ctrl) { setPlayerCtrl(ctrl); return ctrl; }
  slot.el.innerHTML = slot.posterHtml;   // 挂载失败：恢复封面，可重试
  return null;
};

/* ---------- Tab 常量（原 app.js 顶部定义，随结果卡迁入） ---------- */
const TAB_ACTIVE = 'ai-tab -mb-px inline-flex min-w-fit flex-1 items-center justify-center gap-1.5 whitespace-nowrap border-b-2 border-brand-500 px-2 pb-3 pt-1 text-sm font-semibold text-brand-600';
const TAB_IDLE = 'ai-tab -mb-px inline-flex min-w-fit flex-1 items-center justify-center gap-1.5 whitespace-nowrap border-b-2 border-transparent px-2 pb-3 pt-1 text-sm font-semibold text-slate-500 transition hover:text-slate-800';
const FMT_BASE = 'format-card flex w-full items-center gap-3 rounded-2xl border p-3 text-left transition';
const FMT_ON = 'border-brand-400 bg-brand-50';
const FMT_OFF = 'border-slate-200 bg-white hover:border-brand-300';
const TAB_ORDER = ['summary', 'transcript', 'mindmap', 'comments', 'qa'];
const TAB_LABELS = { summary: '总结摘要', transcript: '字幕文本', mindmap: '思维导图', comments: '高赞评论', qa: 'AI 问答' };
const TAB_ICONS = {
  summary: '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3z"/></svg>',
  transcript: '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 15h4M13 15h4M7 11h10"/></svg>',
  mindmap: '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="5" r="2"/><circle cx="5" cy="18" r="2"/><circle cx="19" cy="18" r="2"/><path d="M12 7v4M12 11l-6 5M12 11l6 5"/></svg>',
  comments: '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 9V5a3 3 0 00-6 0v4H5a2 2 0 00-2 2l1 8a2 2 0 002 2h11a2 2 0 002-1.6l1.2-7A2 2 0 0018.2 9H14z"/><path d="M8 9v12"/></svg>',
  qa: '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>',
};
const tabButton = (tab, active) =>
  `<button type="button" role="tab" data-tab="${tab}" aria-selected="${active ? 'true' : 'false'}" class="${active ? TAB_ACTIVE : TAB_IDLE}">${TAB_ICONS[tab]}<span>${TAB_LABELS[tab]}</span></button>`;

/* ---------- 解析视频信息 ---------- */
const parseInfo = async (url) => {
  const res = await fetch(`/api/info?url=${encodeURIComponent(url)}`);
  // 401 = 未登录 / 会话已失效：清本地态并弹登录框（后端门禁是最终裁决）
  if (res.status === 401) {
    markUnauthorized();
    throw new Error('登录状态已失效，请重新登录后再解析。');
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `解析失败 (HTTP ${res.status})`);
  }
  return res.json();
};

/* ---------- 渲染结果卡片 ---------- */
const renderCard = (info, url, opts = {}) => {
  const { compact = false } = opts;
  const formats = info.formats || [];

  const thumb = info.thumbnail
    ? `<img src="${escapeHtml(info.thumbnail)}" alt="封面" referrerpolicy="no-referrer" class="h-full w-full object-cover" onerror="this.style.display='none'"/>`
    : '';

  const metaBits = [
    info.uploader ? `<span class="inline-flex min-w-0 items-center gap-1 text-slate-500">${ICON.person}<span class="truncate">${escapeHtml(info.uploader)}</span></span>` : '',
    info.extractor ? `<span class="shrink-0 rounded-md bg-brand-50 px-2 py-0.5 text-xs font-semibold text-brand-600">${escapeHtml(info.extractor)}</span>` : '',
    info.view_count ? `<span class="inline-flex shrink-0 items-center gap-1 text-slate-500">${ICON.eye}${fmtCount(info.view_count)}</span>` : '',
  ].filter(Boolean).join('');

  const fmtSub = (f) => [
    (f.ext || '').toUpperCase(),
    f.is_audio_only ? '仅音频' : (f.progressive ? '含音频' : '仅视频·需合并'),
    f.filesize ? fmtSize(f.filesize) : '',
  ].filter(Boolean).join(' · ');

  const formatCards = formats.length
    ? formats.map((f, i) => `
        <button type="button" class="${FMT_BASE} ${i === 0 ? FMT_ON : FMT_OFF}" data-format-id="${escapeHtml(f.format_id)}">
          <span class="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-slate-100 text-slate-500">${f.is_audio_only ? ICON.audio : ICON.film}</span>
          <span class="min-w-0 flex-1">
            <span class="block truncate text-sm font-semibold text-slate-800">${escapeHtml(f.label)}</span>
            <span class="block truncate text-xs text-slate-400">${escapeHtml(fmtSub(f))}</span>
          </span>
        </button>`).join('')
    : '<div class="rounded-xl border border-dashed border-slate-200 p-4 text-center text-sm text-slate-400">无可用清晰度</div>';

  const playerTarget = detectPlayer(url);

  const headHtml = `
          <div class="p-4">
            <div class="player-slot relative aspect-video w-full overflow-hidden rounded-2xl bg-slate-100">
              ${thumb}
              ${info.duration ? `<span class="absolute bottom-2 right-2 rounded bg-black/70 px-1.5 py-0.5 text-xs text-white">${fmtDuration(info.duration)}</span>` : ''}
              ${playerTarget ? `
              <button type="button" class="player-play absolute inset-0 grid place-items-center transition hover:bg-black/25" title="在页面内播放视频（与字幕时间戳联动）">
                <span class="grid h-12 w-12 place-items-center rounded-full bg-white/90 text-brand-600 shadow-card transition hover:scale-105">${ICON.play}</span>
              </button>` : ''}
            </div>
            <h3 class="mt-3 line-clamp-2 text-base font-bold leading-snug text-slate-900" title="${escapeHtml(info.title)}">${escapeHtml(info.title)}</h3>
            ${metaBits ? `<div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">${metaBits}</div>` : ''}
            ${!compact && info.description ? `<p class="mt-2 line-clamp-2 text-xs leading-relaxed text-slate-400">${escapeHtml(info.description)}</p>` : ''}
          </div>
          <div class="border-t border-slate-100 p-4">
            <div class="flex items-center gap-2 text-sm font-semibold text-slate-700">${ICON.slider}选择清晰度和格式</div>
            <div class="mt-3 space-y-2">${formatCards}</div>
          </div>`;

  const actionsHtml = `
          <div class="flex shrink-0 flex-col gap-2 border-t border-slate-100 p-4">
            <button class="dl-btn inline-flex items-center justify-center gap-2 rounded-2xl bg-brand-500 px-6 py-3 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600 active:scale-95">${ICON.dl}立即下载</button>
            <button class="ai-btn inline-flex items-center justify-center gap-2 rounded-2xl border border-brand-200 bg-white px-6 py-3 text-sm font-semibold text-brand-600 transition hover:bg-brand-50 active:scale-95">${ICON.spark}一键 AI 分析</button>
            <button class="coll-btn inline-flex items-center justify-center gap-2 rounded-2xl border border-slate-200 bg-white px-6 py-2.5 text-sm font-semibold text-slate-600 transition hover:border-brand-300 hover:text-brand-600 active:scale-95"><svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/><path d="M12 11v4M10 13h4"/></svg>加入合集</button>
            <div class="dl-status hidden text-sm"></div>
          </div>`;

  const panelHtml = `
        <div role="tablist" aria-label="AI 分析" class="flex w-full gap-1 overflow-x-auto border-b border-slate-200 px-2 pt-2">
          ${TAB_ORDER.map((t) => tabButton(t, t === 'transcript')).join('')}
        </div>
        ${TAB_ORDER.map((t) => `<div role="tabpanel" data-panel="${t}" class="ai-tabpanel${t === 'transcript' ? '' : ' hidden'} flex min-h-0 flex-1 flex-col p-4 sm:p-5"></div>`).join('')}`;

  // 批量 compact：单列卡片，右栏默认隐藏（点「一键 AI 分析」再展开）
  if (compact) {
    return `
    <div class="fade-in flex flex-col gap-4" data-compact="1">
      <div class="flex min-w-0 flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-card">
        ${headHtml}
        ${actionsHtml}
      </div>
      <div class="ai-panel hidden flex min-w-0 flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-card">
        ${panelHtml}
      </div>
    </div>`;
  }

  // 单条：左右双栏 + 可拖拽分割线。左栏内部滚动、操作按钮固定底部不随右栏滚；右栏独立滚动。
  return `
    <div class="fade-in flex flex-col gap-4 lg:min-h-0 lg:flex-1 lg:flex-row lg:gap-3" data-compact="" style="--lp:38%">
      <!-- 左栏：视频信息 + 清晰度（内部滚动），操作按钮固定不随右栏滚动 -->
      <div class="left-pane flex min-w-0 flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-card lg:min-h-0 lg:w-[var(--lp)] lg:shrink-0">
        <div class="left-scroll min-h-0 flex-1 overflow-y-auto">
          ${headHtml}
        </div>
        ${actionsHtml}
      </div>
      <!-- 分割线：拖拽调整左右栏宽度 -->
      <div class="pane-split hidden w-1.5 shrink-0 cursor-col-resize rounded-full bg-slate-200/70 transition hover:bg-brand-300 lg:block" title="拖拽调整左右栏宽度"></div>
      <!-- 右栏：五 Tab 分析面板（独立滚动，默认字幕文本） -->
      <div class="ai-panel flex min-h-[26rem] min-w-0 flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-card lg:min-h-0 lg:flex-1">
        ${panelHtml}
      </div>
    </div>`;
};

/* ---------- AI 面板：加载 / 错误 ---------- */
const panelLoading = (panel, textOrStages) => {
  const stages = Array.isArray(textOrStages) ? textOrStages : [textOrStages];
  const paint = (i, elapsed) => {
    panel.innerHTML =
      '<div class="flex items-center gap-2 text-sm text-slate-500"><span class="spinner"></span>' +
      `<span class="stage">${escapeHtml(stages[i % stages.length])}</span>` +
      `<span class="elapsed text-slate-400">${elapsed ? '（已用 ' + elapsed + 's）' : ''}</span></div>`;
  };
  paint(0, 0);
  const t0 = Date.now();
  let i = 0;
  const id = setInterval(() => { i += 1; paint(i, Math.floor((Date.now() - t0) / 1000)); }, 3000);
  return () => clearInterval(id);
};

const panelError = (panel, msg) => {
  panel.innerHTML = `<div class="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm leading-relaxed text-rose-600">${escapeHtml(msg)}</div>`;
};

const renderSummary = (panel, data) => {
  const s = data.summary || {};
  const points = (s.key_points || [])
    .map((p) => `<li class="flex gap-2"><span class="text-brand-500">•</span><span>${escapeHtml(p)}</span></li>`)
    .join('');
  const chapters = (s.chapters || [])
    .map((c, i) => `
      <div class="rounded-xl border border-slate-200 bg-white p-3">
        <div class="text-sm font-semibold text-slate-800">${i + 1}. ${escapeHtml(c.title || '')}</div>
        ${c.summary ? `<div class="mt-1 text-xs leading-relaxed text-slate-500">${escapeHtml(c.summary)}</div>` : ''}
      </div>`)
    .join('');
  const keywords = (s.keywords || [])
    .map((k) => `<span class="rounded-full bg-brand-50 px-2.5 py-1 text-xs text-brand-600">${escapeHtml(k)}</span>`)
    .join('');
  panel.innerHTML = `
    <div class="fade-in">
    <div class="flex items-center justify-between gap-2">
      <h4 class="inline-flex items-center gap-2 text-base font-bold text-slate-900">
        <svg viewBox="0 0 24 24" class="h-5 w-5 text-brand-500" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3z"/></svg>
        AI 总结
        ${(data.cached || s.cached) ? '<span class="rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-semibold text-emerald-600">秒开·已缓存</span>' : ''}
      </h4>
      ${s.model ? `<span class="text-xs text-slate-400">${escapeHtml(s.model)}</span>` : ''}
    </div>
    ${s.one_line ? `<p class="mt-3 text-sm font-semibold text-brand-700">${escapeHtml(s.one_line)}</p>` : ''}
    ${s.summary ? `<p class="mt-2 text-sm leading-relaxed text-slate-600">${escapeHtml(s.summary)}</p>` : ''}
    ${points ? `<div class="mt-4"><div class="text-xs font-semibold text-slate-500">关键要点</div><ul class="mt-2 space-y-1.5 text-sm text-slate-600">${points}</ul></div>` : ''}
    ${chapters ? `<div class="mt-4"><div class="text-xs font-semibold text-slate-500">章节速览</div><div class="mt-2 grid gap-2 sm:grid-cols-2">${chapters}</div></div>` : ''}
    ${keywords ? `<div class="mt-4 flex flex-wrap gap-2">${keywords}</div>` : ''}
    ${s.truncated ? `<p class="mt-3 text-xs text-amber-600">注：字幕较长，总结基于前半部分内容。</p>` : ''}
    </div>`;
};

/* ---------- 字幕工作台辅助：合段拼接 / 搜索高亮 ---------- */
const escapeRegExp = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const CJK_TAIL = /[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]$/;
const CJK_HEAD = /^[\u4e00-\u9fff]/;
// 中日韩文相邻段直接拼接（不插空格），其余以空格连接，合段后读起来像自然段落
const joinSegTexts = (arr) => arr
  .map((s) => (s.text || '').trim())
  .filter(Boolean)
  .reduce((acc, t) => (acc ? acc + (CJK_TAIL.test(acc) && CJK_HEAD.test(t) ? '' : ' ') + t : t), '');

/* ---------- 字幕脚本工作台：搜索筛选 / 分组滑杆 / 章节导航 / 播放器联动 ---------- */
const renderTranscript = async (panel, data, url) => {
  const segs = data.segments || [];
  const chapters = (data.chapters || []).filter((c) => c && c.title && c.start != null);
  // ASR 链路（无 CC 字幕视频）按句切分不含时间轴：时间戳列全部为空，需明确标识而非静默
  const hasTimeline = segs.some((s) => s.start != null);
  const st = { queryRaw: '', query: '', group: 1, follow: false, activeGi: -1, followTimer: null };
  const target = detectPlayer(data.webpage_url || url);   // 平台能力判定（联动可用性 / 时间戳点击行为）
  let ctrl = playerCtrl;   // 共享控制器（左栏封面可能已挂载）；onPlayer 订阅保持最新

  const badgeName = (data.language_name || '').split(' · ')[0];
  const langBadge = [badgeName, data.language].filter(Boolean).join(' · ');
  panel.innerHTML = `
    <div class="fade-in flex h-full flex-col">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div class="flex flex-wrap items-center gap-2">
        <span class="text-sm text-slate-600">共 <b class="font-semibold text-slate-900">${segs.length}</b> 条字幕</span>
        ${langBadge ? `<span class="rounded-md bg-brand-50 px-2 py-0.5 text-xs font-semibold text-brand-600">${escapeHtml(langBadge)}</span>` : ''}
        ${data.cached ? '<span class="rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-semibold text-emerald-600">已缓存</span>' : ''}
        ${segs.length && !hasTimeline ? '<span class="rounded-full bg-amber-50 px-2 py-0.5 text-xs font-semibold text-amber-600" title="该视频无字幕，文本由语音识别（ASR）生成，不含时间轴：时间戳跳转与滚动联动不可用">ASR 转写·无时间轴</span>' : ''}
      </div>
      <div class="flex items-center gap-4 text-sm">
        <button type="button" class="sub-copy inline-flex items-center gap-1 font-semibold text-brand-600 transition hover:text-brand-700">${ICON.copy}复制</button>
        <div class="relative">
          <button type="button" class="sub-dl inline-flex items-center gap-1 font-semibold text-brand-600 transition hover:text-brand-700">${ICON.dl}下载字幕${ICON.caret}</button>
          <div class="sub-dl-menu absolute right-0 z-10 mt-1 hidden w-28 overflow-hidden rounded-xl border border-slate-200 bg-white py-1 shadow-card">
            <button type="button" data-kind="srt" class="sub-dl-opt block w-full px-3 py-1.5 text-left text-sm text-slate-600 transition hover:bg-brand-50 hover:text-brand-600">SRT 字幕</button>
            <button type="button" data-kind="txt" class="sub-dl-opt block w-full px-3 py-1.5 text-left text-sm text-slate-600 transition hover:bg-brand-50 hover:text-brand-600">TXT 纯文本</button>
          </div>
        </div>
        <button type="button" class="sub-expand font-semibold text-brand-600 transition hover:text-brand-700">展开全部</button>
      </div>
    </div>
    <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2">
      <input type="search" class="sub-q min-w-0 flex-1 rounded-xl border border-slate-200 px-3 py-1.5 text-sm outline-none focus:border-brand-400" placeholder="根据「字幕」筛选…" />
      <label class="inline-flex shrink-0 items-center gap-2 text-xs text-slate-500">字幕分组
        <input type="range" min="1" max="8" step="1" value="1" class="sub-group w-24 accent-brand-500" />
        <span class="sub-group-label w-14 text-slate-600">不分组</span>
      </label>
      <button type="button" class="sub-follow shrink-0 rounded-full border border-slate-200 px-3 py-1 text-xs font-semibold text-slate-500 transition" title="开启后字幕列表随播放进度自动滚动高亮（需平台播放器支持进度回读）">字幕滚动：关</button>
    </div>
    <div class="sub-chips mt-2 flex gap-1.5 overflow-x-auto pb-1 ${chapters.length ? '' : 'hidden'}"></div>
    <div class="sub-body mt-2 min-h-0 flex-1 space-y-2 overflow-y-auto pr-2"></div>
    </div>`;

  const listEl = panel.querySelector('.sub-body');
  const chipsEl = panel.querySelector('.sub-chips');
  const followBtn = panel.querySelector('.sub-follow');
  const groupInput = panel.querySelector('.sub-group');
  const groupLabel = panel.querySelector('.sub-group-label');

  /* 搜索命中高亮：先按原文切分再转义，避免转义字符干扰匹配 */
  const highlight = (text) => {
    if (!st.query) return escapeHtml(text);
    const re = new RegExp(escapeRegExp(st.queryRaw), 'gi');
    let out = '';
    let last = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      out += escapeHtml(text.slice(last, m.index)) + '<mark class="sub-hit">' + escapeHtml(m[0]) + '</mark>';
      last = m.index + m[0].length;
      if (m.index === re.lastIndex) re.lastIndex += 1;   // 防零宽匹配死循环
    }
    return out + escapeHtml(text.slice(last));
  };

  /* 列表渲染：筛选 → 按滑杆粒度合段 → 行（时间戳可点） */
  let posOf = new Map();   // seg → 筛选后位置（联动定位用）
  const paintList = () => {
    const filtered = st.query
      ? segs.filter((s) => (s.text || '').toLowerCase().includes(st.query))
      : segs;
    posOf = new Map(filtered.map((s, i) => [s, i]));
    const groups = [];
    for (let i = 0; i < filtered.length; i += st.group) groups.push(filtered.slice(i, i + st.group));
    if (!groups.length) {
      listEl.innerHTML = `<div class="p-4 text-center text-sm text-slate-400">${st.query ? `未找到匹配「${escapeHtml(st.queryRaw)}」的字幕` : '无字幕内容'}</div>`;
      return;
    }
    listEl.innerHTML = groups.map((g, gi) => {
      const t = g[0].start;
      const hasTs = t != null;
      const tsTitle = !hasTs ? '该段无时间戳' : (ctrl ? '跳转播放并定位到该段' : (target ? '在左栏打开播放器并跳转到该段' : '定位到该段'));
      return `
      <div class="sub-row flex gap-3 rounded-xl border px-3 py-2.5 transition ${gi === st.activeGi ? 'sub-active' : ''}" data-gi="${gi}" data-start="${hasTs ? t : ''}">
        <button type="button" class="sub-ts shrink-0 font-mono text-sm font-semibold text-sky-500 transition hover:underline ${hasTs ? '' : 'cursor-default opacity-50'}" data-t="${hasTs ? t : ''}" title="${tsTitle}">${hasTs ? fmtTs(t) : '—'}</button>
        <span class="min-w-0 flex-1 text-sm leading-relaxed text-slate-700">${highlight(joinSegTexts(g))}</span>
      </div>`;
    }).join('');
  };

  const setActiveRow = (gi, scroll) => {
    st.activeGi = gi;
    listEl.querySelectorAll('.sub-row.sub-active').forEach((r) => r.classList.remove('sub-active'));
    const row = gi >= 0 ? listEl.querySelector(`.sub-row[data-gi="${gi}"]`) : null;
    if (row) {
      row.classList.add('sub-active');
      if (scroll) row.scrollIntoView({ block: 'nearest' });
    }
  };

  /* 字幕滚动：轮询播放器进度 → 定位当前段 → 高亮 + 自动滚动 */
  const stopFollow = () => {
    if (st.followTimer) { clearInterval(st.followTimer); st.followTimer = null; }
  };
  const setFollow = (on) => {
    st.follow = on;
    stopFollow();
    followBtn.textContent = `字幕滚动：${on ? '开' : '关'}`;
    followBtn.className = `sub-follow shrink-0 rounded-full border px-3 py-1 text-xs font-semibold transition ${on ? 'border-brand-400 bg-brand-50 text-brand-600' : 'border-slate-200 text-slate-500'}`;
    if (!on || !ctrl || !ctrl.canFollow) return;
    st.followTimer = setInterval(() => {
      if (!panel.isConnected) { stopFollow(); return; }
      if (!ctrl || !ctrl.canFollow) return;   // 播放器尚未挂载（左栏封面未点击）：本轮跳过
      const t = ctrl.getTime();
      if (t == null) return;
      let idx = -1;
      for (let i = 0; i < segs.length; i += 1) {
        const s = segs[i];
        const start = s.start != null ? s.start : 0;
        const next = segs[i + 1];
        const end = s.end != null ? s.end : (next && next.start != null ? next.start : Infinity);
        if (start <= t && t < end) { idx = i; break; }
      }
      if (idx < 0) return;
      const pos = posOf.get(segs[idx]);
      if (pos == null) return;   // 当前段被搜索过滤：不跳滚动位置
      setActiveRow(Math.floor(pos / st.group), true);
    }, 600);
  };
  const disableFollow = (hint) => {
    setFollow(false);
    followBtn.disabled = true;
    followBtn.classList.add('cursor-not-allowed', 'opacity-50');
    followBtn.title = hint;
  };

  /* 章节导航：跳转播放 + 定位字幕 + 选中态 */
  if (chapters.length) {
    chipsEl.innerHTML = chapters.map((c, i) => `
      <button type="button" data-i="${i}" class="sub-chip shrink-0 rounded-full border border-slate-200 bg-white px-2.5 py-1 text-xs text-slate-600 transition hover:border-brand-300 hover:text-brand-600" title="跳转播放并定位字幕">
        <span class="font-mono text-sky-500">${fmtTs(c.start)}</span> ${escapeHtml(c.title)}
      </button>`).join('');
  }
  chipsEl.addEventListener('click', async (e) => {
    const chip = e.target.closest('.sub-chip');
    if (!chip) return;
    const c = chapters[Number(chip.dataset.i)];
    if (!c) return;
    if (ctrl) ctrl.seek(c.start);
    else if (target) await mountLeftPlayer(c.start);
    chipsEl.querySelectorAll('.sub-chip').forEach((x) => x.classList.toggle('sub-chip-on', x === chip));
    const rows = Array.from(listEl.querySelectorAll('.sub-row')).filter((r) => r.dataset.start !== '');
    const row = rows.find((r) => Number(r.dataset.start) >= c.start - 0.5) || rows[rows.length - 1];
    if (row) setActiveRow(Number(row.dataset.gi), true);
  });

  /* 工具栏：搜索 / 分组滑杆 / 字幕滚动开关 */
  panel.querySelector('.sub-q').addEventListener('input', (e) => {
    st.queryRaw = e.target.value.trim();
    st.query = st.queryRaw.toLowerCase();
    st.activeGi = -1;
    paintList();
  });
  groupInput.addEventListener('input', () => {
    st.group = Number(groupInput.value) || 1;
    groupLabel.textContent = st.group === 1 ? '不分组' : `${st.group} 条/段`;
    st.activeGi = -1;
    paintList();
  });
  followBtn.addEventListener('click', () => { if (!followBtn.disabled) setFollow(!st.follow); });

  /* 时间戳点击：播放器跳转（未挂载则先在左栏封面区挂载）+ 本段高亮 */
  listEl.addEventListener('click', async (e) => {
    const ts = e.target.closest('.sub-ts');
    if (!ts || ts.dataset.t === '') return;
    const t = Number(ts.dataset.t);
    if (ctrl) ctrl.seek(t);
    else if (target) await mountLeftPlayer(t);
    const row = ts.closest('.sub-row');
    setActiveRow(row ? Number(row.dataset.gi) : -1, false);
  });

  /* 复制 / 下载 / 展开（沿用既有行为） */
  const menu = panel.querySelector('.sub-dl-menu');
  panel.querySelector('.sub-dl').addEventListener('click', (e) => { e.stopPropagation(); menu.classList.toggle('hidden'); });
  panel.querySelectorAll('.sub-dl-opt').forEach((opt) => opt.addEventListener('click', () => {
    const kind = opt.dataset.kind;
    downloadText(`subtitles.${kind}`, buildSubtitleText(segs, kind), kind === 'srt' ? 'application/x-subrip' : 'text/plain');
    menu.classList.add('hidden');
  }));
  const copyBtn = panel.querySelector('.sub-copy');
  copyBtn.addEventListener('click', async () => {
    const text = buildSubtitleText(segs, 'txt');
    const done = () => {
      copyBtn.innerHTML = `${ICON.check}已复制`;
      setTimeout(() => { copyBtn.innerHTML = `${ICON.copy}复制`; }, 1500);
    };
    try {
      await navigator.clipboard.writeText(text);
      done();
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
      done();
    }
  });
  const expandBtn = panel.querySelector('.sub-expand');
  expandBtn.addEventListener('click', () => {
    const expanded = listEl.style.maxHeight === 'none';
    listEl.style.maxHeight = expanded ? '' : 'none';
    expandBtn.textContent = expanded ? '展开全部' : '收起';
  });

  /* 订阅共享控制器：左栏封面挂载后刷新时间戳 title；滚动联动开启中则恢复轮询 */
  const unsub = onPlayer((c) => {
    if (!panel.isConnected) { unsub(); return; }
    ctrl = c;
    paintList();
    if (st.follow && ctrl && ctrl.canFollow) setFollow(true);
  });

  paintList();

  /* 滚动联动可用性由平台能力决定：B站外链播放器无进度回读；无嵌入播放器平台仅字幕内定位；
   * YouTube 未挂载时轮询自动空转，封面挂载后经订阅回调恢复联动 */
  if (!target || target.platform !== 'youtube') {
    disableFollow(target && target.platform === 'bilibili'
      ? 'B站外链播放器不回读播放进度，滚动联动不可用；点击时间戳跳转正常'
      : '该平台无可嵌入播放器：时间戳仅用于字幕内定位');
  }
};

/* ---------- 思维导图渲染（无第三方库，CSS 缩进树，递归） ---------- */
const mmNode = (n) => {
  const kids = n.children || [];
  const kidsHtml = kids.length
    ? `<div class="mt-1.5 space-y-1.5 border-l border-slate-200 pl-3 sm:pl-4">${kids.map(mmNode).join('')}</div>`
    : '';
  return `<div class="mt-1.5">
      <span class="inline-block rounded-lg bg-slate-100 px-2.5 py-1 text-sm leading-snug text-slate-700">${escapeHtml(n.title || '')}</span>
      ${kidsHtml}
    </div>`;
};

const renderMindmap = (panel, data) => {
  const mm = data.mindmap || {};
  const kids = mm.children || [];
  panel.innerHTML = `
    <div class="fade-in">
    <div class="flex items-center justify-between gap-2">
      <h4 class="inline-flex items-center gap-2 text-base font-bold text-slate-900">
        <span class="text-brand-500">${TAB_ICONS.mindmap}</span>
        思维导图
        ${(data.cached || mm.cached) ? '<span class="rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-semibold text-emerald-600">已缓存</span>' : ''}
      </h4>
      <div class="flex items-center gap-3 text-sm">
        ${mm.model ? `<span class="text-xs text-slate-400">${escapeHtml(mm.model)}</span>` : ''}
        <div class="relative">
          <button type="button" class="mm-dl inline-flex items-center gap-1 font-semibold text-brand-600 transition hover:text-brand-700">${ICON.dl}下载导图${ICON.caret}</button>
          <div class="mm-dl-menu absolute right-0 z-10 mt-1 hidden w-36 overflow-hidden rounded-xl border border-slate-200 bg-white py-1 shadow-card">
            <button type="button" data-kind="png" class="mm-dl-opt block w-full px-3 py-1.5 text-left text-sm text-slate-600 transition hover:bg-brand-50 hover:text-brand-600">PNG 图片</button>
            <button type="button" data-kind="md" class="mm-dl-opt block w-full px-3 py-1.5 text-left text-sm text-slate-600 transition hover:bg-brand-50 hover:text-brand-600">Markdown 大纲</button>
          </div>
        </div>
      </div>
    </div>
    <div class="mt-4">
      <div class="inline-block rounded-xl bg-brand-500 px-4 py-2 text-sm font-bold text-white shadow-glow">${escapeHtml(mm.title || '思维导图')}</div>
      ${kids.length
        ? `<div class="mt-3 border-l-2 border-brand-100 pl-3 sm:pl-4">${kids.map(mmNode).join('')}</div>`
        : '<p class="mt-3 text-sm text-slate-400">（该视频暂无更多可展开的分支）</p>'}
    </div>
    ${mm.truncated ? '<p class="mt-3 text-xs text-amber-600">注：字幕较长，思维导图基于前半部分内容。</p>' : ''}
    </div>`;

  const base = (mm.title || 'mindmap').replace(/[\\/:*?"<>|]/g, '_');
  const mmMenu = panel.querySelector('.mm-dl-menu');
  panel.querySelector('.mm-dl').addEventListener('click', (e) => { e.stopPropagation(); mmMenu.classList.toggle('hidden'); });
  panel.querySelectorAll('.mm-dl-opt').forEach((opt) => opt.addEventListener('click', () => {
    if (opt.dataset.kind === 'png') exportMindmapPng(mm, base);
    else downloadText(`${base}.md`, buildMindmapMarkdown(mm), 'text/markdown');
    mmMenu.classList.add('hidden');
  }));
};

/* ---------- 高赞评论渲染（服务端已按赞排序取 TopN） ---------- */
const renderComments = (panel, data) => {
  const list = data.comments || [];
  const srcLabel = { bilibili: 'B站', douyin: '抖音', generic: '通用' }[data.source] || data.source || '';
  const rows = list.map((c, i) => {
    const d = fmtDate(c.time);
    return `
      <div class="flex gap-3 border-b border-slate-100 py-3 last:border-0">
        <span class="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-brand-50 text-xs font-bold text-brand-600">${i + 1}</span>
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-400">
            <span class="inline-flex items-center gap-1 font-semibold text-slate-700">${ICON.person}${escapeHtml(c.author || '匿名')}</span>
            <span class="inline-flex items-center gap-1 text-rose-500">${ICON.thumb}${fmtCount(c.likes)}</span>
            ${d ? `<span>${d}</span>` : ''}
          </div>
          <p class="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-700">${escapeHtml(c.text)}</p>
        </div>
      </div>`;
  }).join('');
  panel.innerHTML = `
    <div class="fade-in flex h-full flex-col">
    <div class="flex flex-wrap items-center gap-2">
      <span class="text-sm text-slate-600">共 <b class="font-semibold text-slate-900">${list.length}</b> 条高赞评论</span>
      ${srcLabel ? `<span class="rounded-md bg-brand-50 px-2 py-0.5 text-xs font-semibold text-brand-600">${escapeHtml(srcLabel)}</span>` : ''}
      ${data.cached ? '<span class="rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-semibold text-emerald-600">已缓存</span>' : ''}
    </div>
    <div class="mt-3 min-h-0 flex-1 overflow-y-auto pr-2">${rows}</div>
    </div>`;
};

/* ---------- AI 问答渲染（多轮聊天；登录态携带 session_id 落到同一服务端会话） ---------- */
const QA_SUGGESTIONS = ['这个视频主要讲了什么？', '核心结论或要点是什么？', '有哪些关键数据、案例或方法？', '适合什么人群观看？'];

const qaBubble = (m) => {
  if (m.role === 'user') {
    return `<div class="flex justify-end"><div class="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-brand-500 px-3.5 py-2 text-sm text-white">${escapeHtml(m.content)}</div></div>`;
  }
  const cls = m.error ? 'border-rose-200 bg-rose-50 text-rose-600' : 'border-slate-200 bg-white text-slate-700';
  return `<div class="flex justify-start"><div class="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-bl-sm border ${cls} px-3.5 py-2 text-sm leading-relaxed">${escapeHtml(m.content)}</div></div>`;
};

const renderQA = (panel, url) => {
  let conv = qaCache.get(url);
  if (!conv) { conv = { messages: [], sessionId: null }; qaCache.set(url, conv); }

  panel.innerHTML = `
    <div class="fade-in flex h-full min-h-0 flex-col">
      <div class="flex items-center justify-between gap-2">
        <h4 class="inline-flex items-center gap-2 text-base font-bold text-slate-900">
          <span class="text-brand-500">${TAB_ICONS.qa}</span> AI 问答
        </h4>
        <span class="text-xs text-slate-400">仅依据该视频字幕作答</span>
      </div>
      <div class="qa-log mt-3 min-h-0 flex-1 space-y-3 overflow-y-auto pr-1"></div>
      <div class="qa-suggest mt-3 flex flex-wrap gap-2"></div>
      <div class="mt-3 flex gap-2">
        <input type="text" class="qa-input flex-1 rounded-xl border border-slate-200 px-3.5 py-2.5 text-sm outline-none focus:border-brand-400" placeholder="就这个视频提问，如：作者的核心观点是什么？" />
        <button type="button" class="qa-send shrink-0 rounded-xl bg-brand-500 px-5 py-2.5 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600 active:scale-95">发送</button>
      </div>
    </div>`;

  const logEl = panel.querySelector('.qa-log');
  const suggestEl = panel.querySelector('.qa-suggest');
  const inputEl = panel.querySelector('.qa-input');
  const sendBtn = panel.querySelector('.qa-send');

  const paintLog = () => {
    if (!conv.messages.length) {
      logEl.innerHTML = '<div class="rounded-2xl border border-dashed border-slate-200 bg-slate-50 p-4 text-center text-sm text-slate-400">还没有提问。基于该视频字幕，问问任何你想知道的 👇</div>';
      return;
    }
    logEl.innerHTML = conv.messages.map(qaBubble).join('');
    logEl.scrollTop = logEl.scrollHeight;
  };

  const send = async (preset) => {
    const question = String(preset != null ? preset : inputEl.value).trim();
    if (!question) { inputEl.focus(); return; }
    inputEl.value = '';
    suggestEl.innerHTML = '';
    const history = conv.messages.slice(-8);   // 匿名态兜底上下文；登录态服务端以 DB 会话为准
    conv.messages.push({ role: 'user', content: question });
    paintLog();
    const thinking = document.createElement('div');
    thinking.className = 'flex justify-start';
    thinking.innerHTML = '<div class="inline-flex items-center gap-2 rounded-2xl rounded-bl-sm border border-slate-200 bg-white px-3.5 py-2 text-sm text-slate-400"><span class="spinner"></span> 思考中…</div>';
    logEl.appendChild(thinking);
    logEl.scrollTop = logEl.scrollHeight;
    sendBtn.disabled = true; sendBtn.classList.add('opacity-60');
    try {
      const body = { url, question, history };
      if (conv.sessionId) body.session_id = conv.sessionId;   // 续接同一服务端会话
      const { res, data } = await postJson('/api/qa', body);
      if (!res.ok) {
        const msg = res.status === 503
          ? `${data.detail || 'AI 未配置'}。${AI_CONFIG_HINT}`
          : (data.detail || `问答失败 (HTTP ${res.status})`);
        conv.messages.push({ role: 'assistant', content: msg, error: true });
      } else {
        if (data.session_id) conv.sessionId = data.session_id;   // 首轮拿到会话 id，后续复用
        conv.messages.push({ role: 'assistant', content: data.answer });
      }
    } catch (e) {
      conv.messages.push({ role: 'assistant', content: e.message || '网络错误，问答失败', error: true });
    } finally {
      thinking.remove();
      sendBtn.disabled = false; sendBtn.classList.remove('opacity-60');
      paintLog();
      inputEl.focus();
    }
  };

  const paintSuggest = () => {
    if (conv.messages.length) { suggestEl.innerHTML = ''; return; }
    suggestEl.innerHTML = QA_SUGGESTIONS
      .map((q) => `<button type="button" class="qa-chip rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600 transition hover:border-brand-300 hover:text-brand-600">${escapeHtml(q)}</button>`)
      .join('');
    suggestEl.querySelectorAll('.qa-chip').forEach((chip) =>
      chip.addEventListener('click', () => send(chip.textContent)));
  };

  sendBtn.addEventListener('click', () => send());
  inputEl.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing) send(); });
  paintLog();
  paintSuggest();
};

/* ---------- 各 Tab 懒加载器：命中会话缓存即渲染，否则请求并写入（返回是否成功） ---------- */
const loadSummary = async (url, panel) => {
  const hit = sumCache.get(url);
  if (hit) { renderSummary(panel, { ...hit, cached: true }); return true; }
  const stop = panelLoading(panel, [
    '正在提取字幕…', '字幕较长时正在下载音频并识别语音…',
    '正在调用大模型生成结构化总结…', '快好了，正在整理要点与章节…',
  ]);
  try {
    const { res, data } = await dedup('sum:' + url, () => postJson('/api/summarize', { url }));
    if (!res.ok) {
      panelError(panel, res.status === 503
        ? `${data.detail || 'AI 未配置'}。${AI_CONFIG_HINT}`
        : (data.detail || `总结失败 (HTTP ${res.status})`));
      return false;
    }
    sumCache.set(url, data);
    renderSummary(panel, data);
    return true;
  } catch (e) {
    panelError(panel, e.message || '网络错误，总结失败');
    return false;
  } finally { stop(); }
};

const loadTranscript = async (url, panel) => {
  const hit = txCache.get(url);
  if (hit) { await renderTranscript(panel, { ...hit, cached: true }, url); return true; }
  const stop = panelLoading(panel, [
    '正在提取字幕…', '若该视频无字幕，正在下载音频并识别语音…', '快好了…',
  ]);
  try {
    const { res, data } = await dedup('tx:' + url, () => postJson('/api/transcribe', { url }));
    if (!res.ok) { panelError(panel, data.detail || `转写失败 (HTTP ${res.status})`); return false; }
    txCache.set(url, data);
    await renderTranscript(panel, data, url);
    return true;
  } catch (e) {
    panelError(panel, e.message || '网络错误，转写失败');
    return false;
  } finally { stop(); }
};

const loadMindmap = async (url, panel) => {
  const hit = mindmapCache.get(url);
  if (hit) { renderMindmap(panel, { ...hit, cached: true }); return true; }
  const stop = panelLoading(panel, [
    '正在提取字幕…', '正在让大模型梳理内容层级…', '正在生成思维导图…',
  ]);
  try {
    const { res, data } = await dedup('mm:' + url, () => postJson('/api/mindmap', { url }));
    if (!res.ok) {
      panelError(panel, res.status === 503
        ? `${data.detail || 'AI 未配置'}。${AI_CONFIG_HINT}`
        : (data.detail || `思维导图生成失败 (HTTP ${res.status})`));
      return false;
    }
    mindmapCache.set(url, data);
    renderMindmap(panel, data);
    return true;
  } catch (e) {
    panelError(panel, e.message || '网络错误，思维导图生成失败');
    return false;
  } finally { stop(); }
};

const loadComments = async (url, panel) => {
  const hit = commentsCache.get(url);
  if (hit) { renderComments(panel, { ...hit, cached: true }); return true; }
  const stop = panelLoading(panel, ['正在抓取高赞评论…', '评论较多时可能稍慢…']);
  try {
    const { res, data } = await dedup('cm:' + url, () => postJson('/api/comments', { url }));
    if (!res.ok) { panelError(panel, data.detail || `评论抓取失败 (HTTP ${res.status})`); return false; }
    commentsCache.set(url, data);
    renderComments(panel, data);
    return true;
  } catch (e) {
    panelError(panel, e.message || '网络错误，评论抓取失败');
    return false;
  } finally { stop(); }
};

const loadQA = (url, panel) => { renderQA(panel, url); return true; };

const TAB_LOADERS = { summary: loadSummary, transcript: loadTranscript, mindmap: loadMindmap, comments: loadComments, qa: loadQA };

/* ---------- Tab 切换：懒加载 + 防重复请求 ---------- */
const activateTab = (card, tab) => {
  card.querySelectorAll('[role="tab"]').forEach((b) => {
    const on = b.dataset.tab === tab;
    b.setAttribute('aria-selected', on ? 'true' : 'false');
    b.className = on ? TAB_ACTIVE : TAB_IDLE;
  });
  card.querySelectorAll('.ai-tabpanel').forEach((p) => {
    p.classList.toggle('hidden', p.dataset.panel !== tab);
  });
};

const switchTab = async (card, url, tab) => {
  activateTab(card, tab);
  const st = card._aiState;
  if (st.loaded.has(tab)) return;
  st.loaded.add(tab);
  const panel = card.querySelector(`.ai-tabpanel[data-panel="${tab}"]`);
  let ok = true;
  try { ok = await TAB_LOADERS[tab](url, panel); }
  catch (e) { ok = false; panelError(panel, e.message || '加载失败'); }
  if (ok === false) st.loaded.delete(tab);
};

const prefetchTab = (card, url, tab) => {
  const st = card._aiState;
  if (st.loaded.has(tab)) return Promise.resolve(true);
  st.loaded.add(tab);
  const panel = card.querySelector(`.ai-tabpanel[data-panel="${tab}"]`);
  return Promise.resolve()
    .then(() => TAB_LOADERS[tab](url, panel))
    .then((ok) => { if (ok === false) st.loaded.delete(tab); return ok; })
    .catch((e) => { st.loaded.delete(tab); panelError(panel, e.message || '加载失败'); return false; });
};

const runAllAi = (card, url, btn) => {
  const panel = card.querySelector('.ai-panel');
  if (card.dataset.compact === '1') panel.classList.remove('hidden');
  activateTab(card, 'summary');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="inline-flex items-center gap-2"><span class="spinner"></span> 分析中…</span>';
  Promise.all([
    prefetchTab(card, url, 'summary'),
    prefetchTab(card, url, 'transcript'),
    prefetchTab(card, url, 'mindmap'),
  ])
    .finally(() => { btn.disabled = false; btn.innerHTML = orig; });
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
};

/* ---------- 分割线拖拽：调整左右栏宽度（写 --lp 百分比，仅桌面双栏生效） ---------- */
const bindSplitter = (cardEl) => {
  const split = cardEl.querySelector('.pane-split');
  if (!split) return;
  let dragging = false;
  split.addEventListener('pointerdown', (e) => {
    dragging = true;
    split.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  split.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const rect = cardEl.getBoundingClientRect();
    if (!rect.width) return;
    const pct = Math.max(22, Math.min(70, ((e.clientX - rect.left) / rect.width) * 100));
    cardEl.style.setProperty('--lp', `${pct.toFixed(1)}%`);
  });
  const stop = () => { dragging = false; };
  split.addEventListener('pointerup', stop);
  split.addEventListener('pointercancel', stop);
};

/* ---------- 绑定卡片事件 ---------- */
const bindCard = (cardEl, url) => {
  const dlBtn = cardEl.querySelector('.dl-btn');
  const status = cardEl.querySelector('.dl-status');
  cardEl._aiState = { loaded: new Set() };
  bindSplitter(cardEl);

  /* 左栏封面播放器槽位：点封面播放按钮 → 原位挂载嵌入播放器（字幕 Tab 共享控制器） */
  setPlayerCtrl(null);   // 换卡：销毁旧播放器
  const slotEl = cardEl.querySelector('.player-slot');
  const slotTarget = detectPlayer(url);
  playerSlot = slotEl && slotTarget ? { el: slotEl, target: slotTarget, posterHtml: slotEl.innerHTML } : null;
  if (slotEl) slotEl.addEventListener('click', (e) => {
    if (!e.target.closest('.player-play')) return;
    mountLeftPlayer(0);
  });

  const fmtCards = Array.from(cardEl.querySelectorAll('.format-card'));
  const selectFormat = (btn) => {
    fmtCards.forEach((b) => { b.className = `${FMT_BASE} ${b === btn ? FMT_ON : FMT_OFF}`; });
    cardEl.dataset.formatId = btn.dataset.formatId;
  };
  if (fmtCards.length) {
    selectFormat(fmtCards[0]);
    fmtCards.forEach((b) => b.addEventListener('click', () => selectFormat(b)));
  }

  dlBtn.addEventListener('click', () => {
    const formatId = cardEl.dataset.formatId || null;
    status.className = 'dl-status text-sm text-brand-600';
    status.innerHTML = '<span class="inline-flex items-center gap-2"><span class="spinner"></span> 服务端正在下载并回传，请稍候…（大文件可能需要一些时间）</span>';
    triggerDownload(url, formatId);
    setTimeout(() => {
      status.className = 'dl-status text-sm text-emerald-600';
      status.textContent = '已发起下载，请查看浏览器下载列表。若未开始，请重试或更换清晰度。';
    }, 2500);
  });

  const aiBtn = cardEl.querySelector('.ai-btn');
  if (aiBtn) {
    if (!state.aiAvailable) aiBtn.title = '未检测到大模型配置，点击可查看如何启用';
    aiBtn.addEventListener('click', () => runAllAi(cardEl, url, aiBtn));
  }

  const collBtn = cardEl.querySelector('.coll-btn');
  if (collBtn) {
    collBtn.addEventListener('click', () => addToCollection({
      content_key: cardEl.dataset.contentKey || '',
      url,
      title: cardEl.dataset.title || '',
    }));
  }

  cardEl.querySelectorAll('[role="tab"]').forEach((tabBtn) => {
    tabBtn.addEventListener('click', () => switchTab(cardEl, url, tabBtn.dataset.tab));
  });
};

/* ---------- 单条渲染 ---------- */
const renderSingle = async (container, url) => {
  container.innerHTML =
    '<div class="rounded-3xl border border-slate-200 bg-white p-8 text-center text-slate-500 shadow-sm"><span class="inline-flex items-center gap-2"><span class="spinner"></span> 正在解析视频信息…</span></div>';
  try {
    const info = await parseInfo(url);
    container.innerHTML = renderCard(info, url);
    const card = container.firstElementChild;
    card.dataset.contentKey = info.content_key || '';
    card.dataset.title = info.title || '';
    bindCard(card, url);
    switchTab(card, url, 'comments');   // 默认展示高赞评论（自动抓取，零 LLM 成本）
  } catch (e) {
    container.innerHTML =
      `<div class="rounded-3xl border border-rose-300 bg-rose-50 p-6 text-sm text-rose-600">${escapeHtml(e.message)}</div>`;
  }
};

/* ---------- 批量渲染 ---------- */
const renderBatch = async (container, urls) => {
  container.innerHTML = '';
  const bar = document.createElement('div');
  bar.className = 'mb-4 flex items-center justify-between rounded-2xl border border-slate-200 bg-white p-4 shadow-sm';
  bar.innerHTML = `<span class="text-sm text-slate-700">共 ${urls.length} 个链接</span>
    <button id="dl-all" class="rounded-xl bg-brand-500 px-5 py-2 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600">全部下载</button>`;
  const wrap = document.createElement('div');
  wrap.className = 'space-y-4';
  container.appendChild(bar);
  container.appendChild(wrap);

  const parsed = [];
  for (let i = 0; i < urls.length; i++) {
    const url = urls[i];
    const holder = document.createElement('div');
    holder.innerHTML =
      '<div class="rounded-2xl border border-slate-200 bg-white p-5 text-sm text-slate-500 shadow-sm"><span class="inline-flex items-center gap-2"><span class="spinner"></span> 解析中…</span></div>';
    wrap.appendChild(holder);
    try {
      const info = await parseInfo(url);
      holder.innerHTML = renderCard(info, url, { compact: true });
      holder.firstElementChild.dataset.contentKey = info.content_key || '';
      holder.firstElementChild.dataset.title = info.title || '';
      bindCard(holder.firstElementChild, url);
      parsed.push({ url, el: holder.firstElementChild });
    } catch (e) {
      holder.innerHTML =
        `<div class="rounded-2xl border border-rose-300 bg-rose-50 p-4 text-sm text-rose-600">${escapeHtml(url)} — ${escapeHtml(e.message)}</div>`;
    }
  }

  // 全部下载：逐条触发，间隔避免浏览器拦截
  bar.querySelector('#dl-all').addEventListener('click', () => {
    parsed.forEach((item, idx) => {
      setTimeout(() => triggerDownload(item.url, item.el.dataset.formatId || null), idx * 1200);
    });
  });
};

/* ---------- 视图对象 ---------- */
export default {
  title: '解析结果',
  mount(container, ctx) {
    const urls = ctx.query.getAll('url').map((u) => u.trim()).filter(Boolean);
    if (!urls.length) {
      container.innerHTML =
        '<div class="rounded-3xl border border-slate-200 bg-white p-8 text-center text-sm text-slate-500 shadow-sm">缺少视频链接，请返回首页重新解析。</div>';
      return;
    }
    container.classList.add('flex', 'w-full', 'flex-col', 'px-4', 'py-4', 'sm:px-6');
    if (urls.length > 1) {
      renderBatch(container, urls);
    } else {
      // 单条双栏：桌面端主区不整页滚动，改由左右栏各自内部滚动（按钮/Tab 常驻）
      container.classList.add('lg:min-h-0', 'lg:overflow-hidden');
      renderSingle(container, urls[0]);
    }
  },
};
