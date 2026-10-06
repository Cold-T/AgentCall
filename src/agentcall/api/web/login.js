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
    input.value = '';
    if (!response.ok) throw new Error(response.status === 429
      ? '尝试次数过多，请稍后再试。' : 'PIN 不正确。');
    location.reload();
  } catch (error) { document.querySelector('#error').textContent = error.message; }
  finally { button.disabled = false; }
});
