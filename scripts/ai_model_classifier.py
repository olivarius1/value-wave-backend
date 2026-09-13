#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全市场AI模型分类器 —— 用AI API批量把股票归入8种估值模型，结果入 kline_store.db（低频维护）

设计要点:
- 提示词静态部分(模型定义/决策流程/反例规则)全部在 system 消息且逐批不变 → 命中 DeepSeek 上下文缓存;
  变量(股票清单)全部在 user 消息末尾。
- 批级账本 classify_batch 断点续传: 中断重跑, 未入库的股票自动重算。
- source=manual 的行(人工/--set/--import-review)永不被自动刷新覆盖。
- needs_review 标记: 低置信度 / 缺主营 / soe / 行业先验矛盾 / 规则分类器分歧(--crosscheck)。

用法:
  export DEEPSEEK_API_KEY=sk-xxx        # key 从环境变量读, 不落盘
  python scripts/ai_model_classifier.py --status            # 看进度与统计
  python scripts/ai_model_classifier.py --fetch-inputs      # 预取东财F10输入(断点续传, 一次性)
  python scripts/ai_model_classifier.py --limit 10 --dry-run  # 预览将执行什么
  python scripts/ai_model_classifier.py --limit 50          # 试跑50只
  python scripts/ai_model_classifier.py --regression        # 46只watchlist金标准回归
  python scripts/ai_model_classifier.py --all               # 全市场(只跑缺分类的)
  python scripts/ai_model_classifier.py --codes 600887,000338            # 指定股票(补跑)
  python scripts/ai_model_classifier.py --refresh --stale-only           # 半年后刷新超龄/输入变化的
  python scripts/ai_model_classifier.py --refresh --codes 600887         # 强制重跑指定股票
  python scripts/ai_model_classifier.py --export-review                  # 导出待复核CSV
  python scripts/ai_model_classifier.py --import-review artifacts/model_classify_review.csv
  python scripts/ai_model_classifier.py --set 600887 staples             # 手工指定单只
  python scripts/ai_model_classifier.py --crosscheck        # 规则分类器交叉校验(财报缓存内)

API 配置: artifacts/.cache/ai_credentials.json (首次运行自动生成, profiles 可切换, chmod 600)
"""
import argparse
import concurrent.futures
import csv
import datetime
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import kline_store
from financial_fetcher import _cache_financial

MODEL_KEYS = {
    'staples': '必选消费', 'discretionary': '可选消费', 'tech': '科技制造',
    'cyclical': '周期资源', 'soe': '央企基建', 'bank': '银行保险',
    'realestate': '地产', 'pharma': '医药消费',
}
PROMPT_VERSION = '2026-09-c'
CONFIG_PATH = os.path.join(_SKILL_DIR, 'artifacts', '.cache', 'ai_credentials.json')
AI_OUT_DIR = os.path.join(_SKILL_DIR, 'artifacts', '.cache', 'ai_classify')
REVIEW_CSV_DEFAULT = os.path.join(_SKILL_DIR, 'artifacts', 'model_classify_review.csv')
STALE_MONTHS_DEFAULT = 6

DEFAULT_CONFIG = {
    '$doc': 'AI分类API配置。default=当前使用的profile名; --profile 可临时切换。'
            'api_format: openai|anthropic; api_key 从环境变量读取(api_key_env 指定变量名), '
            '也可直接写 api_key(不推荐)。json_mode: response_format|prompt。'
            'batch_size 每批股票数; concurrency 并发批数(DeepSeek为并发数限制: deepseek-flash'
            '上限2500/账号, 429时模块会全局暂停退避); max_retries 网络重试次数。',
    'default': 'deepseek',
    'profiles': {
        'deepseek': {
            'base_url': 'https://api.deepseek.com',
            'api_format': 'openai',
            'api_key_env': 'DEEPSEEK_API_KEY',
            'model': 'deepseek-flash',
            'temperature': 0.1,
            'max_tokens': 8192,
            'json_mode': 'response_format',
            'batch_size': 50,
            'concurrency': 3,
            'max_retries': 3,
        },
        'doubao': {
            'base_url': 'https://ark.cn-beijing.volces.com/api/v3',
            'api_format': 'openai',
            'api_key_env': 'ARK_API_KEY',
            'model': 'doubao-seed-1-6-250615',
            'temperature': 0.1,
            'max_tokens': 8192,
            'json_mode': 'response_format',
            'batch_size': 50,
            'concurrency': 3,
            'max_retries': 3,
        },
    },
}


# ===== 系统提示词（静态部分，逐批不变以命中上下文缓存；变量只放 user 消息） =====

SYSTEM_PROMPT = """你是A股估值系统的股票归类助手。任务：把每只股票归入8种估值模型之一，供后续按模型计分。只依据给定的公司资料判断，不得编造。

## 8种模型
- staples 必选消费：食品饮料、乳制品、调味品、日用品、文具、农业养殖。需求刚性、业绩稳定、现金流好。
- discretionary 可选消费：家电、汽车整车、服装、旅游酒店、零售、珠宝、免税、轻工。品牌溢价、受消费周期影响。
- tech 科技制造：半导体、电子元器件、PCB、光模块、存储、锂电池、光伏、风电、软件、通信设备。科技产业链、高研发投入、高增速。
- cyclical 周期资源：有色、煤炭、石油、钢铁、化工、建材、航运、黄金，以及强周期传统装备（重卡、柴油机、农机、工程机械）。盈利随商品价格/宏观周期大幅波动。
- soe 央企基建：建筑、基建、电力、交运、电信、水务、轨交装备。高股息、央国企背景、订单驱动、经营稳健。
- bank 银行保险：银行、保险、券商、信托、多元金融。
- realestate 地产：房地产开发、物业管理、园区。
- pharma 医药消费：化学制药、中药、生物制品、医疗器械、医疗服务、疫苗。

