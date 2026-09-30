/* 分享弹窗：短链 + 载体多选 + 渠道矩阵 + 海报 canvas + 按渠道定制文案。
 *
 * 渠道能力三分类（详见 docs/分享功能设计方案.md §2）：
 *  A. intent 直跳：微博/X/Telegram/WhatsApp 等有官方 Web intent URL；
 *  B. 海报+文案：微信/朋友圈/小红书/抖音**无 Web 分享 intent**，唯一通路是
 *     下载海报（含 QR）+ 复制按渠道定制的文案；
 *  C. 增强：复制链接 / navigator.share（移动端原生分享面板）。
 * 海报在前端 canvas 绘制（服务端无 CJK 字体）：封面走同源代理避免 canvas 跨域
 * 污染，QR 走后端 SVG → data-URI → drawImage（无外部资源不 taint）。
 * 弹窗支持「全选/多选 Tab」：无论勾选几个 Tab，只生成**一条**聚合分享链接
 * （后端 bundle 载体幂等同码：勾选变化内容更新、链接不变）。
 */
import { escapeHtml, copyText, downloadBlob, me, toast, ICON } from './core.js';

const FONT = '-apple-system,"PingFang SC","Microsoft YaHei",sans-serif';
const POSTER_W = 750;
const POSTER_H = 1200;

export const SHARE_KIND_LABELS = {
  video: '视频', summary: 'AI 总结', transcript: '字幕文本',
  mindmap: '思维导图', comments: '高赞评论', qa: 'AI 问答', notes: '随手笔记',
};

/* 渠道矩阵：type = intent（新窗直跳）/ poster（海报+文案）/ copy / system */
const CHANNELS = [
  { key: 'wechat', label: '微信', type: 'poster' },
  { key: 'moments', label: '朋友圈', type: 'poster' },
  { key: 'xhs', label: '小红书', type: 'poster' },
  { key: 'douyin', label: '抖音', type: 'poster' },
  { key: 'weibo', label: '微博', type: 'intent' },
  { key: 'x', label: 'X', type: 'intent' },
  { key: 'telegram', label: 'Telegram', type: 'intent' },
  { key: 'whatsapp', label: 'WhatsApp', type: 'intent' },
  { key: 'copy', label: '复制链接', type: 'copy' },
];

const intentUrl = (key, s) => {
  const u = encodeURIComponent(s.share_url);
  const t = encodeURIComponent(captionFor('intent', s));
  // 封面绝对 URL 以短链为基准拼（而非地址栏）：开发态地址栏是 localhost，拼出来爬虫取不到
  const pic = s.thumb_url ? encodeURIComponent(new URL(s.thumb_url, s.share_url).href) : '';
  switch (key) {
    case 'weibo': return `https://service.weibo.com/share/share.php?url=${u}&title=${t}&pic=${pic}`;
    case 'x': return `https://twitter.com/intent/tweet?url=${u}&text=${t}`;
    case 'telegram': return `https://t.me/share/url?url=${u}&text=${t}`;
    case 'whatsapp': return `https://api.whatsapp.com/send?text=${t}%20${u}`;
    default: return '';
  }
};

const firstLine = (s) => (s.excerpt || '').split('\n')[0].slice(0, 80);

/* 按渠道定制文案：小红书种草体 / 抖音话题体 / 微信海报体 / 通用链接体 */
export const captionFor = (key, s) => {
  const title = s.title || '一条值得看的视频';
  const bullets = (s.payload && s.payload.bullets) || [];
  if (key === 'xhs') {
    const pts = bullets.length
      ? bullets.map((b, i) => `${i + 1}. ${b}`).join('\n')
      : (firstLine(s) || 'AI 帮你划好了重点');
    return `✨ ${title}\n📌 帮你划好重点：\n${pts}\n🔗 完整总结与思维导图见链接\n#AI总结 #学习笔记 #${title.slice(0, 10)}`;
  }
  if (key === 'douyin') return `${title}｜3 分钟看完重点 #AI总结 #知识分享`;
  if (key === 'wechat' || key === 'moments' || key === 'poster') return `${title}｜AI 总结 + 思维导图，扫码直达`;
  return `${title}｜AI 一句话总结：${firstLine(s)} ${s.share_url}`;
};

/* ---------- 海报绘制（750×1200：品牌条/封面/标题/要点/QR） ---------- */
const loadImg = (src) => new Promise((resolve) => {
  const img = new Image();
  img.onload = () => resolve(img);
  img.onerror = () => resolve(null);
  img.src = src;
});

