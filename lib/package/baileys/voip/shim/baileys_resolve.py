import importlib
import re

from baileys.foundation.node_runtime import JSError


DEFAULT_CANDIDATES = [
    'baileys',
    '@whiskeysockets/baileys',
    '@adiwajshing/baileys',
    '@itsliaaa/baileys',
]

BUNDLED_IMPORT_NAME = 'baileys'

cachedModule = None


def candidateNames():
    return DEFAULT_CANDIDATES


def importNameFor(name):
    if name == 'baileys':
        return BUNDLED_IMPORT_NAME
    return re.sub(r'[@/\-.]+', '_', name).strip('_')


async def resolveBaileysModule():
    global cachedModule
    if cachedModule is not None:
        return cachedModule
    names = candidateNames()
    lastError = None
    for name in names:
        try:
            cachedModule = importlib.import_module(importNameFor(name))
            return cachedModule
        except Exception as err:
            lastError = err
    lastMessage = getattr(lastError, 'message', None) or str(lastError)
    raise JSError(
        f'voip: could not find a Baileys install (tried: {", ".join(names)}). '
        f'Install one of these packages or add an npm alias for it named "baileys" in your package.json. '
        f'Last error: {lastMessage}'
    )
