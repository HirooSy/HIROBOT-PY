import json
import math

from lib.package.ai import mcp
from lib.utils.handler import G


def _args(args, kw):
    merged = dict(args or {})
    merged.update(kw)
    return merged


def _norm(value):
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_norm(v) for v in value]
    return value


def _json(value, indent=None):
    return json.dumps(_norm(value), ensure_ascii=False, indent=indent,
                      separators=None if indent is not None else (',', ':'), default=str)


def _reject_constant(name):
    raise ValueError(name)


def _has(node, key):
    if isinstance(node, dict):
        return key in node
    if isinstance(node, list):
        return str(key).isdigit() and int(key) < len(node)
    return False


def _child(node, key):
    if isinstance(node, list):
        return node[int(key)]
    return node[key]


def _put(node, key, value):
    if isinstance(node, list):
        node[int(key)] = value
    else:
        node[key] = value


def _redact(obj):
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, dict):
        return {k: ('[REDACTED]' if k == 'password' else _redact(v)) for k, v in obj.items()}
    return obj


def _db_ready():
    return G.db is not None and getattr(G.db, 'data', None) is not None


async def _exec_read_database(args=None, **kw):
    a = _args(args, kw)
    key_path = a.get('key_path') or ''
    limit = a.get('limit')
    if limit is None:
        limit = 5
    if not _db_ready():
        return 'FAILED: db.data is not ready (not loaded yet) — make sure await db.read() has run.'
    data = G.db.data

    if not key_path:
        lines = []
        for k, v in data.items():
            count = f' ({len(v)} entries)' if isinstance(v, (dict, list)) else ''
            lines.append(f'- {k}{count}')
        summary = '\n'.join(lines)
        return f'Top-level keys in db.data:\n{summary}\n\nCall again with a key_path (e.g. "users") to see its content.'

    parts = mcp.parse_db_key_path(key_path)
    if not parts:
        return 'FAILED: key_path is not valid.'
    node = data
    for p in parts:
        if node is None or not isinstance(node, (dict, list)) or not _has(node, p):
            return f'FAILED: "{key_path}" was not found in db.data.'
        node = _child(node, p)

    result = _redact(node)
    note = ''
    if isinstance(result, dict):
        entries = list(result.items())
        if len(entries) > limit:
            result = dict(entries[:int(limit)])
            note = f'\n\n[Showing {limit} of {len(entries)} entries — call again with a more specific key_path (e.g. "users.<one of the keys above>") or a larger limit if you need to see the rest.]'

    return f'db.data.{key_path}:\n```json\n{_json(result, indent=2)}\n```{note}'


async def _exec_write_database(args=None, **kw):
    a = _args(args, kw)
    key_path = a.get('key_path')
    value = a.get('value')
    operation = a.get('operation') or 'set'
    if not _db_ready():
        return 'FAILED: db.data is not ready (not loaded yet) — make sure await db.read() has run.'
    if not key_path:
        return 'FAILED: key_path is required.'

    parts = mcp.parse_db_key_path(key_path)
    if not parts:
        return 'FAILED: key_path is not valid.'

    data = G.db.data
    last_key = parts[-1]
    node = data

    if operation == 'delete':
        for p in parts[:-1]:
            if node is None or not isinstance(node, (dict, list)) or not _has(node, p):
                return f'FAILED: path "{key_path}" was not found in db.data.'
            node = _child(node, p)
        if node is None or not isinstance(node, (dict, list)) or not _has(node, last_key):
            return f'FAILED: key "{key_path}" was not found in db.data.'
        old_val = _child(node, last_key)
        if isinstance(node, list):
            node.pop(int(last_key))
        else:
            del node[last_key]
        try:
            if hasattr(G.db, 'write'):
                await G.db.write()
        except Exception as e:
            if isinstance(node, list):
                node.insert(int(last_key), old_val)
            else:
                node[last_key] = old_val
            return f'FAILED to save the change to the database: {e}'
        return f'Deleted: db.data.{key_path}\n(old value: {_json(old_val)})'

    if value is None:
        return 'FAILED: value is required for operation "set".'
    if isinstance(value, str):
        try:
            parsed_value = json.loads(value, parse_constant=_reject_constant)
        except ValueError:
            parsed_value = value
    else:
        parsed_value = value

    auto_created_warning = ''
    if len(parts) >= 2 and parts[0] in ('users', 'chats'):
        collection = data.get(parts[0])
        if isinstance(collection, dict) and parts[1] not in collection:
            auto_created_warning = (
                f'\n\n⚠️ WARNING: the record "{parts[0]}.{parts[1]}" DID NOT EXIST in the database before, so it was just created automatically as an empty object. '
                'If you meant to edit an EXISTING user/chat, the key_path is most likely wrong (JID typo, or forgotten bracket-quote for a JID that contains dots) — check again with read_database before continuing.'
            )
    for p in parts[:-1]:
        current = _child(node, p) if _has(node, p) else None
        if not isinstance(current, (dict, list)):
            _put(node, p, {})
        node = _child(node, p)

    old_present = _has(node, last_key)
    old_val = _child(node, last_key) if old_present else None
    _put(node, last_key, parsed_value)
    try:
        if hasattr(G.db, 'write'):
            await G.db.write()
    except Exception as e:
        if old_present:
            _put(node, last_key, old_val)
        else:
            del node[last_key]
        return f'FAILED to save the change to the database: {e}'
    old_text = _json(old_val) if old_present else 'undefined'
    return f'Set: db.data.{key_path} = {_json(parsed_value)}\n(old value: {old_text}){auto_created_warning}'


