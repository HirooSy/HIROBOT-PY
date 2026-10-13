import weakref

from baileys_lite.foundation.node_runtime import JSObject, clearInterval, now, setInterval


def noop(*args):
    return None


class Cache:
    def __init__(self, options=None):
        if options is None:
            options = JSObject({})
        ttl = options.get('ttl')
        if ttl is None:
            ttl = options.get('stdTTL') * 1000 if options.get('stdTTL') else 0
        self.ttl = ttl
        self.max = options.get('max') or float('inf')
        self.updateAgeOnGet = bool(options.get('updateAgeOnGet'))
        self.dispose = options.get('dispose') or noop
        self._store = {}
        self._sweeper = None

        if self.ttl > 0:
            selfRef = weakref.ref(self)
            holder = {}
            def sweepTick():
                cache = selfRef()
                if cache is None:
                    clearInterval(holder.get('timer'))
                    return
                cache._sweep()
            holder['timer'] = setInterval(sweepTick, min(self.ttl, 60000))
            self._sweeper = holder.get('timer')
            self._sweeper.unref()

    def _isExpired(self, entry):
        return self.ttl > 0 and entry.get('expiresAt') != 0 and entry.get('expiresAt') <= now()

    def _sweep(self):
        current = now()
        for key, entry in list(self._store.items()):
            if entry.get('expiresAt') != 0 and entry.get('expiresAt') <= current:
                del self._store[key]
                self.dispose(entry.get('value'), key)

    def get(self, key):
        entry = self._store.get(key)
        if not entry:
            return None
        if self._isExpired(entry):
            del self._store[key]
            self.dispose(entry.get('value'), key)
            return None
        if self.updateAgeOnGet and self.ttl > 0:
            entry['expiresAt'] = now() + self.ttl

            del self._store[key]
            self._store[key] = entry
        return entry.get('value')

    def set(self, key, value):
        if key in self._store:
            del self._store[key]
        self._store[key] = JSObject({
            'value': value,
            'expiresAt': now() + self.ttl if self.ttl > 0 else 0
        })
        while len(self._store) > self.max:
            oldestKey = next(iter(self._store))
            oldest = self._store[oldestKey]
            del self._store[oldestKey]
            self.dispose(oldest.get('value'), oldestKey)
        return True

    def has(self, key):
        entry = self._store.get(key)
        if not entry:
            return False
        if self._isExpired(entry):
            del self._store[key]
            self.dispose(entry.get('value'), key)
            return False
        return True

    def delete(self, key):
        entry = self._store.get(key)
        if not entry:
            return False
        del self._store[key]
        self.dispose(entry.get('value'), key)
        return True

    def clear(self):
        for key, entry in list(self._store.items()):
            self.dispose(entry.get('value'), key)
        self._store.clear()

    def flushAll(self):
        self.clear()

    def del_(self, key):
        return self.delete(key)

    def close(self):
        if self._sweeper is not None:
            clearInterval(self._sweeper)
            self._sweeper = None

    def mget(self, keys):
        result = JSObject({})
        for key in keys:
            value = self.get(key)
            if value is not None:
                result[key] = value
        return result

    def mset(self, data):
        for item in data:
            self.set(item.get('key'), item.get('value'))
        return True

    @property
    def size(self):
        return len(self._store)


LRUCache = Cache
NodeCache = Cache
