"""页面 + JSON API 视图"""
import datetime
import json

from django.http import FileResponse, JsonResponse
from django.utils import timezone as tz
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from .models import Job, StockGroup, StockGroupMember
from .services import data_status, report_registry, stock_ops
from .services.jobs import SCRIPT_JOBS, executor


# ---------- 页面 ----------

def data_page(request):
    return render(request, 'dashboard/data.html')


def groups_page(request):
    StockGroup.ensure_builtin()
    return render(request, 'dashboard/groups.html')


def scores_page(request):
    return render(request, 'dashboard/scores.html')


def report_page(request):
    choices = [(m, stock_ops.model_label(m)) for m in stock_ops.MODEL_CHOICES]
    return render(request, 'dashboard/report.html', {'model_choices': choices})


# ---------- 数据管理 API ----------

@require_GET
def api_status(request):
    report_registry.backfill_registry()   # 存量报告一次性登记（进程内只跑一次）
    return JsonResponse(data_status.status_json())


@require_GET
def api_search(request):
    q = request.GET.get('q', '')
    return JsonResponse({'results': stock_ops.search_stocks(q)})


# ---------- 分数面板 API ----------

@require_GET
def api_scores(request):
    """分数面板数据（score_store.db，评分器产出）。
    判定口径 = 当前分在自身历史分数序列中的分位（score_pct），阈值过滤在前端
    （页面可调，默认 ≥90% 进高分、≤20% 进低分）。"""
    rows, latest_trading_day = stock_ops.all_stock_scores()
    last_computed = max((r['computed_at'] for r in rows if r['computed_at']), default='')
    return JsonResponse({
        'rows': rows,
        'latest_trading_day': latest_trading_day,
        'last_computed': last_computed,
        'count': len(rows),
    })


# ---------- 报告浏览 API ----------

AUTO_REBUILD_MIN_AGE = 10   # 分钟：刚重建过的报告不再被自动重建判据反复触发（防抖）

@require_GET
def api_report_status(request):
    """报告状态 + 缓存过期判定。?auto=1 时过期自动入队重建（返回 job_id 供前端跟随）。"""
    code = (request.GET.get('code') or '').strip()
    if not (code.isdigit() and len(code) == 6):
        return JsonResponse({'error': '请输入6位股票代码'}, status=400)
    wl_model, model_source = stock_ops.resolve_model(code)
    name = stock_ops.stock_name(code)
    info = report_registry.info(code)
    build_model = wl_model or info['model']

    auto_job_id = None
    auto_note = ''
    if info['stale'] and request.GET.get('auto') in ('1', 'true'):
        cutoff = (tz.localtime(tz.now()) - datetime.timedelta(minutes=AUTO_REBUILD_MIN_AGE)) \
            .strftime('%Y-%m-%d %H:%M')
        just_built = bool(info['generated_at'] and info['generated_at'] >= cutoff)
        if build_model and not just_built:
            job, created = executor.enqueue(
                'build_report',
                label=f"生成报告 {code}({stock_ops.model_label(build_model)})",
                params={'code': code, 'model': build_model})
            auto_job_id = job.id
            auto_note = '已自动触发重建' if created else '已有重建任务进行中'
        elif just_built:
            auto_note = f'{AUTO_REBUILD_MIN_AGE} 分钟内已重建过，暂不自动重试'
        else:
            auto_note = '无模型归属，无法自动重建（请在下方选择模型）'

    return JsonResponse({
        'code': code, 'name': name,
        'known': bool(build_model) or info['exists'] or bool(stock_ops._kline_last_date(code)),
        'model': build_model, 'model_label': stock_ops.model_label(build_model),
        'model_source': model_source or ('report' if info['model'] else ''),
        'report': info['exists'] and {
            'mtime': info['generated_at'], 'size_kb': info['size_kb'],
            'filename': info['file_name']},
        'cache': {
            'generated_at': info['generated_at'],
            'ttl_hours': info['ttl_hours'],
            'stale': info['stale'],
            'stale_reason': info['stale_reason'],
            'data_last_date': info['data_last_date'],
            'latest_trading_day': info['latest_trading_day'],
        },
        'auto_job_id': auto_job_id,
        'auto_note': auto_note,
        'meta': {
            'model_type': info['model'],
            'score': info['score'],
            'price': info['latest_raw_price'],
            'weights': info['weights'],
            'data_last_date': info['data_last_date'],
        },
    })


@require_GET
def report_frame(request, code):
    """报告 HTML 自包含（echarts 内联），直接整文件回传给 iframe"""
    if not (code.isdigit() and len(code) == 6):
        return JsonResponse({'error': 'bad code'}, status=400)
    rep = stock_ops.find_report_file(code)
    if not rep:
        return JsonResponse({'error': '报告不存在，请先生成'}, status=404)
    resp = FileResponse(open(rep['path'], 'rb'), content_type='text/html; charset=utf-8')
    resp['Cache-Control'] = 'no-store'
    return resp


# ---------- 分组管理 API ----------

@require_GET
def api_groups(request):
    StockGroup.ensure_builtin()
    groups = [{'id': g.id, 'name': g.name, 'is_builtin': g.is_builtin,
               'count': g.member_count, 'codes': g.codes()}
              for g in StockGroup.objects.all()]
    return JsonResponse({'groups': groups})


