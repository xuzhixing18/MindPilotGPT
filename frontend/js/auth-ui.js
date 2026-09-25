/* 认证 UI：登录/注册弹窗 + 个人中心弹窗（双 Tab + 常驻底栏）。
 *
 * 由 app.js 迁移，改为读写 core.state 单例，登录态变更通过 core.bus 广播，
 * 供 sidebar（用户卡/登录引导）与 router（登录后重拉当前视图）订阅。
 * 401 的统一处理由本模块注册到 core（setUnauthorizedHandler），core 只触发不反向依赖。
 */
import {
  $, state, authFetch, bus, modelDisplayName,
  setUnauthorizedHandler,
} from './core.js';

/* ===================== 登录 / 注册 ===================== */
let authModal, authForm, authTitle, authSubtitle, authTabLogin, authTabRegister;
let authIdentifierWrap, authIdentifier, authEmailWrap, authEmail, authPhoneWrap, authPhone;
let authNicknameWrap, authNickname, authHint, authPassword, authPasswordLabel;
let authRememberWrap, authRemember, authError, authSubmit;
let navLogin, navRegister, navProfile, navLogout;
let navUser, navUserName, navAvatarImg, navAvatarIcon;
let navModel, navModelName;

const AUTH_TAB_ON = 'auth-tab flex-1 rounded-full bg-white py-1.5 font-semibold text-brand-600 shadow-sm';
const AUTH_TAB_OFF = 'auth-tab flex-1 rounded-full py-1.5 font-semibold text-slate-500 transition hover:text-slate-800';

let authMode = 'login';      // 'login' | 'register'
let pendingAction = null;    // 被登录拦截的动作，登录成功后自动续做

const authMsg = (el, text) => {
  el.textContent = text || '';
  el.classList.toggle('hidden', !text);
};

// 头像渲染：有 avatar_url 则显示图片，否则回退到占位元素（人形图标 / 首字母）
const renderAvatar = (img, fallbackEl, url) => {
  if (url) {
    img.classList.remove('hidden');
    fallbackEl.classList.add('hidden');
    if (img.getAttribute('src') !== url) {   // 仅在地址变化时重设 src，避免闪烁
      img.onerror = () => { img.classList.add('hidden'); fallbackEl.classList.remove('hidden'); };
      img.src = url;
    }
  } else {
    img.classList.add('hidden');
    img.removeAttribute('src');
    fallbackEl.classList.remove('hidden');
  }
};

// 切换登录/注册形态
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

// 导航/侧边栏用户区形态：鉴权关闭时不显示任何登录入口，保持纯工具站体验
export const renderAuthState = () => {
  const logged = !!state.currentUser;
  if (navLogin) navLogin.classList.toggle('hidden', !state.authEnabled || logged);
  if (navRegister) navRegister.classList.toggle('hidden', !state.authEnabled || logged);
  // 已登录：顶栏展示头像 + 昵称 + 退出；未登录整块隐藏
  if (navUser) {
    navUser.classList.toggle('hidden', !logged);
    navUser.classList.toggle('flex', logged);
  }
  if (logged) {
    const u = state.currentUser || {};
    if (navUserName) navUserName.textContent = u.nickname || u.email || u.phone || '';
    if (navAvatarImg && navAvatarIcon) renderAvatar(navAvatarImg, navAvatarIcon, u.avatar_url);
  }
  // 「默认模型」按钮：已登录时展示当前选择（服务商 · 模型；展示名同选择弹窗）
  if (navModel) {
    const u = state.currentUser || {};
    if (navModelName) {
      navModelName.textContent = logged && u.ai_provider
        ? modelDisplayName(u.ai_provider, u.ai_model || null)
        : '跟随平台默认';
    }
  }
  bus.emit('auth:render-nav', { logged });   // 预留：其他模块可据此调整导航
};

