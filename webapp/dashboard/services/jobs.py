"""后台任务执行服务：单 worker 队列 + 子进程/函数两类任务。

设计要点（本地单用户工具，刻意不用 celery）：
- 串行执行：同一时刻最多一个任务，避免多任务抢网络限速与 SQLite 写锁；
- 子进程任务：数据脚本的 CLI 入口原样跑（fetch_all_market.py 等），
  stdout 逐行入日志缓冲，行内 "n/m" 自动解析为进度，退出码决定成败；
- 函数任务：Web 层编排（单股更新 / 评分扫描），通过 JobProxy 汇报进度，
  可被取消（协作式检查 cancel_event）；
- 崩溃恢复：worker 启动时把遗留 pending/running 标记为 failed。
"""
import contextlib
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import traceback

import django.db
from django.utils import timezone

from ..models import Job


class JobCancelled(Exception):
    """函数任务主动取消（协作式检查 proxy.cancelled() 后抛出）"""
    pass

# backend/ 根（webapp/dashboard/services/jobs.py 向上 4 级）
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SCRIPTS_DIR = os.path.join(BACKEND_DIR, 'scripts')
PY = sys.executable

# 子进程脚本任务注册表：kind → (脚本, 固定参数, 默认标签)
# 注意: kline_all 不在此列——账本全 done 后 --fetch 会无事可做，Web 层先重置落后账本再跑（stock_ops.kline_refresh）
SCRIPT_JOBS = {
    'build_list':   ('fetch_all_market.py', ['--build-list'], '重建全A股股票列表'),
    'fin_bulk':     ('fetch_all_financials.py', ['--bulk-reports'], '财务批量入库（业绩报表·断点续传）'),
    'fin_gap':      ('fetch_all_financials.py', ['--gap-reports'], '财务缺口补齐（东财逐股）'),
    'fin_industry': ('fetch_all_financials.py', ['--industry-info'], '行业链+总股本补齐（逐股）'),
    'ai_classify':  ('ai_model_classifier.py', ['--all'], 'AI模型分类（缺分类增量）'),
    'factors':      ('score_factors.py', ['--backfill'], '算分因子回填（30天内跳过）'),
    'bonus_warmup': ('warmup_bonus_events.py', [], '分红送转事件预热'),
    'score_all':    ('score_market.py', [], '全市场评分（增量：K线无新数据的股票跳过）'),
    'reports_rebuild': ('batch_rebuild.py', ['--summary'],
                        '批量重建 watchlist 报告（逐只 build_report，较慢）'),
}

_LOG_LINES = 400          # 日志缓冲保留行数
_FLUSH_INTERVAL = 1.0     # 日志/进度落库节流（秒）
_PROGRESS_RE = re.compile(r'(\d+)\s*/\s*(\d+)')


class _Cancelled(Exception):
    pass


class _JobWriter:
    """把函数任务内部的 print(...) 转发到任务日志（worker 单线程串行，重定向安全）"""

    def __init__(self, ex, job_id):
        self._ex = ex
        self._job_id = job_id

    def write(self, s):
        for ln in s.splitlines():
            if ln.strip():
                self._ex.append_log(self._job_id, ln)
        return len(s)

    def flush(self):
        pass


def run_subprocess_logged(proxy, argv, cwd=BACKEND_DIR, _indent=''):
    """函数任务内跑数据脚本子进程：stdout 逐行入任务日志（可加 _indent 缩进），
    可取消（看护线程 0.5s 轮询，取消立刻 SIGTERM，不等下一行输出——
    抓取脚本可能数分钟不打日志）。退出码非0抛 RuntimeError，被取消抛 JobCancelled。
    argv[0] 为 scripts/ 下脚本名。"""
    import threading
    import time as _time

    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
    proxy.log(f'$ {argv[0]} {" ".join(argv[1:])}')
    proc = subprocess.Popen(
        [PY, os.path.join(SCRIPTS_DIR, argv[0])] + list(argv[1:]),
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding='utf-8', errors='replace', bufsize=1, env=env)

    def _watch():
        while not proxy.cancelled():
            if proc.poll() is not None:
                return
            _time.sleep(0.5)
        try:
            proc.terminate()
        except ProcessLookupError:
            pass

    threading.Thread(target=_watch, daemon=True).start()
    try:
        for line in proc.stdout:
            if line.strip():
                proxy.log(_indent + line.rstrip())
            if proxy.cancelled():
                break
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait()
    if proxy.cancelled():
        raise JobCancelled()
    if code != 0:
        raise RuntimeError(f'{argv[0]} 退出码 {code}')
    return code