## 判定顺序（先特殊后一般）
1. 金融业态（银行/保险/券商/信托/期货/租赁/多元金融）→ bank
2. 房地产开发/物业服务 → realestate
3. 医药/医疗 → pharma
4. 盈利随商品价格或宏观周期大幅波动（资源/化工/航运/建材/传统装备等）→ cyclical
5. 日常必需消费 → staples；品牌/耐用可选消费 → discretionary
6. 科技产业链（新能源/半导体/电子/软件/通信）→ tech
7. 高股息、央国企背景、订单驱动的基建/电力/交运/建筑 → soe

## 反例规则（必须遵守）
- 周期行业但已央企化高股息（实控人为央企/国企、承诺高分红，如航运/煤炭/油运龙头）→ 优先 soe 而非 cyclical。
- soe 的语义=高股息的基建/电力/交运/建筑类央国企。科技类、制造类央企不因央企背景自动归 soe；盈利弱、少分红的"集团/控股"公司也不是 soe。
- 新能源产业链（锂电池、光伏、储能、风电）归 tech，不因制造业属性归 cyclical。
- 强周期传统装备（重卡、发动机、农机、工程机械，与地产/基建/货运周期绑定）→ cyclical 而非 tech。
- 汽车零部件区分：发动机/柴油机/重卡产业链 → cyclical；汽车电子与智能化零部件（车灯、制动、域控、内饰等成长升级逻辑）→ tech。
- 综合集团/多元业务 → 按收入占比最高的主业归类，confidence 给 low。
- 资料不足 → 照常给出最可能的 model，但 confidence 给 low，并在 reasons 里说明缺什么。

## 输入说明
每只股票一行：代码 | 名称 | 行业(东财三级) | 主营 | 实控人 | 上市日期。
行业与主营是首要依据；实控人含"国资/财政/中央汇金/部委"等为 soe 证据；实控人为"无"表示无实际控制人。