export const openAuth = (mode = 'login') => {
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

// 拉取当前登录态：/api/auth/me 返回 401 即未登录（Cookie 为 httpOnly，前端不直读）
export const refreshAuthState = async () => {
  state.currentUser = null;
  if (state.authEnabled) {
    try {
      const res = await fetch('/api/auth/me');
      if (res.ok) state.currentUser = (await res.json()).user || null;
    } catch (e) { /* 网络异常按未登录处理 */ }
  }
  renderAuthState();
  bus.emit('auth:changed', state.currentUser);
};

// 前端门禁：需登录却未登录时弹框并返回 false（后端 401 才是最终裁决）
export const ensureAuth = (onAuthed) => {
  if (!state.authRequired || state.currentUser) return true;
  pendingAction = onAuthed || null;
  openAuth('login');
  return false;
};

// 登录：成功后写入本地态、关框、广播，并续做被拦截的动作
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
  state.currentUser = data.user || null;
  renderAuthState();
  bus.emit('auth:changed', state.currentUser);
  closeAuth();
  authPassword.value = '';
  if (act) setTimeout(act, 0);   // 让弹窗先收起再发起动作，避免抢焦点
  return true;
};

const bindAuthForm = () => {
  authForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const password = authPassword.value;
    authMsg(authError, '');

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
      authSubmit.textContent = authMode === 'register' ? '注册并登录' : '登录';
    }
  });

  navLogin.addEventListener('click', () => openAuth('login'));
  navRegister.addEventListener('click', () => openAuth('register'));
  authTabLogin.addEventListener('click', () => setAuthMode('login'));
  authTabRegister.addEventListener('click', () => setAuthMode('register'));
  $('#auth-close').addEventListener('click', closeAuth);
  authModal.addEventListener('click', (e) => { if (e.target === authModal) closeAuth(); });
};

/* ===================== 个人中心 ===================== */
let profileModal, profileHeadAvatar, profileHeadInitial, profileHeadName, profileHeadSub;
let profileTabBasic, profileTabSecurity, profilePanelBasic, profilePanelSecurity;
let profileFooterBasic, profileFooterSecurity, profileStatus, profileForm, profileSave;
let profileAvatarImg, profileAvatarInitial, profileAvatarBtn, profileAvatarRemove, profileAvatarFile, profileAvatarMsg;
let profileNickname, profileGender, profileBirthday, profileLocation, profileBio, profileBioCount, profileWebsite;
let profileEmailToggle, profileEmailEdit, profileEmailValue, profileEmailPassword, profileEmailMsg;
let profilePhoneToggle, profilePhoneEdit, profilePhoneValue, profilePhonePassword, profilePhoneMsg;
let profilePwdToggle, profilePwdEdit, profilePwdCurrent, profilePwdNew, profilePwdMsg;

const TAB_ON = 'flex-1 rounded-lg bg-white py-1.5 text-sm font-semibold text-brand-600 shadow-sm';
const TAB_OFF = 'flex-1 rounded-lg py-1.5 text-sm font-semibold text-slate-500 transition hover:text-slate-800';
const STATUS_TONE = { idle: 'text-slate-400', dirty: 'text-amber-600', ok: 'text-emerald-600', err: 'text-rose-600' };
const PLAN_LABELS = { free: '免费版', pro: '专业版', team: '团队版' };

let profileSnapshot = {};   // 打开弹窗时的资料快照：保存时只提交变更过的字段

// 行内提示：只改 hidden 与颜色，边距由 HTML 决定
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

const formatDate = (value) => {
  if (!value) return '-';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
};

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

const renderHead = () => {
  const u = state.currentUser || {};
  const name = u.nickname || u.email || u.phone || '个人资料';
  profileHeadName.textContent = name;
  const bits = [];
  if (u.email) bits.push(u.email);
  if (u.phone) bits.push(u.phone);
  profileHeadSub.textContent = bits.join(' · ') || '管理你的资料与账号安全';
  renderAvatar(profileHeadAvatar, profileHeadInitial, u.avatar_url);
  profileHeadInitial.textContent = initialOf(name);
};

