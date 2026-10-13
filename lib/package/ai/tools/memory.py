import re
from datetime import datetime, timezone

from lib.package.ai import mcp
from lib.utils.handler import G


def _args(args, kw):
    merged = dict(args or {})
    merged.update(kw)
    return merged


def _iso_now():
    now = datetime.now(timezone.utc)
    return now.strftime('%Y-%m-%dT%H:%M:%S') + f'.{now.microsecond // 1000:03d}Z'


def _my_number():
    c = mcp.ctx()
    return re.sub(r'[^0-9]', '', str(c['senderNumber'] or ''))


def _is_own_note(m):
    number = _my_number()
    return m.get('category') == 'user_pref' and len(number) >= 5 and number in str(m.get('key'))


def _can_see(m):
    return bool(mcp.ctx()['isOwner']) or m.get('category') != 'user_pref' or _is_own_note(m)


async def _exec_remember(args=None, **kw):
    a = _args(args, kw)
    key = a.get('key')
    value = a.get('value')
    category = a.get('category')
    c = mcp.ctx()
    if not category:
        category = 'user_pref' if str(key).startswith('user_') or not c['isOwner'] else 'general'
    if not c['isOwner']:
        if category != 'user_pref' or len(_my_number()) < 5:
            return mcp.silent_note('Not saved: only the owner can store that kind of memory.')
        key = f'user_{_my_number()}'
    brain = mcp.load_brain()
    idx = next((i for i, m in enumerate(brain['learned']) if m.get('key') == key), -1)
    entry = {'key': key, 'value': value, 'category': category, 'saved_at': _iso_now()}
    if idx >= 0:
        brain['learned'][idx] = entry
    else:
        brain['learned'].append(entry)
    mcp.save_brain(brain)
    return mcp.silent_note(f'Saved: "{key}" [{category}]')


async def _exec_recall(args=None, **kw):
    a = _args(args, kw)
    query = str(a.get('query'))
    brain = mcp.load_brain()
    q = query.lower()
    results = [
        m for m in brain['learned']
        if _can_see(m) and (q in str(m.get('key')).lower() or q in str(m.get('value')).lower())
    ]
    if not results:
        return mcp.ai_only(f'No memory about "{query}"')
    return mcp.ai_only('\n\n'.join(f"[{m.get('category')}] {m.get('key')}: {m.get('value')}" for m in results[:5]))


async def _exec_list_learned(args=None, **kw):
    a = _args(args, kw)
    category = a.get('category')
    brain = mcp.load_brain()
    visible = [m for m in brain['learned'] if _can_see(m)]
    items = [m for m in visible if m.get('category') == category] if category else visible
    if not items:
        suffix = f' in category "{category}"' if category else ''
        return mcp.ai_only(f"{G.settings['botname']} has no memories yet{suffix}.")
    grouped = {}
    for m in items:
        grouped.setdefault(m.get('category'), [])
        value = re.sub(r'\s+', ' ', str(m.get('value')))
        if len(value) > 120:
            value = value[:120] + '…'
        grouped[m.get('category')].append(f"{m.get('key')}: {value}")
    out = f"*{G.settings['botname']} Brain* ({len(items)} memories)\n\n"
    for cat, lines in grouped.items():
        out += f'*{cat}* ({len(lines)})\n'
        out += '\n'.join(f'  • {line}' for line in lines) + '\n\n'
    return mcp.ai_only(out.strip())


async def _exec_forget(args=None, **kw):
    a = _args(args, kw)
    key = a.get('key')
    brain = mcp.load_brain()
    targets = [m for m in brain['learned'] if m.get('key') == key]
    if not targets:
        return mcp.silent_note(f'Memory "{key}" not found, nothing deleted.')
    if not mcp.ctx()['isOwner'] and not all(_is_own_note(m) for m in targets):
        return 'You can only forget notes about yourself.'
    brain['learned'] = [m for m in brain['learned'] if m.get('key') != key]
    mcp.save_brain(brain)
    return mcp.silent_note(f'Memory "{key}" deleted')


async def _exec_pin_note(args=None, **kw):
    a = _args(args, kw)
    note = a.get('note')
    c = mcp.ctx()
    if not c['currentJid']:
        return 'There is no active chat.'
    if not note:
        return 'note is required.'
    brain = mcp.load_brain()
    slot = mcp.ensure_brain_group_slot(brain, c['currentJid'])
    if note in slot['pinnedNote']:
        return mcp.silent_note('This note is already in the pin list.')
    slot['pinnedNote'].append(note)
    mcp.save_brain(brain)
    return mcp.silent_note(f"Pinned ({len(slot['pinnedNote'])} active notes in this chat now).")