@require_GET
def api_group_detail(request, group_id):
    """组成员明细（带名称），按代码升序"""
    g = StockGroup.objects.filter(id=group_id).first()
    if not g:
        return JsonResponse({'error': '分组不存在'}, status=404)
    members = []
    for code in g.codes():
        members.append({'code': code, 'name': stock_ops.stock_name(code)})
    members.sort(key=lambda m: m['code'])
    return JsonResponse({'id': g.id, 'name': g.name, 'is_builtin': g.is_builtin,
                         'members': members})


@require_POST
def api_group_create(request):
    try:
        body = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'bad json'}, status=400)
    name = (body.get('name') or '').strip()
    if not name:
        return JsonResponse({'error': '分组名不能为空'}, status=400)
    if len(name) > 60:
        return JsonResponse({'error': '分组名过长'}, status=400)
    if StockGroup.objects.filter(name=name).exists():
        return JsonResponse({'error': f'分组「{name}」已存在'}, status=400)
    g = StockGroup.objects.create(name=name)
    return JsonResponse({'id': g.id, 'name': g.name, 'count': 0})


@require_POST
def api_group_delete(request, group_id):
    g = StockGroup.objects.filter(id=group_id).first()
    if not g:
        return JsonResponse({'error': '分组不存在'}, status=404)
    if g.is_builtin:
        return JsonResponse({'error': '内置分组「自选」不可删除'}, status=400)
    g.delete()
    return JsonResponse({'ok': True})


@require_POST
def api_group_set_stock(request):
    """单组增删语义：{code, group_id, add: true|false}。
    一只股票可同时属于多个分组，每次调用只影响一个分组。"""
    try:
        body = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'bad json'}, status=400)
    code = str(body.get('code', '')).strip()
    if not (code.isdigit() and len(code) == 6):
        return JsonResponse({'error': '股票代码须为6位数字'}, status=400)
    g = StockGroup.objects.filter(id=int(body.get('group_id') or 0)).first()
    if not g:
        return JsonResponse({'error': '分组不存在'}, status=404)
    add = bool(body.get('add'))
    if add:
        StockGroupMember.objects.get_or_create(group=g, code=code)
    else:
        g.members.filter(code=code).delete()
    return JsonResponse({'ok': True, 'code': code, 'group_id': g.id,
                         'group': g.name, 'member': add})


# ---------- 任务 API ----------

@require_GET
def api_job_list(request):
    # 串行队列：优先展示 running 的那个；没有 running 时才是队首 pending
    active = Job.objects.filter(status='running').first() \
        or Job.objects.filter(status='pending').first()
    out = {'jobs': [], 'active': None}
    jobs = list(Job.objects.all()[:25])
    for j in jobs:
        item = {
            'id': j.id, 'kind': j.kind, 'label': j.label, 'status': j.status,
            'progress': j.progress, 'total': j.total, 'message': j.message,
            'pid': j.pid, 'returncode': j.returncode,
            'created_at': tz.localtime(j.created_at).strftime('%m-%d %H:%M:%S'),
            'started_at': tz.localtime(j.started_at).strftime('%m-%d %H:%M:%S') if j.started_at else None,
            'finished_at': tz.localtime(j.finished_at).strftime('%m-%d %H:%M:%S') if j.finished_at else None,
        }
        if active and j.id == active.id:
            item['tail'] = executor.tail_log(j.id, 30)
            out['active'] = item
        out['jobs'].append(item)
    return JsonResponse(out)


@require_POST
def api_job_create(request):
    try:
        body = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'bad json'}, status=400)
    kind = body.get('kind', '')

    if kind in SCRIPT_JOBS:
        job, created = executor.enqueue(kind)
        return JsonResponse({'job_id': job.id, 'created': created, 'label': job.label})

    if kind == 'kline_all':
        # 账本全 done 时 --fetch 会无事可做：先重置落后账本再增量抓取（stock_ops.kline_refresh）
        job, created = executor.enqueue(
            'kline_all', label='全市场K线增量更新', fn=stock_ops.kline_refresh)
        return JsonResponse({'job_id': job.id, 'created': created, 'label': job.label})

    if kind == 'stock_update':
        code = str(body.get('code', '')).strip()
        if not (code.isdigit() and len(code) == 6):
            return JsonResponse({'error': '股票代码须为6位数字'}, status=400)
        def _run(proxy):
            stock_ops.update_single_stock(proxy, code)

        job, created = executor.enqueue(
            'stock_update', label=f'单股更新 {code}',
            params={'code': code}, fn=_run)
        return JsonResponse({'job_id': job.id, 'created': created, 'label': job.label})

    if kind == 'build_report':
        code = str(body.get('code', '')).strip()
        if not (code.isdigit() and len(code) == 6):
            return JsonResponse({'error': '股票代码须为6位数字'}, status=400)
        raw = body.get('model')
        # 模型留空/「自动」→ 先自动归属（watchlist 人工 > AI 分类）；中英文均接受
        model = stock_ops.normalize_model(raw) if raw not in (None, '', 'auto') else ''
        if not model:
            model, _src = stock_ops.resolve_model(code)
        if not model:
            return JsonResponse(
                {'error': '该股票无模型归属（不在watchlist且无AI分类），请在下拉框指定模型'},
                status=400)
        job, created = executor.enqueue(
            'build_report', label=f'生成报告 {code}({stock_ops.model_label(model)})',
            params={'code': code, 'model': model})
        return JsonResponse({'job_id': job.id, 'created': created, 'label': job.label})

    return JsonResponse({'error': f'未知任务类型: {kind}'}, status=400)


@require_POST
def api_job_cancel(request, job_id):
    ok = executor.cancel(job_id)
    return JsonResponse({'ok': ok})
