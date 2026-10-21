"""Shared, read-only WebSocket LTP relay for the configuration helper."""
from __future__ import annotations

import math
import os
import threading
import time
import uuid

class LiveQuoteHub:
    MAX_INSTRUMENTS = 160
    MAX_VIEWERS = 12

    def __init__(self, host):
        self.host = host
        self.client = None
        self.viewers = {}
        self.subscribed = set()
        self.prices = {}
        self.revision = 0
        self.subscription_lock = threading.Lock()
        self.state_lock = threading.Lock()

    def _on_ltp(self, message):
        if not isinstance(message, dict):
            return
        data = message.get("data") or {}
        try:
            price = float(data.get("ltp"))
        except (TypeError, ValueError):
            return
        if not math.isfinite(price) or price < 0:
            return
        symbol, exchange = message.get("symbol"), message.get("exchange")
        if not symbol or not exchange:
            return
        key = (str(exchange), str(symbol))
        with self.state_lock:
            self.prices[key] = {"ltp": price, "received_at": int(time.time() * 1000)}
            self.revision += 1

    @staticmethod
    def _instruments(keys):
        return [{"exchange": exchange, "symbol": symbol} for exchange, symbol in sorted(keys)]

    def acquire(self, instruments):
        keys = {(item["exchange"], item["symbol"]) for item in instruments}
        with self.subscription_lock:
            if len(self.viewers) >= self.MAX_VIEWERS:
                raise RuntimeError("Live helper is busy; try again shortly.")
            desired = keys | self.subscribed
            if len(desired) > self.MAX_INSTRUMENTS:
                raise RuntimeError("Live helper subscription limit reached; close another chain view.")
            if self.client is None:
                # FeedAPI deliberately exposes market data only, no order methods.
                from openalgo.feed import FeedAPI
                api_key = os.getenv("OPENALGO_API_KEY")
                if not api_key:
                    raise RuntimeError("Live market feed is not configured.")
                self.client = FeedAPI(
                    api_key=api_key, host=self.host,
                    ws_url=os.getenv("OPENALGO_WS_URL", "ws://127.0.0.1:8765"),
                    auto_reconnect=True, verbose=False,
                )
            if not self.client.connected or not self.client.authenticated:
                if self.client.ws_thread is not None and self.client.ws_thread.is_alive():
                    raise RuntimeError("Live feed is reconnecting; snapshot prices remain available.")
                if not self.client.connect():
                    raise RuntimeError("Live feed is unavailable; option-chain snapshot remains available.")
            additional = keys - self.subscribed
            if additional and not self.client.subscribe_ltp(
                instruments=self._instruments(additional), on_data_received=self._on_ltp
            ):
                raise RuntimeError("Live quote subscription could not be sent.")
            self.subscribed |= additional
            viewer = uuid.uuid4().hex
            self.viewers[viewer] = keys
            return viewer

    def snapshot(self, viewer):
        with self.subscription_lock:
            keys = set(self.viewers.get(viewer, set()))
            connected = bool(self.client and self.client.connected and self.client.authenticated)
        with self.state_lock:
            return {
                "revision": self.revision,
                "connected": connected,
                "prices": {
                    f"{exchange}:{symbol}": self.prices[(exchange, symbol)]
                    for exchange, symbol in keys if (exchange, symbol) in self.prices
                },
            }

    def release(self, viewer):
        with self.subscription_lock:
            self.viewers.pop(viewer, None)
            retained = set().union(*self.viewers.values()) if self.viewers else set()
            removed = self.subscribed - retained
            if removed and self.client:
                try:
                    self.client.unsubscribe_ltp(self._instruments(removed))
                except Exception:
                    pass
            self.subscribed = retained
            with self.state_lock:
                for key in removed:
                    self.prices.pop(key, None)
