"""竞价轨迹与 AI 意图推断的证据、留档和后续价格复核；不产生交易许可。"""
from __future__ import annotations

import datetime as dt
from contextlib import closing
import math
from pathlib import Path
import re
import sqlite3
import json

import auction_tracking as tracking
import data_paths

INTENTS = frozenset({'accumulation', 'distribution', 'support', 'withdrawal', 'unclear'})
DIRECTIONS = frozenset({'up', 'down', 'flat', 'unclear'})
MAX_STOCKS = 20
MAX_JUDGMENTS = 8


def _time(value):
    return tracking._observed_at(value)


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _json(value, default):
    try:
        return json.loads(value) if isinstance(value, str) else (value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _selection_codes(now):
    path = Path(data_paths.data_path('selection_tracking.db'))
    if not path.is_file():
        return set()
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
            conn.execute('PRAGMA query_only=ON')
            rows = conn.execute('SELECT p.code,r.created_at FROM paper_selection_picks p JOIN paper_selection_runs r ON r.id=p.run_id WHERE r.trade_date=? ORDER BY p.rank_no LIMIT 100', (now.date().isoformat(),)).fetchall()
        return {str(code) for code, stamp in rows if _time(stamp) is not None and _time(stamp) <= now}
    except sqlite3.Error:
        return set()


def _current_quotes(rows, now):
    result = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get('code') or '')
        stamp = _time(row.get('quote_at'))
        price = _number(row.get('price'))
        if (re.fullmatch(r'\d{6}', code) and stamp is not None and stamp.date() == now.date()
                and 0 <= (now-stamp).total_seconds() <= 240 and price is not None and price > 0):
            if code not in result or stamp > _time(result[code]['quote_at']):
                result[code] = {'price': price, 'quote_at': stamp.isoformat(), 'source': row.get('source')}
    return result


def feedback(conn, quotes, now):
    """复核的是本次可见价格相对预测基准的方向，不宣称知道真实主力意图。"""
    try:
        rows = conn.execute('SELECT id,finished_at,reviewers FROM dual_ai_tuning_runs ORDER BY id DESC LIMIT 20').fetchall()
    except sqlite3.Error:
        return []
    result = []
    for run_id, finished_at, raw in rows:
        issued = _time(finished_at)
        if issued is None or issued > now or issued.date() != now.date():
            continue
        reviewers = _json(raw, {})
        if not isinstance(reviewers, dict):
            continue
        for slot in ('ai1', 'ai2'):
            reviewer = reviewers.get(slot) or {}
            judgments = reviewer.get('auction_intents', []) if isinstance(reviewer, dict) else []
            for judgment in judgments[:MAX_JUDGMENTS] if isinstance(judgments, list) else []:
                if not isinstance(judgment, dict):
                    continue
                code = str(judgment.get('code') or '')
                reference = judgment.get('reference_quote') or {}
                base = _number(reference.get('price')) if isinstance(reference, dict) else None
                base_at = _time(reference.get('quote_at')) if isinstance(reference, dict) else None
                quote = quotes.get(code)
                verdict, change = 'pending', None
                # 模型回答前已经发生的行情不能拿来证明预测正确；过期基准也不参与。
                if (base is not None and base > 0 and base_at is not None
                        and 0 <= (issued-base_at).total_seconds() <= 240
                        and quote and _time(quote['quote_at']) > issued):
                    change = (quote['price']/base-1)*100
                    expected = judgment.get('expected_direction')
                    if expected in {'up', 'down', 'flat'}:
                        actual = 'up' if change > 0.2 else ('down' if change < -0.2 else 'flat')
                        verdict = 'supported' if actual == expected else 'contradicted'
                result.append({'run_id': run_id, 'slot': slot, 'code': code,
                               'intent': judgment.get('intent'), 'expected_direction': judgment.get('expected_direction'),
                               'confidence':judgment.get('confidence'), 'rationale':judgment.get('rationale'),
                               'counter_evidence':judgment.get('counter_evidence'), 'evidence_grade':judgment.get('evidence_grade'),
                               'issued_at': issued.isoformat(), 'observed_at': quote.get('quote_at') if quote else None,
                               'change_pct': round(change, 3) if change is not None else None,
                               'direction_verdict': verdict, 'intent_truth': 'not_directly_observable'})
    return result[:40]


def _archive(now):
    saved = tracking._read(Path(data_paths.data_path('auction_tracking','feedback.json')))
    return [r for r in saved.get('reviews',[]) if isinstance(r,dict)
            and _time(r.get('observed_at')) is not None and _time(r['observed_at']) <= now]


def _merge_feedback(archived, current, limit=40):
    combined = {}
    for row in archived + current:
        key = (row.get('run_id'),row.get('slot'),row.get('code'))
        old = combined.get(key)
        if old and row.get('direction_verdict') == 'pending' and old.get('direction_verdict') != 'pending':
            continue
        combined[key] = row
    return sorted(combined.values(),key=lambda r:r.get('observed_at') or r.get('issued_at') or '',reverse=True)[:limit]


def record_outcomes(market_rows, now):
    """复用三分钟扫描已有报价，保存最近100条复核；失败不进入交易决策。"""
    now = now.replace(tzinfo=tracking.CN) if now.tzinfo is None else now.astimezone(tracking.CN)
    path = Path(data_paths.data_path('adaptive_learning.sqlite3'))
    if not path.is_file():
        return 0
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as conn:
            conn.execute('PRAGMA query_only=ON')
            if not feedback(conn,{},now):
                return 0
            rows = feedback(conn,_current_quotes(market_rows,now),now)
        valid = [row for row in rows if row['direction_verdict'] != 'pending']
        if valid:
            merged = _merge_feedback(_archive(now),valid,limit=100)
            tracking._write(Path(data_paths.data_path('auction_tracking','feedback.json')),{'reviews':merged})
        return len(valid)
    except (OSError,sqlite3.Error,ValueError):
        return 0


