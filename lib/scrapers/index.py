import os
import sys

from lib.utils import helper


SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')


class Scraper:
    def __init__(self, directory):
        self._dir = directory
        self._cache = {}

    def _path(self, name):
        return os.path.join(self._dir, f'{name}.py')

    def _module(self, name):
        path = self._path(name)
        if not os.path.exists(path):
            self._cache.pop(name, None)
            return None
        mtime = os.path.getmtime(path)
        cached = self._cache.get(name)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            mod = helper.import_file(path)
        except Exception as e:
            print(f'[scraper:{name}]', e, file=sys.stderr)
            self._cache.pop(name, None)
            return None
        self._cache[name] = (mtime, mod)
        return mod

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return self._module(name)

    def __getitem__(self, name):
        return getattr(self, name.replace('-', '_'))

    def names(self):
        return sorted(f[:-3] for f in os.listdir(self._dir) if f.endswith('.py') and not f.startswith('_'))

    def load(self):
        loaded = []
        for name in self.names():
            if self._module(name) is not None:
                loaded.append(name)
        return loaded

    def reload(self, name):
        key = name.replace('-', '_')
        self._cache.pop(key, None)
        return self._module(key)


scraper = Scraper(SRC_DIR)
