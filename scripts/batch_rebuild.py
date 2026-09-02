#!/usr/bin/env python3
"""
批量重建 watchlist 估值报告（watchlist.txt 驱动，逐只调用 build_report.py）

用法:
  python scripts/batch_rebuild.py                          # 全量
  python scripts/batch_rebuild.py --stocks 601799,600887   # 指定代码子集
  python scripts/batch_rebuild.py --model tech,cyclical    # 按模型过滤
  python scripts/batch_rebuild.py --dry-run                # 只打印将执行的命令
  python scripts/batch_rebuild.py --retry                  # 只重跑上次失败的股票
  python scripts/batch_rebuild.py --summary                # 完成后刷新估值汇总筛选.html
"""
import argparse
import os
import subprocess
import sys
import io

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _SCRIPT_DIR)

from scan_watchlist import load_watchlist

_FAILED_LIST = os.path.join(_SKILL_DIR, 'artifacts', '.cache', 'batch_failed.txt')


def _sync_watchlist_dates(path, codes):
    """批量成功后把 watchlist 最后报告时间列更新为今天（保持 CSV 四列语义）"""
    import csv
    import datetime
    if not codes:
        return
    today = datetime.date.today().strftime('%Y-%m-%d')
    with open(path, encoding='utf-8', newline='') as f:
        rows_csv = list(csv.reader(f))
    changed = 0
    for r in rows_csv[1:]:
        if len(r) >= 4 and r[1] in codes and r[3] != today:
            r[3] = today
            changed += 1
    if changed:
        with open(path, 'w', encoding='utf-8', newline='') as f:
            csv.writer(f).writerows(rows_csv)
        print(f'watchlist 最后报告时间列同步: {changed} 只 → {today}', flush=True)


# ===== 股票池解析复用 scan_watchlist.load_watchlist =====


def main():
    parser = argparse.ArgumentParser(
        description='批量重建估值报告（watchlist.txt 驱动）',
        epilog='示例: python scripts/batch_rebuild.py --model tech --summary')
    parser.add_argument('--stocks', help='逗号分隔股票代码子集，如 601799,600887')
    parser.add_argument('--model', help='逗号分隔模型过滤，如 tech,cyclical')
    parser.add_argument('--dry-run', action='store_true', help='只打印将执行的命令，不实际生成')
    parser.add_argument('--retry', action='store_true',
                        help='只重跑上次失败的股票（artifacts/.cache/batch_failed.txt）')
    parser.add_argument('--summary', action='store_true', help='完成后刷新估值汇总筛选.html')
    args = parser.parse_args()

    if not sys.stdout.isatty():
        # 管道/重定向场景统一 UTF-8（配合 PowerShell Console 编码）；控制台直出走 console API 不需要
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    watchlist_path = os.path.join(_SKILL_DIR, 'watchlist.txt')
    if not os.path.exists(watchlist_path):
        print('错误: watchlist.txt 不存在')
        sys.exit(1)
    rows = load_watchlist(watchlist_path)
    if not rows:
        print('错误: watchlist.txt 为空或无法解析')
        sys.exit(1)

    if args.retry:
        if not os.path.exists(_FAILED_LIST):
            print('没有上次失败清单，无需重跑')
            sys.exit(0)
        with open(_FAILED_LIST, encoding='utf-8') as f:
            retry_codes = {line.strip().split(',')[1] for line in f if ',' in line.strip()}
        rows = [r for r in rows if r[1] in retry_codes]

    if args.stocks:
        codes = {c.strip() for c in args.stocks.split(',') if c.strip()}
        rows = [r for r in rows if r[1] in codes]
    if args.model:
        models = {m.strip() for m in args.model.split(',') if m.strip()}
        rows = [r for r in rows if r[2] in models]

    if not rows:
        print('过滤后无匹配股票')
        sys.exit(0)

    print(f'共 {len(rows)} 只待重建', flush=True)
    failed = []
    for i, (name, code, model) in enumerate(rows, 1):
        cmd = [sys.executable, os.path.join(_SCRIPT_DIR, 'build_report.py'), code, '--model', model]
        print(f'\n[{i}/{len(rows)}] {name} {code} {model}', flush=True)
        if args.dry_run:
            print('  ' + ' '.join(cmd), flush=True)
            continue
        r = subprocess.run(cmd, cwd=_SKILL_DIR, capture_output=True,
                           text=True, encoding='utf-8', errors='replace',
                           env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        # 只透出关键行：警告、自动填充、评分、输出路径
        for t in (r.stdout or '').strip().splitlines():
            if any(k in t for k in ('[警告]', '[auto]', '评分:', '->')):
                print('  ' + t, flush=True)
        if r.returncode != 0:
            failed.append((name, code))
            tail = (r.stderr or '').strip().splitlines()[-1:]
            print('  STDERR: ' + (tail[0] if tail else '?'), flush=True)

    if args.dry_run:
        print('\nDRY-RUN 结束，未生成报告')
        return

    print('\n===== 汇总 =====', flush=True)
    print(f'成功 {len(rows) - len(failed)} / {len(rows)}', flush=True)
    os.makedirs(os.path.dirname(_FAILED_LIST), exist_ok=True)
    if failed:
        with open(_FAILED_LIST, 'w', encoding='utf-8') as f:
            for n, c in failed:
                f.write(f'{n},{c}\n')
        print('失败: ' + ', '.join(f'{n}({c})' for n, c in failed), flush=True)
        print(f'失败清单已写入 {_FAILED_LIST}，可用 --retry 重跑', flush=True)
    elif os.path.exists(_FAILED_LIST):
        os.remove(_FAILED_LIST)

    # 同步 watchlist 最后报告时间列（成功股票 → 今天）
    ok_codes = {c for _, c, _ in rows} - {c for _, c in failed}
    _sync_watchlist_dates(watchlist_path, ok_codes)

    if args.summary:
        print('\n刷新汇总报告...', flush=True)
        subprocess.run([sys.executable, os.path.join(_SCRIPT_DIR, 'summary_report.py')],
                       cwd=_SKILL_DIR)


if __name__ == '__main__':
    main()
