#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测本地 Web 服务：页面输入 → 后端重跑 → 跳转最新报告

用途（回测报告第 6 节"更换回测时间段"）：
    用户发现默认回测区间（全部股票首个分数的最晚日期）不符合预期时，
    通过本服务在页面上填写起始日期/股票子集提交，自动重跑 run_backtest.py，
    完成后跳转到新报告。也提供全部历史 run 的浏览入口。

用法：
    python scripts/backtest_web.py            # 默认 127.0.0.1:8643
    python scripts/backtest_web.py --port 9000

页面：
    http://127.0.0.1:8643/                    主页（最新报告 + 重新回测表单 + run 列表）
    报告内第 6 节表单 action="/run" 也提交到本服务
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _SCRIPT_DIR)

from run_backtest import OUTPUT_ROOT, load_watchlist, WATCHLIST_PATH

# 运行状态表：{run_prefix: {'proc', 'done', 'log_tail', 'started_at'}}
_RUNS = {}
_RUNS_LOCK = threading.Lock()

_PAGE_CSS = """
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0f172a; color: #e2e8f0; font-family: 'Microsoft YaHei', sans-serif; padding: 32px; }
  .wrap { max-width: 860px; margin: 0 auto; }
  h1 { font-size: 22px; margin-bottom: 8px; }
  .sub { color: #94a3b8; font-size: 13px; margin-bottom: 24px; }
  .card { background: #1e293b; border-radius: 10px; padding: 18px; margin-bottom: 20px; }
  .card h2 { font-size: 16px; margin-bottom: 12px; color: #f1f5f9; border-left: 4px solid #3b82f6; padding-left: 10px; }
  label { display: block; font-size: 12px; color: #94a3b8; margin: 10px 0 4px; }
  input { background: #0f172a; color: #e2e8f0; border: 1px solid #334155; border-radius: 6px; padding: 8px 10px; width: 100%; }
  input[type=checkbox] { width: auto; }
  button { background: #3b82f6; color: #fff; border: none; border-radius: 6px; padding: 10px 22px; margin-top: 14px; cursor: pointer; font-size: 14px; }
  a { color: #93c5fd; text-decoration: none; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #334155; }
  th { color: #93c5fd; }
  .run { color: #34d399; }
  .fail { color: #f87171; }
  pre { background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 10px; font-size: 12px; overflow-x: auto; max-height: 300px; }
"""


def _list_runs():
    """按时间倒序列出全部 run 目录及状态"""
    if not os.path.isdir(OUTPUT_ROOT):
        return []
    runs = []
    for name in sorted(os.listdir(OUTPUT_ROOT), reverse=True):
        d = os.path.join(OUTPUT_ROOT, name)
        if not os.path.isdir(d) or not name[:8].isdigit():
            continue
        meta_path = os.path.join(d, 'meta.json')
        info = {'run_id': name, 'meta': None, 'log_tail': ''}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding='utf-8') as f:
                    info['meta'] = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        log_path = os.path.join(d, 'run.log')
        if os.path.exists(log_path):
            try:
                with open(log_path, encoding='utf-8', errors='replace') as f:
                    lines = f.read().strip().splitlines()
                info['log_tail'] = lines[-1] if lines else ''
            except IOError:
                pass
        runs.append(info)
    return runs


def _home_page():
    runs = _list_runs()
    rows = ''
    for r in runs[:20]:
        m = r['meta'] or {}
        ok = '完成' if os.path.exists(os.path.join(OUTPUT_ROOT, r['run_id'], 'backtest_report.html')) else '中断'
        cls = 'run' if ok == '完成' else 'fail'
        stock_n = m.get('stock_count', '?')
        gen = (m.get('generated_at') or '')[:16]
        link = f"<a href='/reports/{r['run_id']}/backtest_report.html'>{r['run_id']}</a>"
        rows += (f"<tr><td>{link}</td><td>{stock_n}</td><td>{gen}</td>"
                 f"<td class='{cls}'>{ok}</td><td style='color:#64748b;font-size:12px'>{r['log_tail']}</td></tr>")
    latest = runs[0]['run_id'] if runs else None
    latest_link = (f"<p><a href='/reports/{latest}/backtest_report.html' style='font-size:15px'>"
                   f"➜ 最新回测报告（{latest}）</a></p>") if latest else '<p>暂无回测结果</p>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>估值回测服务</title><style>{_PAGE_CSS}</style></head>
