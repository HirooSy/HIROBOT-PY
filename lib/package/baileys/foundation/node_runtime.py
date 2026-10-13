import asyncio
import threading
import time


_NO_DEFAULT = object()


class JSObject(dict):
    __slots__ = ()

    def __missing__(self, key):
        return None

    def get(self, key, default=_NO_DEFAULT):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        if default is _NO_DEFAULT:
            return self.__missing__(key)
        return default

    def __getattr__(self, key):
        if key.startswith('__'):
            raise AttributeError(key)
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        raise AttributeError(f"'JSObject' object has no attribute '{key}'")


class JSError(Exception):
    def __init__(self, message='', **props):
        super().__init__(message)
        self.message = message
        self.name = 'Error'
        self.code = None
        self.cause = None
        for key, value in props.items():
            setattr(self, key, value)

    def __str__(self):
        return str(self.message)


class EventEmitter:
    def __init__(self):
        self._events = {}
        self._maxListeners = 10

    def _ensure(self):
        if '_events' not in self.__dict__:
            self._events = {}
        return self._events

    def on(self, event, listener):
        events = self._ensure()
        events.setdefault(event, []).append((listener, False))
        return self

    addListener = on

    def prependListener(self, event, listener):
        events = self._ensure()
        events.setdefault(event, []).insert(0, (listener, False))
        return self

    def once(self, event, listener):
        events = self._ensure()
        events.setdefault(event, []).append((listener, True))
        return self

    def prependOnceListener(self, event, listener):
        events = self._ensure()
        events.setdefault(event, []).insert(0, (listener, True))
        return self

    def off(self, event, listener):
        events = self._ensure()
        entries = events.get(event)
        if not entries:
            return self
        for index in range(len(entries) - 1, -1, -1):
            if entries[index][0] is listener:
                del entries[index]
                break
        if not entries:
            del events[event]
        return self

    removeListener = off

    def removeAllListeners(self, event=None):
        events = self._ensure()
        if event is None:
            events.clear()
        else:
            events.pop(event, None)
        return self

    def emit(self, event, *args):
        events = self._ensure()
        entries = events.get(event)
        if not entries:
            if event == 'error':
                err = args[0] if args else None
                if isinstance(err, BaseException):
                    raise err
                raise JSError('Unhandled error.', cause=err)
            return False
        snapshot = list(entries)
        for listener, once in snapshot:
            if once:
                current = events.get(event)
                if current is not None:
                    for index, entry in enumerate(current):
                        if entry[0] is listener and entry[1]:
                            del current[index]
                            break
                    if not current:
                        events.pop(event, None)
            listener(*args)
        return True

    def listeners(self, event):
        return [entry[0] for entry in self._ensure().get(event, [])]

    def rawListeners(self, event):
        return self.listeners(event)

    def listenerCount(self, event):
        return len(self._ensure().get(event, []))

    def eventNames(self):
        return [name for name, entries in self._ensure().items() if entries]

    def setMaxListeners(self, n):
        self._maxListeners = n
        return self

    def getMaxListeners(self):
        return self._maxListeners


class TimerHandle:
    def __init__(self):
        self._handle = None
        self._timer = None
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        if self._handle is not None:
            self._handle.cancel()
        if self._timer is not None:
            self._timer.cancel()

    def unref(self):
        return self

    def ref(self):
        return self


def _schedule(delay_seconds, callback):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        return loop.call_later(delay_seconds, callback)
    timer = threading.Timer(delay_seconds, callback)
    timer.daemon = True
    timer.start()
    return timer


def setTimeout(callback, ms=0, *args):
    handle = TimerHandle()

    def run():
        if not handle.cancelled:
            handle.cancelled = True
            callback(*args)

    scheduled = _schedule(max(ms or 0, 0) / 1000.0, run)
    if isinstance(scheduled, threading.Timer):
        handle._timer = scheduled
    else:
        handle._handle = scheduled
    return handle


def clearTimeout(handle):
    if handle is not None:
        handle.cancel()


def setInterval(callback, ms=0, *args):
    handle = TimerHandle()
    interval = max(ms or 0, 1) / 1000.0

    def tick():
        if handle.cancelled:
            return
        callback(*args)
        if not handle.cancelled:
            scheduled = _schedule(interval, tick)
            if isinstance(scheduled, threading.Timer):
                handle._timer = scheduled
            else:
                handle._handle = scheduled

    scheduled = _schedule(interval, tick)
    if isinstance(scheduled, threading.Timer):
        handle._timer = scheduled
    else:
        handle._handle = scheduled
    return handle


def clearInterval(handle):
    if handle is not None:
        handle.cancel()


def setImmediate(callback, *args):
    return setTimeout(callback, 0, *args)


def queueMicrotask(callback, *args):
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon(callback, *args)
    except RuntimeError:
        callback(*args)


def nextTick(callback, *args):
    queueMicrotask(callback, *args)


async def sleep(ms=0):
    await asyncio.sleep(max(ms or 0, 0) / 1000.0)


def now():
    return time.time_ns() // 1000000


def performanceNow():
    return time.perf_counter() * 1000.0


class AbortSignal(EventEmitter):
    def __init__(self):
        super().__init__()
        self.aborted = False
        self.reason = None
        self.onabort = None

    def throwIfAborted(self):
        if self.aborted:
            raise self.reason if isinstance(self.reason, BaseException) else JSError('This operation was aborted')

    def addEventListener(self, event, listener, options=None):
        if isinstance(options, dict) and options.get('once'):
            return self.once(event, listener)
        return self.on(event, listener)

    def removeEventListener(self, event, listener):
        return self.off(event, listener)

    def _abort(self, reason):
        if self.aborted:
            return
        self.aborted = True
        self.reason = reason
        event = JSObject({'type': 'abort', 'target': self})
        if callable(self.onabort):
            self.onabort(event)
        self.emit('abort', event)

    @staticmethod
    def timeout(ms):
        controller = AbortController()
        handle = setTimeout(lambda: controller._abort(JSError('The operation was aborted due to timeout', name='TimeoutError')), ms)
        controller.signal._timeoutHandle = handle
        return controller.signal

    @staticmethod
    def abort(reason=None):
        controller = AbortController()
        controller.abort(reason)
        return controller.signal


class AbortController:
    def __init__(self):
        self.signal = AbortSignal()

    def abort(self, reason=None):
        if reason is None:
            reason = JSError('This operation was aborted', name='AbortError')
        self.signal._abort(reason)


def jsonDefault(value):
    if isinstance(value, (bytes, bytearray)):
        return {'type': 'Buffer', 'data': list(value)}
    if hasattr(value, 'toNumber'):
        return value.toNumber()
    if hasattr(value, 'toJSON'):
        return value.toJSON()
    raise TypeError(f'Object of type {type(value).__name__} is not JSON serializable')
