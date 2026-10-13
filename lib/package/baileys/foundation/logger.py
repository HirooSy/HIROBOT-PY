import json
import sys
import traceback

from baileys.foundation.node_runtime import JSObject, now

LEVELS = JSObject({'trace': 10, 'debug': 20, 'info': 30, 'warn': 40, 'error': 50, 'fatal': 60, 'silent': 1000})


def serializeError(err):
    if not isinstance(err, BaseException):
        return err
    stack = ''.join(traceback.format_exception(type(err), err, err.__traceback__))
    return JSObject({'type': err.name if hasattr(err, 'name') else type(err).__name__, 'message': str(err), 'stack': stack, **vars(err)})


def jsonDefault(value):
    if isinstance(value, (bytes, bytearray)):
        return JSObject({'type': 'Buffer', 'data': list(value)})
    if isinstance(value, BaseException):
        return str(value)
    return str(value)


def isObjectLike(value):
    return value is not None and not isinstance(value, (str, int, float, bool))


class Logger:
    def __init__(self, bindings=None, level='info'):
        self.bindings = bindings if bindings is not None else JSObject({})
        self.state = JSObject({'level': level})

    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)

    @property
    def level(self):
        return self.state.get('level')

    @level.setter
    def level(self, value):
        self.state['level'] = value

    def write(self, levelName, args):
        threshold = LEVELS[self.state.get('level')]
        if threshold is not None and LEVELS[levelName] < threshold:
            return

        obj = JSObject({})
        msg = ''
        if len(args) and isObjectLike(args[0]):
            obj = args[0]
            msg = args[1] if len(args) > 1 and args[1] is not None else ''
        else:
            msg = args[0] if len(args) and args[0] is not None else ''
        if isinstance(obj, dict):
            obj = JSObject(obj)
        else:
            obj = JSObject(vars(obj)) if hasattr(obj, '__dict__') else JSObject({})
        if obj.get('err'):
            obj = JSObject({**obj, 'err': serializeError(obj.get('err'))})

        line = JSObject({
            'level': LEVELS[levelName],
            'time': now(),
            **self.bindings,
            **obj,
            'msg': msg
        })

        out = sys.stderr if levelName in ('error', 'fatal') else sys.stdout
        out.write(json.dumps(line, default=jsonDefault) + '\n')

    def trace(self, *args):
        return self.write('trace', args)

    def debug(self, *args):
        return self.write('debug', args)

    def info(self, *args):
        return self.write('info', args)

    def media(self, *args):
        return self.write('info', args)

    def warn(self, *args):
        return self.write('warn', args)

    def error(self, *args):
        return self.write('error', args)

    def fatal(self, *args):
        return self.write('fatal', args)

    def child(self, childBindings=None):
        merged = JSObject({**self.bindings, **(childBindings if childBindings is not None else {})})
        return Logger(merged, self.state.get('level'))


def createLogger(bindings=None, level='info'):
    return Logger(bindings, level)


default = createLogger()