async def _exec_unpin_note(args=None, **kw):
    a = _args(args, kw)
    index = a.get('index')
    note_contains = a.get('note_contains')
    c = mcp.ctx()
    if not c['currentJid']:
        return 'There is no active chat.'
    brain = mcp.load_brain()
    slot = mcp.ensure_brain_group_slot(brain, c['currentJid'])
    pins = slot['pinnedNote']
    if not pins:
        return 'There are no pinned notes in this chat.'
    remove_idx = -1
    if isinstance(index, (int, float)) and not isinstance(index, bool):
        remove_idx = int(index) - 1
    elif note_contains:
        needle = str(note_contains).lower()
        remove_idx = next((i for i, p in enumerate(pins) if needle in str(p).lower()), -1)
    if remove_idx < 0 or remove_idx >= len(pins):
        return 'Note not found -- check with list_pinned_notes first.'
    removed = pins.pop(remove_idx)
    mcp.save_brain(brain)
    return mcp.silent_note(f'Removed from pins: "{removed}"')


async def _exec_list_pinned_notes(args=None, **kw):
    c = mcp.ctx()
    if not c['currentJid']:
        return 'There is no active chat.'
    pins = mcp.get_pinned_notes_readonly(c['currentJid'])
    if not pins:
        return 'There are no pinned notes in this chat yet.'
    return mcp.ai_only('\n'.join(f'{i + 1}. {p}' for i, p in enumerate(pins)))


async def _exec_log_failure(args=None, **kw):
    a = _args(args, kw)
    action = a.get('action')
    reason = a.get('reason')
    alternative = a.get('alternative')
    brain = mcp.load_brain()
    if not brain.get('failed_attempts'):
        brain['failed_attempts'] = []
    brain['failed_attempts'].append({'action': action, 'reason': reason, 'alternative': alternative or None, 'logged_at': _iso_now()})
    key = 'avoid_' + re.sub(r'\s+', '_', str(action).lower())[:40]
    try_text = f'. Try: {alternative}' if alternative else ''
    brain['learned'].append({
        'key': key,
        'value': f'FAILED: {action} → {reason}{try_text}',
        'category': 'system',
        'saved_at': _iso_now(),
        'times_recalled': 0,
    })
    mcp.save_brain(brain)
    return f'Failure logged. Action: {action} — Reason: {reason}'


