'use strict';

const $ = id => document.getElementById(id);
let csrf = '';

function buttonLabel(text) {
  $('save-password').innerHTML = '<span>' + text + '</span><b aria-hidden="true">↗</b>';
}

async function loadSession() {
  const response = await fetch('/api/auth/session');
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '无法读取登录状态。');
  if (!data.authenticated) {
    location.replace('/login');
    return;
  }
  csrf = data.csrf;
  $('account-name').textContent = '当前账号：' + data.user.username + (data.user.role === 'admin' ? ' · 管理员' : '');
  $('save-password').disabled = false;
  buttonLabel('保存新密码');
}

$('show-account-passwords').addEventListener('change', event => {
  const type = event.currentTarget.checked ? 'text' : 'password';
  for (const id of ['old-password','new-password','confirm-password']) $(id).type = type;
});

$('password-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('message').textContent = '';
  if (!event.currentTarget.checkValidity()) {
    event.currentTarget.reportValidity();
    return;
  }
  if ($('new-password').value !== $('confirm-password').value) {
    $('message').textContent = '两次输入的新密码不一致。';
    $('confirm-password').focus();
    return;
  }
  if ($('old-password').value === $('new-password').value) {
    $('message').textContent = '新密码不能与当前密码相同。';
    $('new-password').focus();
    return;
  }
  $('save-password').disabled = true;
  buttonLabel('正在保存…');
  try {
    const response = await fetch('/api/auth/password', {
      method:'POST',
      headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrf},
      body:JSON.stringify({
        current_password:$('old-password').value,
        new_password:$('new-password').value
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '密码修改失败。');
    location.replace('/login');
  } catch (error) {
    $('message').textContent = error.message;
    $('save-password').disabled = false;
    buttonLabel('保存新密码');
  }
});

loadSession().catch(error => {
  $('message').textContent = '连接失败：' + error.message;
  buttonLabel('无法连接');
});