## 输出格式
只输出一个 JSON 对象，禁止输出任何其他文字或解释：
{"results": [{"code": "600887", "model": "staples", "confidence": "high", "reasons": ["乳制品龙头需求刚性", "实控人无国资背景"]}]}
- model 只能取: staples/discretionary/tech/cyclical/soe/bank/realestate/pharma
- confidence 只能取: high/medium/low
- reasons 为1~3条、每条不超过20字的中文短语
- 清单中每只股票必须且只能出现一次，code 原样返回"""

USER_INTRO = '请对以下股票逐一独立判断归类，只返回JSON：'


# ===== 配置 =====

def load_config():
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        os.chmod(CONFIG_PATH, 0o600)
        print(f'[config] 已生成默认配置 {CONFIG_PATH}')
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


def resolve_profile(config, profile_name=None):
    name = profile_name or os.environ.get('AI_PROFILE') or config.get('default')
    profiles = config.get('profiles', {})
    if name not in profiles:
        raise SystemExit(f'[config] profile "{name}" 不存在, 可选: {list(profiles)}')
    cfg = dict(profiles[name])
    cfg['profile'] = name
    key_env = cfg.get('api_key_env', 'API_KEY')
    api_key = os.environ.get(key_env) or cfg.get('api_key')
    if not api_key:
        print(f'[config] 警告: 未配置API key (export {key_env}=... 或写入配置), '
              f'仅本地命令可用(--status/--export-review等)')
    cfg['api_key'] = api_key or ''
    return cfg


# ===== AI API 客户端（OpenAI / Anthropic 双格式, 重试退避） =====

class ApiPermanentError(Exception):
    """不可重试（401/400参数错等）"""


class ApiRetryableError(Exception):
    """可重试（429/5xx/超时/空响应）"""


_RETRYABLE_HTTP = {408, 409, 425, 429, 500, 502, 503, 504, 524, 529}

# 限流说明（api-docs.deepseek.com/quick_start/rate_limit）: DeepSeek 采用并发数限制
# （deepseek-flash 上限2500并发/账号级，非TPM/RPM制），超限返回429、无 Retry-After 头。
# 本模块并发默认3远低于上限；任一线程收到429时做全局暂停，避免其他线程在限流窗口内继续施压。
_rate_lock = threading.Lock()
_pause_until = 0.0
_consecutive_429 = 0


def _respect_global_pause():
    while True:
        with _rate_lock:
            wait = _pause_until - time.time()
        if wait <= 0:
            return
        time.sleep(min(wait, 5))


def _note_response(is_429, pause_seconds=0.0):
    global _pause_until, _consecutive_429
    with _rate_lock:
        if is_429:
            _consecutive_429 += 1
            _pause_until = max(_pause_until, time.time() + pause_seconds)
        else:
            _consecutive_429 = 0


def call_ai(cfg, system, user, timeout=300):
    """调用一次AI，返回 (text, prompt_tokens, completion_tokens, cache_hit_tokens)。内部带重试退避。"""
    if not cfg.get('api_key'):
        raise ApiPermanentError('缺少API key: export 对应环境变量或写入 ai_credentials.json')
    api_format = cfg.get('api_format', 'openai')
    delay = 4.0
    last_err = None
    for attempt in range(int(cfg.get('max_retries', 3)) + 1):
        try:
            return _call_once(cfg, system, user, api_format, timeout)
        except ApiRetryableError as e:
            last_err = e
            if attempt < int(cfg.get('max_retries', 3)):
                sleep_s = delay + random.uniform(0, 2)
                print(f'  [api] 重试{attempt + 1}/{cfg.get("max_retries", 3)}: {e} ({sleep_s:.0f}s后)',
                      flush=True)
                time.sleep(sleep_s)
                delay *= 3
        except ApiPermanentError as e:
            raise
    raise ApiRetryableError(f'重试耗尽: {last_err}')


def _call_once(cfg, system, user, api_format, timeout):
    _respect_global_pause()
    if api_format == 'anthropic':
        url = cfg['base_url'].rstrip('/') + '/v1/messages'
        body = {
            'model': cfg['model'], 'system': system, 'max_tokens': cfg.get('max_tokens', 8192),
            'temperature': cfg.get('temperature', 0.1),
            'messages': [{'role': 'user', 'content': user}],
        }
        headers = {'x-api-key': cfg['api_key'], 'anthropic-version': '2023-06-01',
                   'content-type': 'application/json'}
    else:
        url = cfg['base_url'].rstrip('/') + '/chat/completions'
        body = {
            'model': cfg['model'],
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
            'temperature': cfg.get('temperature', 0.1),
            'max_tokens': cfg.get('max_tokens', 8192),
            'stream': False,
        }
        if cfg.get('json_mode', 'response_format') == 'response_format':
            body['response_format'] = {'type': 'json_object'}
        headers = {'Authorization': f'Bearer {cfg["api_key"]}', 'content-type': 'application/json'}

    req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        _note_response(False)
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:300]
        except Exception:
            pass
        if e.code == 429:
            ra = (e.headers or {}).get('Retry-After') if hasattr(e, 'headers') else None
            try:
                pause = float(ra)
            except (TypeError, ValueError):
                with _rate_lock:
                    n = _consecutive_429
                pause = min(10.0 * (2 ** min(n, 4)), 160.0)  # 无Retry-After: 指数全局退避
            _note_response(True, pause + 0.5)
            raise ApiRetryableError(f'HTTP 429 限流, 全局暂停{pause:.0f}s {detail}')
        if e.code in _RETRYABLE_HTTP:
            raise ApiRetryableError(f'HTTP {e.code} {detail}')
        raise ApiPermanentError(f'HTTP {e.code} {detail}')
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
        raise ApiRetryableError(f'网络/解码错误: {e}')

    if api_format == 'anthropic':
        text = ''.join(b.get('text', '') for b in payload.get('content', []))
        usage = payload.get('usage', {})
        ptok = usage.get('input_tokens', 0)
        ctok = usage.get('output_tokens', 0)
        chit = 0
    else:
        choices = payload.get('choices') or []
        if not choices:
            raise ApiRetryableError(f'空choices: {str(payload)[:200]}')
        text = choices[0].get('message', {}).get('content') or ''
        usage = payload.get('usage', {})
        ptok = usage.get('prompt_tokens', 0)
        ctok = usage.get('completion_tokens', 0)
        chit = usage.get('prompt_cache_hit_tokens', 0)  # DeepSeek 上下文缓存命中
    if not text.strip():
        raise ApiRetryableError('空响应(json mode偶发, 重试)')
    return text, ptok, ctok, chit


def parse_ai_json(text):
    t = text.strip()
    if t.startswith('```'):
        t = re.sub(r'^```[a-zA-Z]*\s*', '', t)
        t = re.sub(r'\s*```$', '', t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find('{'), t.rfind('}')
        if 0 <= i < j:
            return json.loads(t[i:j + 1])
        raise ValueError(f'JSON解析失败: {t[:200]}')


def validate_results(payload, expected_codes):
    """校验AI输出。返回 (valid: {code: (model, confidence, reasons_list)}, invalid_codes)"""
    valid, seen = {}, set()
    results = payload.get('results') if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return valid, list(expected_codes)
    for item in results:
        if not isinstance(item, dict):
            continue
        code = str(item.get('code', '')).strip()
        model = str(item.get('model', '')).strip()
        conf = str(item.get('confidence', '')).strip()
        if code not in expected_codes or code in seen:
            continue
        if model not in MODEL_KEYS or conf not in ('high', 'medium', 'low'):
            continue
        reasons = [str(r)[:40] for r in (item.get('reasons') or []) if str(r).strip()][:3]
        valid[code] = (model, conf, reasons)
        seen.add(code)
    invalid = [c for c in expected_codes if c not in valid]
    return valid, invalid


# ===== 输入构建（东财F10 orginfo, 经 fin_store 缓存30天TTL） =====

def _em_fetch_json(url, timeout=15):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8-sig'))


def _fetch_orginfo_uncached(stock_code, market):
    secucode = f'{stock_code}.{"SH" if market == "sh" else "SZ"}'
    url = ('https://datacenter.eastmoney.com/securities/api/data/v1/get'
           '?reportName=RPT_F10_BASIC_ORGINFO&columns=ALL'
           f'&filter=(SECUCODE=%22{secucode}%22)&source=HSF10&client=PC')
    try:
        data = _em_fetch_json(url)
    except Exception as e:
        print(f'  [orginfo] {stock_code} 请求失败: {e}', file=sys.stderr)
        return {'_empty': True}
    items = (data.get('result') or {}).get('data') if data.get('success') else None
    if not items:
        return {'_empty': True}
    it = items[0]
    business = (it.get('MAIN_BUSINESS') or '').strip()
    if not business:
        business = ((it.get('ORG_PROFILE') or '').strip())[:150]
    return {
        'industry': (it.get('EM2016') or it.get('BOARD_NAME_LEVEL')
                     or it.get('INDUSTRYCSRC1') or '').strip(),
        'business': business,
        'holder': (it.get('ACTUAL_HOLDER') or '').strip(),
        'profile': ((it.get('ORG_PROFILE') or '').strip())[:300],
    }


def get_orginfo(code, market):
    """返回 orginfo dict（fin_store 30天TTL缓存，空结果也缓存占位避免反复重拉）"""
    data = _cache_financial(code, 'orginfo', lambda: _fetch_orginfo_uncached(code, market))
    return data or {'_empty': True}


def ensure_inputs(stocks, polite=True):
    """stocks: stock_basic 行列表。返回 {code: input_dict}，缺缓存的先礼貌预取（断点续传）。"""
    inputs, missing = {}, []
    for s in stocks:
        cached = _cache_financial(s['code'], 'orginfo', lambda: None)
        if cached:
            inputs[s['code']] = cached
        else:
            missing.append(s)
    if missing and polite:
        print(f'[inputs] 需预取东财F10: {len(missing)}只 (断点续传, Ctrl+C可中断重跑续传)')
        t0, done = time.time(), 0
        for i, s in enumerate(missing):
            inputs[s['code']] = get_orginfo(s['code'], s['market'])
            done += 1
            time.sleep(random.uniform(0.15, 0.35))
            if done % 200 == 0:
                rate = done / max(time.time() - t0, 1)
                remain = (len(missing) - done) / max(rate, 0.01) / 60
                print(f'  [inputs] {done}/{len(missing)} ({rate:.1f}只/s, 预计还需{remain:.0f}分钟)',
                      flush=True)
        empties = [c for c, v in inputs.items() if v.get('_empty')]
        print(f'[inputs] 预取完成: 新取{done}, 其中空记录{len(empties)}只, 耗时{(time.time() - t0) / 60:.1f}分钟')
    for s in stocks:
        if s['code'] not in inputs:
            inputs[s['code']] = {'_empty': True}
    return inputs


def build_input_entry(stock, orginfo):
    industry = (orginfo.get('industry') or '').strip()
    business = (orginfo.get('business') or '').strip()
    holder = (orginfo.get('holder') or '').strip()
    return (f"{stock['code']} | {stock['name']} | 行业: {industry or '-'} | "
            f"主营: {business or '-'} | 实控人: {holder or '-'} | "
            f"上市: {stock.get('list_date') or '-'}")


def inputs_hash(name, orginfo, list_date):
    raw = json.dumps([name, orginfo.get('industry', ''), orginfo.get('business', ''),
                      orginfo.get('holder', ''), list_date], ensure_ascii=False)
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


# ===== 分类标记与复核触发 =====

def industry_hint(industry):
    """行业关键词先验（复用规则分类器的关键词表），无命中返回 ''"""
    from model_classifier import _KEYWORD_HINTS
    if not industry:
        return ''
    for model, kws in _KEYWORD_HINTS.items():
        if any(kw in industry for kw in kws):
            return model
    return ''


# 行业先验矛盾旗标只对这些模型生效：它们的行业关键词无歧义（金融/地产/医药/资源/食品）。
# tech/discretionary 的关键词过宽（"汽车""电子"覆盖机械与科技两族），矛盾旗标噪声大，只留给
# --crosscheck（有财务特征的规则分类器）去判。
_HINT_FLAG_MODELS = {'bank', 'realestate', 'pharma', 'cyclical', 'staples'}


def review_flags(industry, business, model, confidence, rule_model=''):
    flags = []
    # medium 不单独触发复核（全市场实测 deepseek-flash 对~40%股票给medium，且回归中
    # medium-无其他旗标的与人工标注全部一致；low 仍触发）
    if confidence == 'low':
        flags.append('置信度low')
    if not business:
        flags.append('缺主营描述')
    if model == 'soe':
        flags.append('soe需财务证据复核')
    hint = industry_hint(industry)
    if hint in _HINT_FLAG_MODELS and hint != model:
        flags.append(f'行业先验指向{hint}')
    if rule_model and rule_model != model:
        flags.append(f'规则分类={rule_model}')
    return flags


# ===== 目标选择 =====

def select_targets(args, stocks_universe, existing):
    """返回 pending 的 stock_basic 行列表。existing: {code: row}"""
    uni = [s for s in stocks_universe if s.get('status') == 'listed']
    uni_map = {s['code']: s for s in uni}

    if args.codes:
        wanted = [c.strip() for c in args.codes.split(',') if c.strip()]
        unknown = [c for c in wanted if c not in uni_map]
        if unknown:
            print(f'[warn] 不在宇宙中(退市/代码错): {unknown}')
        targets = [uni_map[c] for c in wanted if c in uni_map]
    else:
        targets = uni

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=int(args.stale_months) * 30)

    def _is_stale(row):
        try:
            return (row.get('classified_at') or '')[:10] < cutoff.isoformat()
        except Exception:
            return True

    def _keep(code):
        row = existing.get(code)
        if row is None:
            return True  # 未分类 → 待办
        if row.get('source') == 'manual':
            return False  # 人工结果永不自动覆盖
        if args.refresh:
            if args.stale_only:
                return _is_stale(row)
            return True
        return False  # 默认只补缺

    pending = [s for s in targets if _keep(s['code'])]
    if args.limit and args.limit > 0:
        pending = pending[:args.limit]
    return pending, uni_map


# ===== 批量分类执行 =====

def _build_rows(batch, orginfos, judged, batch_id, cfg):
    rows = []
    for s in batch:
        code = s['code']
        org = orginfos.get(code, {})
        info = {'industry': (org.get('industry') or '').strip(),
                'business': (org.get('business') or '').strip(),
                'holder': (org.get('holder') or '').strip()}
        if code not in judged:
            continue
        model, conf, reasons = judged[code]
        flags = review_flags(info['industry'], info['business'], model, conf)
        rows.append({
            'code': code, 'name': s['name'], 'model': model, 'confidence': conf,
            'reasons': json.dumps(reasons, ensure_ascii=False), 'source': 'ai',
            'needs_review': 1 if flags else 0, 'review_done': 0,
            'review_note': ';'.join(flags), 'rule_model': '',
            'industry': info['industry'], 'business': info['business'],
            'holder': info['holder'], 'list_date': s.get('list_date') or '',
            'inputs_hash': inputs_hash(s['name'], org, s.get('list_date') or ''),
            'batch_id': batch_id, 'ai_engine': cfg['model'],
            'prompt_version': PROMPT_VERSION,
            'classified_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })
    return rows


def run_classify(pending, uni_map, cfg, args, skip_upsert_codes=None):
    """批量分类。返回 (rows_all, failed_list, token_stats)。
    skip_upsert_codes: 这些代码的AI结果只参与对比报告、不入库（保护 source=manual 行）。"""
    skip_upsert_codes = skip_upsert_codes or set()
    batch_size = int(args.batch_size or cfg.get('batch_size', 50))
    conc = int(args.concurrency or cfg.get('concurrency', 3))
    print(f'[classify] 引擎={cfg["profile"]}/{cfg["model"]} 提示词v={PROMPT_VERSION} '
          f'批大小={batch_size} 并发={conc}')
    orginfos = ensure_inputs(pending)

    with_inputs = [s for s in pending]
    batches = [with_inputs[i:i + batch_size] for i in range(0, len(with_inputs), batch_size)]
    print(f'[classify] 待分类 {len(pending)} 只 → {len(batches)} 批')

    ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    rows_all, failed = [], []
    token = {'prompt': 0, 'completion': 0, 'cache_hit': 0}
    lock = threading.Lock()
    dry_run = args.dry_run

    def _call_and_judge(batch_stocks, batch_id):
        user_msg = USER_INTRO + '\n' + '\n'.join(
            f'{i + 1}. {build_input_entry(s, orginfos.get(s["code"], {}))}'
            for i, s in enumerate(batch_stocks))
        codes = {s['code'] for s in batch_stocks}

        def _archive(tag, text):
            try:
                raw_dir = os.path.join(AI_OUT_DIR, 'raw')
                os.makedirs(raw_dir, exist_ok=True)
                with open(os.path.join(raw_dir, f'{tag}.txt'), 'w', encoding='utf-8') as f:
                    f.write(text)
            except OSError:
                pass

        def _batch_call():
            nonlocal ptok, ctok, chit
            text, p, c, h = call_ai(cfg, SYSTEM_PROMPT, user_msg)
            ptok, ctok, chit = ptok + p, ctok + c, chit + h
            _archive(batch_id, text)
            return validate_results(parse_ai_json(text), codes)

        ptok = ctok = chit = 0
        try:
            valid, invalid = _batch_call()
        except Exception as e:
            # 整批JSON不合法/网络失败 → 重试一次批调用, 仍失败则全部单只补跑
            print(f'  [classify] 批{batch_id} 整批解析失败({e}), 重试一次', file=sys.stderr)
            try:
                valid, invalid = _batch_call()
            except Exception as e2:
                print(f'  [classify] 批{batch_id} 重试仍失败({e2}), 降级为单只补跑', file=sys.stderr)
                valid, invalid = {}, sorted(codes)
        # 失败股票单只补跑（仍走同一静态提示词, 缓存命中）
        for code in list(invalid):
            one = [s for s in batch_stocks if s['code'] == code]
            try:
                text1, p1, c1, h1 = call_ai(cfg, SYSTEM_PROMPT, USER_INTRO + '\n' + build_input_entry(
                    one[0], orginfos.get(code, {})))
                ptok, ctok, chit = ptok + p1, ctok + c1, chit + h1
                _archive(f'{batch_id}_{code}', text1)
                v1, inv1 = validate_results(parse_ai_json(text1), {code})
                valid.update(v1)
                invalid = [c for c in invalid if c in inv1]
            except Exception as e:
                print(f'  [classify] {code} 单只补跑失败: {e}', file=sys.stderr)
        return valid, invalid, ptok, ctok, chit

    def work(item):
        idx, batch = item
        if not batch:
            return 0, 0
        batch_id = f'{ts}-{idx:03d}'
        if dry_run:
            preview = build_input_entry(batch[0], orginfos.get(batch[0]['code'], {}))
            print(f'  [dry-run] 批{batch_id}: {len(batch)}只, 首条: {preview}')
            return 0, 0
        kline_store.classify_batch_mark(batch_id, 'running',
                                        codes=[s['code'] for s in batch], n=len(batch))
        try:
            valid, invalid, ptok, ctok, cache_hit = _call_and_judge(batch, batch_id)
            rows = _build_rows(batch, orginfos, valid, batch_id, cfg)
            to_save = [r for r in rows if r['code'] not in skip_upsert_codes]
            if to_save:
                kline_store.model_classify_upsert(to_save)
            skipped = len(rows) - len(to_save)
            if skipped:
                print(f'  [classify] 批{batch_id}: {skipped}只为manual行, 仅对比不入库')
            status = 'done' if not invalid else 'partial'
            kline_store.classify_batch_mark(batch_id, status, n=len(batch),
                                            prompt_tokens=ptok, completion_tokens=ctok)
            fail_items = [(s['code'], f'批{batch_id}未返回/校验失败') for s in batch
                          if s['code'] in invalid]
            with lock:
                token['prompt'] += ptok
                token['completion'] += ctok
                token['cache_hit'] += cache_hit
                rows_all.extend(rows)
                failed.extend(fail_items)
            return len(rows), len(fail_items)
        except Exception as e:
            kline_store.classify_batch_mark(batch_id, 'failed', error=str(e)[:500])
            print(f'  [classify] 批{batch_id} 失败: {e}', file=sys.stderr)
            return 0, len(batch)

    if dry_run:
        work((0, batches[0]) if batches else (0, []))
        print(f'[dry-run] 共{len(batches)}批, 仅预览以上1批, 未调用API未写库')
        return [], [], token

    done_n, fail_n = 0, 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=conc) as ex:
        for i, (r, f) in enumerate(ex.map(work, enumerate(batches))):
            done_n += r
            fail_n += f
            if (i + 1) % 5 == 0 or i + 1 == len(batches):
                print(f'  [classify] 批进度 {i + 1}/{len(batches)}, 已入库{done_n}只, '
                      f'失败{fail_n}只, tokens p{token["prompt"]}/c{token["completion"]}', flush=True)

    classified_codes = {r['code'] for r in rows_all}
    failed = [(code, err) for (code, err) in failed if code not in classified_codes]
    return rows_all, failed, token


# ===== watchlist / 复核 CSV =====

def load_watchlist_models(path=None):
    path = path or os.path.join(_SKILL_DIR, 'watchlist.txt')
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('名称') or ',' not in line:
                continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) >= 3 and parts[1].isdigit() and parts[2] in MODEL_KEYS:
                out[parts[1]] = {'model': parts[2], 'name': parts[0]}
    return out


# ===== 子命令 =====

def cmd_fetch_inputs(args, cfg):
    uni = [s for s in kline_store.stock_basic_all(excluded=False) if s.get('status') == 'listed']
    print(f'[fetch-inputs] 宇宙 {len(uni)} 只')
    ensure_inputs(uni)
    print('[fetch-inputs] 完成')


def cmd_classify(args, cfg):
    existing = kline_store.model_classify_get()
    uni_all = kline_store.stock_basic_all(excluded=False)
    pending, uni_map = select_targets(args, uni_all, existing)
    print(f'[classify] 宇宙{len(uni_map)} 已分类{len(existing)} → 本次待跑 {len(pending)} 只')
    if not pending:
        print('无事可做（如需重跑已分类的用 --refresh）')
        return
    rows_all, failed, token = run_classify(pending, uni_map, cfg, args)
    if failed and not args.dry_run:
        os.makedirs(AI_OUT_DIR, exist_ok=True)
        fp = os.path.join(AI_OUT_DIR, 'failed.jsonl')
        with open(fp, 'a', encoding='utf-8') as f:
            for code, err in failed:
                f.write(json.dumps({'code': code, 'error': err,
                                    'at': datetime.datetime.now().isoformat(timespec='seconds')},
                                   ensure_ascii=False) + '\n')
        print(f'[classify] 失败{len(failed)}只已记录: {fp} (下次 --all 自动补跑)')
    print(f'[classify] 完成: 入库{len(rows_all)}, 失败{len(failed)}, '
          f'tokens 输入{token["prompt"]}(缓存命中{token["cache_hit"]})/输出{token["completion"]}')


def cmd_regression(args, cfg):
    """金标准回归: watchlist.txt 46只人工标注 vs AI分类, 一致率≥90%过关"""
    wl = load_watchlist_models(args.watchlist)
    if not wl:
        raise SystemExit('[regression] watchlist.txt 无可用的 代码,模型 标注')
    existing = kline_store.model_classify_get()
    uni_all = kline_store.stock_basic_all(excluded=False)
    uni_map = {s['code']: s for s in uni_all}
    # 回归总是重新跑AI判断（manual保护仍生效: manual行只对比不入库覆盖）
    targets = [uni_map[c] for c in wl if c in uni_map]
    missing = [c for c in wl if c not in uni_map]
    if missing:
        print(f'[regression] watchlist中不在宇宙: {missing}')
    print(f'[regression] 金标准 {len(targets)} 只, 开始AI分类...')
    args_copy = argparse.Namespace(**{**vars(args), 'codes': None, 'refresh': True,
                                      'stale_only': False, 'limit': 0, 'dry_run': False})
    manual_codes = {c for c, r in existing.items() if r.get('source') == 'manual'}
    rows_all, failed, token = run_classify(targets, uni_map, cfg, args_copy,
                                           skip_upsert_codes=manual_codes)

    ai_map = {r['code']: r for r in rows_all}
    agree, diff = 0, []
    for code, info in wl.items():
        row = ai_map.get(code)
        if row is None:
            diff.append((code, info['name'], info['model'], '失败/无结果', '-', ''))
            continue
        if row['model'] == info['model']:
            agree += 1
        else:
            flags = row.get('review_note', '')
            diff.append((code, info['name'], info['model'], row['model'],
                         row['confidence'], flags))
    n = len(targets)
    rate = agree / n * 100 if n else 0
    print(f'\n===== 金标准回归结果: 一致 {agree}/{n} = {rate:.1f}% (过关线90%) =====')
    if diff:
        print(f'{"代码":<8}{"名称":<10}{"watchlist":<14}{"AI":<14}{"置信度":<8}标记')
        print('-' * 80)
        for code, name, wl_model, ai_model, conf, flags in diff:
            print(f'{code:<8}{name:<10}{wl_model:<14}{ai_model:<14}{conf:<8}{flags}')
    verdict = 'PASS ✅ 可全量跑' if rate >= 90 else 'FAIL ❌ 先看分歧改提示词(PROMPT_VERSION+1)再回归'
    print(f'结论: {verdict}')


def cmd_status(args, cfg):
    existing = kline_store.model_classify_get()
    uni = [s for s in kline_store.stock_basic_all(excluded=False) if s.get('status') == 'listed']
    today = datetime.date.today()
    cutoff = (today - datetime.timedelta(days=int(args.stale_months) * 30)).isoformat()

    by_model, by_conf, by_source, review_pending, stale = {}, {}, {}, [], []
    for row in existing.values():
        by_model[row['model']] = by_model.get(row['model'], 0) + 1
        by_conf[row['confidence']] = by_conf.get(row['confidence'], 0) + 1
        by_source[row['source']] = by_source.get(row['source'], 0) + 1
        if not row['review_done'] and row['needs_review']:
            review_pending.append(row)
        if (row['classified_at'] or '')[:10] < cutoff:
            stale.append(row)

    print(f'===== AI模型分类状态 =====')
    print(f'宇宙: {len(uni)} 只 | 已分类: {len(existing)} '
          f'({", ".join(f"{k}={v}" for k, v in sorted(by_source.items())) or "无"}) '
          f'| 覆盖率: {len(existing) / len(uni) * 100:.1f}%')
    print(f'按模型: ' + ', '.join(f'{k}({MODEL_KEYS.get(k, "?")})={v}'
                                  for k, v in sorted(by_model.items(), key=lambda x: -x[1])))
    print(f'按置信度: ' + ', '.join(f'{k}={v}' for k, v in sorted(by_conf.items())))
    print(f'待复核(review_done=0): {len(review_pending)} 只 | 超龄(>{args.stale_months}月): {len(stale)} 只')
    if review_pending[:20]:
        print('待复核样例(前20): ')
        for r in review_pending[:20]:
            print(f'  {r["code"]} {r["name"]:<10} {r["model"]:<13} {r["review_note"]}')
    batches = kline_store.classify_batch_all()
    if batches:
        print(f'最近批次(末5): 批次/状态/数量/尝试/tokens')
        for b in batches[-5:]:
            print(f'  {b["batch_id"]} {b["status"]:<8} n={b["n"]} att={b["attempts"]} '
                  f'p{b["prompt_tokens"]}/c{b["completion_tokens"]} {b["finished_at"] or ""}')
    total_tok = sum(b['prompt_tokens'] or 0 for b in batches), sum(b['completion_tokens'] or 0 for b in batches)
    print(f'累计tokens: 输入{total_tok[0]} / 输出{total_tok[1]}')


def cmd_export_review(args, cfg):
    path = args.export_review if isinstance(args.export_review, str) else REVIEW_CSV_DEFAULT
    existing = kline_store.model_classify_get()
    rows = [r for r in existing.values() if r['needs_review'] and not r['review_done']]
    rows.sort(key=lambda r: r['code'])
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['code', 'name', 'industry', 'business', 'holder', 'ai_model', 'confidence',
                    'reasons', 'review_note', 'review_model', 'review_note_manual'])
        for r in rows:
            w.writerow([r['code'], r['name'], r['industry'], r['business'], r['holder'],
                        r['model'], r['confidence'],
                        ' / '.join(json.loads(r['reasons'] or '[]')), r['review_note'], '', ''])
    print(f'[export] 待复核 {len(rows)} 只 → {path}')
    print('  填 review_model 列(8模型key之一)后 --import-review 导入; '
          '留空=维持AI结果并标记已复核')


def cmd_import_review(args, cfg):
    path = args.import_review
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    override, keep, bad = 0, 0, []
    for r in rows:
        code = (r.get('code') or '').strip()
        if not code:
            continue
        rev = (r.get('review_model') or '').strip()
        old = kline_store.model_classify_get(code)
        if old is None:
            bad.append(code)
            continue
        if rev:
            if rev not in MODEL_KEYS:
                bad.append(code)
                continue
            old.update({'model': rev, 'source': 'manual', 'needs_review': 0,
                        'review_done': 1, 'confidence': 'high',
                        'ai_engine': 'human', 'prompt_version': 'manual',
                        'classified_at': now, 'review_note': r.get('review_note_manual', '')})
            override += 1
        else:
            old.update({'review_done': 1,
                        'review_note': (old['review_note'] + '|人工维持AI').strip('|')})
            keep += 1
        kline_store.model_classify_upsert([old])
    print(f'[import] 共{len(rows)}行: 人工改判{override}, 维持AI{keep}, 无效{len(bad)} {bad[:10]}')


def cmd_set(args, cfg):
    code, model = args.set[0], args.set[1]
    if model not in MODEL_KEYS:
        raise SystemExit(f'[set] model 必须是 {list(MODEL_KEYS)}')
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    old = kline_store.model_classify_get(code) or {}
    uni = {s['code']: s for s in kline_store.stock_basic_all(excluded=False)}
    s = uni.get(code, {'code': code, 'name': old.get('name', ''), 'market': 'sh',
                       'list_date': ''})
    org = get_orginfo(code, s['market'])
    row = {
        'code': code, 'name': s.get('name') or old.get('name', ''), 'model': model,
        'confidence': 'high', 'reasons': json.dumps(['人工指定'], ensure_ascii=False),
        'source': 'manual', 'needs_review': 0, 'review_done': 1,
        'review_note': f'原{old.get("model", "")}→{model}' if old.get('model') else '',
        'industry': (org.get('industry') or '').strip(),
        'business': (org.get('business') or '').strip(),
        'holder': (org.get('holder') or '').strip(),
        'list_date': s.get('list_date') or '',
        'inputs_hash': inputs_hash(s.get('name', ''), org, s.get('list_date') or ''),
        'ai_engine': 'human', 'prompt_version': 'manual', 'classified_at': now,
    }
    kline_store.model_classify_upsert([row])
    print(f'[set] {code} {row["name"]} → {model}({MODEL_KEYS[model]}) source=manual')


def cmd_crosscheck(args, cfg):
    """规则分类器交叉校验（只用已缓存数据: fin_store财报缓存 + score_factors因子 + 行业关键词;
    不联网拉财报）。分歧旗标只在规则分类器自评 confidence='high' 时触发——低置信度的规则
    结果对多主业公司常为噪声（2026-09-13 全市场实测：不设门槛时分歧率43%多为假阳性，
    如无realestate规则时万科被判bank）。已复核行(review_done=1)跳过。"""
    from model_classifier import classify_model
    from financial_fetcher import compute_financial_metrics
    import fin_store

    existing = kline_store.model_classify_get()
    targets = {c: r for c, r in existing.items()
               if r['source'] in ('ai', 'rule') and not r['review_done']}
    print(f'[crosscheck] 待校验 {len(targets)} 只（manual/已复核跳过）')
    agree, gated, disagree, no_data = 0, 0, [], 0
    for i, (code, row) in enumerate(sorted(targets.items())):
        rule_model, rule_conf = '', ''
        try:
            reps = fin_store.load_reports(code)
        except Exception:
            reps = []
        if reps:
            try:
                sf = kline_store.score_factors_get(code) or {}
                res = classify_model(compute_financial_metrics(reps), row['industry'],
                                     rd_ratio=sf.get('rd_ratio'),
                                     dividend_yield=sf.get('div_yield'))
                rule_model, rule_conf = res['model'], res.get('confidence', '')
            except Exception:
                pass
        if not rule_model:
            no_data += 1
            rule_model = industry_hint(row['industry'])
            rule_conf = 'high' if rule_model in _HINT_FLAG_MODELS else ''
        # 高置信规则结果无论一致与否都存 rule_model（审计可查一致率）；仅分歧时触发旗标
        effective = rule_model if rule_conf == 'high' else ''
        if not rule_model:
            pass
        elif effective and rule_model != row['model']:
            disagree.append((code, row['name'], row['model'], rule_model))
        elif effective:
            agree += 1
        else:
            gated += 1
        new_flags = review_flags(row['industry'], row['business'], row['model'],
                                 row['confidence'], effective)
        new_note = ';'.join(new_flags)
        if new_note != (row['review_note'] or '') or (row['rule_model'] or '') != effective:
            row.update({'rule_model': effective, 'needs_review': 1 if new_flags else 0,
                        'review_note': new_note})
            kline_store.model_classify_upsert([row])
        if (i + 1) % 500 == 0:
            print(f'  [crosscheck] 进度 {i + 1}/{len(targets)}', flush=True)
    print(f'[crosscheck] 完成: 高置信一致{agree} | 高置信分歧{len(disagree)}(已标needs_review) | '
          f'低置信规则结果不触发旗标{gated} | 无财报仅行业先验{no_data}')
    if disagree:
        print('分歧样例(前30): 代码 名称 AI模型 规则模型')
        for code, name, m, rm in disagree[:30]:
            print(f'  {code} {name:<10} {m:<13} {rm}')


def cmd_recompute_flags(args, cfg):
    """按当前 review_flags 规则重算全部 ai 行的 needs_review/review_note（不动 review_done）。
    用于旗标规则校准后对存量结果生效，无需重新调用AI。"""
    existing = kline_store.model_classify_get()
    targets = {c: r for c, r in existing.items() if r['source'] == 'ai'}
    n_changed = 0
    for row in targets.values():
        old_note = row['review_note']
        flags = review_flags(row['industry'], row['business'], row['model'],
                             row['confidence'], row['rule_model'])
        row['needs_review'] = 1 if flags else 0
        row['review_note'] = ';'.join(flags)
        if row['review_note'] != old_note:
            kline_store.model_classify_upsert([row])
            n_changed += 1
    still = sum(1 for r in targets.values() if r['needs_review'])
    print(f'[recompute] {len(targets)}只重算: 写入变化{n_changed}条, 待复核{still}只')


def resolve_model(code, watchlist_path=None):
    """统一读取接口: watchlist.txt手工标注 > DB manual > DB ai/rule > None。
    全市场扫描/汇总消费分类结果时用本函数。"""
    wl = load_watchlist_models(watchlist_path)
    if code in wl:
        return {'model': wl[code]['model'], 'source': 'watchlist'}
    row = kline_store.model_classify_get(code)
    if row and row.get('model'):
        return {'model': row['model'], 'source': row['source']}
    return None


# ===== main =====

def main():
    ap = argparse.ArgumentParser(description='全市场AI模型分类器')
    ap.add_argument('--profile', help='API配置profile名(默认取配置文件default)')
    ap.add_argument('--all', action='store_true', help='全市场, 只跑缺分类的')
    ap.add_argument('--codes', help='逗号分隔的股票代码(与--all二选一)')
    ap.add_argument('--limit', type=int, default=0, help='本次最多跑N只(按代码序截断)')
    ap.add_argument('--batch-size', type=int, default=0, help='每批股票数(默认取profile配置)')
    ap.add_argument('--concurrency', type=int, default=0, help='并发批数(默认取profile配置)')
    ap.add_argument('--refresh', action='store_true', help='重跑已分类的(manual除外)')
    ap.add_argument('--stale-only', action='store_true', help='配合--refresh: 只重跑超龄/输入变化的')
    ap.add_argument('--stale-months', type=int, default=STALE_MONTHS_DEFAULT,
                    help=f'超龄阈值月数(默认{STALE_MONTHS_DEFAULT})')
    ap.add_argument('--dry-run', action='store_true', help='只预览不调用API不写库')
    ap.add_argument('--fetch-inputs', action='store_true', help='只预取东财F10输入不分类')
    ap.add_argument('--regression', action='store_true', help='watchlist金标准回归(46只)')
    ap.add_argument('--watchlist', help='金标准watchlist路径(默认 watchlist.txt)')
    ap.add_argument('--status', action='store_true', help='查看分类进度统计')
    ap.add_argument('--export-review', nargs='?', const=REVIEW_CSV_DEFAULT,
                    help='导出待复核CSV(默认 artifacts/model_classify_review.csv)')
    ap.add_argument('--import-review', metavar='CSV', help='导入复核CSV')
    ap.add_argument('--set', nargs=2, metavar=('CODE', 'MODEL'), help='手工指定单只模型')
    ap.add_argument('--crosscheck', action='store_true', help='规则分类器交叉校验')
    ap.add_argument('--recompute-flags', action='store_true',
                    help='按当前规则重算存量ai行的needs_review(不调用AI)')
    args = ap.parse_args()

    config = load_config()
    cfg = resolve_profile(config, args.profile)

    if args.status:
        cmd_status(args, cfg)
    elif args.fetch_inputs:
        cmd_fetch_inputs(args, cfg)
    elif args.regression:
        cmd_regression(args, cfg)
    elif args.export_review:
        cmd_export_review(args, cfg)
    elif args.import_review:
        cmd_import_review(args, cfg)
    elif args.set:
        cmd_set(args, cfg)
    elif args.crosscheck:
        cmd_crosscheck(args, cfg)
    elif args.recompute_flags:
        cmd_recompute_flags(args, cfg)
    elif args.all or args.codes or args.limit:
        cmd_classify(args, cfg)
    else:
        ap.print_help()


if __name__ == '__main__':
    main()
