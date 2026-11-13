"""Bounded LRU observation cache with fixed TTL and shared synchronous flights."""
from collections import OrderedDict
from concurrent.futures import Future
import threading
import time
import sys

def _memory_size(value,seen=None):
    seen=set() if seen is None else seen
    identity=id(value)
    if identity in seen: return 0
    seen.add(identity); size=sys.getsizeof(value)
    if isinstance(value,dict): return size+sum(_memory_size(k,seen)+_memory_size(v,seen) for k,v in value.items())
    if isinstance(value,(list,tuple,set,frozenset)): return size+sum(_memory_size(v,seen) for v in value)
    if hasattr(value,'__dict__'): size+=_memory_size(value.__dict__,seen)
    return size

class TrafficCache:
    def __init__(self,max_entries=256,max_bytes=32*1024*1024,ttl=1800,clock=time.time):
        self.max_entries=max_entries; self.max_bytes=max_bytes; self.ttl=min(1800,ttl); self.clock=clock
        self._entries=OrderedDict(); self._flights={}; self._bytes=0; self._lock=threading.RLock()

    def get(self,key):
        with self._lock:
            item=self._entries.get(key)
            if item is None: return None
            value,expiry,size=item
            if self.clock()>=expiry:
                self._entries.pop(key); self._bytes-=size; return None
            self._entries.move_to_end(key); return value

    def put(self,key,value,received_at=None):
        received_at=self.clock() if received_at is None else received_at
        size=_memory_size(value)+_memory_size(key)+256
        expiry=received_at+self.ttl
        if isinstance(value,(tuple,list)) and value and hasattr(value[0],'expires_at'):
            expiry=min(expiry,min(o.expires_at for o in value))
        with self._lock:
            previous=self._entries.pop(key,None)
            if previous: self._bytes-=previous[2]
            if size>self.max_bytes or expiry<=self.clock(): return
            self._entries[key]=(value,expiry,size); self._bytes+=size
            while self._entries and (len(self._entries)>self.max_entries or self._bytes>self.max_bytes):
                _,item=self._entries.popitem(last=False); self._bytes-=item[2]

    def get_or_load(self,key,loader,timeout=None):
        with self._lock:
            cached=self.get(key)
            if cached is not None: return cached
            future=self._flights.get(key); owner=future is None
            if owner:
                future=Future(); self._flights[key]=future
        if not owner: return future.result(timeout=timeout)
        try:
            value=loader(); self.put(key,value); future.set_result(value); return value
        except BaseException as exc:
            future.set_exception(exc); raise
        finally:
            with self._lock: self._flights.pop(key,None)

    @property
    def size_bytes(self): return self._bytes

    def values(self):
        with self._lock:
            return [value for key in list(self._entries) if (value:=self.get(key)) is not None]

OBSERVATION_CACHE=TrafficCache()