const wrapLines = (g, text, maxWidth, maxLines) => {
  const lines = [];
  let cur = '';
  for (const ch of String(text || '')) {
    if (g.measureText(cur + ch).width > maxWidth && cur) {
      lines.push(cur);
      cur = ch;
      if (lines.length === maxLines) break;
    } else cur += ch;
  }
  if (lines.length < maxLines && cur) lines.push(cur);
  if (lines.length === maxLines && cur && lines[maxLines - 1] !== cur) {
    lines[maxLines - 1] = `${lines[maxLines - 1].slice(0, -1)}…`;
  }
  return lines;
};

const drawPoster = async (canvas, share) => {
  canvas.width = POSTER_W;
  canvas.height = POSTER_H;
  const g = canvas.getContext('2d');
  g.fillStyle = '#FFFFFF';
  g.fillRect(0, 0, POSTER_W, POSTER_H);

  // 品牌条
  g.fillStyle = '#1677FF';
  g.beginPath();
  g.arc(40, 36, 10, 0, Math.PI * 2);
  g.fill();
  g.fillStyle = '#334155';
  g.font = `600 24px ${FONT}`;
  g.textBaseline = 'middle';
  g.fillText('MindPilot · AI 视频分析', 62, 38);

  // 封面区（同源代理，不污染 canvas）；失败回退品牌渐变
  const coverY = 72, coverH = 422;
  const thumb = share.thumb_url ? await loadImg(share.thumb_url) : null;
  if (thumb && thumb.width) {
    const scale = Math.max(POSTER_W / thumb.width, coverH / thumb.height);
    const dw = thumb.width * scale, dh = thumb.height * scale;
    g.drawImage(thumb, (POSTER_W - dw) / 2, coverY + (coverH - dh) / 2, dw, dh);
  } else {
    const grad = g.createLinearGradient(0, coverY, POSTER_W, coverY + coverH);
    grad.addColorStop(0, '#1677FF');
    grad.addColorStop(1, '#69b1ff');
    g.fillStyle = grad;
    g.fillRect(0, coverY, POSTER_W, coverH);
    g.fillStyle = 'rgba(255,255,255,.92)';
    g.font = `700 40px ${FONT}`;
    g.textAlign = 'center';
    g.fillText('MindPilot', POSTER_W / 2, coverY + coverH / 2);
    g.textAlign = 'left';
  }
  // 模型角标
  const model = (share.payload || {}).model_label;
  if (model) {
    g.font = `500 20px ${FONT}`;
    const w = g.measureText(model).width + 24;
    g.fillStyle = 'rgba(15,23,42,.72)';
    g.fillRect(POSTER_W - w - 16, coverY + coverH - 40, w, 32);
    g.fillStyle = '#FFFFFF';
    g.textBaseline = 'middle';
    g.fillText(model, POSTER_W - w - 4, coverY + coverH - 24);
  }

  // 标题（≤2 行）
  g.fillStyle = '#0F172A';
  g.font = `700 36px ${FONT}`;
  g.textBaseline = 'top';
  const titleLines = wrapLines(g, share.title || '一条值得看的视频', POSTER_W - 80, 2);
  let y = coverY + coverH + 32;
  titleLines.forEach((ln) => { g.fillText(ln, 40, y); y += 50; });

  // 要点 / 摘要（≤4 行）
  const bullets = (share.payload || {}).bullets || [];
  const bodyText = bullets.length ? bullets.map((b) => `• ${b}`).join('\n') : (share.excerpt || '');
  g.fillStyle = '#475569';
  g.font = `400 25px ${FONT}`;
  y += 12;
  for (const para of String(bodyText).split('\n').slice(0, 4)) {
    const ln = wrapLines(g, para, POSTER_W - 80, 1)[0];
    if (!ln) continue;
    g.fillText(ln, 40, y);
    y += 38;
    if (y > POSTER_H - 320) break;
  }

  // 底部：QR + 引导文案 + 域名
  const qrSize = 200;
  const qrX = POSTER_W - 40 - qrSize, qrY = POSTER_H - 40 - qrSize;
  try {
    const res = await fetch(`/api/share/qr?data=${encodeURIComponent(share.share_url)}&box=4`);
    if (res.ok) {
      const svg = await res.text();
      const qrImg = await loadImg(`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`);
      if (qrImg) {
        g.fillStyle = '#FFFFFF';
        g.fillRect(qrX - 8, qrY - 8, qrSize + 16, qrSize + 16);
        g.drawImage(qrImg, qrX, qrY, qrSize, qrSize);
      }
    }
  } catch (e) { /* 无 QR 海报仍可用（文案+链接） */ }
  g.fillStyle = '#0F172A';
  g.font = `700 28px ${FONT}`;
  g.fillText('长按或扫码', 40, qrY + 40);
  g.fillText('查看完整 AI 分析', 40, qrY + 80);
  g.fillStyle = '#94A3B8';
  g.font = `400 22px ${FONT}`;
  // 域名文字与短链一致（PUBLIC_BASE_URL 事实源），而非浏览器地址栏的 location.host
  let hostLabel = location.host;
  try { hostLabel = new URL(share.share_url).host || hostLabel; } catch (e) { /* 相对地址等异常回退当前域 */ }
  g.fillText(hostLabel, 40, qrY + 140);
};

