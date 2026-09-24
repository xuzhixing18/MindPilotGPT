/* hash 路由：视图注册表 + 挂载/卸载 + 深链 + 登录后重拉当前视图。
 *
 * 采用「注册表」而非直接 import 各视图：main.js 负责装配路由，视图只单向依赖
 * router 的 navigate/currentRoute，规避 router ↔ views 循环依赖。
 * 视图接口：{ title?, mount(container, ctx), unmount?() }，ctx = { params, query, path }。
 */
import { bus, state } from './core.js';

const routes = [];          // [{ match(path, query) -> params|null, view }]
let rootEl = null;          // #view-root
let baseRootClass = '';     // #view-root 的初始类：每次渲染前复位，避免视图布局类跨路由残留
let current = null;         // { path, query, params, view }
let wasLogged = false;      // 识别「未登录→已登录」跃迁，触发重拉当前视图

// 注册一条路由：match 返回 params 对象（命中）或 null（不匹配）
export const registerRoute = (match, view) => { routes.push({ match, view }); };

export const navigate = (to) => {
  const hash = to.startsWith('#') ? to : `#${to}`;
  if (location.hash === hash) render();   // 目标与当前相同：手动重渲染
  else location.hash = hash;              // 否则交给 hashchange
};

export const currentRoute = () => current;

// 解析 location.hash → { path, query }
const parseHash = () => {
  const raw = location.hash.replace(/^#/, '') || '/';
  const qIndex = raw.indexOf('?');
  const path = (qIndex >= 0 ? raw.slice(0, qIndex) : raw) || '/';
  const query = new URLSearchParams(qIndex >= 0 ? raw.slice(qIndex + 1) : '');
  return { path, query };
};

const resolve = (path, query) => {
  for (const r of routes) {
    const params = r.match(path, query);
    if (params) return { view: r.view, params };
  }
  return null;
};

const render = () => {
  const { path, query } = parseHash();
  const hit = resolve(path, query);
  if (!hit) { location.replace('#/'); return; }   // 未匹配 → 回首页（replace 不留历史）

  // 卸载上一个视图（清理定时器/事件）
  if (current && current.view.unmount) { try { current.view.unmount(); } catch (e) { /* 忽略 */ } }

  current = { path, query, params: hit.params, view: hit.view };
  rootEl.innerHTML = '';
  rootEl.className = baseRootClass;   // 复位容器类：上一个视图加的布局类（如 max-w）不得带到本视图
  if (hit.view.title) document.title = `${hit.view.title} · MindPilot`;
  try {
    hit.view.mount(rootEl, { params: hit.params, query, path });
  } catch (e) {
    rootEl.innerHTML = '<div class="p-8 text-center text-sm text-rose-600">视图加载失败，请刷新重试。</div>';
  }
  bus.emit('route:changed', current);
};

export const startRouter = (rootSelector = '#view-root') => {
  rootEl = document.querySelector(rootSelector);
  window.addEventListener('hashchange', render);

  // 登录跃迁（未登录→已登录）时重拉当前视图：401 降级后登录即可恢复受门禁的内容
  wasLogged = !!state.currentUser;
  bus.on('auth:changed', (user) => {
    const logged = !!user;
    if (logged && !wasLogged) render();            // 刚登录：重渲染当前路由
    else if (!logged && wasLogged) navigate('/');  // 登出/会话失效：回首页，清掉上一账号的深链视图
    wasLogged = logged;
  });

  baseRootClass = rootEl.className;
  render();   // 首屏（支持深链直接进入任意视图）
};
