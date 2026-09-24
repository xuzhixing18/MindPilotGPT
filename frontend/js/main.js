/* 应用引导：装配路由表 → 拉健康态 → 初始化认证/侧边栏 → 启动 hash 路由。
 *
 * 采用「注册表」装配：main.js 是唯一同时 import router 与各视图的地方，视图之间
 * 只单向依赖 router.navigate，规避循环依赖。启动顺序有依赖：loadHealth 写入
 * state（authEnabled/authRequired/ffmpeg/aiAvailable）后，refreshAuthState 才能据
 * authEnabled 决定是否拉 /api/auth/me；sidebar 与 router 依赖登录态就绪。
 */
import { loadHealth, $ } from './core.js';
import { initAuthUI, refreshAuthState } from './auth-ui.js';
import { initSidebar } from './sidebar.js';
import { registerRoute, startRouter } from './router.js';

import parseView from './views/parse.js';
import resultView from './views/result.js';
import historyView from './views/history.js';
import { collectionsView, collectionDetailView } from './views/collections.js';

// 路由表：match(path, query) 命中返回 params 对象，否则 null
const registerRoutes = () => {
  registerRoute((path) => (path === '/' ? {} : null), parseView);
  registerRoute((path) => (path === '/v' ? {} : null), resultView);
  registerRoute((path) => (path === '/history' || path === '/search' ? {} : null), historyView);
  registerRoute((path) => (path === '/collections' ? {} : null), collectionsView);
  registerRoute((path) => (path.startsWith('/c/') && path.length > 3
    ? { id: decodeURIComponent(path.slice(3)) }
    : null), collectionDetailView);
};

// 全局付费占位弹窗的关闭（打开在各视图内，关闭统一在此绑定一次）
const bindPayModal = () => {
  const payModal = $('#pay-modal');
  if (!payModal) return;
  const close = () => { payModal.classList.add('hidden'); payModal.classList.remove('flex'); };
  const closeBtn = $('#pay-close');
  if (closeBtn) closeBtn.addEventListener('click', close);
  payModal.addEventListener('click', (e) => { if (e.target === payModal) close(); });
};

const bootstrap = async () => {
  bindPayModal();
  await loadHealth();          // 写入 state（含 authEnabled，决定后续是否拉登录态）
  initAuthUI();                // 缓存弹窗 DOM 引用 + 绑定表单 + 注册 401 处理
  await refreshAuthState();    // 拉 /api/auth/me（仅 authEnabled 时），广播 auth:changed
  initSidebar();               // 订阅 bus，渲染历史/合集/登录引导
  registerRoutes();
  startRouter('#view-root');   // 首屏渲染（支持深链）
};

bootstrap();