default = [
    {
        'name': 'read_database',
        'description': (
            'Read the structure/content of the running bot database (db.data) — read DIRECTLY from memory, '
            'so it works whatever the adapter is (local JSON file, MongoDB, MySQL, Cloud DB). Use it to '
            'check the REAL data structure (key/field names, value types, sample content) while '
            'debugging/auto-healing — do not guess from the code alone, especially when the error involves '
            'db.data. Note: read_file can only read database.json if the adapter is a local file; for remote '
            'adapters (Mongo/MySQL/Cloud DB) read_database is the ONLY way to see the content. The '
            '"password" field is automatically masked for security.'
        ),
        'parameters': {
            'key_path': {
                'type': 'string',
                'description': (
                    'Key path in db.data. Plain dot notation for keys without dots, e.g. "users" (all users), '
                    '"settings". Keys that CONTAIN A DOT (most often a WhatsApp JID) MUST be wrapped in '
                    'bracket+quote so they are not split wrongly, for example: '
                    '\'users["6281234567890@s.whatsapp.net"]\' (one specific user), \'chats["1234@g.us"].settings\'. '
                    '(There is an auto-merge fallback if you forget the brackets, but brackets are more certain to '
                    'be correct.) Leave empty to first see the list of top-level keys along with their entry counts.'
                ),
                'required': False,
            },
            'limit': {
                'type': 'number',
                'description': (
                    'If the result is an object with many entries (e.g. all users), limit the number of entries '
                    'shown (default 5) to save tokens — call again with a more specific key_path to see particular '
                    'entries.'
                ),
                'required': False,
            },
        },
        'execute': _exec_read_database,
    },
    {
        'name': 'write_database',
        'description': (
            'Change/delete content in the running bot database (db.data) — directly in memory, just like '
            '"db.data.users[user].name = \'Hiro\'" in code. NEVER use write_file to edit the database (e.g. '
            'database.json) — that only writes a file on disk, is NOT in sync with the db.data running in '
            'memory, and can corrupt data/get overwritten back whatever the adapter is (local JSON, MongoDB, '
            'MySQL, Cloud DB). write_database is the ONLY correct way to edit the database, because it '
            'automatically persists through db.write() (whatever the adapter). OWNER-ONLY — regular users '
            'may not use this tool.'
        ),
        'parameters': {
            'key_path': {
                'type': 'string',
                'description': (
                    'Key path in db.data, same as used in read_database. Plain dot notation is fine for keys without '
                    'dots, e.g. "settings.prefix". Keys that CONTAIN A DOT (most often a WhatsApp JID, e.g. '
                    '"628xxx@s.whatsapp.net", "1234@g.us") MUST be wrapped in bracket+quote so they are not split '
                    'wrongly, for example: \'users["628xxx@s.whatsapp.net"].name\', '
                    '\'chats["1234@g.us"].settings.welcome\'. (There is an auto-merge fallback if you forget the '
                    'brackets, but brackets are safer/more certain to be correct.) If an intermediate key does not '
                    'exist yet, it is created automatically as an empty object (except for operation "delete") — the '
                    'result will give a WARNING if this creates a new users/chats record, check that warning to make '
                    'sure it is not a typo.'
                ),
                'required': True,
            },
            'value': {
                'type': 'string',
                'description': (
                    'The new value, as a JSON literal (strings must use double quotes, e.g. "Hiro"; number: 5; '
                    'boolean: true; object: {"a":1}; array: [1,2,3]). Required when operation is "set" (default). '
                    'Ignored when operation is "delete".'
                ),
                'required': False,
            },
            'operation': {
                'type': 'string',
                'description': '"set" (default) to change/add a value, or "delete" to remove that key completely from db.data.',
                'required': False,
            },
        },
        'execute': _exec_write_database,
    },
]