def evidence(now, market_rows, conn, focus_codes=()):
    now = now.astimezone(tracking.CN)
    saved = tracking._read(Path(data_paths.data_path('auction_tracking', 'current.json')))
    stocks = {}
    if saved.get('trade_date') == now.date().isoformat():
        for observation in saved.get('observations', []):
            captured = _time(observation.get('captured_at'))
            if captured is None or captured > now:
                continue
            end = dt.time(9,28) if observation.get('phase') == 'final_snapshot' else dt.time(9,25)
            for row in observation.get('rows', []):
                code = str(row.get('code') or '')
                stamp, price = _time(row.get('quote_at')), _number(row.get('price'))
                if (not re.fullmatch(r'\d{6}', code) or stamp is None or price is None or price <= 0
                        or stamp.date() != now.date() or stamp > now
                        or not dt.time(9, 15) <= stamp.time() < end):
                    continue
                stocks.setdefault(code, {'code': code, 'points': []})['points'].append({
                    'quote_at': stamp.isoformat(), 'captured_at': captured.isoformat(), 'price': price,
                    'pct': row.get('pct'), 'volume': row.get('volume'), 'amount': row.get('amount'),
                    'main_net': row.get('main_net'),
                    'phase': observation.get('phase'), 'source': row.get('source')})
    focus = set(focus_codes) | _selection_codes(now)
    current = _current_quotes(market_rows, now)
    for code, item in stocks.items():
        item['points'] = sorted({p['quote_at']: p for p in item['points']}.values(), key=lambda p:p['quote_at'])[-5:]
        points = item['points']
        item['auction_change_pct'] = round((points[-1]['price']/points[0]['price']-1)*100, 3)
        item['activity_available'] = any((_number(p.get('volume')) or 0) > 0 or (_number(p.get('amount')) or 0) > 0 for p in points)
        item['reference_quote'] = current.get(code) or {key:points[-1].get(key) for key in ('price','quote_at','source')}
    selected = sorted(stocks.values(), key=lambda s:(s['code'] not in focus,-abs(s['auction_change_pct']),s['code']))[:MAX_STOCKS]
    # 竞价成交额只在最终快照上聚合：更早的轮次还在可撤单阶段，金额会被撤单改写。
    final_rows = []
    for observation in saved.get('observations', []) if saved.get('trade_date') == now.date().isoformat() else []:
        if observation.get('phase') == 'final_snapshot':
            final_rows = observation.get('rows') or []
    amount_summary = tracking.summarize(final_rows) if final_rows else None
    for item in selected:
        latest = item['points'][-1]
        item['amount_latest'] = latest.get('amount')
        item['volume_latest'] = latest.get('volume')
    return {'status': 'observed' if selected else 'not_collected', 'as_of': now.isoformat(),
            'observed_stocks': len(stocks), 'included_stocks': len(selected), 'stocks': selected,
            'amount_summary': amount_summary,
            'sampling': 'current_candidates_and_positions_then_largest_price_change',
            'review_feedback': _merge_feedback(_archive(now),feedback(conn,current,now)),
            'assessment_boundary': '意图是AI推断；后续方向复核只比较截至观察时点的价格，不证明真实主力意图。0.2%以内按横盘记录，不等于交易收益。竞价金额只覆盖本次观测到的个股。'}


def normalize_intents(raw, context):
    stocks = {s['code']:s for s in (context or {}).get('stocks', [])}
    result, seen = [], set()
    for item in raw[:MAX_JUDGMENTS] if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        code = str(item.get('code') or '')
        if code not in stocks or code in seen:
            continue
        intent = item.get('intent') if isinstance(item.get('intent'), str) and item['intent'] in INTENTS else 'unclear'
        points = stocks[code].get('points', [])
        if len(points) < 2:
            intent = 'unclear'
        confidence = _number(item.get('confidence'))
        result.append({'code':code,'intent':intent,'confidence':max(0,min(confidence or 0,100)),
                       'expected_direction':item.get('expected_direction') if isinstance(item.get('expected_direction'), str) and item['expected_direction'] in DIRECTIONS else 'unclear',
                       'rationale':str(item.get('rationale') or '')[:240],
                       'counter_evidence':str(item.get('counter_evidence') or '')[:240],
                       'evidence_grade':'price_and_activity' if stocks[code].get('activity_available') else 'price_only',
                       'reference_quote':stocks[code]['reference_quote'], 'intent_is_hypothesis':True})
        seen.add(code)
    return result


def status(conn):
    import market_data_service as market
    now = dt.datetime.now(tracking.CN)
    archived = _archive(now)
    reviews = feedback(conn, {}, now)
    if not reviews:
        return {'tracking':tracking.status(), 'reviews':_merge_feedback(archived,[]), 'boundary':'仅显示已有判断和复核，不生成准确率。'}
    try:
        rows = market.read_snapshot(now=now).rows()
        reviews = feedback(conn, _current_quotes(rows, now), now)
    except Exception:
        reviews = []
    return {'tracking':tracking.status(), 'reviews':_merge_feedback(archived,reviews),
            'boundary':'复核后续价格方向，不等于证明真实主力意图；等待有时点的新数据，不补造准确率。'}
