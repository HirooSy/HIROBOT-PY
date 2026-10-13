import asyncio
import inspect
import json
import math
import os
import re
import sys
import time
import uuid

from lib.package.ai import mcp


ROOT = os.getcwd()
REMINDER_PATH = os.path.join(ROOT, 'data', 'reminder.json')
DAY_MS = 24 * 60 * 60 * 1000
_reminders = {}
_unscheduled = []
_tasks = set()


async def _await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _warn(*parts):
    print(*parts, file=sys.stderr)


def _g(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    return default if value is None else value


def _args(args, kw):
    merged = dict(args or {})
    merged.update(kw)
    return merged


def _now_ms():
    return int(time.time() * 1000)


def _round(value):
    return math.floor(value + 0.5)


def _load_file():
    try:
        with open(REMINDER_PATH, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {'reminders': []}


def _save_file():
    try:
        os.makedirs(os.path.dirname(REMINDER_PATH), exist_ok=True)
        plain = [
            {'id': r['id'], 'jid': r['jid'], 'message': r['message'], 'fireAt': r['fireAt']}
            for r in _reminders.values()
        ]
        with open(REMINDER_PATH, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps({'reminders': plain}, indent=2, ensure_ascii=False))
    except Exception as e:
        _warn(f'[reminder] Gagal simpan reminder.json: {e}')


_RELATIVE = re.compile(r'([0-9]+)\s*(hari|days?|d|jam|hours?|h|j|menit|minutes?|min|m|detik|seconds?|sec|s)\b', re.I)


def _parse_relative_time(text):
    total = 0
    matched = False
    for m in _RELATIVE.finditer(str(text or '')):
        matched = True
        num = int(m.group(1))
        unit = m.group(2).lower()
        if re.fullmatch(r'hari|days?|d', unit):
            total += num * 24 * 60 * 60 * 1000
        elif re.fullmatch(r'jam|hours?|h|j', unit):
            total += num * 60 * 60 * 1000
        elif re.fullmatch(r'menit|minutes?|min|m', unit):
            total += num * 60 * 1000
        elif re.fullmatch(r'detik|seconds?|sec|s', unit):
            total += num * 1000
    return total if matched else None


class _MessageView:
    def __init__(self, base, overrides):
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name):
        overrides = self.__dict__.get('_overrides', {})
        if name in overrides:
            return overrides[name]
        return getattr(self.__dict__['_base'], name)


def _clone_message(cur, jid):
    key = dict(_g(cur, 'key') or {})
    key['remoteJid'] = jid
    if isinstance(cur, dict):
        out = dict(cur)
        out.update({'key': key, 'chat': jid, 'sender': jid})
        return out
    return _MessageView(cur, {'key': key, 'chat': jid, 'sender': jid})


async def _fire(rid, jid, message):
    _reminders.pop(rid, None)
    _save_file()
    try:
        c = mcp.ctx()
        conn = c['conn']
        cur = c['currentM']
        if conn is not None and cur is not None:
            fake = _clone_message(cur, jid)
            result = await _await(mcp.run_agent(
                conn,
                fake,
                f'[Reminder fired] Tell the user that the time has come for: "{message}". Say it in a natural style, not stiff.',
                {'senderJid': jid},
            ))
            text = _g(result, 'text')
            if text:
                await conn.sendMessage(jid, {'text': text})
        else:
            _warn(f'[reminder] conn/currentM belum tersedia saat reminder {rid} harusnya jalan.')
    except Exception as e:
        _warn(f'[reminder] Gagal kirim reminder {rid}:', e)


def _launch(rid, jid, message):
    task = asyncio.ensure_future(_fire(rid, jid, message))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _schedule(rid, jid, message, fire_at):
    delay = max(0, fire_at - _now_ms()) / 1000.0
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        if rid not in _unscheduled:
            _unscheduled.append(rid)
        return
    handle = loop.call_later(delay, _launch, rid, jid, message)
    entry = _reminders.get(rid)
    if entry is not None:
        entry['timer'] = handle


def _ensure_scheduled():
    pending = list(_unscheduled)
    _unscheduled.clear()
    for rid in pending:
        entry = _reminders.get(rid)
        if entry is not None and entry['timer'] is None:
            _schedule(rid, entry['jid'], entry['message'], entry['fireAt'])


def _create(jid, message, delay_ms):
    rid = uuid.uuid4().hex[:8]
    fire_at = _now_ms() + delay_ms
    _reminders[rid] = {'id': rid, 'jid': jid, 'message': message, 'fireAt': fire_at, 'timer': None}
    _schedule(rid, jid, message, fire_at)
    _save_file()
    return rid, fire_at


def _list(jid):
    return sorted(
        [r for r in _reminders.values() if r['jid'] == jid],
        key=lambda r: r['fireAt'],
    )


def _remove(rid):
    entry = _reminders.get(rid)
    if entry is None:
        return False
    if entry['timer'] is not None:
        entry['timer'].cancel()
    _reminders.pop(rid, None)
    if rid in _unscheduled:
        _unscheduled.remove(rid)
    _save_file()
    return True


def _restore():
    data = _load_file()
    items = data.get('reminders') if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = []
    for r in items:
        if not isinstance(r, dict) or not r.get('id') or not r.get('jid') or not r.get('fireAt'):
            continue
        _reminders[r['id']] = {'id': r['id'], 'jid': r['jid'], 'message': r.get('message'), 'fireAt': r['fireAt'], 'timer': None}
        _schedule(r['id'], r['jid'], r.get('message'), r['fireAt'])
    if items:
        print(f'[reminder] {len(items)} reminder di-restore dari reminder.json')


async def _exec_create_reminder(args=None, **kw):
    a = _args(args, kw)
    time_text = a.get('time_text')
    message = a.get('message')
    delay_ms = _parse_relative_time(time_text)
    if not delay_ms:
        return f'Cannot parse time from "{time_text}". Ask for a format like "20 minutes" or "1 hour 30 minutes".'
    if delay_ms > 30 * DAY_MS:
        return 'Max reminder duration is 30 days.'

    c = mcp.ctx()
    current_jid = c['currentJid']
    if c['conn'] is None or not current_jid:
        return 'WA connection not ready'

    _ensure_scheduled()
    clean_msg = (str(message).strip() if message is not None else '') or "It's time!"
    rid, _fire_at = _create(current_jid, clean_msg, delay_ms)

    total_min = _round(delay_ms / 60000)
    if total_min >= 60:
        display_time = f'{total_min // 60} hours {total_min % 60} minutes'
    else:
        display_time = f'{total_min} minutes'
    return f'reminder_created:{rid}:{display_time}:{clean_msg}'


async def _exec_list_reminders(args=None, **kw):
    current_jid = mcp.ctx()['currentJid']
    if not current_jid:
        return 'Chat context not available'
    mine = _list(current_jid)
    if not mine:
        return 'There are no active reminders in this chat yet.'
    lines = []
    for i, r in enumerate(mine):
        minutes_left = max(0, _round((r['fireAt'] - _now_ms()) / 60000))
        lines.append(f'{i + 1}. "{r["message"]}" — {minutes_left} minutes left (ID: {r["id"]})')
    return '\n'.join(lines)


async def _exec_cancel_reminder(args=None, **kw):
    a = _args(args, kw)
    reminder_id = a.get('reminder_id')
    ok = _remove(reminder_id)
    if ok:
        return f'Reminder {reminder_id} cancelled.'
    return f'Reminder "{reminder_id}" not found.'


_restore()


default = [
    {
        'name': 'create_reminder',
        'description': (
            'Create a reminder that will be sent automatically to this chat after a certain amount of time. '
            'Use it when the user asks to be reminded of something (e.g. "remind me in 20 minutes to take a '
            'bath", "reminder in 1 hour for a meeting").'
        ),
        'parameters': {
            'time_text': {
                'type': 'string',
                'description': (
                    'Text containing a time duration, in the natural wording the user used (examples: "20 minutes", '
                    '"1 hour 30 minutes", "2 days", or the Indonesian "20 menit lagi"). This tool parses the '
                    'duration itself.'
                ),
                'required': True,
            },
            'message': {
                'type': 'string',
                'description': (
                    'The reminder message content (examples: "take a bath", "take medicine", "meeting"). If it is '
                    "unclear, fill it with a short summary of the user's request."
                ),
                'required': True,
            },
        },
        'execute': _exec_create_reminder,
    },
    {
        'name': 'list_reminders',
        'description': 'See all active reminders in this chat.',
        'parameters': {},
        'execute': _exec_list_reminders,
    },
    {
        'name': 'cancel_reminder',
        'description': 'Cancel a reminder that was already created, by its ID (get the ID from list_reminders).',
        'parameters': {
            'reminder_id': {
                'type': 'string',
                'description': 'The ID of the reminder to cancel',
                'required': True,
            },
        },
        'execute': _exec_cancel_reminder,
    },
]