/* ---------- 弹窗（多勾选 → 单链接：bundle 聚合载体，链接恒定） ---------- */
let overlayEl = null;

const closeShare = () => {
  if (overlayEl) { overlayEl.remove(); overlayEl = null; }
  document.removeEventListener('keydown', _onKey);
};
const _onKey = (e) => { if (e.key === 'Escape') closeShare(); };

/**
 * 打开分享弹窗。ctx = { contentKey, url, title, activeKind, targets }
 * targets = [{ kind, label, available, snapshot, refId }]：「所见即所享」载体列表
 * （原视频 + 各 Tab）。无论勾选几个，只生成一条 bundle 分享链接（后端幂等
 * 复用同码：勾选变化内容更新、链接不变），落地页按勾选 Tab 分段展示；
 * 海报/渠道/文案都作用于这条链接。activeKind 默认勾选。
 * 兼容旧单载体调用：无 ctx.targets 时由 ctx.kind/snapshot 组装。
 */
export const openShare = async (ctx) => {
  if (overlayEl) closeShare();
  const targets = (Array.isArray(ctx.targets) && ctx.targets.length)
    ? ctx.targets
    : [{
        kind: ctx.kind || 'video',
        label: SHARE_KIND_LABELS[ctx.kind] || '视频',
        available: true, snapshot: ctx.snapshot || {}, refId: ctx.refId || '',
      }];

  overlayEl = document.createElement('div');
  overlayEl.className = 'fixed inset-0 z-[60] grid place-items-center bg-slate-900/45 p-4';
  overlayEl.innerHTML = `
    <div class="flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-3xl bg-white shadow-card">
      <div class="flex items-center justify-between gap-2 border-b border-slate-100 px-5 py-3.5">
        <h3 class="inline-flex min-w-0 items-center gap-2 text-base font-bold text-slate-900">${ICON.share}<span class="truncate">分享 · ${escapeHtml(ctx.title || '这条 AI 解析')}</span></h3>
        <button type="button" class="share-close rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600" title="关闭">
          <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
      </div>
      <div class="share-body flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto p-5 sm:flex-row">
        <div class="flex w-full shrink-0 flex-col gap-2 sm:w-[236px]">
          <canvas class="share-canvas w-full rounded-2xl border border-slate-200 bg-slate-50"></canvas>
          <button type="button" class="share-dl-poster rounded-xl bg-brand-500 px-4 py-2 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600 active:scale-95">下载海报</button>
          <button type="button" class="share-copy-caption rounded-xl border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-600 transition hover:border-brand-300 hover:text-brand-600">复制分享文案</button>
        </div>
        <div class="min-w-0 flex-1">
          <div class="flex items-center justify-between gap-2">
            <div class="text-xs font-semibold text-slate-500">分享内容（可多选）</div>
            <label class="inline-flex cursor-pointer items-center gap-1.5 text-xs font-semibold text-slate-500">
              <input type="checkbox" class="share-all accent-brand-500" />全选
            </label>
          </div>
          <div class="share-scope mt-2 grid grid-cols-3 gap-2 sm:grid-cols-4"></div>
          <div class="mt-4 flex items-center justify-between gap-2">
            <div class="text-xs font-semibold text-slate-500">分享链接</div>
            <button type="button" class="share-copy-all text-xs font-semibold text-brand-600 transition hover:text-brand-700">复制链接</button>
          </div>
          <div class="share-links mt-2 space-y-1.5"></div>
          ${/MicroMessenger/i.test(navigator.userAgent) ? '<div class="mt-2 rounded-xl bg-brand-50 px-3 py-2 text-xs text-brand-600">微信内：点右上角「···」即可分享给好友 / 朋友圈（本页标题与封面已自动适配）。</div>' : ''}
          <div class="mt-4 text-xs font-semibold text-slate-500">或选择分享渠道</div>
          <div class="share-channels mt-2 grid grid-cols-3 gap-2"></div>
          <div class="share-sys mt-3 hidden">
            <button type="button" class="share-sys-btn w-full rounded-xl border border-brand-200 bg-white px-4 py-2 text-sm font-semibold text-brand-600 transition hover:bg-brand-50">调用系统分享（微信等已装 App）</button>
          </div>
          <p class="mt-4 text-xs leading-relaxed text-slate-400">未生成的内容置灰不可选（如思维导图需先生成）；无论勾选几个 Tab 都只生成一条链接，落地页按 Tab 依次展示；微信 / 朋友圈 / 小红书 / 抖音无网页直分享通道：点渠道会自动下载海报并复制对应文案，到 App 内发图粘贴即可。</p>
        </div>
      </div>
    </div>`;
  document.body.appendChild(overlayEl);
  document.addEventListener('keydown', _onKey);
  overlayEl.addEventListener('click', (e) => { if (e.target === overlayEl) closeShare(); });
  overlayEl.querySelector('.share-close').addEventListener('click', closeShare);

  const canvas = overlayEl.querySelector('.share-canvas');
  const scopeEl = overlayEl.querySelector('.share-scope');
  const linksEl = overlayEl.querySelector('.share-links');
  const allEl = overlayEl.querySelector('.share-all');
  const grid = overlayEl.querySelector('.share-channels');
  grid.innerHTML = CHANNELS.map((c) => `
    <button type="button" data-ch="${c.key}" class="rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm font-semibold text-slate-600 transition hover:border-brand-300 hover:text-brand-600">${escapeHtml(c.label)}</button>`).join('');

  /* 单链接态：bundle 为已创建的聚合分享对象（后端幂等同码更新）；selected 为当前勾选集合 */
  let bundle = null;
  let posterCode = '';
  let syncing = false;
  let again = false;
  const selected = new Set();
  const pre = targets.find((t) => t.kind === ctx.activeKind && t.available) || targets.find((t) => t.available);
  if (pre) selected.add(pre.kind);

  const syncAllBox = () => {
    const avail = targets.filter((t) => t.available);
    allEl.checked = avail.length > 0 && avail.every((t) => selected.has(t.kind));
  };
  const renderScope = () => {
    scopeEl.innerHTML = targets.map((t) => `
      <label class="inline-flex items-center gap-1.5 rounded-xl border px-2.5 py-1.5 text-xs font-semibold transition ${t.available ? 'cursor-pointer border-slate-200 text-slate-600 hover:border-brand-300' : 'cursor-not-allowed border-slate-100 text-slate-300'}" title="${t.available ? '' : '该内容尚未生成，无法分享'}">
        <input type="checkbox" class="scope-chk accent-brand-500" data-kind="${t.kind}" ${t.available ? '' : 'disabled'} ${selected.has(t.kind) ? 'checked' : ''}/>${escapeHtml(t.label)}${t.available ? '' : '·未生成'}
      </label>`).join('');
    syncAllBox();
  };
  const renderLinks = () => {
    const names = targets.filter((t) => t.available && selected.has(t.kind)).map((t) => t.label);
    if (!bundle) {
      linksEl.innerHTML = '<div class="rounded-xl border border-dashed border-slate-200 px-3 py-2.5 text-xs text-slate-400">勾选上方内容生成分享链接</div>';
      return;
    }
    linksEl.innerHTML = `
      <div class="flex items-center gap-2">
        <input readonly class="min-w-0 flex-1 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600 outline-none" value="${escapeHtml(bundle.share_url)}"/>
        <button type="button" data-copy-link class="grid h-9 w-9 shrink-0 place-items-center rounded-xl border border-slate-200 text-slate-500 transition hover:border-brand-300 hover:text-brand-600" title="复制链接">${ICON.copy}</button>
      </div>
      <div class="mt-1.5 text-xs text-slate-400">包含 ${names.length} 项：${escapeHtml(names.join('、'))}——无论勾选几个 Tab 都只有这一条链接，落地页按 Tab 分段展示</div>`;
  };

  const ensurePoster = async () => {
    if (!bundle) return;
    // 同码更新：code 不变则海报无需重画（标题/封面/链接恒定）
    if (posterCode !== bundle.code) { await drawPoster(canvas, bundle); posterCode = bundle.code; }
  };
  const downloadPoster = async () => {
    await ensurePoster();
    if (!bundle) { toast('请先勾选要分享的内容'); return; }
    canvas.toBlob((blob) => {
      if (blob) downloadBlob(`mindpilot-share-${bundle.code}.png`, blob);
    }, 'image/png');
  };

  /* 勾选 → 唯一 bundle 链接：后端幂等同码，每次把全部勾选载体的快照整体上报 */
  const doSync = async () => {
    const picked = targets.filter((t) => t.available && selected.has(t.kind));
    if (picked.length) {
      const sections = {};
      for (const t of picked) sections[t.kind] = t.snapshot || {};
      try {
        const data = await me.shareCreate({
          content_key: ctx.contentKey, kind: 'bundle', url: ctx.url,
          snapshot: { sections },
        });
        bundle = data.share;
      } catch (err) {
        toast(err.message || '分享创建失败');
      }
    }
    renderScope();
    renderLinks();
    ensurePoster();
  };
  /* 串行化：请求期间的勾选变化在完成后按最新状态补发一次（trailing） */
  const syncSelection = () => {
    if (syncing) { again = true; return; }
    syncing = true;
    doSync().finally(() => { syncing = false; if (again) { again = false; syncSelection(); } });
  };

  scopeEl.addEventListener('change', (e) => {
    const chk = e.target.closest('.scope-chk');
    if (!chk) return;
    if (chk.checked) selected.add(chk.dataset.kind); else selected.delete(chk.dataset.kind);
    syncAllBox();
    syncSelection();
  });
  allEl.addEventListener('change', () => {
    targets.forEach((t) => {
      if (!t.available) return;
      if (allEl.checked) selected.add(t.kind); else selected.delete(t.kind);
    });
    renderScope();
    syncSelection();
  });
  linksEl.addEventListener('click', async (e) => {
    if (!e.target.closest('[data-copy-link]') || !bundle) return;
    toast((await copyText(bundle.share_url)) ? '链接已复制' : '复制失败，请手动选择');
  });
  overlayEl.querySelector('.share-copy-all').addEventListener('click', async () => {
    if (!bundle) { toast('还没有可复制的链接'); return; }
    toast((await copyText(bundle.share_url)) ? '链接已复制' : '复制失败');
  });

  const act = async (key) => {
    const share = bundle;
    if (!share) { toast('请先勾选要分享的内容'); return; }
    const ch = CHANNELS.find((c) => c.key === key);
    if (!ch) return;
    if (ch.type === 'intent') {
      window.open(intentUrl(key, share), '_blank', 'noopener,width=720,height=560');
      return;
    }
    if (ch.type === 'copy') {
      toast((await copyText(share.share_url)) ? '链接已复制' : '复制失败，请手动选择');
      return;
    }
    // poster 类：下载海报 + 复制渠道文案，一步拿齐两件套
    await downloadPoster();
    const ok = await copyText(captionFor(key, share));
    toast(`海报已下载，${ok ? '文案已复制，' : ''}到${ch.label}内发图粘贴即可`);
  };

  grid.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-ch]');
    if (btn) act(btn.dataset.ch);
  });
  overlayEl.querySelector('.share-dl-poster').addEventListener('click', downloadPoster);
  overlayEl.querySelector('.share-copy-caption').addEventListener('click', async () => {
    const share = bundle;
    if (!share) { toast('请先勾选要分享的内容'); return; }
    toast((await copyText(captionFor('poster', share))) ? '文案已复制' : '复制失败');
  });
  if (navigator.canShare) {
    const box = overlayEl.querySelector('.share-sys');
    box.classList.remove('hidden');
    box.querySelector('.share-sys-btn').addEventListener('click', async () => {
      const share = bundle;
      if (!share) { toast('请先勾选要分享的内容'); return; }
      try {
        await navigator.share({ title: share.title || 'MindPilot', text: captionFor('intent', share), url: share.share_url });
      } catch (e) { /* 用户取消分享不报错 */ }
    });
  }

  // 初始：渲染勾选矩阵，并为当前勾选（含 activeKind 预选）创建/更新唯一聚合链接
  renderScope();
  renderLinks();
  syncSelection();
};
