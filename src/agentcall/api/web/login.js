'use strict';
const base = document.querySelector('meta[name="api-base"]').content;
document.querySelector('#login').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.target.querySelector('button');
  const input = document.querySelector('#pin');
  button.disabled = true;
  document.querySelector('#error').textContent = '';
  try {
    const response = await fetch(base + '/session/login', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-AgentCall-CSRF': '1'},
      body: JSON.stringify({pin: input.value})
    });
    if (!response.ok) {
      if (response.status === 429) throw new Error(`尝试次数过多，请在 ${response.headers.get('Retry-After') || 60} 秒后重试。`);
      if (response.status === 403) throw new Error('登录请求被拒绝，请使用本站 HTTPS 地址重新打开页面。');
      if (response.status === 401) throw new Error('PIN 不正确，请使用当前的 4 位 PIN。');
      throw new Error('服务暂时无法登录，请稍后重试。');
    }
    input.value = '';
    location.reload();
  } catch (error) { document.querySelector('#error').textContent = error.message; }
  finally { button.disabled = false; }
});
