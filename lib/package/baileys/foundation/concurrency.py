import asyncio
import inspect

from baileys.foundation.node_runtime import JSObject


class SerialQueue:
    def __init__(self):
        self._tail = None

    def run(self, task):
        previous = self._tail

        async def runTask():
            if previous is not None:
                try:
                    await previous
                except BaseException:
                    pass
            result = task()
            if inspect.isawaitable(result):
                result = await result
            return result

        current = asyncio.ensure_future(runTask())
        self._tail = current
        return current


def makeMutex():
    queue = SerialQueue()

    def mutex(task):
        return queue.run(task)
    return JSObject({'mutex': mutex})


def makeKeyedMutex():
    lanes = {}

    async def mutex(key, task):
        entry = lanes.get(key)
        if not entry:
            entry = JSObject({'queue': SerialQueue(), 'refCount': 0})
            lanes[key] = entry
        entry['refCount'] += 1
        try:
            return await entry.get('queue').run(task)
        finally:
            entry['refCount'] -= 1
            if entry.get('refCount') == 0 and lanes.get(key) is entry:
                del lanes[key]
    return JSObject({'mutex': mutex})


def makeSerialTaskQueue():
    queue = SerialQueue()

    def add(task):
        return queue.run(task)
    return JSObject({'add': add})


def makeKeyedSerialTaskQueues():
    queues = {}

    def get(key):
        q = queues.get(key)
        if q is None:
            q = makeSerialTaskQueue()
            queues[key] = q
        return q
    return JSObject({'get': get})