<body><div class="wrap">
  <h1>估值评分回测服务</h1>
  <div class="sub">artifacts/backtest/ 浏览与重新回测 · 提交后约 1-2 分钟完成全量 watchlist</div>
  <div class="card">
    <h2>最新结果</h2>
    {latest_link}
  </div>
  <div class="card">
    <h2>重新回测（更换时间段 / 股票子集）</h2>
    <form method="POST" action="/run">
      <label>回测起点（空 = 自动，从全部股票都有分数的日期起；填如 2018-01-01 / 2020-01-01 可避开特定行情段）</label>
      <input type="date" name="start">
      <label>回测终点（空 = 最新数据日；与起点配合可限定时间段，如 2018-01-01 ~ 2022-12-31）</label>
      <input type="date" name="end">
      <label>股票子集（空 = 全量 watchlist；逗号分隔代码，如 600887,601899）</label>
      <input type="text" name="stocks" placeholder="600887,601899">
      <label><input type="checkbox" name="refresh" value="1"> 强制刷新数据（默认使用缓存）</label>
      <button type="submit">开始回测</button>
    </form>
  </div>
  <div class="card">
    <h2>历史运行</h2>
    <table>
      <tr><th>run_id</th><th>股票数</th><th>生成时间</th><th>状态</th><th>日志</th></tr>
      {rows}
    </table>
  </div>
</div></body></html>"""


def _running_page(run_id):
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>回测运行中...</title><style>{_PAGE_CSS}</style></head>
<body><div class="wrap">
  <div class="card">
    <h2>回测运行中（run_id: {run_id}）</h2>
    <pre id="log">启动中...</pre>
  </div>
</div>
<script>
var runId = '{run_id}';
function poll() {{
  fetch('/status/' + runId).then(function(r){{ return r.json(); }}).then(function(s) {{
    document.getElementById('log').textContent = s.log || '(等待输出...)';
    if (s.done) {{
      if (s.ok) {{ window.location.href = '/reports/' + runId + '/backtest_report.html'; }}
      else {{ document.getElementById('log').textContent = '回测失败:\\n' + (s.log || ''); }}
    }} else {{
      setTimeout(poll, 2000);
    }}
  }}).catch(function(){{ setTimeout(poll, 2000); }});
}}
poll();
</script></body></html>"""


