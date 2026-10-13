import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
PACKAGE_DIR = os.path.join(ROOT, 'lib', 'package')
for _p in (ROOT, PACKAGE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_LINE = re.compile(
    r"^\s*(?:export\s+)?([\w.-]+)(?:\s*=\s*?|:\s+?)(\s*'(?:\\'|[^'])*'|\s*\"(?:\\\"|[^\"])*\"|\s*`(?:\\`|[^`])*`|[^#\r\n]+)?\s*(?:#.*)?$",
    re.M,
)
_QUOTED = re.compile(r'^([\'"`])([\s\S]*)\1$', re.M)


def parse_env(text):
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    out = {}
    for m in _LINE.finditer(text):
        key = m.group(1)
        value = (m.group(2) or '').strip()
        quote = value[:1]
        value = _QUOTED.sub(r'\2', value)
        if quote == '"':
            value = value.replace('\\n', '\n').replace('\\r', '\r')
        out[key] = value
    return out


def load_env_file(path):
    if not os.path.isfile(path):
        return False
    with open(path, 'r', encoding='utf-8') as fh:
        content = fh.read()
    for key, value in parse_env(content).items():
        if key not in os.environ:
            os.environ[key] = value
    return True


load_env_file(os.path.join(os.getcwd(), '.env'))

from baileys_lite import Browsers
from lib.utils.handler import G


def _split_env(name, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    return [v.strip() for v in raw.split(',') if v.strip()]


G.readmore = chr(8206) * 4001
G.package = {'name': 'hiro', 'version': '1.0', 'description': 'Simple Whatsapp Bot made by HirooSy', 'homepage': None}
if G.website_state is None:
    G.website_state = {'mode': 'tunnel', 'url': None}
if G.getServerUrl is None:
    G.getServerUrl = lambda: G.website_state.get('url') or 'https://not-loaded.yet'
os.environ['TMPDIR'] = os.path.join(os.getcwd(), 'data', 'tmp')
os.makedirs(os.environ['TMPDIR'], exist_ok=True)

G.settings = {
    'botname': 'HIROBOT',
    'owner': [[v, '', True] for v in _split_env('OWNER', [''])],
    'mods': _split_env('MODERATOR', ['']),
    'icon': 'https://files.catbox.moe/imxgh6.png',
    'ai': {
        'thinking': True,
        'autoheal': False,
        'loadStore': True,
        'limitHistory': 50,
    },
    'connection': {
        'main': {
            'loadhistory': False,
            'type': 'sql',
            'browser': Browsers['windows']('Firefox'),
            'paircode': 'HIROHIRO',
            'qr': False,
        },
        'subbot': {
            'maxConnect': 100,
            'autoConnect': True,
            'loadhistory': False,
            'type': 'sql',
            'paircode': 'HIROHIRO',
            'browser': Browsers['windows']('Firefox'),
            'qr': False,
        },
    },
    'system': {
        'online': False,
        'autoTyping': False,
        'autoRead': True,
        'OnlyRespondTo': 'all',
        'queue': True,
        'noPrefix': False,
        'prefix': ['.', '/', '!'],
        'cooldown': 3000,
        'autoReactStatus': [False, ['🖤', '❤️']],
        'didyoumean': True,
        'autoblocknumber': ['265', '234'],
    },
    'tier': {
        'name': ['✧✧✧✧✧', '✦✧✧✧✧', '✦✦✦✧✧', '✦✦✦✧✧', '✦✦✦✦✧', '✦✦✦✦✦'],
        'exp_required': [10000, 20000, 30000, 50000, 100000],
        'limit_capacity': [10, 15, 20, 30, 40, 50],
    },
    'sticker_wm': ['H I R O   B O T', ''],
    'msg': {
        'rowner': 'This command is only for developer bot',
        'owner': 'This command is only for bot owners',
        'mods': 'This command is only for bot moderators.',
        'premium': 'This command is only for premium users.',
        'group': 'This command can only be used in group chat.',
        'private': 'This command can only be used in private chat',
        'admin': 'This command can only be used by the admin group.',
        'botAdmin': 'This command can only be used if the bot is an admin.',
        'restrict': 'Restrict is disabled in this chat',
        'unreg': 'Sorry User, You can only use this command after registering to the bot database.',
    },
}

from lib.scrapers.index import scraper as _scraper

G.scraper = _scraper