class JobProxy:
    """函数任务与执行器之间的进度/日志通道 + 取消协作"""

    def __init__(self, job_id, cancel_event, executor_ref):
        self.job_id = job_id
        self._cancel = cancel_event
        self._ex = executor_ref

    def log(self, msg):
        self._ex.append_log(self.job_id, str(msg))

    def progress(self, cur, total, msg=''):
        self._ex.set_progress(self.job_id, cur, total, msg)

    def cancelled(self):
        return self._cancel.is_set()


class _JobRuntime:
    """活跃任务的内存态：日志环形缓冲 + 进度缓存 + 取消事件"""

    def __init__(self):
        self.lines = []
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.cur = 0
        self.total = 0


class Executor:
    def __init__(self):
        self._queue = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self._runtimes = {}       # job_id -> _JobRuntime（仅活跃任务）
        self._fns = {}            # job_id -> callable（函数任务，不落库）
        self._last_flush = 0.0

    # ---------- 对外接口 ----------

    def ensure_worker(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._recover_stale()
                self._thread = threading.Thread(target=self._loop, name='job-worker', daemon=True)
                self._thread.start()

    def enqueue(self, kind, label='', params=None, fn=None):
        """入队。同 kind+params 且仍在排队/运行的任务直接复用（幂等防连点）。
        返回 (job, created)；created=False 表示复用了已有任务。"""
        self.ensure_worker()
        params = params or {}
        if fn is None and kind not in SCRIPT_JOBS and kind != 'build_report':
            raise ValueError(f'未知任务类型: {kind}')
        for j in Job.objects.filter(kind=kind, status__in=('pending', 'running')):
            if (j.params or {}) == params:
                return j, False
        job = Job.objects.create(kind=kind, label=label or kind, params=params)
        with self._lock:
            self._runtimes[job.id] = _JobRuntime()
            if fn is not None:
                self._fns[job.id] = fn
        self._queue.put(job.id)
        return job, True

    def cancel(self, job_id):
        job = Job.objects.filter(id=job_id).first()
        if not job:
            return False
        rt = self._runtimes.get(job_id)
        if rt is not None:
            rt.cancel_event.set()
        if job.status == 'pending':
            self.finalize(job_id, status='canceled', message='排队中被取消')
            return True
        if job.status == 'running':
            if job.pid:   # 子进程任务：直接 SIGTERM
                try:
                    os.kill(job.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                self.append_log(job_id, f'[取消] 已向进程 {job.pid} 发送 SIGTERM')
                return True
            return rt is not None   # 函数任务：协作式取消（事件已置位）
        return False

    def tail_log(self, job_id, n=40):
        rt = self._runtimes.get(job_id)
        if rt is not None:
            with rt.lock:
                return list(rt.lines[-n:])
        job = Job.objects.filter(id=job_id).first()
        if job and job.log:
            return job.log.splitlines()[-n:]
        return []

    # ---------- worker 主循环 ----------

    def _loop(self):
        while True:
            job_id = self._queue.get()
            fn = self._fns.pop(job_id, None)
            job = Job.objects.filter(id=job_id).first()
            if job is None or job.status != 'pending':
                with self._lock:
                    self._runtimes.pop(job_id, None)   # 取消/已清理的任务
                continue
            django.db.close_old_connections()
            job.status = 'running'
            job.started_at = timezone.now()
            job.save(update_fields=['status', 'started_at'])
            rt = self._runtimes.get(job_id)
            cancel_event = rt.cancel_event if rt else threading.Event()
            try:
                if fn is not None:
                    with contextlib.redirect_stdout(_JobWriter(self, job_id)):
                        fn(JobProxy(job_id, cancel_event, self))
                    self._flush(job_id, force=True)
                    last = self._last_line(job_id) or '完成'
                    self.finalize(job_id, status='done', message=last[:300])
                else:
                    self._run_proc(job)
            except (JobCancelled, _Cancelled):
                self.finalize(job_id, status='canceled', message='任务被取消')
            except Exception as e:
                self.append_log(job_id, f'[异常] {type(e).__name__}: {e}')
                self.append_log(job_id, traceback.format_exc()[-1200:])
                self.finalize(job_id, status='failed', message=f'{type(e).__name__}: {str(e)[:200]}')
            finally:
                with self._lock:
                    self._runtimes.pop(job_id, None)
                django.db.close_old_connections()

    def _last_line(self, job_id):
        rt = self._runtimes.get(job_id)
        if rt is None:
            return ''
        with rt.lock:
            return rt.lines[-1] if rt.lines else ''

    # ---------- 子进程任务 ----------

    def _run_proc(self, job):
        params = job.params or {}
        if job.kind == 'build_report':
            script, argv = 'build_report.py', [params['code'], '--model', params.get('model', 'cyclical')]
        else:
            base_script, base_args, _label = SCRIPT_JOBS[job.kind]
            script, argv = base_script, list(base_args)

        cmd = [PY, os.path.join(SCRIPTS_DIR, script)] + argv
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        self.append_log(job.id, f'$ {script} {" ".join(argv)}')
        proc = subprocess.Popen(
            cmd, cwd=BACKEND_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', bufsize=1, env=env)
        Job.objects.filter(id=job.id).update(pid=proc.pid)
        rt = self._runtimes.get(job.id)
        for line in proc.stdout:
            if rt is not None and rt.cancel_event.is_set():
                proc.terminate()
                raise _Cancelled()
            self.append_log(job.id, line.rstrip())
        code = proc.wait()
        if rt is not None and rt.cancel_event.is_set():
            raise _Cancelled()
        Job.objects.filter(id=job.id).update(returncode=code)
        if code != 0:
            self.finalize(job.id, status='failed', message=f'退出码 {code}（详见日志）')
        else:
            self.finalize(job.id, status='done',
                          message=(self._last_line(job.id) or '完成')[:300])

    # ---------- 日志 / 进度（worker 与函数任务线程共同调用） ----------

    def append_log(self, job_id, line):
        rt = self._runtimes.get(job_id)
        if rt is None:
            return
        with rt.lock:
            rt.lines.append(line[:500])
            if len(rt.lines) > _LOG_LINES * 2:
                del rt.lines[:-_LOG_LINES]
            m = _PROGRESS_RE.search(line)
            if m:
                rt.cur, rt.total = min(int(m.group(1)), int(m.group(2))), int(m.group(2))
        self._flush(job_id, force=False)

    def set_progress(self, job_id, cur, total, msg=''):
        rt = self._runtimes.get(job_id)
        if rt is None:
            return
        with rt.lock:
            rt.cur, rt.total = cur, total
        if msg:
            self.append_log(job_id, msg)
        else:
            self._flush(job_id, force=True)

    def _flush(self, job_id, force=False):
        now = time.monotonic()
        if not force and now - self._last_flush < _FLUSH_INTERVAL:
            return
        self._last_flush = now
        rt = self._runtimes.get(job_id)
        if rt is None:
            return
        with rt.lock:
            lines_snapshot = list(rt.lines[-_LOG_LINES:])
            cur, total = rt.cur, rt.total
        fields = {'log': '\n'.join(lines_snapshot),
                  'message': (lines_snapshot[-1][:300] if lines_snapshot else '')}
        if total:
            fields['progress'] = min(cur, total)
            fields['total'] = total
        try:
            Job.objects.filter(id=job_id, status='running').update(**fields)
        except Exception:
            pass

    def finalize(self, job_id, status, message=''):
        log_text = ''
        rt = self._runtimes.get(job_id)
        if rt is not None:
            with rt.lock:
                log_text = '\n'.join(rt.lines[-_LOG_LINES:])
        try:
            Job.objects.filter(id=job_id).update(
                status=status, message=message[:300], finished_at=timezone.now(), log=log_text)
        except Exception:
            pass
        django.db.close_old_connections()

    def _recover_stale(self):
        """worker 启动时清理上次进程遗留的任务"""
        n = Job.objects.filter(status__in=('pending', 'running')).update(
            status='failed', message='服务重启导致中断', finished_at=timezone.now())
        if n:
            print(f'[jobs] 恢复：{n} 个遗留任务标记为失败')


executor = Executor()