def _start_run(start, end, stocks, refresh):
    """后台启动 run_backtest.py，返回 run_id 前缀"""
    now = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    cmd = [sys.executable, os.path.join(_SCRIPT_DIR, 'run_backtest.py')]
    if start:
        cmd += ['--start', start]
    if end:
        cmd += ['--end', end]
    if stocks:
        cmd += ['--stocks', stocks]
    if refresh:
        cmd += ['--refresh-data']
    with _RUNS_LOCK:
        _RUNS[now] = {'done': False, 'ok': None, 'log': ''}
    env = dict(os.environ)

    def _worker():
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                                  errors='replace', timeout=1800, env=env, cwd=_PROJECT_ROOT)
            tail = (proc.stdout or '')[-2000:]
            if proc.returncode != 0:
                tail += '\n[stderr] ' + (proc.stderr or '')[-800:]
            with _RUNS_LOCK:
                _RUNS[now]['log'] = tail
                _RUNS[now]['done'] = True
                _RUNS[now]['ok'] = (proc.returncode == 0)
        except Exception as e:
            with _RUNS_LOCK:
                _RUNS[now]['log'] = str(e)
                _RUNS[now]['done'] = True
                _RUNS[now]['ok'] = False
    threading.Thread(target=_worker, daemon=True).start()
    return now


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, code=200, ctype='text/html; charset=utf-8'):
        data = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj):
        self._send(json.dumps(obj, ensure_ascii=False), ctype='application/json; charset=utf-8')

    def _serve_static(self, rel):
        """/reports/<path> → artifacts/backtest/<path>，仅允许安全扩展名"""
        path = os.path.normpath(os.path.join(OUTPUT_ROOT, rel))
        if not path.startswith(os.path.normpath(OUTPUT_ROOT)):
            self._send('forbidden', 403)
            return
        if not os.path.isfile(path):
            self._send('not found', 404)
            return
        ext = os.path.splitext(path)[1].lower().lstrip('.')
        ctype = {'html': 'text/html; charset=utf-8', 'md': 'text/markdown; charset=utf-8',
                 'json': 'application/json; charset=utf-8', 'csv': 'text/csv; charset=utf-8',
                 'png': 'image/png', 'jpg': 'image/jpeg', 'js': 'text/javascript'}.get(ext, 'application/octet-stream')
        with open(path, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_shared(self, rel):
        """/_shared/<path> → 项目根 _shared 目录（echarts 等静态资源）"""
        path = os.path.normpath(os.path.join(_PROJECT_ROOT, '_shared', rel))
        if not path.startswith(os.path.normpath(os.path.join(_PROJECT_ROOT, '_shared'))):
            self._send('forbidden', 403)
            return
        ext = os.path.splitext(path)[1].lower()
        if ext != '.js':
            self._send('not found', 404)
            return
        if not os.path.isfile(path):
            self._send('not found', 404)
            return
        with open(path, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/javascript; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        p = url.path
        if p == '/' or p == '/index.html':
            self._send(_home_page())
        elif p.startswith('/_shared/'):
            self._serve_shared(p[len('/_shared/'):])
        elif p.startswith('/reports/'):
            self._serve_static(p[len('/reports/'):])
        elif p.startswith('/status/'):
            run_id = p[len('/status/'):].strip('/')
            with _RUNS_LOCK:
                st = _RUNS.get(run_id, {'done': True, 'ok': None, 'log': '未知 run_id'})
                if run_id in _RUNS:
                    st = dict(st)
            self._send_json(st)
        else:
            self._send('not found', 404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != '/run':
            self._send('not found', 404)
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8', errors='replace')
        except Exception:
            body = ''
        form = urllib.parse.parse_qs(body)
        start = (form.get('start') or [''])[0].strip()
        end = (form.get('end') or [''])[0].strip()
        stocks = (form.get('stocks') or [''])[0].strip()
        refresh = bool(form.get('refresh'))
        # 校验：start/end 格式 YYYY-MM-DD；stocks 仅数字/逗号
        for label, val in (('起始日期', start), ('结束日期', end)):
            if val:
                try:
                    datetime.datetime.strptime(val, '%Y-%m-%d')
                except ValueError:
                    self._send(f'<h2>{label}格式错误，应为 YYYY-MM-DD</h2><p><a href="/">返回</a></p>', 400)
                    return
        if start and end and end < start:
            self._send('<h2>结束日期不能早于起始日期</h2><p><a href="/">返回</a></p>', 400)
            return
        if stocks and not all(part.strip().isdigit() for part in stocks.split(',') if part.strip()):
            self._send('<h2>股票代码格式错误，应为逗号分隔的数字代码</h2><p><a href="/">返回</a></p>', 400)
            return
        run_id = _start_run(start, end, stocks, refresh)
        self._send(_running_page(run_id))


def main():
    parser = argparse.ArgumentParser(description='估值回测本地 Web 服务')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8643)
    args = parser.parse_args()
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    print(f"回测服务已启动: http://{args.host}:{args.port}")
    print(f"watchlist: {WATCHLIST_PATH}（{len(load_watchlist(WATCHLIST_PATH))} 只）")
    print("Ctrl+C 退出")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
