/* 公共助手：CSRF / fetch / 轮询 / 任务指示灯 */
(function () {
  function getCookie(name) {
    const m = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return m ? m.pop() : '';
  }
  window.api = {
    get: (url) => fetch(url, {credentials: 'same-origin'}).then(r => r.json()),
    post: (url, body) => fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCookie('csrftoken')},
      body: JSON.stringify(body || {}),
    }).then(async r => ({status: r.status, data: await r.json()})),
    esc: (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
    fmtNum: (v, d = 2) => (v == null || isNaN(v)) ? '-' : Number(v).toFixed(d),
  };

  // 顶栏任务指示灯：有活动任务时变黄。每个页面轮询 /api/jobs/ 时顺带调用。
  window.jobDot = function (payload) {
    const el = document.querySelector('.topbar .job-dot');
    if (!el) return;
    const busy = payload && payload.active;
    el.classList.toggle('busy', !!busy);
    el.innerHTML = '<span class="dot"></span>' + (busy
      ? (payload.active.label || '任务运行中')
      : '空闲');
  };
})();