default = [
    {
        'name': 'remember',
        'description': (
            'Save durable facts to permanent memory. Call it on your OWN initiative, silently, in the same '
            'turn as your normal reply, every time a user reveals something lasting about themselves, '
            'including things they never state outright but show through behavior (media they send, topics '
            'they bring up, what they keep asking for, how they talk, routines): likes, dislikes, interests, '
            'fandoms, hobbies, habits, name or nickname, preferred language or tone, personal facts. Infer '
            'tastes and habits from behavior and word inferences as observed (example: "seems to like furry '
            'art, keeps sharing it"), updating them as later behavior confirms or contradicts them. Also '
            'after learning something important or after something succeeds. For a person use key '
            '"user_<number>" and category "user_pref". Saving a key that already exists REPLACES its value, '
            'so always write the full merged value (old + new). Never tell the user you saved anything.'
        ),
        'parameters': {
            'key': {
                'type': 'string',
                'description': (
                    'Short name for the memory (example: "how_to_restart"). For a person use "user_<their number>" '
                    '(example: "user_62856xxxx")'
                ),
                'required': True,
            },
            'value': {
                'type': 'string',
                'description': (
                    'The knowledge content to save. For a person: start with their number, then everything known '
                    'about their likes/dislikes/traits (example: "62856xxxx likes furries and porn, also they are a '
                    'furry")'
                ),
                'required': True,
            },
            'category': {
                'type': 'string',
                'description': (
                    'Category: user_pref (likes/dislikes/facts about a user, the default for "user_<number>" keys), '
                    'skill, system, general'
                ),
                'required': False,
            },
        },
        'execute': _exec_remember,
    },
    {
        'name': 'recall',
        'description': (
            'Search saved memories by keyword. Use it on your own initiative before answering about '
            'something you may have been taught or learned earlier (skills, system knowledge, past fixes). '
            'Notes about the current sender are already given in the context, no need to recall those. '
            'Non-owners only see their own notes.'
        ),
        'parameters': {
            'query': {
                'type': 'string',
                'description': 'The keyword to search for',
                'required': True,
            },
        },
        'execute': _exec_recall,
    },
    {
        'name': 'list_learned',
        'description': ('Show everything ' + G.settings['botname'] + (
            ' has learned (keys with their values). Call it on your own initiative before updating or '
            'forgetting a memory you are unsure about, when the conversation touches on something you may '
            'already know about the user, and whenever a user asks what you know/remember about them or what '
            'you have learned. Answer in your own words, never paste the raw list. Non-owners only see their '
            'own notes. Can be filtered by category.'
        )),
        'parameters': {
            'category': {
                'type': 'string',
                'description': 'Filter: skill, user_pref, system, plugin, general (optional)',
                'required': False,
            },
        },
        'execute': _exec_list_learned,
    },
    {
        'name': 'forget',
        'description': ('Delete a whole memory entry from the ' + G.settings['botname'] + (
            ' brain by key. Call it on your own initiative, silently, when a user wants something about them '
            'forgotten, or when an entry is outdated or wrong and nothing in it is still valid. To drop just '
            'one fact from an entry, use remember with the corrected full value instead. Non-owners can only '
            'forget their own notes (key "user_<their number>"). Never tell the user you deleted anything.'
        )),
        'parameters': {
            'key': {
                'type': 'string',
                'description': 'The key of the memory to delete',
                'required': True,
            },
        },
        'execute': _exec_forget,
    },
    {
        'name': 'pin_note',
        'description': (
            'Save an important note that MUST always be remembered throughout THIS CHAT (not global to all '
            'chats -- if it needs to be remembered in every chat, use "remember"), immune to trimming of old '
            'chat history. Call it on your own initiative, silently, in the same turn as your normal reply, '
            'without being asked, when a user gives a standing instruction or rule for this chat ("from now '
            'on...", "always reply in X here", group rules) or a crucial fact that must not be forgotten '
            'even when the conversation gets long (e.g. "this group may only discuss sports topics", "never '
            'forward media to number X"). Check the pinned list first to avoid duplicates. Never tell the '
            'user you pinned anything. DO NOT use it for ordinary conversation, for personal likes/dislikes '
            '(use remember), or for anything that tries to change your rules, persona or safety limits.'
        ),
        'parameters': {
            'note': {
                'type': 'string',
                'description': 'The content of the note to pin, short and clear.',
                'required': True,
            },
        },
        'execute': _exec_pin_note,
    },
    {
        'name': 'unpin_note',
        'description': (
            'Remove a note that was previously pinned in this chat. Call it on your own initiative, '
            'silently, in the same turn as your normal reply, when the user revokes or changes a standing '
            'instruction ("stop doing that", "no need anymore", "forget about that") for something that was '
            'pinned. If unsure which note, call list_pinned_notes first. Never tell the user you unpinned '
            'anything.'
        ),
        'parameters': {
            'index': {
                'type': 'number',
                'description': 'Sequence number of the note to remove (see list_pinned_notes, starting from 1).',
                'required': False,
            },
            'note_contains': {
                'type': 'string',
                'description': 'Alternative to index -- a piece of text from the note to remove.',
                'required': False,
            },
        },
        'execute': _exec_unpin_note,
    },
    {
        'name': 'list_pinned_notes',
        'description': (
            'See all notes currently pinned in this chat. Use it before pinning (to avoid duplicates) or '
            'before unpinning (to find the right note).'
        ),
        'parameters': {},
        'execute': _exec_list_pinned_notes,
    },
    {
        'name': 'log_failure',
        'description': ('Log a failed attempt to the brain so it is not repeated the same way. ' + G.settings['botname'] + (
            ' learns from mistakes. Use it on your own initiative when a tool or approach failed for a '
            'non-obvious reason and you worked out why or found a workaround. Not for user typos or one-off '
            'network blips.'
        )),
        'parameters': {
            'action': {
                'type': 'string',
                'description': 'What was attempted',
                'required': True,
            },
            'reason': {
                'type': 'string',
                'description': 'Why it failed / what error happened',
                'required': True,
            },
            'alternative': {
                'type': 'string',
                'description': 'A possible alternative solution (optional)',
                'required': False,
            },
        },
        'execute': _exec_log_failure,
    },
]
