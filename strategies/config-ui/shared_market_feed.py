"""One persistent automatic-mode LTP socket, with isolated consumer subscriptions."""
from __future__ import annotations
import logging
import threading
import time

_REGISTRY = {}
_REGISTRY_LOCK = threading.Lock()


def shared_feed(factory):
    with _REGISTRY_LOCK:
        hub = _REGISTRY.get(factory)
        if hub is None:
            hub = _REGISTRY[factory] = SharedFeed(factory())
        return hub


class SharedFeed:
    def __init__(self, client):
        self.client = client
        self._guard = threading.RLock()
        self._io = threading.RLock()
        self._consumers = {}
        self._subscribed = set()
        self._latest = {}
        self.connect_count = 0
        self.disconnect_count = 0
        self.logger = logging.getLogger('automatic_shared_feed')

    def acquire(self, token, callback):
        with self._guard:
            if token in self._consumers:
                raise RuntimeError('Feed consumer is already registered')
            self._consumers[token] = (set(), callback)
        return FeedLease(self, token)

    def connect(self):
        with self._io:
            if self.client.connected and self.client.authenticated:
                return True
            reconnect = getattr(self.client, '_reconnect_thread', None)
            if reconnect is not None and reconnect.is_alive():
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if self.client.connected and self.client.authenticated:
                        return True
                    if not reconnect.is_alive():
                        break
                    time.sleep(.1)
            self.connect_count += 1
            return bool(self.client.connect())

    @staticmethod
    def _instruments(keys):
        return [{'exchange': exchange, 'symbol': symbol} for exchange, symbol in sorted(keys)]

    def update(self, token, keys, callback=None, replay=False, refresh=False):
        with self._io:
            with self._guard:
                old = self._consumers.get(token)
                if old is None:
                    raise RuntimeError('Feed lease has been released')
                self._consumers[token] = (set(keys), callback or old[1])
                desired = set().union(*(entry[0] for entry in self._consumers.values()))
            if not self.connect():
                return False
            missing = desired if refresh else desired - self._subscribed
            if missing:
                if not self.client.subscribe_ltp(self._instruments(missing), on_data_received=self._dispatch):
                    return False
                self._subscribed |= missing
            obsolete = self._subscribed - desired
            if obsolete:
                if self.client.unsubscribe_ltp(self._instruments(obsolete)):
                    self._subscribed -= obsolete
                    with self._guard:
                        for key in obsolete:
                            self._latest.pop(key, None)
            with self._guard:
                current_callback = self._consumers[token][1]
                latest = [packet[1] for key in keys if (packet := self._latest.get(key))
                          and time.monotonic() - packet[0] <= 3]
        if replay:
            for message in latest:
                current_callback(message)
        return True

    def _dispatch(self, message):
        if not isinstance(message, dict):
            return
        message = {**message, '_received_monotonic': time.monotonic()}
        key = (message.get('exchange'), message.get('symbol'))
        with self._guard:
            callbacks = [entry[1] for entry in self._consumers.values() if key in entry[0]]
            if not callbacks:
                return
            self._latest[key] = (time.monotonic(), message)
        for callback in callbacks:
            try:
                callback(message)
            except Exception:
                self.logger.exception('Shared LTP consumer callback failed')

    def release(self, token):
        with self._io:
            with self._guard:
                self._consumers.pop(token, None)
                desired = set().union(*(entry[0] for entry in self._consumers.values())) if self._consumers else set()
                last = not self._consumers
            obsolete = self._subscribed - desired
            if obsolete and self.client.connected and self.client.authenticated:
                if self.client.unsubscribe_ltp(self._instruments(obsolete)):
                    self._subscribed -= obsolete
            if last:
                self.client.disconnect()
                self.disconnect_count += 1
                self._subscribed.clear()
                store = getattr(self.client, '_active_subs', {})
                if 1 in store:
                    store[1].clear()
                with self._guard:
                    self._latest.clear()

    def metrics(self):
        with self._guard:
            return {'feed_connections': self.connect_count, 'feed_disconnects': self.disconnect_count,
                    'feed_consumers': len(self._consumers), 'feed_subscriptions': len(self._subscribed)}


class FeedLease:
    def __init__(self, hub, token):
        self.hub, self.token = hub, token
        self.keys = set()
        self._closed = False

    @property
    def connected(self):
        return self.hub.client.connected and not self._closed

    @property
    def authenticated(self):
        return self.hub.client.authenticated and not self._closed

    def connect(self):
        return not self._closed and self.hub.connect()

    def set_instruments(self, instruments, on_data_received=None, replay=False, refresh=False):
        self.keys = {(item['exchange'], item['symbol']) for item in instruments}
        return self.hub.update(self.token, self.keys, on_data_received, replay, refresh)

    def subscribe_ltp(self, instruments, on_data_received=None):
        keys = self.keys | {(item['exchange'], item['symbol']) for item in instruments}
        return self.set_instruments(self.hub._instruments(keys), on_data_received, replay=True)

    def disconnect(self):
        if not self._closed:
            self._closed = True
            self.hub.release(self.token)
