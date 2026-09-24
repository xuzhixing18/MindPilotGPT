/* 解析首页视图（路由 #/）：hero + 链接输入（单条/批量）+ 营销段落。
 *
 * 由 index.html 的落地页内容迁入，改为动态渲染进 #view-root。解析成功后导航到
 * 结果视图 #/v?url=...（批量则多个 url 参数），本页不再内联渲染结果卡。
 */
import { $, state } from '../core.js';
import { navigate } from '../router.js';
import { ensureAuth } from '../auth-ui.js';

let batchMode = false;
let root = null;

const template = () => `
  <!-- 页内快捷导航（滚动到各段） -->
  <div class="sticky top-0 z-20 border-b border-slate-200/70 bg-white/80 backdrop-blur">
    <div class="mx-auto flex h-12 max-w-6xl items-center justify-end gap-6 px-4 text-sm text-slate-600 sm:px-6">
      <button type="button" class="pv-scroll transition hover:text-brand-500" data-target="pv-features">功能</button>
      <button type="button" class="pv-scroll transition hover:text-brand-500" data-target="pv-how">如何使用</button>
      <button type="button" class="pv-scroll transition hover:text-brand-500" data-target="pv-pricing">定价</button>
    </div>
  </div>

  <!-- Hero -->
  <section class="relative mx-auto max-w-4xl px-4 pt-12 pb-10 text-center sm:px-6 sm:pt-16">
    <div class="mx-auto mb-6 inline-flex items-center gap-2 rounded-full border border-brand-100 bg-brand-50 px-4 py-1.5 text-xs font-medium text-brand-600">
      <span class="relative flex h-2 w-2">
        <span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand-400 opacity-75"></span>
        <span class="relative inline-flex h-2 w-2 rounded-full bg-brand-500"></span>
      </span>
      已服务 100,000+ 创作者 · 支持 20+ 主流平台
    </div>
    <h1 class="text-4xl font-extrabold leading-tight tracking-tight text-slate-900 sm:text-6xl">
      万能视频下载，一键保存<br />任意平台视频
    </h1>
    <p class="mx-auto mt-5 max-w-2xl text-base text-slate-500 sm:text-lg">
      粘贴链接，秒速解析。支持高清清晰度选择、批量下载、仅音频提取。
      手机、电脑、平板随时随地都能用 —— 再也不用受制于平台「不支持下载」。
    </p>

    <div class="mx-auto mt-9 max-w-2xl">
      <div class="rounded-full border border-slate-200 bg-white p-1.5 shadow-card">
        <div id="single-input" class="flex flex-col gap-2 sm:flex-row sm:items-center">
          <input id="url" type="url" inputmode="url" autocomplete="off"
            placeholder="粘贴视频链接，如 https://www.bilibili.com/video/..."
            class="w-full flex-1 rounded-full bg-transparent px-5 py-3 text-sm text-slate-900 placeholder-slate-400 outline-none sm:text-base" />
          <button id="parse-btn"
            class="shrink-0 rounded-full bg-brand-500 px-7 py-3 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600 active:scale-95 sm:text-base">
            解析视频
          </button>
        </div>
        <div id="batch-input" class="hidden flex-col gap-2 px-2 py-1">
          <textarea id="batch-urls" rows="4"
            placeholder="每行粘贴一个视频链接，支持批量解析下载…"
            class="w-full rounded-2xl bg-transparent px-3 py-2 text-sm text-slate-900 placeholder-slate-400 outline-none"></textarea>
          <button id="batch-btn"
            class="rounded-full bg-brand-500 px-7 py-3 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600 active:scale-95">
            批量解析
          </button>
        </div>
      </div>
      <div class="mt-3 flex items-center justify-center gap-4 text-xs text-slate-500">
        <button id="mode-toggle" class="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1.5 transition hover:border-brand-300 hover:text-brand-600">
          <svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
          <span id="mode-label">切换到批量模式</span>
        </button>
        <span id="ffmpeg-tip" class="hidden items-center gap-1 text-amber-600"></span>
      </div>
    </div>

    <div class="mt-8 flex flex-wrap items-center justify-center gap-x-5 gap-y-2 text-xs font-medium text-slate-400">
      <span>哔哩哔哩</span><span>·</span><span>YouTube</span><span>·</span><span>抖音</span>
      <span>·</span><span>小红书</span><span>·</span><span>TikTok</span><span>·</span>
      <span>Twitter/X</span><span>·</span><span>播客</span><span>·</span><span>及 1000+ 站点</span>
    </div>
  </section>

  <!-- 功能卡片 -->
  <section id="pv-features" class="mx-auto max-w-6xl px-4 py-16 sm:px-6">
    <h2 class="text-center text-3xl font-bold tracking-tight text-slate-900 sm:text-4xl">为什么选择 MindPilot</h2>
    <p class="mx-auto mt-3 max-w-xl text-center text-slate-500">把「下载难」这件事彻底解决，专注实用价值。</p>
    <div class="mt-12 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
      <div class="group rounded-2xl border border-slate-200/80 bg-white p-6 shadow-sm transition hover:-translate-y-1 hover:border-brand-300 hover:shadow-card">
        <div class="mb-4 grid h-12 w-12 place-items-center rounded-xl bg-brand-50 text-brand-500"><svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z"/></svg></div>
        <h3 class="text-lg font-semibold text-slate-900">极速解析下载</h3>
        <p class="mt-2 text-sm text-slate-500">粘贴链接秒出结果，服务端高速下载后直接回传，省去客户端安装与配置。</p>
      </div>
      <div class="group rounded-2xl border border-slate-200/80 bg-white p-6 shadow-sm transition hover:-translate-y-1 hover:border-brand-300 hover:shadow-card">
        <div class="mb-4 grid h-12 w-12 place-items-center rounded-xl bg-cyan-50 text-cyan-600"><svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16M4 12h16M4 17h10"/></svg></div>
        <h3 class="text-lg font-semibold text-slate-900">批量下载</h3>
        <p class="mt-2 text-sm text-slate-500">一次粘贴多个链接，逐条解析与下载，效率翻倍，适合素材采集。</p>
      </div>
      <div class="group rounded-2xl border border-slate-200/80 bg-white p-6 shadow-sm transition hover:-translate-y-1 hover:border-brand-300 hover:shadow-card">
        <div class="mb-4 grid h-12 w-12 place-items-center rounded-xl bg-indigo-50 text-indigo-600"><svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v18M5 8l7-5 7 5"/></svg></div>
        <h3 class="text-lg font-semibold text-slate-900">高清清晰度</h3>
        <p class="mt-2 text-sm text-slate-500">自由选择 1080P / 720P 等清晰度，或仅提取音频，满足不同场景。</p>
      </div>
      <div class="group rounded-2xl border border-slate-200/80 bg-white p-6 shadow-sm transition hover:-translate-y-1 hover:border-brand-300 hover:shadow-card">
        <div class="mb-4 grid h-12 w-12 place-items-center rounded-xl bg-emerald-50 text-emerald-600"><svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="2" width="12" height="20" rx="2"/><path d="M11 18h2"/></svg></div>
        <h3 class="text-lg font-semibold text-slate-900">手机随时用</h3>
        <p class="mt-2 text-sm text-slate-500">移动端完全适配，无需 App，浏览器打开即可下载到本地相册/文件。</p>
      </div>
      <div class="group rounded-2xl border border-slate-200/80 bg-white p-6 shadow-sm transition hover:-translate-y-1 hover:border-brand-300 hover:shadow-card">
        <div class="mb-4 grid h-12 w-12 place-items-center rounded-xl bg-sky-50 text-sky-600"><svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l8 4v6c0 5-3.5 8-8 10-4.5-2-8-5-8-10V6l8-4z"/></svg></div>
        <h3 class="text-lg font-semibold text-slate-900">开源内核 · 稳定</h3>
        <p class="mt-2 text-sm text-slate-500">底层基于 10 万+ Star 的 yt-dlp 开源项目，持续跟进各平台变更。</p>
      </div>
      <div class="group rounded-2xl border border-slate-200/80 bg-white p-6 shadow-sm transition hover:-translate-y-1 hover:border-brand-300 hover:shadow-card">
        <div class="mb-4 grid h-12 w-12 place-items-center rounded-xl bg-brand-50 text-brand-500"><svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3z"/><path d="M18.5 14.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7.7-1.8z"/></svg></div>
        <h3 class="text-lg font-semibold text-slate-900">AI 总结 & 字幕</h3>
        <p class="mt-2 text-sm text-slate-500">一键提取视频字幕，并用大模型生成摘要、要点与章节速览，长视频也能快速抓重点。</p>
      </div>
    </div>
  </section>

  <!-- 如何使用 -->
  <section id="pv-how" class="mx-auto max-w-5xl px-4 py-16 sm:px-6">
    <h2 class="text-center text-3xl font-bold tracking-tight text-slate-900 sm:text-4xl">三步搞定</h2>
    <div class="mt-12 grid gap-8 md:grid-cols-3">
      <div class="text-center">
        <div class="mx-auto grid h-14 w-14 place-items-center rounded-2xl bg-brand-500 text-xl font-bold text-white shadow-glow">1</div>
        <h3 class="mt-4 font-semibold text-slate-900">粘贴链接</h3>
        <p class="mt-2 text-sm text-slate-500">复制任意平台的视频地址，粘贴到输入框。</p>
      </div>
      <div class="text-center">
        <div class="mx-auto grid h-14 w-14 place-items-center rounded-2xl bg-brand-500 text-xl font-bold text-white shadow-glow">2</div>
        <h3 class="mt-4 font-semibold text-slate-900">选择清晰度</h3>
        <p class="mt-2 text-sm text-slate-500">解析后挑选想要的画质，或仅提取音频。</p>
      </div>
      <div class="text-center">
        <div class="mx-auto grid h-14 w-14 place-items-center rounded-2xl bg-brand-500 text-xl font-bold text-white shadow-glow">3</div>
        <h3 class="mt-4 font-semibold text-slate-900">立即下载</h3>
        <p class="mt-2 text-sm text-slate-500">点击即可保存到本地，手机电脑都支持。</p>
      </div>
    </div>
  </section>

  <!-- 定价（付费 UI 占位） -->
  <section id="pv-pricing" class="mx-auto max-w-6xl px-4 py-16 sm:px-6">
    <h2 class="text-center text-3xl font-bold tracking-tight text-slate-900 sm:text-4xl">选择适合你的方案</h2>
    <p class="mx-auto mt-3 max-w-xl text-center text-slate-500">从免费开始，随需升级解锁更强能力。</p>
    <div class="mt-12 grid gap-6 lg:grid-cols-3">
      <div class="rounded-3xl border border-slate-200/80 bg-white p-8 shadow-sm">
        <h3 class="text-lg font-semibold text-slate-900">Free</h3>
        <p class="mt-1 text-sm text-slate-500">个人轻度使用</p>
        <div class="mt-6 flex items-end gap-1"><span class="text-4xl font-extrabold text-slate-900">¥0</span><span class="text-slate-400">/月</span></div>
        <ul class="mt-6 space-y-3 text-sm text-slate-600">
          <li class="flex gap-2"><span class="text-brand-500">✓</span> 每日 5 次下载</li>
          <li class="flex gap-2"><span class="text-brand-500">✓</span> 最高 720P 清晰度</li>
          <li class="flex gap-2"><span class="text-brand-500">✓</span> 单条下载</li>
        </ul>
        <button class="mt-8 w-full rounded-2xl border border-slate-200 py-3 text-sm font-semibold text-slate-700 transition hover:border-brand-400 hover:text-brand-600">当前方案</button>
      </div>
      <div class="relative rounded-3xl border border-brand-300 bg-gradient-to-b from-brand-50 to-white p-8 shadow-glow">
        <span class="absolute -top-3 left-1/2 -translate-x-1/2 rounded-full bg-brand-500 px-4 py-1 text-xs font-bold text-white">最受欢迎</span>
        <h3 class="text-lg font-semibold text-slate-900">Pro</h3>
        <p class="mt-1 text-sm text-slate-500">重度创作者</p>
        <div class="mt-6 flex items-end gap-1"><span class="text-4xl font-extrabold text-slate-900">¥29</span><span class="text-slate-400">/月</span></div>
        <ul class="mt-6 space-y-3 text-sm text-slate-600">
          <li class="flex gap-2"><span class="text-brand-500">✓</span> 无限次下载</li>
          <li class="flex gap-2"><span class="text-brand-500">✓</span> 最高 4K 清晰度</li>
          <li class="flex gap-2"><span class="text-brand-500">✓</span> 批量下载</li>
          <li class="flex gap-2"><span class="text-brand-500">✓</span> 高速专用通道</li>
        </ul>
        <button data-pay="pro" class="pay-btn mt-8 w-full rounded-2xl bg-brand-500 py-3 text-sm font-bold text-white shadow-glow transition hover:bg-brand-600">升级 Pro</button>
      </div>
      <div class="rounded-3xl border border-slate-200/80 bg-white p-8 shadow-sm">
        <h3 class="text-lg font-semibold text-slate-900">Team</h3>
        <p class="mt-1 text-sm text-slate-500">团队协作 / API</p>
        <div class="mt-6 flex items-end gap-1"><span class="text-4xl font-extrabold text-slate-900">¥99</span><span class="text-slate-400">/月</span></div>
        <ul class="mt-6 space-y-3 text-sm text-slate-600">
          <li class="flex gap-2"><span class="text-brand-500">✓</span> 包含 Pro 全部能力</li>
          <li class="flex gap-2"><span class="text-brand-500">✓</span> API 接入</li>
          <li class="flex gap-2"><span class="text-brand-500">✓</span> 多成员席位</li>
        </ul>
        <button data-pay="team" class="pay-btn mt-8 w-full rounded-2xl border border-slate-200 py-3 text-sm font-semibold text-slate-700 transition hover:border-brand-400 hover:text-brand-600">联系我们</button>
      </div>
    </div>
  </section>

  <!-- 页脚 -->
  <footer class="border-t border-slate-200/70 py-10 text-center text-sm text-slate-400">
    <p>© 2026 MindPilot · 请仅下载你拥有权利或已获授权的内容，遵守各平台条款与当地法律。</p>
    <p class="mt-2 text-xs">Powered by <a href="https://github.com/yt-dlp/yt-dlp" target="_blank" rel="noopener" class="text-brand-500 hover:underline">yt-dlp</a></p>
  </footer>`;

