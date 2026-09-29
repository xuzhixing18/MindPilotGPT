/* 播放器适配层：为左栏封面槽提供统一的 seek / 进度回读能力。
 *
 * - B站：官方外链播放器 player.bilibili.com/player.html，官方支持 ``t`` 参数（秒）
 *   指定初始播放时间点，故 seek = 带 t 参数重载 iframe 并 autoplay；但**无进度回读
 *   API**，canFollow=false（字幕滚动联动不可用，点击时间戳跳转正常）；另提供
 *   getEstTime()：用「最近一次 seek 起点 + 墙钟流逝」估算当前播放位，供笔记插入
 *   时间戳在 getTime 不可用时取更贴近画面的值；
 * - YouTube：IFrame API（enablejsapi=1），seekTo / getCurrentTime 全支持，canFollow=true；
 * - 其他平台（抖音等，detectPlayer 返回 null）：无官方嵌入播放器且被 CSP 禁止 iframe
 *   嵌入，改用 mountNative：后端 /api/stream 流式代理 + HTML5 <video> 原生播放，
 *   seek / 进度回读全支持，canFollow=true（体验反而优于外链播放器）。
 *
 * 控制器接口：{ platform, canFollow, seek(sec), getTime(), destroy() }
 * getTime() 在不支持回读时返回 null。
 *
 * createPlayer(box, target, opts)：opts.sec = 初始播放时间点（秒），opts.autoplay =
 * 挂载后即播放（封面/时间戳点击属用户显式手势，允许自动播放）。
 */

const BV_RE = /(BV[0-9A-Za-z]{10})/;
const AV_RE = /av(\d+)/i;
const YT_RE = /(?:v=|youtu\.be\/|embed\/)([A-Za-z0-9_-]{11})/;

/* 平台探测：返回 {platform, bvid|aid|vid} 或 null。ID 从原始 url 提取（BV 号区分大小写）。 */
export const detectPlayer = (url) => {
  const u = (url || '').toLowerCase();
  if (u.includes('bilibili.com') || u.includes('b23.tv')) {
    const bv = (url || '').match(BV_RE);
    if (bv) return { platform: 'bilibili', bvid: bv[1] };
    const av = (url || '').match(AV_RE);
    if (av) return { platform: 'bilibili', aid: av[1] };
    return null;
  }
  if (u.includes('youtube.com') || u.includes('youtu.be')) {
    const m = (url || '').match(YT_RE);
    return m ? { platform: 'youtube', vid: m[1] } : null;
  }
  return null;
};

/* YouTube IFrame API 全局只加载一次；window.YT.Player 可用时 resolve */
let ytApiPromise = null;
const ytApi = () => {
  if (ytApiPromise) return ytApiPromise;
  ytApiPromise = new Promise((resolve) => {
    if (window.YT && window.YT.Player) { resolve(); return; }
    const prev = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => { if (prev) prev(); resolve(); };
    if (!document.getElementById('yt-iframe-api')) {
      const s = document.createElement('script');
      s.id = 'yt-iframe-api';
      s.src = 'https://www.youtube.com/iframe_api';
      document.head.appendChild(s);
    }
  });
  return ytApiPromise;
};

const biliSrc = (target, sec, autoplay) => {
  const params = new URLSearchParams({
    danmaku: '0',        // 默认关弹幕，保证字幕阅读体验
    high_quality: '1',
    autoplay: autoplay ? '1' : '0',
  });
  if (target.bvid) params.set('bvid', target.bvid);
  else params.set('aid', target.aid);
  if (sec) params.set('t', String(Math.max(0, Math.floor(sec))));
  return `https://player.bilibili.com/player.html?${params.toString()}`;
};

