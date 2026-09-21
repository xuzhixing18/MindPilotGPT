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

  /* ---------- 健康检查 / ffmpeg 提示 ---------- */
  const checkHealth = async () => {
    try {
      const res = await fetch('/api/health');
      const data = await res.json();
      if (!data.ffmpeg) {
        ffmpegTip.classList.remove('hidden');
        ffmpegTip.classList.add('inline-flex');
        ffmpegTip.innerHTML =
          '<svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>' +
          '<span>未检测到 ffmpeg，仅提供已合成的清晰度（安装后可解锁高清合并）</span>';
      }
    } catch (e) { /* 忽略 */ }
  };

  /* ---------- 解析单个视频 ---------- */
  const parseInfo = async (url) => {
    const res = await fetch(`/api/info?url=${encodeURIComponent(url)}`);
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
    const formatOptions = formats
      .map(
        (f, i) => `<option value="${escapeHtml(f.format_id)}" data-index="${i}">
          ${escapeHtml(f.label)}${f.filesize ? ' · ' + fmtSize(f.filesize) : ''}
        </option>`
      )
      .join('');

    const thumb = info.thumbnail
      ? `<img src="${escapeHtml(info.thumbnail)}" alt="封面" referrerpolicy="no-referrer" class="h-full w-full object-cover" onerror="this.style.display='none'"/>`
      : '';

    return `
      <div class="fade-in rounded-3xl border border-slate-200 bg-white p-4 shadow-card sm:p-6">
        <div class="flex flex-col gap-5 sm:flex-row">
          <div class="relative h-40 w-full shrink-0 overflow-hidden rounded-2xl bg-slate-100 sm:w-64">
            ${thumb}
            ${info.duration ? `<span class="absolute bottom-2 right-2 rounded bg-black/70 px-1.5 py-0.5 text-xs">${fmtDuration(info.duration)}</span>` : ''}
          </div>
          <div class="min-w-0 flex-1">
            <h3 class="truncate text-lg font-semibold text-slate-900" title="${escapeHtml(info.title)}">${escapeHtml(info.title)}</h3>
            <p class="mt-1 text-sm text-slate-500">
              ${info.uploader ? escapeHtml(info.uploader) + ' · ' : ''}${info.extractor ? escapeHtml(info.extractor) : ''}
            </p>
            ${compact ? '' : `<a href="${escapeHtml(info.webpage_url)}" target="_blank" rel="noopener" class="mt-1 inline-block max-w-full truncate text-xs text-brand-500 hover:underline">${escapeHtml(info.webpage_url)}</a>`}
            <div class="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center">
              <select class="format-select w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none focus:border-brand-400 sm:w-56">
                ${formatOptions || '<option value="">无可用清晰度</option>'}
              </select>
              <button class="dl-btn inline-flex items-center justify-center gap-2 rounded-xl bg-brand-500 px-6 py-2.5 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600 active:scale-95">
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 21h16"/></svg>
                下载
              </button>
            </div>
          </div>
        </div>
        <div class="dl-status mt-3 hidden text-sm"></div>
      </div>`;
  };

  /* ---------- 绑定卡片事件 ---------- */
  const bindCard = (cardEl, url) => {
    const dlBtn = cardEl.querySelector('.dl-btn');
    const select = cardEl.querySelector('.format-select');
    const status = cardEl.querySelector('.dl-status');
    dlBtn.addEventListener('click', () => {
      const formatId = select ? select.value : null;
      status.className = 'dl-status mt-3 text-sm text-brand-600';
      status.innerHTML = '<span class="inline-flex items-center gap-2"><span class="spinner"></span> 服务端正在下载并回传，请稍候…（大文件可能需要一些时间）</span>';
      triggerDownload(url, formatId);
      // 浏览器原生下载无法精确捕获完成事件，给出提示后延时收起
      setTimeout(() => {
        status.className = 'dl-status mt-3 text-sm text-emerald-600';
        status.textContent = '已发起下载，请查看浏览器下载列表。若未开始，请重试或更换清晰度。';
      }, 2500);
    });
  };

  /* ---------- 单条解析流程 ---------- */
  const handleParse = async () => {
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
      bindCard(resultSection.firstElementChild, url);
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
          const sel = item.el.querySelector('.format-select');
          triggerDownload(item.url, sel ? sel.value : null);
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

  /* ---------- 初始化 ---------- */
  checkHealth();
})();