const collectForm = () => ({
  nickname: profileNickname.value.trim(),
  gender: profileGender.value,
  birthday: profileBirthday.value,
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

const renderProfileForm = () => {
  const u = state.currentUser || {};
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

const renderAccount = () => {
  const u = state.currentUser || {};
  $('#profile-email').textContent = u.email || '未绑定';
  $('#profile-email-badge').classList.toggle('hidden', !u.email || !!u.email_verified);
  profileEmailToggle.textContent = u.email ? '更换' : '绑定';
  $('#profile-phone').textContent = u.phone || '未绑定';
  $('#profile-phone-badge').classList.toggle('hidden', !u.phone || !!u.phone_verified);
  profilePhoneToggle.textContent = u.phone ? '更换' : '绑定';
  $('#profile-created').textContent = formatDate(u.created_at);
  $('#profile-plan').textContent = PLAN_LABELS[u.plan_id] || u.plan_id || '免费版';
  $('#profile-email-verified').textContent = u.email ? (u.email_verified ? '已验证' : '未验证') : '-';
  $('#profile-phone-verified').textContent = u.phone ? (u.phone_verified ? '已验证' : '未验证') : '-';
  $('#profile-uid').textContent = u.id || '-';
};

// 应用服务端回传的最新资料；keepForm=true 时不回填表单，避免覆盖用户正在编辑的内容
const applyUser = (data, keepForm = false) => {
  if (!data || !data.user) return;
  state.currentUser = data.user;
  renderHead();
  if (!keepForm) renderProfileForm();
  renderAccount();
  renderAuthState();
  bus.emit('auth:changed', state.currentUser);
};

const closeProfile = () => {
  profileModal.classList.add('hidden');
  profileModal.classList.remove('flex');
  setProfileTab('basic');
  [profileEmailEdit, profilePhoneEdit, profilePwdEdit].forEach((p) => p.classList.add('hidden'));
  [profileAvatarMsg, profileEmailMsg, profilePhoneMsg, profilePwdMsg].forEach((el) => setMsg(el, ''));
  setStatus('');
  profileModal.querySelectorAll('input[type="password"]').forEach((el) => { el.value = ''; });
};

// 先用本地态立即渲染（不白屏），再拉一次服务端最新值覆盖
export const openProfile = async () => {
  if (!state.currentUser) return;
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
    if (err.status === 401) { closeProfile(); return; }   // markUnauthorized 已由 authFetch 触发
    setStatus(err.message || '资料加载失败，请稍后重试。', 'err');
  }
};

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
    setStatus('资料已保存。', 'ok');
  } catch (err) {
    setStatus(err.message || '保存失败，请稍后重试。', 'err');
    profileSave.disabled = false;
  } finally {
    profileSave.textContent = label;
  }
};

// 邮箱/手机号/密码三个折叠面板行为同构：展开 → 填当前密码 → 确认/取消
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

const bindProfile = () => {
  profileSave.addEventListener('click', saveProfile);
  profileForm.addEventListener('submit', (e) => { e.preventDefault(); saveProfile(); });
  // 保存按钮在底栏（form 外），表单内又无 submit 按钮，浏览器不做隐式提交；补回车路径（textarea 除外）
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

  // 头像上传：用 FormData 交给浏览器生成 multipart boundary（不可手设 Content-Type）
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
  navProfile.addEventListener('click', () => { openProfile(); });
  $('#profile-close').addEventListener('click', closeProfile);
  profileModal.addEventListener('click', (e) => { if (e.target === profileModal) closeProfile(); });
  $('#profile-logout').addEventListener('click', doLogout);
  navLogout.addEventListener('click', doLogout);
};

// 登出：撤销服务端会话 + 清本地态（网络异常也照样清，避免卡在「假登录」态）
const doLogout = async () => {
  try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (e) { /* 忽略：本地态照常清除 */ }
  state.currentUser = null;
  closeProfile();
  renderAuthState();
  bus.emit('auth:changed', null);
};

