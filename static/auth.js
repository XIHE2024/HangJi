'use strict';

const $ = id => document.getElementById(id);
let csrf = '';
let registrationEnabled = true;
let mode = location.pathname === '/register' ? 'register' : 'login';

function buttonLabel(text) {
  $('auth-submit').innerHTML = '<span>' + text + '</span><b aria-hidden="true">↗</b>';
}

function setMode(value) {
  if (value === 'register' && !registrationEnabled) value = 'login';
  mode = value;
  const register = mode === 'register';
  $('login-tab').setAttribute('aria-selected', String(!register));
  $('register-tab').setAttribute('aria-selected', String(register));
  $('confirm-row').hidden = !register;
  $('registration-hint').hidden = !register;
  $('password-strength').hidden = !register;
  $('remember-row').hidden = register;
  $('confirm-password').required = register;
  $('password').autocomplete = register ? 'new-password' : 'current-password';
  $('password').minLength = register ? 10 : 1;
  $('form-title').textContent = register ? '创建你的研究账号' : '欢迎回到航迹';
  $('form-subtitle').textContent = register
    ? '注册后即可进入航线与空间天气研究工作台。'
    : '登录研究账号，继续查看航线与空间天气。';
  buttonLabel(register ? '创建账号' : '登录工作台');
  $('auth-message').textContent = '';
  $('auth-message').classList.remove('success');
  document.title = (register ? '注册' : '登录') + ' · 航迹';
  history.replaceState(null, '', register ? '/register' : '/login');
}

function updateStrength() {
  const value = $('password').value;
  const score = [
    value.length >= 10,
    /[A-Z]/i.test(value) && /\d/.test(value),
    /[^A-Za-z0-9]/.test(value) && value.length >= 14
  ].filter(Boolean).length;
  const box = $('password-strength');
  box.className = 'password-strength' + (score >= 3 ? ' strong' : score >= 2 ? ' medium' : '');
  box.querySelector('span').textContent = score >= 3 ? '密码强度较好' : score >= 2 ? '密码强度一般，可再增加长度或符号' : '建议至少 10 位，并组合字母、数字与符号';
}

$('login-tab').addEventListener('click', () => setMode('login'));
$('register-tab').addEventListener('click', () => setMode('register'));
$('password').addEventListener('input', updateStrength);
$('show-password').addEventListener('click', () => {
  const show = $('password').type === 'password';
  $('password').type = show ? 'text' : 'password';
  $('confirm-password').type = show ? 'text' : 'password';
  $('show-password').textContent = show ? '隐藏' : '显示';
  $('show-password').setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
});

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    throw new Error('服务响应无法读取，请刷新后重试。');
  }
}

async function session() {
  let response;
  try {
    response = await fetch('/api/auth/session');
  } catch {
    throw new Error('无法连接网站服务，请检查网络。');
  }
  const data = await readJson(response);
  if (!response.ok) throw new Error(data.error || '连接失败');
  if (data.authenticated) {
    location.replace(data.user.role === 'admin' ? '/admin' : '/');
    return;
  }
  csrf = data.csrf;
  registrationEnabled = data.registration_enabled !== false;
  $('register-tab').disabled = !registrationEnabled;
  if (!registrationEnabled) $('register-tab').title = '当前网站未开放自主注册';
  $('service-status').classList.remove('error');
  $('service-status').querySelector('span').textContent = '服务已连接 · 可以安全登录';
  $('auth-submit').disabled = false;
  setMode(mode);
  $('username').focus();
}

$('auth-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('auth-message').textContent = '';
  $('auth-message').classList.remove('success');
  if (!event.currentTarget.checkValidity()) {
    event.currentTarget.reportValidity();
    return;
  }
  if (mode === 'register' && $('password').value !== $('confirm-password').value) {
    $('auth-message').textContent = '两次输入的密码不一致。';
    $('confirm-password').focus();
    return;
  }
  const activeMode = mode;
  $('auth-submit').disabled = true;
  buttonLabel('正在处理…');
  try {
    const response = await fetch('/api/auth/' + activeMode, {
      method:'POST',
      headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrf},
      body:JSON.stringify({
        username:$('username').value,
        password:$('password').value,
        remember:activeMode === 'login' && $('remember-me').checked
      })
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.error || '请求失败');
    if (activeMode === 'login') {
      location.replace(data.redirect);
      return;
    }
    const username = data.username || $('username').value;
    $('password').value = '';
    $('confirm-password').value = '';
    setMode('login');
    $('username').value = username;
    $('auth-message').textContent = data.message;
    $('auth-message').classList.add('success');
    $('password').focus();
  } catch (error) {
    $('auth-message').textContent = error.message;
  } finally {
    $('auth-submit').disabled = false;
    buttonLabel(mode === 'register' ? '创建账号' : '登录工作台');
  }
});

session().catch(error => {
  $('service-status').classList.add('error');
  $('service-status').querySelector('span').textContent = '服务暂时无法连接';
  $('auth-message').textContent = error.message + ' 请稍后刷新页面。';
});
