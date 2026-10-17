import threading
import time
from collections import OrderedDict


class CacheManager:
    def __init__(self, ttl_seconds=30, max_size=100):
        self._cache = OrderedDict()
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max_size = max_size

    def get(self, key, default=None):
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if entry and now - entry["ts"] < self._ttl:
                self._cache.move_to_end(key)
                return entry["data"]
            if entry:
                del self._cache[key]
        return default

    def set(self, key, data):
        now = time.time()
        with self._lock:
            self._cache[key] = {"data": data, "ts": now}
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def invalidate(self, key=None):
        with self._lock:
            if key:
                self._cache.pop(key, None)
            else:
                self._cache.clear()

    def get_or_set(self, key, factory):
        cached = self.get(key)
        if cached is not None:
            return cached
        data = factory()
        self.set(key, data)
        return data

    def delete(self, key):
        with self._lock:
            self._cache.pop(key, None)

    def delete_prefix(self, prefix):
        with self._lock:
            keys = [key for key in self._cache if str(key).startswith(prefix)]
            for key in keys:
                self._cache.pop(key, None)

    def clear(self):
        with self._lock:
            self._cache.clear()

    def has(self, key):
        return self.get(key) is not None

    def size(self):
        with self._lock:
            return len(self._cache)
