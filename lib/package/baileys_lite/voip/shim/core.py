import json
import sys

from baileys_lite.foundation.node_crypto import randomBytes
from baileys_lite.foundation.node_runtime import JSError, JSObject
from baileys_lite.voip.shim.util import optGet


LEVELS = JSObject({'trace': 0, 'debug': 1, 'media': 2, 'info': 3, 'warn': 4, 'error': 5, 'silent': 6})


def noop(*args, **kwargs):
    return None


def createNoopLogger(level='trace'):
    logger = JSObject({
        'level': level,
        'trace': noop,
        'debug': noop,
        'media': noop,
        'info': noop,
        'warn': noop,
        'error': noop,
    })

    def child(bindings=None, options=None):
        childLevel = optGet(options, 'level')
        return createNoopLogger(childLevel) if childLevel else logger

    logger['child'] = child
    return logger


def jsonReplacerBytes(value):
    if isinstance(value, (bytes, bytearray)):
        return f'<{len(value)} bytes>'
    raise TypeError(f'not serializable: {type(value).__name__}')


def safeJson(meta):
    try:
        return json.dumps(meta, default=jsonReplacerBytes, separators=(',', ':'), ensure_ascii=False)
    except Exception:
        return str(meta)


def createConsoleLogger(level='info', bindings=None):
    bindings = JSObject({}) if bindings is None else bindings
    threshold = LEVELS[level]
    if threshold is None:
        threshold = LEVELS['info']
    prefix = ' '.join(f'{key}={value}' for key, value in dict.items(bindings))

    def line(lvl, msg, meta=None):
        if LEVELS[lvl] < threshold:
            return
        extra = '' if meta is None else ' ' + safeJson(meta)
        tag = f'[voip {prefix}]' if prefix else '[voip]'
        stream = sys.stderr if lvl in ('error', 'warn') else sys.stdout
        print(f'{tag} {msg}{extra}', file=stream)

    def child(childBindings=None, options=None):
        childBindings = JSObject({}) if childBindings is None else childBindings
        childLevel = optGet(options, 'level')
        return createConsoleLogger(level if childLevel is None else childLevel, JSObject({**bindings, **childBindings}))

    return JSObject({
        'level': level,
        'trace': lambda msg, meta=None: line('trace', msg, meta),
        'debug': lambda msg, meta=None: line('debug', msg, meta),
        'media': lambda msg, meta=None: line('media', msg, meta),
        'info': lambda msg, meta=None: line('info', msg, meta),
        'warn': lambda msg, meta=None: line('warn', msg, meta),
        'error': lambda msg, meta=None: line('error', msg, meta),
        'child': child,
    })


def writeRandomPadMax16(message):
    padLength = (randomBytes(1)[0] & 0x0f) + 1
    return bytes(message) + bytes([padLength]) * padLength


def unpadPkcs7(value):
    if len(value) == 0:
        raise JSError('unpadPkcs7 given empty bytes')
    padLength = value[len(value) - 1]
    if padLength > len(value):
        raise JSError(f'unpadPkcs7 given {len(value)} bytes, but pad is {padLength}')
    return value[:len(value) - padLength]
