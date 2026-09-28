/* 嵌入播放器适配层：为字幕 Tab 提供统一的 seek / 进度回读能力。
 *
 * - B站：官方外链播放器 player.bilibili.com/player.html，官方支持 ``t`` 参数（秒）
 *   指定初始播放时间点，故 seek = 带 t 参数重载 iframe 并 autoplay；但**无进度回读
 *   API**，canFollow=false（字幕滚动联动不可用，点击时间戳跳转正常）；
 * - YouTube：IFrame API（enablejsapi=1），seekTo / getCurrentTime 全支持，canFollow=true；
 * - 其他平台（抖音等）：detectPlayer 返回 null，调用方降级为「仅字幕内定位」。
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
  return {
    platform: 'bilibili',
    canFollow: false,   // 外链播放器无进度回读 API：可跳转、不可联动
    seek: (sec) => { iframe.src = biliSrc(target, sec, true); },
    getTime: () => null,
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