const mountBilibili = (box, target, opts = {}) => {
  const iframe = document.createElement('iframe');
  iframe.className = 'h-full w-full';
  iframe.allowFullscreen = true;
  iframe.src = biliSrc(target, opts.sec || 0, !!opts.autoplay);
  box.appendChild(iframe);
  // 无进度回读：用「最近一次 seek 起点 + 墙钟流逝」估算当前播放位（getEstTime）。
  // 锚点只在显式起播动作（挂载即 autoplay / seek 重载 iframe）时建立；用户在
  // iframe 内暂停/拖进度条感知不到，估算会偏离，仅作 getTime 不可用时的回退。
  let anchor = (opts.autoplay || opts.sec) ? { sec: opts.sec || 0, at: Date.now() } : null;
  return {
    platform: 'bilibili',
    canFollow: false,   // 外链播放器无进度回读 API：可跳转、不可联动
    seek: (sec) => { anchor = { sec: Math.max(0, sec), at: Date.now() }; iframe.src = biliSrc(target, sec, true); },
    getTime: () => null,
    getEstTime: () => (anchor ? anchor.sec + (Date.now() - anchor.at) / 1000 : null),
    destroy: () => { iframe.remove(); },
  };
};

const mountYoutube = async (box, target, opts = {}) => {
  await ytApi();
  const host = document.createElement('div');
  box.appendChild(host);
  const player = new window.YT.Player(host, {
    videoId: target.vid,
    width: '100%',        // 替换 host 后的 iframe 默认 640×360，需显式自适应容器
    height: '100%',
    playerVars: {
      enablejsapi: 1, origin: location.origin, rel: 0, playsinline: 1,
      autoplay: opts.autoplay ? 1 : 0,
      ...(opts.sec ? { start: Math.max(0, Math.floor(opts.sec)) } : {}),
    },
  });
  return {
    platform: 'youtube',
    canFollow: true,
    seek: (sec) => {
      try { player.seekTo(Math.max(0, Math.floor(sec)), true); } catch (e) { /* 播放器未就绪 */ }
    },
    getTime: () => {
      try { return player.getCurrentTime ? player.getCurrentTime() : null; } catch (e) { return null; }
    },
    destroy: () => {
      try { player.destroy(); } catch (e) { /* 已销毁 */ }
    },
  };
};

/* 在容器 box 内按探测结果创建控制器；平台不支持或挂载失败返回 null */
export const createPlayer = async (box, target, opts = {}) => {
  if (!target || !box) return null;
  try {
    if (target.platform === 'bilibili') return mountBilibili(box, target, opts);
    if (target.platform === 'youtube') return await mountYoutube(box, target, opts);
  } catch (e) {
    return null;   // 挂载失败（如 YT API 被墙）：降级为无播放器
  }
  return null;
};

/* HTML5 原生播放器（抖音等无外链播放器平台）：src 指向后端 /api/stream 流式代理。
 * 同步挂载立即返回控制器（el = video 元素，供调用方监听 loadeddata/error 控制遮罩）；
 * 首次播放需等服务端拉取完整视频，期间 video 展示 poster 封面。
 * opts: { src, poster, sec, autoplay } */
export const mountNative = (box, opts = {}) => {
  if (!box || !opts.src) return null;
  const v = document.createElement('video');
  v.className = 'h-full w-full bg-black object-contain';
  v.controls = true;
  v.playsInline = true;
  v.preload = 'auto';
  if (opts.poster) v.poster = opts.poster;
  v.src = opts.src;
  box.appendChild(v);
  const safeSeek = (sec) => {
    try { v.currentTime = Math.max(0, sec); } catch (e) { /* 元数据未就绪 */ }
  };
  if (opts.sec) v.addEventListener('loadedmetadata', () => safeSeek(opts.sec), { once: true });
  if (opts.autoplay) {
    const play = () => { const p = v.play(); if (p && p.catch) p.catch(() => {}); };
    v.addEventListener('canplay', play, { once: true });
    play();   // 点击手势链路内直接尝试，不等缓冲
  }
  return {
    platform: 'native',
    canFollow: true,   // 原生元素完整回读 currentTime：滚动联动可用
    el: v,
    seek: (sec) => { safeSeek(sec); const p = v.play(); if (p && p.catch) p.catch(() => {}); },
    getTime: () => (v.readyState >= 1 && Number.isFinite(v.currentTime) ? v.currentTime : null),
    destroy: () => {
      try { v.pause(); v.removeAttribute('src'); v.load(); } catch (e) { /* 已销毁 */ }
      v.remove();
    },
  };
};
