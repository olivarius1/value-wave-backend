"""页面 + JSON API 视图"""
import json

from django.http import FileResponse, JsonResponse
from django.utils import timezone as tz
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from .models import Job, StockGroup, StockGroupMember
from .services import data_status, stock_ops
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

@require_GET
def api_report_status(request):
    code = (request.GET.get('code') or '').strip()
    if not (code.isdigit() and len(code) == 6):
        return JsonResponse({'error': '请输入6位股票代码'}, status=400)
    model, model_source = stock_ops.resolve_model(code)
    name = stock_ops.stock_name(code)
    known = bool(model) or stock_ops._kline_last_date(code) or \
        stock_ops.find_meta_json(code) is not None
    rep = stock_ops.find_report_file(code)
    meta_doc = stock_ops.find_meta_json(code)
    meta = (meta_doc or {}).get('meta') or {}
    data_last = ''
    if meta_doc and meta_doc.get('data'):
        data_last = meta_doc['data'][-1].get('date', '')
    return JsonResponse({
        'code': code, 'name': name, 'known': known,
        'model': model, 'model_label': stock_ops.model_label(model),
        'model_source': model_source,
        'report': rep and {'mtime': rep['mtime'], 'size_kb': rep['size_kb'],
                           'filename': rep['filename']},
        'meta': {
            'model_type': meta.get('model_type'),
            'score': (meta_doc['data'][-1].get('score') if meta_doc and meta_doc.get('data') else None),
            'price': meta.get('latest_raw_price'),
            'weights': meta.get('weights'),
            'window_reason': meta.get('window_reason'),
            'data_last_date': data_last,
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
