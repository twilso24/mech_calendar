'''Local RRULE occurrence expansion for recurring calendar events.

Supports the same conservative subset as store._clean_rrule: FREQ (DAILY,
WEEKLY, MONTHLY, YEARLY), INTERVAL, COUNT, UNTIL and BYDAY. Invalid rules
return an empty list. Output is capped to keep the UI responsive.
'''
from __future__ import annotations

import datetime

MAX_OCCURRENCES = 1000
MAX_SCAN_DAYS = 366 * 20
_DAYS = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')
_KEYS = ('FREQ', 'INTERVAL', 'COUNT', 'UNTIL', 'BYDAY')
_FREQS = ('DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY')


def _parse(rule):
    parts = {}
    for piece in rule.split(';'):
        key, sep, value = piece.partition('=')
        if not sep or key not in _KEYS or key in parts:
            return None
        parts[key] = value
    if parts.get('FREQ') not in _FREQS:
        return None
    try:
        interval = int(parts.get('INTERVAL', '1'))
        count = int(parts['COUNT']) if 'COUNT' in parts else None
    except ValueError:
        return None
    if interval < 1 or (count is not None and count < 1):
        return None
    until = None
    if 'UNTIL' in parts:
        try:
            until = datetime.datetime.strptime(parts['UNTIL'][:8], '%Y%m%d').date()
        except ValueError:
            return None
    byday = None
    if 'BYDAY' in parts:
        days = parts['BYDAY'].split(',')
        if not days or any(d not in _DAYS for d in days):
            return None
        byday = {_DAYS.index(d) for d in days}
    return {'freq': parts['FREQ'], 'interval': interval, 'count': count,
            'until': until, 'byday': byday}


def _matches(spec, first, cur):
    freq, interval = spec['freq'], spec['interval']
    if freq == 'DAILY':
        return (cur - first).days % interval == 0
    if freq == 'WEEKLY':
        monday = first - datetime.timedelta(days=first.weekday())
        week = (cur - monday).days // 7
        if week % interval:
            return False
        allowed = spec['byday'] or {first.weekday()}
        return cur.weekday() in allowed
    if freq == 'MONTHLY':
        months = (cur.year - first.year) * 12 + cur.month - first.month
        return months % interval == 0 and cur.day == first.day
    years = cur.year - first.year
    return years % interval == 0 and cur.month == first.month and cur.day == first.day


def expand_occurrences(rule, start, window_start, window_end):
    '''Return ISO dates of occurrences inside [window_start, window_end].'''
    try:
        first = datetime.date.fromisoformat(start)
        lo = datetime.date.fromisoformat(window_start)
        hi = datetime.date.fromisoformat(window_end)
    except (TypeError, ValueError):
        return []
    rule = (rule or '').strip()
    if not rule:
        return [first.isoformat()] if lo <= first <= hi else []
    spec = _parse(rule)
    if spec is None:
        return []
    out = []
    generated = 0
    for offset in range(MAX_SCAN_DAYS):
        cur = first + datetime.timedelta(days=offset)
        if cur > hi or generated >= MAX_OCCURRENCES:
            break
        if spec['until'] and cur > spec['until']:
            break
        if not _matches(spec, first, cur):
            continue
        generated += 1
        if spec['count'] and generated > spec['count']:
            break
        if lo <= cur:
            out.append(cur.isoformat())
    return out