/* ===================== 初始化 ===================== */
export const initAuthUI = () => {
  // 登录框
  authModal = $('#auth-modal'); authForm = $('#auth-form');
  authTitle = $('#auth-title'); authSubtitle = $('#auth-subtitle');
  authTabLogin = $('#auth-tab-login'); authTabRegister = $('#auth-tab-register');
  authIdentifierWrap = $('#auth-identifier-wrap'); authIdentifier = $('#auth-identifier');
  authEmailWrap = $('#auth-email-wrap'); authEmail = $('#auth-email');
  authPhoneWrap = $('#auth-phone-wrap'); authPhone = $('#auth-phone');
  authNicknameWrap = $('#auth-nickname-wrap'); authNickname = $('#auth-nickname');
  authHint = $('#auth-hint'); authPassword = $('#auth-password'); authPasswordLabel = $('#auth-password-label');
  authRememberWrap = $('#auth-remember-wrap'); authRemember = $('#auth-remember');
  authError = $('#auth-error'); authSubmit = $('#auth-submit');
  navLogin = $('#nav-login'); navRegister = $('#nav-register');
  navProfile = $('#nav-profile'); navLogout = $('#nav-logout');
  navUser = $('#nav-user'); navUserName = $('#nav-user-name');
  navAvatarImg = $('#nav-avatar-img'); navAvatarIcon = $('#nav-avatar-icon');
  navModel = $('#nav-model'); navModelName = $('#nav-model-name');

  // 个人中心
  profileModal = $('#profile-modal');
  profileHeadAvatar = $('#profile-head-avatar'); profileHeadInitial = $('#profile-head-initial');
  profileHeadName = $('#profile-head-name'); profileHeadSub = $('#profile-head-sub');
  profileTabBasic = $('#profile-tab-basic'); profileTabSecurity = $('#profile-tab-security');
  profilePanelBasic = $('#profile-panel-basic'); profilePanelSecurity = $('#profile-panel-security');
  profileFooterBasic = $('#profile-footer-basic'); profileFooterSecurity = $('#profile-footer-security');
  profileStatus = $('#profile-status'); profileForm = $('#profile-form'); profileSave = $('#profile-save');
  profileAvatarImg = $('#profile-avatar-img'); profileAvatarInitial = $('#profile-avatar-initial');
  profileAvatarBtn = $('#profile-avatar-btn'); profileAvatarRemove = $('#profile-avatar-remove');
  profileAvatarFile = $('#profile-avatar-file'); profileAvatarMsg = $('#profile-avatar-msg');
  profileNickname = $('#profile-nickname'); profileGender = $('#profile-gender');
  profileBirthday = $('#profile-birthday'); profileLocation = $('#profile-location');
  profileBio = $('#profile-bio'); profileBioCount = $('#profile-bio-count'); profileWebsite = $('#profile-website');
  profileEmailToggle = $('#profile-email-toggle'); profileEmailEdit = $('#profile-email-edit');
  profileEmailValue = $('#profile-email-value'); profileEmailPassword = $('#profile-email-password');
  profileEmailMsg = $('#profile-email-msg');
  profilePhoneToggle = $('#profile-phone-toggle'); profilePhoneEdit = $('#profile-phone-edit');
  profilePhoneValue = $('#profile-phone-value'); profilePhonePassword = $('#profile-phone-password');
  profilePhoneMsg = $('#profile-phone-msg');
  profilePwdToggle = $('#profile-pwd-toggle'); profilePwdEdit = $('#profile-pwd-edit');
  profilePwdCurrent = $('#profile-pwd-current'); profilePwdNew = $('#profile-pwd-new');
  profilePwdMsg = $('#profile-pwd-msg');

  bindAuthForm();
  bindProfile();

  // 默认模型设置变更（model-picker 保存后广播）→ 刷新按钮上的当前模型名
  bus.on('ai-settings:changed', () => renderAuthState());

  // 注册 401 统一处理：清态已由 core.markUnauthorized 完成，这里补渲染 + 弹登录框
  setUnauthorizedHandler(() => {
    renderAuthState();
    if (state.authRequired) openAuth('login');
  });

  // Esc：优先关最上层的个人资料，其次关登录框
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!profileModal.classList.contains('hidden')) closeProfile();
    else if (!authModal.classList.contains('hidden')) closeAuth();
  });
};
