/* MindPilot 前端交互逻辑 */
(() => {
  const $ = (sel) => document.querySelector(sel);

  const urlInput = $('#url');
  const parseBtn = $('#parse-btn');
  const batchUrls = $('#batch-urls');
  const batchBtn = $('#batch-btn');
  const singleBox = $('#single-input');
  const batchBox = $('#batch-input');
  const modeToggle = $('#mode-toggle');
  const modeLabel = $('#mode-label');
  const resultSection = $('#result');
  const ffmpegTip = $('#ffmpeg-tip');

  let batchMode = false;
  let aiAvailable = false; // 由 /api/health 告知，用于 AI 总结按钮的可用性提示
  // 认证态（均由 /api/health 与 /api/auth/me 告知；Cookie 为 httpOnly，前端不直读）
  let authEnabled = false;  // 是否提供登录能力（决定导航是否显示登录/注册）
  let authRequired = false; // 业务端点是否强制登录（与后端门禁同一判据）
  let currentUser = null;   // 已登录用户的完整资料（/api/auth/me 回传）或 null

  // 会话级缓存与并发去重：同一 url 重复点击秒回、并发点击只发一次请求
  const txCache = new Map();       // url -> 转写结果
  const sumCache = new Map();      // url -> 总结响应
  const mindmapCache = new Map();  // url -> 思维导图响应
  const commentsCache = new Map(); // url -> 高赞评论响应
  const qaCache = new Map();       // url -> { messages: [{role, content}] } 问答会话（多轮，切 Tab 保留）
  const inflight = new Map();      // `${type}:${url}` -> Promise

  // AI 输出区五 Tab（总结/字幕/思维导图/高赞评论/问答）的样式、图标与文案
  const AI_CONFIG_HINT = '请复制 .env.example 为 .env 并填入 API Key 后重启服务。';
  const TAB_ACTIVE = 'ai-tab -mb-px inline-flex min-w-fit flex-1 items-center justify-center gap-1.5 whitespace-nowrap border-b-2 border-brand-500 px-2 pb-3 pt-1 text-sm font-semibold text-brand-600';
  const TAB_IDLE = 'ai-tab -mb-px inline-flex min-w-fit flex-1 items-center justify-center gap-1.5 whitespace-nowrap border-b-2 border-transparent px-2 pb-3 pt-1 text-sm font-semibold text-slate-500 transition hover:text-slate-800';
  // 清晰度卡片（单选）的基类与选中/未选中态
  const FMT_BASE = 'format-card flex w-full items-center gap-3 rounded-2xl border p-3 text-left transition';
  const FMT_ON = 'border-brand-400 bg-brand-50';
  const FMT_OFF = 'border-slate-200 bg-white hover:border-brand-300';
  // 复用小图标
  const ICON = {
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

  /* ---------- 工具函数 ---------- */
  const fmtDuration = (sec) => {
    if (!sec && sec !== 0) return '';
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${m}:${String(s).padStart(2, '0')}`;
  };

  const fmtSize = (bytes) => {
    if (!bytes) return '';
    const mb = bytes / 1024 / 1024;
    if (mb >= 1024) return `${(mb / 1024).toFixed(2)} GB`;
    return `${mb.toFixed(1)} MB`;
  };

  // 播放量等数字的中文简写：894000 -> 89.4万
  const fmtCount = (n) => {
    if (!n && n !== 0) return '';
    if (n >= 100000000) return `${(n / 100000000).toFixed(1)}亿`;
    if (n >= 10000) return `${(n / 10000).toFixed(1)}万`;
    return String(n);
  };

  // 字幕时间戳：支持超过 1 小时（HH:MM:SS）
  const fmtTs = (sec) => {
    if (!sec && sec !== 0) return '';
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
    const mm = h > 0 ? String(m).padStart(2, '0') : m;
    const ss = String(s).padStart(2, '0');
    return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  };

  // 评论时间戳（unix 秒）→ YYYY-MM-DD；无值返回 ''
  const fmtDate = (ts) => {
    const n = Number(ts);
    if (!n) return '';
    const d = new Date(n * 1000);
    if (Number.isNaN(d.getTime())) return '';
    const p = (v) => String(v).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };

  const escapeHtml = (str = '') =>
    String(str).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // 触发浏览器原生下载（GET，流式，对大文件/手机友好）
  const triggerDownload = (videoUrl, formatId) => {
    const params = new URLSearchParams({ url: videoUrl });
    if (formatId) params.set('format_id', formatId);
    const a = document.createElement('a');
    a.href = `/api/download?${params.toString()}`;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  /* ---------- 字幕导出（纯前端 Blob 下载 SRT / TXT） ---------- */
  const srtTime = (sec) => {
    const t = Math.max(0, sec || 0);
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = Math.floor(t % 60);
    const ms = Math.round((t - Math.floor(t)) * 1000);
    const p = (v, l = 2) => String(v).padStart(l, '0');
    return `${p(h)}:${p(m)}:${p(s)},${p(ms, 3)}`;
  };
  const buildSubtitleText = (segs, kind) => segs
    .map((seg, i) => {
      if (kind !== 'srt') return seg.text;
      const end = seg.end != null ? seg.end : (segs[i + 1] ? segs[i + 1].start : seg.start);
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
  const downloadText = (filename, text, mime) => downloadBlob(filename, new Blob([text], { type: mime }));

  /* ---------- 思维导图导出（Markdown 大纲 / Canvas PNG，无第三方库） ---------- */
  const buildMindmapMarkdown = (mm) => {
    const lines = [`# ${mm.title || '思维导图'}`];
    const walk = (n, depth) => (n.children || []).forEach((k) => {
      lines.push(`${'  '.repeat(depth)}- ${k.title || ''}`);
      walk(k, depth + 1);
    });
    walk(mm, 0);
    return lines.join('\n');
  };

  const exportMindmapPng = (mm, baseName) => {
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

  /* ---------- 健康检查 / ffmpeg + AI 状态提示 ---------- */
  const checkHealth = async () => {
    try {
      const res = await fetch('/api/health');
      const data = await res.json();
      aiAvailable = !!data.ai;
      authEnabled = !!data.auth;
      authRequired = !!data.auth_required;
      if (!data.ffmpeg) {
        ffmpegTip.classList.remove('hidden');
        ffmpegTip.classList.add('inline-flex');
        ffmpegTip.innerHTML =
          '<svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>' +
          '<span>未检测到 ffmpeg，仅提供已合成的清晰度（安装后可解锁高清合并）</span>';
      }
    } catch (e) { /* 忽略 */ }
    // 无论健康检查成败都要刷一次登录态（决定导航形态与是否拦截解析）
    await refreshAuthState();
  };

  /* ---------- 解析单个视频 ---------- */
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

    // 元信息行：上传者 / 来源徽标 / 播放量
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

    return `
      <div class="fade-in ${compact ? 'flex flex-col gap-4' : 'grid items-start gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]'}" data-compact="${compact ? '1' : ''}">
        <!-- 左栏：视频信息 + 清晰度 + 操作按钮 -->
        <div class="flex min-w-0 flex-col gap-4">
          <div class="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-card">
            <div class="p-4">
              <div class="relative aspect-video w-full overflow-hidden rounded-2xl bg-slate-100">
                ${thumb}
                ${info.duration ? `<span class="absolute bottom-2 right-2 rounded bg-black/70 px-1.5 py-0.5 text-xs text-white">${fmtDuration(info.duration)}</span>` : ''}
              </div>
              <h3 class="mt-3 line-clamp-2 text-base font-bold leading-snug text-slate-900" title="${escapeHtml(info.title)}">${escapeHtml(info.title)}</h3>
              ${metaBits ? `<div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">${metaBits}</div>` : ''}
              ${!compact && info.description ? `<p class="mt-2 line-clamp-2 text-xs leading-relaxed text-slate-400">${escapeHtml(info.description)}</p>` : ''}
            </div>
            <div class="border-t border-slate-100 p-4">
              <div class="flex items-center gap-2 text-sm font-semibold text-slate-700">${ICON.slider}选择清晰度和格式</div>
              <div class="mt-3 space-y-2">${formatCards}</div>
            </div>
            <div class="flex flex-col gap-2 border-t border-slate-100 p-4">
              <button class="dl-btn inline-flex items-center justify-center gap-2 rounded-2xl bg-brand-500 px-6 py-3 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600 active:scale-95">${ICON.dl}立即下载</button>
              <button class="ai-btn inline-flex items-center justify-center gap-2 rounded-2xl border border-brand-200 bg-white px-6 py-3 text-sm font-semibold text-brand-600 transition hover:bg-brand-50 active:scale-95">${ICON.spark}一键 AI 分析</button>
              <div class="dl-status hidden text-sm"></div>
            </div>
          </div>
        </div>
        <!-- 右栏：五 Tab 分析面板（默认字幕文本） -->
        <div class="ai-panel ${compact ? 'hidden' : ''} flex min-h-[26rem] min-w-0 flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-card">
          <div role="tablist" aria-label="AI 分析" class="flex w-full gap-1 overflow-x-auto border-b border-slate-200 px-2 pt-2">
            ${TAB_ORDER.map((t) => tabButton(t, t === 'transcript')).join('')}
          </div>
          ${TAB_ORDER.map((t) => `<div role="tabpanel" data-panel="${t}" class="ai-tabpanel${t === 'transcript' ? '' : ' hidden'} flex-1 p-4 sm:p-5"></div>`).join('')}
        </div>
      </div>`;
  };

  /* ---------- AI 面板：加载 / 错误（写入 tabpanel，不再整体替换其 className） ---------- */
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
    // 每 3s 轮换阶段文案并刷新已用秒数，缓解长等待焦虑
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

  const renderTranscript = (panel, data) => {
    const segs = data.segments || [];
    const rows = segs
      .map((seg) => `
        <div class="flex gap-3 border-b border-slate-100 py-1.5 last:border-0">
          <span class="shrink-0 font-mono text-xs text-brand-500">${fmtTs(seg.start)}</span>
          <span class="text-sm leading-relaxed text-slate-600">${escapeHtml(seg.text)}</span>
        </div>`)
      .join('');
    // 徽标不展示 ASR 服务商名（旧缓存的 language_name 可能带「· 服务商」后缀，仅取首段）
    const badgeName = (data.language_name || '').split(' · ')[0];
    const langBadge = [badgeName, data.language].filter(Boolean).join(' · ');
    panel.innerHTML = `
      <div class="fade-in flex h-full flex-col">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-sm text-slate-600">共 <b class="font-semibold text-slate-900">${segs.length}</b> 条字幕</span>
          ${langBadge ? `<span class="rounded-md bg-brand-50 px-2 py-0.5 text-xs font-semibold text-brand-600">${escapeHtml(langBadge)}</span>` : ''}
          ${data.cached ? '<span class="rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-semibold text-emerald-600">已缓存</span>' : ''}
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
      <div class="sub-body mt-3 max-h-[28rem] flex-1 space-y-0.5 overflow-y-auto pr-2">${rows}</div>
      </div>`;

    // 下载字幕下拉（SRT / TXT，纯前端 Blob 导出）
    const menu = panel.querySelector('.sub-dl-menu');
    panel.querySelector('.sub-dl').addEventListener('click', (e) => { e.stopPropagation(); menu.classList.toggle('hidden'); });
    panel.querySelectorAll('.sub-dl-opt').forEach((opt) => opt.addEventListener('click', () => {
      const kind = opt.dataset.kind;
      downloadText(`subtitles.${kind}`, buildSubtitleText(segs, kind), kind === 'srt' ? 'application/x-subrip' : 'text/plain');
      menu.classList.add('hidden');
    }));
    // 一键复制全文（纯文本，clipboard API 失败时降级 execCommand）
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
    // 展开全部 / 收起：切换内层滚动高度（inline style 覆盖 max-h 类）
    const body = panel.querySelector('.sub-body');
    const expandBtn = panel.querySelector('.sub-expand');
    expandBtn.addEventListener('click', () => {
      const expanded = body.style.maxHeight === 'none';
      body.style.maxHeight = expanded ? '' : 'none';
      expandBtn.textContent = expanded ? '展开全部' : '收起';
    });
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

    // 下载导图下拉（PNG 图片 / Markdown 大纲）
    const base = (mm.title || 'mindmap').replace(/[\\/:*?"<>|]/g, '_');
    const mmMenu = panel.querySelector('.mm-dl-menu');
    panel.querySelector('.mm-dl').addEventListener('click', (e) => { e.stopPropagation(); mmMenu.classList.toggle('hidden'); });
    panel.querySelectorAll('.mm-dl-opt').forEach((opt) => opt.addEventListener('click', () => {
      if (opt.dataset.kind === 'png') exportMindmapPng(mm, base);
      else downloadText(`${base}.md`, buildMindmapMarkdown(mm), 'text/markdown');
      mmMenu.classList.add('hidden');
    }));
  };

  /* ---------- 高赞评论渲染（无大模型：服务端已按赞排序取 TopN） ---------- */
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
      <div class="mt-3 max-h-[28rem] flex-1 overflow-y-auto pr-2">${rows}</div>
      </div>`;
  };

  /* ---------- AI 问答渲染（多轮聊天，会话状态存于 qaCache，切 Tab 保留） ---------- */
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
    if (!conv) { conv = { messages: [] }; qaCache.set(url, conv); }

    panel.innerHTML = `
      <div class="fade-in flex flex-col">
        <div class="flex items-center justify-between gap-2">
          <h4 class="inline-flex items-center gap-2 text-base font-bold text-slate-900">
            <span class="text-brand-500">${TAB_ICONS.qa}</span> AI 问答
          </h4>
          <span class="text-xs text-slate-400">仅依据该视频字幕作答</span>
        </div>
        <div class="qa-log mt-3 max-h-[26rem] space-y-3 overflow-y-auto pr-1"></div>
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
      const history = conv.messages.slice(-8);   // 仅携带本轮之前的对话作为上下文
      conv.messages.push({ role: 'user', content: question });
      paintLog();
      const thinking = document.createElement('div');
      thinking.className = 'flex justify-start';
      thinking.innerHTML = '<div class="inline-flex items-center gap-2 rounded-2xl rounded-bl-sm border border-slate-200 bg-white px-3.5 py-2 text-sm text-slate-400"><span class="spinner"></span> 思考中…</div>';
      logEl.appendChild(thinking);
      logEl.scrollTop = logEl.scrollHeight;
      sendBtn.disabled = true; sendBtn.classList.add('opacity-60');
      try {
        const { res, data } = await postJson('/api/qa', { url, question, history });
        if (!res.ok) {
          const msg = res.status === 503
            ? `${data.detail || 'AI 未配置'}。${AI_CONFIG_HINT}`
            : (data.detail || `问答失败 (HTTP ${res.status})`);
          conv.messages.push({ role: 'assistant', content: msg, error: true });
        } else {
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

  const postJson = async (path, body) => {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) markUnauthorized();   // 会话中途失效：统一弹登录框
    return { res, data };
  };

  // 客户端 single-flight：相同 key 的并发请求复用同一 Promise
  const dedup = (key, factory) => {
    if (inflight.has(key)) return inflight.get(key);
    const p = factory().finally(() => inflight.delete(key));
    inflight.set(key, p);
    return p;
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
    if (hit) { renderTranscript(panel, { ...hit, cached: true }); return true; }
    const stop = panelLoading(panel, [
      '正在提取字幕…', '若该视频无字幕，正在下载音频并识别语音…', '快好了…',
    ]);
    try {
      const { res, data } = await dedup('tx:' + url, () => postJson('/api/transcribe', { url }));
      if (!res.ok) { panelError(panel, data.detail || `转写失败 (HTTP ${res.status})`); return false; }
      txCache.set(url, data);
      renderTranscript(panel, data);
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

  // 高赞评论 Tab：无大模型，纯抓取（点到才请求，会话级缓存 + dedup）
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

  // 问答 Tab：仅渲染聊天界面（不预请求），真正提问时再打 /api/qa
  const loadQA = (url, panel) => { renderQA(panel, url); return true; };

  const TAB_LOADERS = { summary: loadSummary, transcript: loadTranscript, mindmap: loadMindmap, comments: loadComments, qa: loadQA };

  /* ---------- Tab 切换：懒加载 + 防重复请求（思维导图/问答点到才加载） ---------- */
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
    if (st.loaded.has(tab)) return;          // 已加载：仅切显隐，不重复请求
    st.loaded.add(tab);
    const panel = card.querySelector(`.ai-tabpanel[data-panel="${tab}"]`);
    let ok = true;
    try { ok = await TAB_LOADERS[tab](url, panel); }
    catch (e) { ok = false; panelError(panel, e.message || '加载失败'); }
    if (ok === false) st.loaded.delete(tab); // 失败允许再次点击重试
  };

  // 后台预取某个 Tab（不切显隐）：已加载跳过，失败允许重试
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

  // 一键 AI 分析：并行预取「总结摘要 + 字幕文本 + 思维导图」三个 Tab，并定位到右栏
  const runAllAi = (card, url, btn) => {
    const panel = card.querySelector('.ai-panel');
    if (card.dataset.compact === '1') panel.classList.remove('hidden');
    activateTab(card, 'summary');   // 先展示总结 Tab（内容由下方预取写入）
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

  /* ---------- 绑定卡片事件 ---------- */
  const bindCard = (cardEl, url) => {
    const dlBtn = cardEl.querySelector('.dl-btn');
    const status = cardEl.querySelector('.dl-status');
    cardEl._aiState = { loaded: new Set() };   // 该卡片已加载的 Tab（懒加载 + 防重复请求）

    // 清晰度卡片单选：默认选中第一项，选中态写回 dataset.formatId 供下载读取
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
      // 浏览器原生下载无法精确捕获完成事件，给出提示后延时收起
      setTimeout(() => {
        status.className = 'dl-status text-sm text-emerald-600';
        status.textContent = '已发起下载，请查看浏览器下载列表。若未开始，请重试或更换清晰度。';
      }, 2500);
    });

    // 一键 AI 分析：并行预取总结+字幕+思维导图并定位右栏（compact 时先展开面板）
    const aiBtn = cardEl.querySelector('.ai-btn');
    if (aiBtn) {
      if (!aiAvailable) aiBtn.title = '未检测到大模型配置，点击可查看如何启用';
      aiBtn.addEventListener('click', () => runAllAi(cardEl, url, aiBtn));
    }

    // Tab 栏点击切换（除默认高赞评论外均点到才加载，避免一进入就连打多次 LLM/ASR）
    cardEl.querySelectorAll('[role="tab"]').forEach((tabBtn) => {
      tabBtn.addEventListener('click', () => switchTab(cardEl, url, tabBtn.dataset.tab));
    });
  };

  /* ---------- 单条解析流程 ---------- */
  const handleParse = async () => {
    if (!ensureAuth(handleParse)) return;   // 未登录：弹框拦截，登录成功后自动续做
    const url = urlInput.value.trim();
    if (!url) { urlInput.focus(); return; }
    parseBtn.disabled = true;
    parseBtn.innerHTML = '<span class="inline-flex items-center gap-2"><span class="spinner"></span> 解析中…</span>';
    resultSection.classList.remove('hidden');
    resultSection.innerHTML =
      '<div class="rounded-3xl border border-slate-200 bg-white p-8 text-center text-slate-500 shadow-sm"><span class="inline-flex items-center gap-2"><span class="spinner"></span> 正在解析视频信息…</span></div>';
    try {
      const info = await parseInfo(url);
      resultSection.innerHTML = renderCard(info, url);
      const card = resultSection.firstElementChild;
      bindCard(card, url);
      switchTab(card, url, 'comments');   // 默认展示高赞评论 Tab（自动抓取，零 LLM 成本）；字幕改为点 Tab 或一键分析时才抓
      resultSection.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } catch (e) {
      resultSection.innerHTML =
        `<div class="rounded-3xl border border-rose-300 bg-rose-50 p-6 text-sm text-rose-600">${escapeHtml(e.message)}</div>`;
    } finally {
      parseBtn.disabled = false;
      parseBtn.textContent = '解析视频';
    }
  };

  /* ---------- 批量解析流程 ---------- */
  const handleBatch = async () => {
    if (!ensureAuth(handleBatch)) return;
    const urls = batchUrls.value.split('\n').map((s) => s.trim()).filter(Boolean);
    if (!urls.length) { batchUrls.focus(); return; }
    batchBtn.disabled = true;
    batchBtn.innerHTML = '<span class="inline-flex items-center gap-2"><span class="spinner"></span> 批量解析中…</span>';
    resultSection.classList.remove('hidden');
    resultSection.innerHTML = '';

    const wrap = document.createElement('div');
    wrap.className = 'space-y-4';
    const bar = document.createElement('div');
    bar.className = 'mb-2 flex items-center justify-between rounded-2xl border border-slate-200 bg-white p-4 shadow-sm';
    bar.innerHTML = `<span class="text-sm text-slate-700">共 ${urls.length} 个链接</span>
      <button id="dl-all" class="rounded-xl bg-brand-500 px-5 py-2 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600">全部下载</button>`;
    resultSection.appendChild(bar);
    resultSection.appendChild(wrap);

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
        bindCard(holder.firstElementChild, url);
        parsed.push({ url, formatId: null, el: holder.firstElementChild });
      } catch (e) {
        holder.innerHTML =
          `<div class="rounded-2xl border border-rose-300 bg-rose-50 p-4 text-sm text-rose-600">${escapeHtml(url)} — ${escapeHtml(e.message)}</div>`;
      }
    }

    // 全部下载：逐条触发，间隔避免浏览器拦截
    bar.querySelector('#dl-all').addEventListener('click', () => {
      parsed.forEach((item, idx) => {
        setTimeout(() => {
          triggerDownload(item.url, item.el.dataset.formatId || null);
        }, idx * 1200);
      });
    });

    batchBtn.disabled = false;
    batchBtn.textContent = '批量解析';
    resultSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  /* ---------- 模式切换 ---------- */
  modeToggle.addEventListener('click', () => {
    batchMode = !batchMode;
    singleBox.classList.toggle('hidden', batchMode);
    singleBox.classList.toggle('flex', !batchMode);
    batchBox.classList.toggle('hidden', !batchMode);
    batchBox.classList.toggle('flex', batchMode);
    modeLabel.textContent = batchMode ? '切换到单条模式' : '切换到批量模式';
    resultSection.classList.add('hidden');
    resultSection.innerHTML = '';
  });

  /* ---------- 事件绑定 ---------- */
  parseBtn.addEventListener('click', handleParse);
  urlInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') handleParse(); });
  batchBtn.addEventListener('click', handleBatch);

  /* ---------- 付费占位弹窗 ---------- */
  const payModal = $('#pay-modal');
  const payText = $('#pay-modal-text');
  document.querySelectorAll('.pay-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const plan = btn.dataset.pay === 'team' ? 'Team' : 'Pro';
      payText.textContent = `你正在尝试升级到 ${plan} 方案。当前为演示版本，支付通道正在接入中，敬请期待。`;
      payModal.classList.remove('hidden');
      payModal.classList.add('flex');
    });
  });
  const closePay = () => { payModal.classList.add('hidden'); payModal.classList.remove('flex'); };
  $('#pay-close').addEventListener('click', closePay);
  payModal.addEventListener('click', (e) => { if (e.target === payModal) closePay(); });

  /* ---------- 认证：登录 / 注册 / 记住我 / 登出 ---------- */
  const authModal = $('#auth-modal');
  const authForm = $('#auth-form');
  const authTitle = $('#auth-title');
  const authSubtitle = $('#auth-subtitle');
  const authTabLogin = $('#auth-tab-login');
  const authTabRegister = $('#auth-tab-register');
  const authIdentifierWrap = $('#auth-identifier-wrap');
  const authIdentifier = $('#auth-identifier');
  const authEmailWrap = $('#auth-email-wrap');
  const authEmail = $('#auth-email');
  const authPhoneWrap = $('#auth-phone-wrap');
  const authPhone = $('#auth-phone');
  const authNicknameWrap = $('#auth-nickname-wrap');
  const authNickname = $('#auth-nickname');
  const authHint = $('#auth-hint');
  const authPassword = $('#auth-password');
  const authPasswordLabel = $('#auth-password-label');
  const authRememberWrap = $('#auth-remember-wrap');
  const authRemember = $('#auth-remember');
  const authError = $('#auth-error');
  const authSubmit = $('#auth-submit');
  const navLogin = $('#nav-login');
  const navRegister = $('#nav-register');
  const navUser = $('#nav-user');
  const navProfile = $('#nav-profile');
  const navAvatarImg = $('#nav-avatar-img');
  const navAvatarIcon = $('#nav-avatar-icon');
  const navUserName = $('#nav-user-name');
  const navLogout = $('#nav-logout');

  // 胶囊 Tab 的选中/未选中态（风格对齐顶部 TAB_ACTIVE / TAB_IDLE）
  const AUTH_TAB_ON = 'auth-tab flex-1 rounded-full bg-white py-1.5 font-semibold text-brand-600 shadow-sm';
  const AUTH_TAB_OFF = 'auth-tab flex-1 rounded-full py-1.5 font-semibold text-slate-500 transition hover:text-slate-800';

  let authMode = 'login';      // 'login' | 'register'
  let pendingAction = null;    // 被登录拦截的动作，登录成功后自动续做

  const authMsg = (el, text) => {
    el.textContent = text || '';
    el.classList.toggle('hidden', !text);
  };

  // 头像渲染：有 avatar_url 则显示图片，否则回退到占位元素（导航人形图标 / 个人资料首字母）
  const renderAvatar = (img, fallbackEl, url) => {
    if (url) {
      img.classList.remove('hidden');
      fallbackEl.classList.add('hidden');
      // 仅在地址变化时重设 src：避免每次渲染都重新拉图造成闪烁
      if (img.getAttribute('src') !== url) {
        img.onerror = () => { img.classList.add('hidden'); fallbackEl.classList.remove('hidden'); };
        img.src = url;
      }
    } else {
      img.classList.add('hidden');
      img.removeAttribute('src');
      fallbackEl.classList.remove('hidden');
    }
  };

  // 切换登录/注册形态：
  //   登录 = 单个「邮箱或手机号」输入框 + 记住我
  //   注册 = 邮箱（选填）+ 手机号（选填）+ 昵称，二者至少填一项（后端最终裁决）
  const setAuthMode = (mode) => {
    authMode = mode;
    const reg = mode === 'register';
    authTitle.textContent = reg ? '注册 MindPilot' : '登录 MindPilot';
    authSubtitle.textContent = reg ? '注册后即可解析、下载与使用 AI 分析。' : '登录后即可解析、下载与使用 AI 分析。';
    authSubmit.textContent = reg ? '注册并登录' : '登录';
    authIdentifierWrap.classList.toggle('hidden', reg);
    authEmailWrap.classList.toggle('hidden', !reg);
    authPhoneWrap.classList.toggle('hidden', !reg);
    authNicknameWrap.classList.toggle('hidden', !reg);
    authHint.classList.toggle('hidden', !reg);
    authRememberWrap.classList.toggle('hidden', reg);
    authPasswordLabel.textContent = reg ? '设置密码' : '密码';
    authPassword.setAttribute('autocomplete', reg ? 'new-password' : 'current-password');
    authTabLogin.className = reg ? AUTH_TAB_OFF : AUTH_TAB_ON;
    authTabRegister.className = reg ? AUTH_TAB_ON : AUTH_TAB_OFF;
    authMsg(authError, '');
  };

  // 导航形态：鉴权关闭时不显示任何登录入口，保持原有纯工具站体验
  const renderAuthState = () => {
    const logged = !!currentUser;
    navLogin.classList.toggle('hidden', !authEnabled || logged);
    navRegister.classList.toggle('hidden', !authEnabled || logged);
    navUser.classList.toggle('hidden', !logged);
    navUser.classList.toggle('flex', logged);
    if (!logged) return;
    navUserName.textContent = currentUser.nickname || currentUser.email || currentUser.phone || '';
    renderAvatar(navAvatarImg, navAvatarIcon, currentUser.avatar_url);
  };

  const openAuth = (mode = 'login') => {
    setAuthMode(mode);
    authModal.classList.remove('hidden');
    authModal.classList.add('flex');
    setTimeout(() => (mode === 'register' ? authEmail : authIdentifier).focus(), 60);
  };

  const closeAuth = () => {
    authModal.classList.add('hidden');
    authModal.classList.remove('flex');
    pendingAction = null;      // 主动关闭 = 放弃被拦截的动作
  };

  // 会话失效（任意业务接口 401）：清本地态并弹登录框
  const markUnauthorized = () => {
    currentUser = null;
    renderAuthState();
    if (authRequired) openAuth('login');
  };

  // 拉取当前登录态：/api/auth/me 返回 401 即未登录（Cookie 为 httpOnly，前端不直读）
  const refreshAuthState = async () => {
    currentUser = null;
    if (authEnabled) {
      try {
        const res = await fetch('/api/auth/me');
        if (res.ok) currentUser = (await res.json()).user || null;
      } catch (e) { /* 网络异常按未登录处理 */ }
    }
    renderAuthState();
  };

  // 前端门禁：需登录却未登录时弹框并返回 false（后端 401 才是最终裁决）
  const ensureAuth = (onAuthed) => {
    if (!authRequired || currentUser) return true;
    pendingAction = onAuthed || null;
    openAuth('login');
    return false;
  };

  // 登录：成功后写入本地态、关框，并续做被拦截的动作
  // identifier 为「邮箱或手机号」，具体类型由后端判定（含 @ 视为邮箱）
  const doLogin = async (identifier, password, remember) => {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ identifier, password, remember }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      authMsg(authError, data.detail || `登录失败 (HTTP ${res.status})`);
      return false;
    }
    const act = pendingAction;      // 必须先取出：closeAuth() 会清空 pendingAction
    currentUser = data.user || null;
    renderAuthState();
    closeAuth();
    authPassword.value = '';
    if (act) setTimeout(act, 0);   // 让弹窗先收起再发起解析，避免界面抢焦点
    return true;
  };

  authForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const password = authPassword.value;
    authMsg(authError, '');

    // 两种模式走不同校验：注册收集邮箱/手机号（至少一项），登录只收一个标识符
    const reg = authMode === 'register';
    let identifier = '';
    let registerPayload = null;
    if (reg) {
      const email = authEmail.value.trim();
      const phone = authPhone.value.trim();
      if (!email && !phone) { authMsg(authError, '请至少填写邮箱或手机号其中一项。'); return; }
      if (!password) { authMsg(authError, '请设置密码。'); return; }
      registerPayload = {
        email: email || null,
        phone: phone || null,
        password,
        nickname: authNickname.value.trim() || null,
      };
      identifier = email || phone;   // 注册成功后用它自动登录
    } else {
      identifier = authIdentifier.value.trim();
      if (!identifier || !password) { authMsg(authError, '请填写邮箱/手机号与密码。'); return; }
    }

    authSubmit.disabled = true;
    authSubmit.innerHTML = '<span class="inline-flex items-center justify-center gap-2"><span class="spinner"></span> 处理中…</span>';
    try {
      if (registerPayload) {
        const res = await fetch('/api/auth/register', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(registerPayload),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { authMsg(authError, data.detail || `注册失败 (HTTP ${res.status})`); return; }
        // 注册成功即自动登录（少一次交互）；若登录失败则退回登录 Tab 并保留原因
        const okLogin = await doLogin(identifier, password, authRemember.checked);
        if (!okLogin) {
          const reason = authError.textContent;
          setAuthMode('login');
          authIdentifier.value = identifier;   // 回填标识符，用户只需再输一次密码
          authMsg(authError, reason || '注册成功，请用刚才的账号密码登录。');
        }
      } else {
        await doLogin(identifier, password, authRemember.checked);
      }
    } catch (err) {
      authMsg(authError, err.message || '网络错误，请稍后重试。');
    } finally {
      authSubmit.disabled = false;
      // 按当前模式恢复按钮文案（注册失败退回登录 Tab 时 label 已过期）
      authSubmit.textContent = authMode === 'register' ? '注册并登录' : '登录';
    }
  });

  navLogin.addEventListener('click', () => openAuth('login'));
  navRegister.addEventListener('click', () => openAuth('register'));
  authTabLogin.addEventListener('click', () => setAuthMode('login'));
  authTabRegister.addEventListener('click', () => setAuthMode('register'));
  $('#auth-close').addEventListener('click', closeAuth);
  authModal.addEventListener('click', (e) => { if (e.target === authModal) closeAuth(); });

  /* ---------- 个人资料：双 Tab + 常驻底部操作栏 ----------
     报错信息一律跟随触发它的上下文：头像行 / 各安全卡片 / 底部状态栏，
     不再共用一个远离操作点的提示框。 */
  const profileModal = $('#profile-modal');
  const profileHeadAvatar = $('#profile-head-avatar');
  const profileHeadInitial = $('#profile-head-initial');
  const profileHeadName = $('#profile-head-name');
  const profileHeadSub = $('#profile-head-sub');
  const profileTabBasic = $('#profile-tab-basic');
  const profileTabSecurity = $('#profile-tab-security');
  const profilePanelBasic = $('#profile-panel-basic');
  const profilePanelSecurity = $('#profile-panel-security');
  const profileFooterBasic = $('#profile-footer-basic');
  const profileFooterSecurity = $('#profile-footer-security');
  const profileStatus = $('#profile-status');
  const profileForm = $('#profile-form');
  const profileSave = $('#profile-save');
  const profileAvatarImg = $('#profile-avatar-img');
  const profileAvatarInitial = $('#profile-avatar-initial');
  const profileAvatarBtn = $('#profile-avatar-btn');
  const profileAvatarRemove = $('#profile-avatar-remove');
  const profileAvatarFile = $('#profile-avatar-file');
  const profileAvatarMsg = $('#profile-avatar-msg');
  const profileNickname = $('#profile-nickname');
  const profileGender = $('#profile-gender');
  const profileBirthday = $('#profile-birthday');
  const profileLocation = $('#profile-location');
  const profileBio = $('#profile-bio');
  const profileBioCount = $('#profile-bio-count');
  const profileWebsite = $('#profile-website');
  const profileEmailToggle = $('#profile-email-toggle');
  const profileEmailEdit = $('#profile-email-edit');
  const profileEmailValue = $('#profile-email-value');
  const profileEmailPassword = $('#profile-email-password');
  const profileEmailMsg = $('#profile-email-msg');
  const profilePhoneToggle = $('#profile-phone-toggle');
  const profilePhoneEdit = $('#profile-phone-edit');
  const profilePhoneValue = $('#profile-phone-value');
  const profilePhonePassword = $('#profile-phone-password');
  const profilePhoneMsg = $('#profile-phone-msg');
  const profilePwdToggle = $('#profile-pwd-toggle');
  const profilePwdEdit = $('#profile-pwd-edit');
  const profilePwdCurrent = $('#profile-pwd-current');
  const profilePwdNew = $('#profile-pwd-new');
  const profilePwdMsg = $('#profile-pwd-msg');

  const TAB_ON = 'flex-1 rounded-lg bg-white py-1.5 text-sm font-semibold text-brand-600 shadow-sm';
  const TAB_OFF = 'flex-1 rounded-lg py-1.5 text-sm font-semibold text-slate-500 transition hover:text-slate-800';
  const STATUS_TONE = { idle: 'text-slate-400', dirty: 'text-amber-600', ok: 'text-emerald-600', err: 'text-rose-600' };
  const PLAN_LABELS = { free: '免费版', pro: '专业版', team: '团队版' };

  // 打开弹窗时的资料快照：保存时只提交**变更过**的字段，配合后端 exclude_unset 语义
  let profileSnapshot = {};

  // 行内提示：只改 hidden 与颜色，边距由 HTML 决定（各上下文自带合适的 mt-）
  const setMsg = (el, text, ok = true) => {
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('hidden', !text);
    el.classList.toggle('text-rose-600', Boolean(text) && !ok);
    el.classList.toggle('text-emerald-600', Boolean(text) && ok);
  };

  // 底部状态栏：未修改 / 有未保存 / 成功 / 失败 四态
  const setStatus = (text, tone = 'idle') => {
    profileStatus.textContent = text || '';
    profileStatus.className = `min-w-0 flex-1 text-xs leading-relaxed ${STATUS_TONE[tone] || STATUS_TONE.idle}`;
  };

  // 统一的认证接口调用：解包 {"detail": ...} 信封，失败抛出带状态码的 Error
  const authFetch = async (url, options = {}) => {
    const res = await fetch(url, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.detail || `请求失败 (HTTP ${res.status})`);
      err.status = res.status;
      throw err;
    }
    return data;
  };

  const formatDate = (value) => {
    if (!value) return '-';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };

  // 无头像时的首字母占位（取昵称/邮箱/手机号的首字符）
  const initialOf = (name) => {
    const text = String(name || '').trim();
    return text ? text[0].toUpperCase() : '·';
  };

  const setProfileTab = (tab) => {
    const basic = tab !== 'security';
    profileTabBasic.className = basic ? TAB_ON : TAB_OFF;
    profileTabSecurity.className = basic ? TAB_OFF : TAB_ON;
    profilePanelBasic.classList.toggle('hidden', !basic);
    profilePanelSecurity.classList.toggle('hidden', basic);
    profileFooterBasic.classList.toggle('hidden', !basic);
    profileFooterBasic.classList.toggle('flex', basic);
    profileFooterSecurity.classList.toggle('hidden', basic);
    profileFooterSecurity.classList.toggle('flex', !basic);
  };

  // 头部身份锚点：头像 + 昵称 + 主标识符
  const renderHead = () => {
    const u = currentUser || {};
    const name = u.nickname || u.email || u.phone || '个人资料';
    profileHeadName.textContent = name;
    const bits = [];
    if (u.email) bits.push(u.email);
    if (u.phone) bits.push(u.phone);
    profileHeadSub.textContent = bits.join(' · ') || '管理你的资料与账号安全';
    renderAvatar(profileHeadAvatar, profileHeadInitial, u.avatar_url);
    profileHeadInitial.textContent = initialOf(name);
  };

  // 表单当前值（与快照对比得出「是否有未保存修改」）
  const collectForm = () => ({
    nickname: profileNickname.value.trim(),
    gender: profileGender.value,
    birthday: profileBirthday.value,   // <input type="date"> 清空即 ''，后端按「清除生日」处理
    location: profileLocation.value.trim(),
    bio: profileBio.value,             // 不 trim：保留用户有意的换行
    website: profileWebsite.value.trim(),
  });

  const refreshDirty = () => {
    const next = collectForm();
    const dirty = Object.keys(next).some((k) => next[k] !== profileSnapshot[k]);
    profileSave.disabled = !dirty;
    setStatus(dirty ? '有未保存的修改' : '', dirty ? 'dirty' : 'idle');
  };

  // 表单区（头像 + 可编辑资料）
  const renderProfileForm = () => {
    const u = currentUser || {};
    profileSnapshot = {
      nickname: u.nickname || '',
      gender: u.gender || 'unknown',
      birthday: u.birthday ? String(u.birthday).slice(0, 10) : '',
      location: u.location || '',
      bio: u.bio || '',
      website: u.website || '',
    };
    profileNickname.value = profileSnapshot.nickname;
    profileGender.value = profileSnapshot.gender;
    profileBirthday.value = profileSnapshot.birthday;
    profileLocation.value = profileSnapshot.location;
    profileBio.value = profileSnapshot.bio;
    profileWebsite.value = profileSnapshot.website;
    profileBioCount.textContent = String(profileBio.value.length);

    renderAvatar(profileAvatarImg, profileAvatarInitial, u.avatar_url);
    profileAvatarInitial.textContent = initialOf(u.nickname || u.email || u.phone);
    profileAvatarRemove.classList.toggle('hidden', !u.avatar_url);
    refreshDirty();
  };

  // 账号与安全区（标识符、验证状态、只读信息）
  const renderAccount = () => {
    const u = currentUser || {};
    $('#profile-email').textContent = u.email || '未绑定';
    $('#profile-email-badge').classList.toggle('hidden', !u.email || !!u.email_verified);
    $('#profile-email-toggle').textContent = u.email ? '更换' : '绑定';
    $('#profile-phone').textContent = u.phone || '未绑定';
    $('#profile-phone-badge').classList.toggle('hidden', !u.phone || !!u.phone_verified);
    $('#profile-phone-toggle').textContent = u.phone ? '更换' : '绑定';
    $('#profile-created').textContent = formatDate(u.created_at);
    $('#profile-plan').textContent = PLAN_LABELS[u.plan_id] || u.plan_id || '免费版';
    $('#profile-email-verified').textContent = u.email ? (u.email_verified ? '已验证' : '未验证') : '-';
    $('#profile-phone-verified').textContent = u.phone ? (u.phone_verified ? '已验证' : '未验证') : '-';
    $('#profile-uid').textContent = u.id || '-';
  };

  // 应用服务端回传的最新资料；keepForm=true 时不回填表单，避免覆盖用户正在编辑的内容
  const applyUser = (data, keepForm = false) => {
    if (!data || !data.user) return;
    currentUser = data.user;
    renderHead();
    if (!keepForm) renderProfileForm();
    renderAccount();
    renderAuthState();
  };

  const closeProfile = () => {
    profileModal.classList.add('hidden');
    profileModal.classList.remove('flex');
    setProfileTab('basic');
    [profileEmailEdit, profilePhoneEdit, profilePwdEdit].forEach((p) => p.classList.add('hidden'));
    [profileAvatarMsg, profileEmailMsg, profilePhoneMsg, profilePwdMsg].forEach((el) => setMsg(el, ''));
    setStatus('');
    // 收起即清空密码类输入，避免明文残留在 DOM 里
    profileModal.querySelectorAll('input[type="password"]').forEach((el) => { el.value = ''; });
  };

  // 先用本地态立即渲染（不白屏），再拉一次服务端最新值覆盖
  const openProfile = async () => {
    if (!currentUser) return;
    setProfileTab('basic');
    setStatus('');
    renderHead();
    renderProfileForm();
    renderAccount();
    profileModal.classList.remove('hidden');
    profileModal.classList.add('flex');
    try {
      applyUser(await authFetch('/api/auth/me'));
    } catch (err) {
      if (err.status === 401) { closeProfile(); markUnauthorized(); return; }
      setStatus(err.message || '资料加载失败，请稍后重试。', 'err');
    }
  };

  // 保存资料：只提交变更过的字段；结果反馈在底部状态栏（紧邻保存按钮）
  const saveProfile = async () => {
    const next = collectForm();
    const changes = {};
    Object.keys(next).forEach((k) => { if (next[k] !== profileSnapshot[k]) changes[k] = next[k]; });
    if (!Object.keys(changes).length) { setStatus('没有需要保存的改动。'); return; }

    const label = profileSave.textContent;
    profileSave.disabled = true;
    profileSave.textContent = '保存中…';
    setStatus('');
    try {
      applyUser(await authFetch('/api/auth/me', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(changes),
      }));
      setStatus('资料已保存。', 'ok');   // applyUser 已把按钮置回禁用（无未保存项）
    } catch (err) {
      setStatus(err.message || '保存失败，请稍后重试。', 'err');
      profileSave.disabled = false;      // 失败保留可重试
    } finally {
      profileSave.textContent = label;
    }
  };

  profileSave.addEventListener('click', saveProfile);
  profileForm.addEventListener('submit', (e) => { e.preventDefault(); saveProfile(); });
  // 保存按钮在底部栏（form 之外），表单内又无 submit 按钮，浏览器不会对多输入框表单做隐式提交；
  // 这里补上回车路径（textarea 的回车是换行，排除）
  profileForm.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.target.tagName === 'TEXTAREA') return;
    e.preventDefault();
    saveProfile();
  });
  [profileNickname, profileGender, profileBirthday, profileLocation, profileBio, profileWebsite]
    .forEach((el) => {
      const onChange = () => {
        if (el === profileBio) profileBioCount.textContent = String(profileBio.value.length);
        refreshDirty();
      };
      el.addEventListener('input', onChange);
      el.addEventListener('change', onChange);
    });

  // 头像上传：用 FormData 交给浏览器生成 multipart boundary（**不可**手设 Content-Type）
  profileAvatarBtn.addEventListener('click', () => profileAvatarFile.click());
  profileAvatarFile.addEventListener('change', async () => {
    const file = profileAvatarFile.files && profileAvatarFile.files[0];
    profileAvatarFile.value = '';   // 立刻清空：允许再次选同一文件重试
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    setMsg(profileAvatarMsg, '');
    try {
      applyUser(await authFetch('/api/auth/me/avatar', { method: 'POST', body: form }));
      setMsg(profileAvatarMsg, '头像已更新。');
    } catch (err) {
      setMsg(profileAvatarMsg, err.message || '头像上传失败。', false);
    }
  });

  profileAvatarRemove.addEventListener('click', async () => {
    setMsg(profileAvatarMsg, '');
    try {
      applyUser(await authFetch('/api/auth/me/avatar', { method: 'DELETE' }));
      setMsg(profileAvatarMsg, '头像已移除。');
    } catch (err) {
      setMsg(profileAvatarMsg, err.message || '头像移除失败。', false);
    }
  });

  // 邮箱/手机号/密码三个折叠面板行为同构：展开 → 填当前密码 → 确认/取消。
  // 成功与失败都反馈在**本卡片内**的 msg 行，不跨区提示。
  const bindSecurePanel = (toggle, panel, saveBtn, cancelBtn, msgEl, secretInputs, onSubmit) => {
    const collapse = () => {
      panel.classList.add('hidden');
      secretInputs.forEach((el) => { el.value = ''; });
    };
    toggle.addEventListener('click', () => {
      const willOpen = panel.classList.contains('hidden');
      panel.classList.toggle('hidden', !willOpen);
      setMsg(msgEl, '');
      if (willOpen) setTimeout(() => secretInputs[0].focus(), 30);
    });
    cancelBtn.addEventListener('click', () => { collapse(); setMsg(msgEl, ''); });
    saveBtn.addEventListener('click', async () => {
      const label = saveBtn.textContent;
      saveBtn.disabled = true;
      saveBtn.textContent = '处理中…';
      setMsg(msgEl, '');
      try {
        // onSubmit 返回 false = 本地校验未过：保持展开；否则收起编辑区（msg 留在卡片内）
        if (await onSubmit() !== false) collapse();
      } catch (err) {
        setMsg(msgEl, err.message || '操作失败，请稍后重试。', false);
      } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = label;
      }
    });
  };

  const postJSON = (url, payload) => authFetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  bindSecurePanel(profileEmailToggle, profileEmailEdit, $('#profile-email-save'), $('#profile-email-cancel'),
    profileEmailMsg, [profileEmailValue, profileEmailPassword], async () => {
      const email = profileEmailValue.value.trim();
      if (!email) { setMsg(profileEmailMsg, '请填写新邮箱。', false); return false; }
      applyUser(await postJSON('/api/auth/me/email', { email, password: profileEmailPassword.value }), true);
      setMsg(profileEmailMsg, '邮箱已更新。');
    });

  bindSecurePanel(profilePhoneToggle, profilePhoneEdit, $('#profile-phone-save'), $('#profile-phone-cancel'),
    profilePhoneMsg, [profilePhoneValue, profilePhonePassword], async () => {
      const phone = profilePhoneValue.value.trim();
      if (!phone) { setMsg(profilePhoneMsg, '请填写新手机号。', false); return false; }
      applyUser(await postJSON('/api/auth/me/phone', { phone, password: profilePhonePassword.value }), true);
      setMsg(profilePhoneMsg, '手机号已更新。');
    });

  bindSecurePanel(profilePwdToggle, profilePwdEdit, $('#profile-pwd-save'), $('#profile-pwd-cancel'),
    profilePwdMsg, [profilePwdCurrent, profilePwdNew], async () => {
      await postJSON('/api/auth/me/password', {
        current_password: profilePwdCurrent.value,
        new_password: profilePwdNew.value,
      });
      setMsg(profilePwdMsg, '密码已修改，其他设备的登录已失效。');
    });

  profileTabBasic.addEventListener('click', () => setProfileTab('basic'));
  profileTabSecurity.addEventListener('click', () => setProfileTab('security'));

  // 登出：撤销服务端会话 + 清本地态（网络异常也照样清，避免卡在「假登录」态）
  const doLogout = async () => {
    try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (e) { /* 忽略：本地态照常清除 */ }
    currentUser = null;
    closeProfile();
    renderAuthState();
  };

  navProfile.addEventListener('click', () => { openProfile(); });
  $('#profile-close').addEventListener('click', closeProfile);
  profileModal.addEventListener('click', (e) => { if (e.target === profileModal) closeProfile(); });
  $('#profile-logout').addEventListener('click', doLogout);
  navLogout.addEventListener('click', doLogout);

  // Esc：优先关最上层的个人资料，其次关登录框
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!profileModal.classList.contains('hidden')) closeProfile();
    else if (!authModal.classList.contains('hidden')) closeAuth();
  });

  /* ---------- 初始化 ---------- */
  checkHealth();
})();
