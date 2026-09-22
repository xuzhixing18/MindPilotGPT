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

  // 字幕时间戳：支持超过 1 小时（HH:MM:SS）
  const fmtTs = (sec) => {
    if (!sec && sec !== 0) return '';
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
    const mm = h > 0 ? String(m).padStart(2, '0') : m;
    const ss = String(s).padStart(2, '0');
    return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
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

  /* ---------- 健康检查 / ffmpeg + AI 状态提示 ---------- */
  const checkHealth = async () => {
    try {
      const res = await fetch('/api/health');
      const data = await res.json();
      aiAvailable = !!data.ai;
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
            <div class="mt-3 flex flex-wrap items-center gap-2">
              <button class="ai-btn inline-flex items-center gap-1.5 rounded-xl border border-brand-200 bg-brand-50 px-4 py-2 text-sm font-semibold text-brand-600 transition hover:bg-brand-100 active:scale-95">
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3z"/><path d="M18.5 14.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7.7-1.8z"/></svg>
                AI 总结
              </button>
              <button class="sub-btn inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-4 py-2 text-sm font-semibold text-slate-600 transition hover:border-brand-300 hover:text-brand-600 active:scale-95">
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 15h4M13 15h4M7 11h10"/></svg>
                查看字幕
              </button>
            </div>
          </div>
        </div>
        <div class="dl-status mt-3 hidden text-sm"></div>
        <div class="ai-panel mt-4 hidden"></div>
      </div>`;
  };

  /* ---------- AI 总结 / 字幕：面板与渲染 ---------- */
  const setLoading = (btn, loading) => {
    if (!btn) return;
    if (loading) {
      btn.dataset.html = btn.innerHTML;
      btn.disabled = true;
      btn.classList.add('opacity-60');
      btn.innerHTML = '<span class="inline-flex items-center gap-2"><span class="spinner"></span> 处理中…</span>';
    } else {
      btn.disabled = false;
      btn.classList.remove('opacity-60');
      if (btn.dataset.html) btn.innerHTML = btn.dataset.html;
    }
  };

  const panelLoading = (panel, text) => {
    panel.className = 'ai-panel mt-4 rounded-2xl border border-slate-200 bg-white p-5 text-sm text-slate-500';
    panel.innerHTML = `<span class="inline-flex items-center gap-2"><span class="spinner"></span> ${text}</span>`;
  };

  const panelError = (panel, msg) => {
    panel.className = 'ai-panel mt-4 rounded-2xl border border-rose-200 bg-rose-50 p-4 text-sm leading-relaxed text-rose-600';
    panel.innerHTML = escapeHtml(msg);
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
    panel.className = 'ai-panel mt-4 fade-in rounded-2xl border border-brand-100 bg-gradient-to-b from-brand-50/60 to-white p-5';
    panel.innerHTML = `
      <div class="flex items-center justify-between gap-2">
        <h4 class="inline-flex items-center gap-2 text-base font-bold text-slate-900">
          <svg viewBox="0 0 24 24" class="h-5 w-5 text-brand-500" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3z"/></svg>
          AI 总结
        </h4>
        ${s.model ? `<span class="text-xs text-slate-400">${escapeHtml(s.model)}</span>` : ''}
      </div>
      ${s.one_line ? `<p class="mt-3 text-sm font-semibold text-brand-700">${escapeHtml(s.one_line)}</p>` : ''}
      ${s.summary ? `<p class="mt-2 text-sm leading-relaxed text-slate-600">${escapeHtml(s.summary)}</p>` : ''}
      ${points ? `<div class="mt-4"><div class="text-xs font-semibold text-slate-500">关键要点</div><ul class="mt-2 space-y-1.5 text-sm text-slate-600">${points}</ul></div>` : ''}
      ${chapters ? `<div class="mt-4"><div class="text-xs font-semibold text-slate-500">章节速览</div><div class="mt-2 grid gap-2 sm:grid-cols-2">${chapters}</div></div>` : ''}
      ${keywords ? `<div class="mt-4 flex flex-wrap gap-2">${keywords}</div>` : ''}
      ${s.truncated ? `<p class="mt-3 text-xs text-amber-600">注：字幕较长，总结基于前半部分内容。</p>` : ''}`;
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
    panel.className = 'ai-panel mt-4 fade-in rounded-2xl border border-slate-200 bg-white p-5';
    panel.innerHTML = `
      <div class="flex items-center justify-between gap-2">
        <h4 class="text-base font-bold text-slate-900">字幕全文</h4>
        <span class="text-xs text-slate-400">${escapeHtml(data.language_name || data.language || '')} · ${segs.length} 段 · ${data.char_count || 0} 字</span>
      </div>
      <div class="mt-3 max-h-96 space-y-0.5 overflow-y-auto pr-2">${rows}</div>`;
  };

  const handleSummarize = async (url, panel, btn) => {
    setLoading(btn, true);
    panelLoading(panel, '正在提取字幕并生成 AI 总结，请稍候…（首次约需十几秒）');
    try {
      const res = await fetch('/api/summarize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        if (res.status === 503) {
          panelError(panel, `${data.detail || 'AI 未配置'}。请复制 .env.example 为 .env 并填入 API Key 后重启服务。`);
        } else {
          panelError(panel, data.detail || `总结失败 (HTTP ${res.status})`);
        }
        return;
      }
      renderSummary(panel, data);
    } catch (e) {
      panelError(panel, e.message || '网络错误，总结失败');
    } finally {
      setLoading(btn, false);
    }
  };

  const handleTranscribe = async (url, panel, btn) => {
    setLoading(btn, true);
    panelLoading(panel, '正在提取字幕…');
    try {
      const res = await fetch('/api/transcribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { panelError(panel, data.detail || `转写失败 (HTTP ${res.status})`); return; }
      renderTranscript(panel, data);
    } catch (e) {
      panelError(panel, e.message || '网络错误，转写失败');
    } finally {
      setLoading(btn, false);
    }
  };

  /* ---------- 绑定卡片事件 ---------- */
  const bindCard = (cardEl, url) => {
    const dlBtn = cardEl.querySelector('.dl-btn');
    const select = cardEl.querySelector('.format-select');
    const status = cardEl.querySelector('.dl-status');
    const panel = cardEl.querySelector('.ai-panel');
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

    const aiBtn = cardEl.querySelector('.ai-btn');
    if (aiBtn) {
      if (!aiAvailable) aiBtn.title = '未检测到大模型配置，点击可查看如何启用';
      aiBtn.addEventListener('click', () => handleSummarize(url, panel, aiBtn));
    }
    const subBtn = cardEl.querySelector('.sub-btn');
    if (subBtn) subBtn.addEventListener('click', () => handleTranscribe(url, panel, subBtn));
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