/* ---------- 解析动作 ---------- */
const submitSingle = () => {
  if (!ensureAuth(submitSingle)) return;   // 未登录：弹框拦截，登录后自动续做
  const url = $('#url', root).value.trim();
  if (!url) { $('#url', root).focus(); return; }
  navigate(`/v?url=${encodeURIComponent(url)}`);
};

const submitBatch = () => {
  if (!ensureAuth(submitBatch)) return;
  const urls = $('#batch-urls', root).value.split('\n').map((s) => s.trim()).filter(Boolean);
  if (!urls.length) { $('#batch-urls', root).focus(); return; }
  const qs = urls.map((u) => `url=${encodeURIComponent(u)}`).join('&');
  navigate(`/v?${qs}`);
};

/* ---------- 付费占位弹窗（全局 modal 在壳里） ---------- */
const openPayModal = (plan) => {
  const payModal = $('#pay-modal');
  const payText = $('#pay-modal-text');
  if (!payModal || !payText) return;
  payText.textContent = `你正在尝试升级到 ${plan} 方案。当前为演示版本，支付通道正在接入中，敬请期待。`;
  payModal.classList.remove('hidden');
  payModal.classList.add('flex');
};

/* ---------- 视图对象 ---------- */
export default {
  title: '万能视频下载',
  mount(container) {
    root = container;
    batchMode = false;
    container.innerHTML = template();

    // ffmpeg 缺失提示（读 bootstrap 阶段写入的 state）
    if (!state.ffmpeg) {
      const tip = $('#ffmpeg-tip', container);
      tip.classList.remove('hidden');
      tip.classList.add('inline-flex');
      tip.innerHTML =
        '<svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>' +
        '<span>未检测到 ffmpeg，仅提供已合成的清晰度（安装后可解锁高清合并）</span>';
    }

    // 单条 / 批量
    $('#parse-btn', container).addEventListener('click', submitSingle);
    $('#url', container).addEventListener('keydown', (e) => { if (e.key === 'Enter') submitSingle(); });
    $('#batch-btn', container).addEventListener('click', submitBatch);

    // 模式切换
    $('#mode-toggle', container).addEventListener('click', () => {
      batchMode = !batchMode;
      const singleBox = $('#single-input', container);
      const batchBox = $('#batch-input', container);
      singleBox.classList.toggle('hidden', batchMode);
      singleBox.classList.toggle('flex', !batchMode);
      batchBox.classList.toggle('hidden', !batchMode);
      batchBox.classList.toggle('flex', batchMode);
      $('#mode-label', container).textContent = batchMode ? '切换到单条模式' : '切换到批量模式';
    });

    // 页内快捷导航：滚动到对应段（在可滚动的 #view-root 内）
    container.querySelectorAll('.pv-scroll').forEach((btn) =>
      btn.addEventListener('click', () => {
        const el = document.getElementById(btn.dataset.target);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }));

    // 付费占位
    container.querySelectorAll('.pay-btn').forEach((btn) =>
      btn.addEventListener('click', () => openPayModal(btn.dataset.pay === 'team' ? 'Team' : 'Pro')));

    // 自动聚焦输入框（桌面端）
    if (window.matchMedia('(min-width: 640px)').matches) {
      const u = $('#url', container);
      if (u) setTimeout(() => u.focus(), 60);
    }
  },
  unmount() { root = null; },
};
