"""Two independent premium-selected workers, using the existing SRSI engine."""
from __future__ import annotations
import copy
import math
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from runtime_control import BASE, atomic_json, config_lock
from shared_market_feed import shared_feed

JOURNAL = BASE / 'automatic_contracts.json'
STATUS_FILE = BASE / 'automatic_status.json'
IST = ZoneInfo('Asia/Kolkata')
_CHAIN_CACHE = {}
_CHAIN_LOCK = threading.Lock()


def journal_entries():
    import json
    return json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {}


def save_selected(name, value):
    with config_lock():
        rows = journal_entries()
        if value is None:
            rows.pop(name, None)
        else:
            rows[name] = value
        atomic_json(JOURNAL, rows)


def strike_search_order(strikes, spot, side, levels=15):
    """ATM, then outward to OTM, then the opposite direction; listed strikes only."""
    strikes = sorted(set(strikes))
    if not strikes or not math.isfinite(spot) or spot <= 0:
        return []
    atm = min(range(len(strikes)), key=lambda i: (abs(strikes[i] - spot), -strikes[i]))
    direction = 1 if side == 'CE' else -1
    indices = [atm] + [atm + direction * n for n in range(1, levels + 1)]
    indices += [atm - direction * n for n in range(1, levels + 1)]
    return [strikes[i] for i in indices if 0 <= i < len(strikes)]


class AutomaticWorker:
    def __init__(self, row, settings, engine):
        self.configuration = copy.deepcopy(row)
        self.settings = copy.deepcopy(settings)
        self.engine = engine
        self.worker_name = row['name']
        self.runtime_pause = threading.Event()
        self.stop_event = threading.Event()
        self.runtime_state_lock = threading.RLock()
        self.runtime_retired_safely = False
        self._ready = False
        self.child = None
        self._state = SimpleNamespace(phase='WAITING_TO_BUY', position_quantity=0, pending=None, halted=False)
        self._selection = {'code':'starting', 'status': 'Starting scanner', 'confirmed':False, 'symbol': None, 'ltp': None, 'atm': None, 'spot': None}
        self._status_saved_at = 0
        self._status_logged_at = 0
        self._last_valid_tick = None
        self._rejected_ticks = 0
        self._retry_count = 0
        # Import lazily: the entry point defines the existing engine before importing us.
        import strategy
        engine_globals = getattr(engine.__init__, '__globals__', {})
        self.client_factory = engine_globals.get('make_client', strategy.make_client)
        self.feed_hub = shared_feed(self.client_factory)
        self.logger = strategy.make_logger(self.worker_name)
        self.wake = threading.Event()
        self.prices = {}
        self.prices_lock = threading.Lock()
        self.scan_client = None
        self.settings['entry_premium_min'] = max(float(settings['entry_premium_min']), row['premium_min'])
        self.settings['entry_premium_max'] = min(float(settings['entry_premium_max']), row['premium_max'])
        if self.settings['entry_premium_min'] > self.settings['entry_premium_max']:
            raise ValueError('Shared entry premium band does not overlap automatic premium band')

    @property
    def state(self):
        return self.child.state if self.child else self._state

    @property
    def runtime_ready(self):
        return self.child.runtime_ready if self.child else self._ready

    def _set_status(self, code, text, **details):
        with self.runtime_state_lock:
            self._selection.update(code=code, status=text, **details)
        now = time.monotonic()
        if now - self._status_logged_at >= 15:
            self._status_logged_at = now
            self.logger.info('[AUTO STATUS] %s code=%s %s spot=%s ATM=%s fresh=%s/%s rejected=%s',
                self.worker_name, code, text, self._selection.get('spot'), self._selection.get('atm'),
                self._selection.get('fresh_candidates', 0), self._selection.get('scanned', 0), self._rejected_ticks)

    def runtime_details(self):
        with self.runtime_state_lock:
            info = dict(self._selection)
            info.update(underlying=self.configuration['underlying'], expiry_date=self.configuration['expiry_date'], lots=self.configuration['lots'], candle_minutes=self.configuration['candle_minutes'])
            child = self.child
            client = child.market_data_client if child else self.scan_client
            info.update(self.feed_hub.metrics())
            info.update(connected=bool(getattr(client, 'connected', False)),
                        authenticated=bool(getattr(client, 'authenticated', False)),
                        rejected_ticks=self._rejected_ticks, retries=self._retry_count)
            if child:
                age = time.monotonic() - child.last_ws_tick_monotonic if child.last_ws_tick_monotonic is not None else None
                phase = child.state.phase
                code = ('halted' if child.state.halted else 'buy_pending' if phase == 'BUY_PENDING' else
                        'sell_pending' if phase == 'SELL_PENDING' else 'waiting_to_sell' if child.state.position_quantity else
                        'preparing_contract' if not child.runtime_ready else 'waiting_to_buy')
                info.update(symbol=child.symbol, strike=child.strike_price, confirmed=True, ltp=child.state.last_ltp,
                            lot_size=child.lot_size, order_quantity=child.order_quantity,
                            history_cache=child.history_cache_status, srsi_cache_hits=child.srsi_cache_hits,
                            srsi_reference_checks=child.srsi_reference_checks,
                            k=child.state.last_k if math.isfinite(child.state.last_k) else None,
                            entry_k=child.entry_k_value, exit_k=child.exit_k_level,
                            code=code, tick_age_seconds=round(age,1) if age is not None else None,
                            feed_stale=child.state.last_ltp is None or age is None or age > 15,
                            entry_price=child.state.entry_average_price or None,
                            stop_price=child.state.entry_average_price-child.stop_loss_points if child.state.entry_average_price else None,
                            status='Managing held contract' if child.state.position_quantity or child.state.pending else
                                   'Loading history and reconciling orders' if not child.runtime_ready else 'Watching entry signal')
            else:
                with self.prices_lock:
                    tick = self.prices.get((self.configuration['underlying_exchange'], self.configuration['underlying']))
                info.update(spot_age_seconds=round(time.monotonic()-tick[1],1) if tick else None)
            info['updated_at'] = datetime.now(IST).isoformat(timespec='seconds')
        # Keep the last observed state visible when the parent process is stopped.
        if time.monotonic() - self._status_saved_at >= 15:
            self._status_saved_at = time.monotonic()
            try:
                import json
                with config_lock():
                    rows = json.loads(STATUS_FILE.read_text()) if STATUS_FILE.exists() else {}
                    rows[self.worker_name] = info
                    atomic_json(STATUS_FILE, rows)
            except Exception:
                self.logger.warning('[AUTO STATUS] Could not persist status snapshot')
        return info

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.child:
            self.child.stop()

    def _on_tick(self, message):
        try:
            data = message.get('data') or {}
            price = float(data.get('ltp', 0))
            if not math.isfinite(price) or price <= 0:
                return
            stamp = float(data.get('timestamp', 0) or 0)
            if stamp > 100_000_000_000:
                stamp /= 1000
            if stamp and abs(time.time() - stamp) > 3:
                self._rejected_ticks += 1
                return
            key = (message.get('exchange'), message.get('symbol'))
            with self.prices_lock:
                self.prices[key] = (price, float(message.get('_received_monotonic') or time.monotonic()))
            self._last_valid_tick = time.monotonic()
            self.wake.set()
        except (AttributeError, TypeError, ValueError):
            self._rejected_ticks += 1
            return

    def _price(self, exchange, symbol):
        with self.prices_lock:
            tick = self.prices.get((exchange, symbol))
        return tick[0] if tick and time.monotonic() - tick[1] <= 3 else None

    def _load_chain(self, force=False):
        row = self.configuration
        self._set_status('loading_chain', 'Loading expiry and strike metadata', last_error=None)
        cache_key = (row['underlying'], row['underlying_exchange'], row['expiry_date'])
        with _CHAIN_LOCK:
            cached = _CHAIN_CACHE.get(cache_key)
            if not force and cached and time.monotonic()-cached[0] < 60:
                response = cached[1]
            else:
                # Metadata uses REST; it never creates another live-feed socket.
                client = self.client_factory()
                response = client.optionchain(underlying=row['underlying'], exchange=row['underlying_exchange'],
                                              expiry_date=row['expiry_date'], strike_count=2*row['max_levels'])
                if isinstance(response, dict) and response.get('status') == 'success':
                    _CHAIN_CACHE[cache_key] = (time.monotonic(), response)
        if not isinstance(response, dict) or response.get('status') != 'success':
            code = response.get('code') if isinstance(response, dict) else None
            message = str(response.get('message', '')) if isinstance(response, dict) else ''
            raise RuntimeError('Broker rate limit (HTTP 429)' if code == 429 or '429' in message else 'Option chain unavailable')
        engine_globals = getattr(self.engine.__init__, '__globals__', {})
        lock, cache = engine_globals.get('OPTION_CHAIN_CACHE_LOCK'), engine_globals.get('OPTION_CHAIN_CACHE')
        if lock is not None and cache is not None:
            with lock:
                cache[cache_key] = response.get('chain', [])
        chain = {int(float(item['strike'])): item for item in response.get('chain', [])
                 if isinstance(item, dict) and (item.get(row['option_type'].lower()) or {}).get('symbol')}
        initial_spot = float(response.get('underlying_ltp') or response.get('atm_strike') or 0)
        if not chain or not math.isfinite(initial_spot) or initial_spot <= 0:
            raise RuntimeError('No valid contract metadata or spot for subscription window')
        return chain, initial_spot

    def _select(self):
        row = self.configuration
        started_at = time.monotonic()
        chain, anchor_spot = self._load_chain()
        if self.scan_client is None:
            self.scan_client = self.feed_hub.acquire(('scanner', id(self)), self._on_tick)
        client = self.scan_client
        self._set_status('connecting_feed', 'Connecting shared live spot and option feed')
        if not client.connect():
            raise RuntimeError('Live scanner feed unavailable')
        subscriptions = set()
        refresh_at = time.monotonic()+60
        last_spot_at = time.monotonic()
        window_deadline = time.monotonic()+2
        initial = self._price(row['underlying_exchange'], row['underlying']) or anchor_spot
        window = strike_search_order(chain, initial, row['option_type'], row['max_levels'])
        while not self.stop_event.is_set() and not self.runtime_pause.is_set():
            desired = {(row['underlying_exchange'], row['underlying'])}
            desired.update((row['option_exchange'], chain[strike][row['option_type'].lower()]['symbol']) for strike in window)
            if desired != subscriptions or not client.connected or not client.authenticated:
                self._set_status('subscribing', 'Subscribing to shared live candidate premiums', subscribed_candidates=len(desired)-1)
                if not client.set_instruments([{'exchange': ex, 'symbol': sy} for ex, sy in sorted(desired)], replay=True):
                    raise RuntimeError('Scanner subscriptions rejected')
                subscriptions = desired
                window_deadline = time.monotonic()+2
            self.wake.wait(.5)
            self.wake.clear()
            if self.stop_event.is_set() or self.runtime_pause.is_set():
                break
            now = datetime.now(IST)
            minutes = now.hour*60+now.minute
            if now.weekday() >= 5 or not (555 <= minutes < 915):
                cutoff = now.weekday() < 5 and 915 <= minutes < 930
                self._set_status('entry_cutoff' if cutoff else 'market_closed',
                                 'Entry cutoff reached · no new buys' if cutoff else 'Outside market hours · waiting for next session')
                continue
            spot = self._price(row['underlying_exchange'], row['underlying'])
            if spot is None:
                self._set_status('waiting_spot', 'Waiting for fresh spot price')
                if time.monotonic()-last_spot_at >= 20:
                    # Repair subscriptions without destroying another worker's held-contract feed.
                    if not client.set_instruments([{'exchange': ex, 'symbol': sy} for ex, sy in sorted(desired)], refresh=True):
                        raise RuntimeError('Shared spot feed unavailable')
                    raise RuntimeError('No fresh index spot tick for 20 seconds')
                continue
            last_spot_at = time.monotonic()
            order = strike_search_order(chain, spot, row['option_type'], row['max_levels'])
            listed = sorted(chain)
            original_atm = min(listed, key=lambda strike: (abs(strike-anchor_spot), -strike))
            recenter = abs(listed.index(order[0])-listed.index(original_atm)) > row['max_levels']
            if recenter or time.monotonic() >= refresh_at:
                self._set_status('refreshing_chain', 'Refreshing metadata on the existing live feed')
                chain, anchor_spot = self._load_chain(force=recenter)
                refresh_at = time.monotonic()+60
                window = strike_search_order(chain, spot, row['option_type'], row['max_levels'])
                continue
            self._selection.update(spot=spot, atm=order[0], scanned=len(order))
            if set(order) != set(window):
                window = order
                continue
            quotes = {strike:self._price(row['option_exchange'], chain[strike][row['option_type'].lower()]['symbol']) for strike in order}
            fresh = sum(value is not None for value in quotes.values())
            eligible = next((strike for strike in order if quotes[strike] is not None
                             and row['premium_min'] <= quotes[strike] <= row['premium_max']), None)
            # Select immediately when all earlier candidates have fresh quotes.
            # Missing earlier quotes get the existing two-second grace before skipping them.
            earlier_ready = eligible is not None and all(quotes[strike] is not None for strike in order[:order.index(eligible)+1])
            if not earlier_ready and time.monotonic() < window_deadline and fresh < len(order):
                self._set_status('receiving_prices', 'Waiting for required candidate quotes', fresh_candidates=fresh)
                continue
            if eligible is not None:
                premium = quotes[eligible]
                leg = chain[eligible][row['option_type'].lower()]
                selected = {**row, 'strike_price':eligible}
                elapsed = round((time.monotonic()-started_at)*1000,1)
                self._set_status('selected', 'Contract selected · loading its history', symbol=leg['symbol'], strike=eligible,
                                 ltp=premium, confirmed=True, fresh_candidates=fresh, selection_ms=elapsed, last_error=None)
                self.logger.info('[AUTO SELECT] %s spot=%.2f ATM=%d selected=%s premium=%.2f latency_ms=%.1f',
                                 row['name'], spot, order[0], leg['symbol'], premium, elapsed)
                # Retain spot and the selected contract through history loading and child handoff.
                client.set_instruments([{'exchange':row['underlying_exchange'],'symbol':row['underlying']},
                                        {'exchange':row['option_exchange'],'symbol':leg['symbol']}])
                self._retry_count = 0
                return selected
            candidates = [(min(abs(value-row['premium_min']),abs(value-row['premium_max'])),strike,value)
                          for strike,value in quotes.items() if value is not None]
            nearest = min(candidates) if candidates else None
            self._set_status('no_match' if fresh else 'waiting_option_prices',
                             'No fresh premium inside the configured band' if fresh else 'No fresh candidate option prices received',
                             fresh_candidates=fresh, nearest_strike=nearest[1] if nearest else None,
                             nearest_premium=nearest[2] if nearest else None)
        return None

    def _child(self, selected):
        owner, parent = self, self.engine
        class SelectedWorker(parent):
            def _connect_market_data(self):
                self.market_data_client = owner.feed_hub.acquire(('selected', id(self)), self._on_ltp)
                try:
                    while not self.stop_event.is_set():
                        if self._runtime_retire_if_flat():
                            self.market_data_client.disconnect()
                            return False
                        if self.market_data_client.subscribe_ltp(
                                [{'exchange':self.option_exchange,'symbol':self.symbol}], on_data_received=self._on_ltp):
                            self.logger.info('[WS SHARED] %s uses persistent automatic feed', self.symbol)
                            return True
                        self.stop_event.wait(5)
                    self.market_data_client.disconnect()
                    return False
                except Exception:
                    self.market_data_client.disconnect()
                    raise

            def _disconnect_market_data(self):
                self.market_data_client.disconnect()

            def _runtime_retire_if_flat(self):
                if owner.runtime_pause.is_set():
                    self.runtime_pause.set()
                return super()._runtime_retire_if_flat()

            def _stop_after_stop_loss(self, *trade):
                # The whole side turns off, so it does not pick a new strike after the stop-loss.
                super()._stop_after_stop_loss(*trade)
                owner.stopped_after_sl = self.stopped_after_sl
                owner.runtime_pause.set()

            def _tick(self, ltp=None, quote_time=None, *, poll_pending=False):
                with self.runtime_state_lock:
                    if (ltp is not None and self.runtime_ready and not self.state.halted
                            and self.state.phase == 'WAITING_TO_BUY' and self.state.pending is None
                            and self.state.position_quantity == 0
                            and not owner.configuration['premium_min'] <= ltp <= owner.configuration['premium_max']):
                        self.state.last_ltp = ltp
                        self.logger.info('[AUTO RESELECT] %s LTP=%.2f outside premium band; flat contract released', self.symbol, ltp)
                        self.runtime_pause.set()
                    super()._tick(ltp, quote_time, poll_pending=poll_pending)
        child = SelectedWorker(selected, self.settings)
        child.runtime_state_lock = self.runtime_state_lock
        return child

    def run(self):
        try:
            self.logger.info('[AUTO START] %s index=%s expiry=%s premium=[%.2f, %.2f] lots=%s', self.worker_name, self.configuration['underlying'], self.configuration['expiry_date'], self.configuration['premium_min'], self.configuration['premium_max'], self.configuration['lots'])
            saved = journal_entries().get(self.worker_name)
            if saved:
                if saved['configuration'] != self.configuration:
                    raise RuntimeError('Automatic recovery configuration changed; restore it before starting')
                self.settings = copy.deepcopy(saved['settings'])
                selected = saved['selected']
                self._set_status('recovering', 'Recovering previous contract and checking orders')
            else:
                selected = None
                self._ready = True
            while not self.stop_event.is_set():
                if self.runtime_pause.is_set() and self.child is None and selected is None:
                    self.runtime_retired_safely = True
                    return
                if selected is None:
                    try:
                        selected = self._select()
                    except Exception as exc:
                        self._retry_count += 1
                        reason = str(exc).replace('\n', ' ')[:160]
                        delay = min(30, 5 * self._retry_count)
                        self._set_status('rate_limited' if '429' in reason else 'scanner_error', 'Scanner unavailable · retrying', last_error=reason, retry_seconds=delay)
                        self.logger.warning('[AUTO SCAN] %s %s; retry in %ss', self.worker_name, reason, delay)
                        self.stop_event.wait(delay)
                        continue
                    if selected is None:
                        continue
                with self.runtime_state_lock:
                    if self.runtime_pause.is_set() and not saved:
                        selected = None
                        continue
                    self._ready = False
                    save_selected(self.worker_name, {'configuration': self.configuration, 'settings': self.settings, 'selected': selected})
                    self.child = self._child(selected)
                    if self.runtime_pause.is_set() or saved:
                        # Recovered working/unknown orders must be reconciled before any new buy.
                        self.child.runtime_pause.set()
                self.child.run()
                with self.runtime_state_lock:
                    if not self.child.runtime_retired_safely:
                        if not self.stop_event.is_set():
                            self.child.state.halted = True
                        return
                    save_selected(self.worker_name, None)
                    if self.runtime_pause.is_set():
                        self.runtime_retired_safely = True
                        return
                    self.child = None
                    selected = saved = None
                    self._ready = True
                    self._selection.update(symbol=None, strike=None, ltp=None, confirmed=False, code='reselecting', status='Reselecting premium band')
        except Exception:
            self.state.halted = True
            self._set_status('halted', 'Recovery or worker initialization failed · check strategy logs')
            self.logger.exception('[AUTO HALTED] Recovery or worker initialization failed')
        finally:
            if self.scan_client is not None:
                try:
                    self.scan_client.disconnect()
                except Exception:
                    self.logger.exception('[AUTO FEED] Could not release scanner feed')
                self.scan_client = None


# ---------------------------------------------------------------------------
# Capture all: one SRSI worker per strike whose premium is inside the band.
# ---------------------------------------------------------------------------

CAPTURE_HARD_CAP = 10          # most strike workers one side may run at once
CAPTURE_RESPAWN_SECONDS = 15   # hysteresis after a strike is released, to avoid edge churn
# "Until balance allows": keep max(2% of available cash, Rs 500) unused, and treat buys submitted in
# the last few seconds as already spent in case the funds figure has not caught up yet.
BALANCE_RESERVE_FRACTION = 0.02
BALANCE_RESERVE_MIN = 500.0
RECENT_BUY_SECONDS = 5.0
# One account backs both sides: balance checks and the buy they approve run one at a time.
BALANCE_LOCK = threading.Lock()
_RECENT_BUYS = []              # (monotonic time, cost) of buys approved by the balance check
OPEN_PHASES = {'BUY_SUBMIT_UNKNOWN', 'BUY_PENDING', 'SELLING', 'SELL_SUBMIT_UNKNOWN', 'SELL_PENDING'}


class CaptureAllWorker(AutomaticWorker):
    """Scanner plus a pool of existing SelectedWorker children, one per in-band strike.

    Claims: a strike is claimed in ``self.pool`` before its child starts and released only after
    the child thread has ended. Children keep their own state lock; the parent lock serialises
    entry permits so two children cannot take the last slot.
    """

    def __init__(self, row, settings, engine):
        super().__init__(row, settings, engine)
        self.pool = {}                 # strike -> {"child", "thread", "selected"}
        self.pool_lock = threading.RLock()
        self.released_at = {}          # strike -> monotonic time it was released
        self.limit_refusals = 0
        self._scan = None              # persistent scanner state
        self._ready = True

    # ----- supervisor-facing aggregate state --------------------------------
    def _children(self):
        with self.pool_lock:
            return [entry['child'] for entry in self.pool.values()]

    @property
    def state(self):
        children = self._children()
        state = self._state
        state.position_quantity = sum(int(c.state.position_quantity or 0) for c in children)
        pending = [c.state.pending for c in children if c.state.pending is not None]
        state.pending = pending[0] if pending else None
        busy = any(c.state.phase != 'WAITING_TO_BUY' or c.state.position_quantity for c in children)
        state.phase = 'SELLING' if busy else 'WAITING_TO_BUY'
        if any(c.state.halted for c in children):
            state.halted = True
        return state

    @property
    def runtime_ready(self):
        return self._ready and all(c.runtime_ready for c in self._children())

    @property
    def position_sync_blocked(self):
        return any(getattr(c, 'position_sync_blocked', False) for c in self._children())

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        for child in self._children():
            child.stop()

    # ----- entry limits -------------------------------------------------------
    def _is_open(self, child):
        return child.state.phase in OPEN_PHASES or bool(child.state.position_quantity) or child.state.pending is not None

    def permit(self, child, limit_price):
        """Called with the parent lock held, immediately before a child submits a buy."""
        if self.runtime_pause.is_set() or self.stop_event.is_set() or self._state.halted:
            return False
        others = [c for c in self._children() if c is not child]
        if any(c.state.halted for c in others):
            return False
        row = self.configuration
        if row['capture_limit'] == 'balance':
            # Caller holds BALANCE_LOCK, so this read and the buy it approves are not interleaved
            # with another approval on either side.
            cost = float(limit_price) * int(child.order_quantity)
            available = self._available_cash(child)
            if available is None:
                allowed, reason = False, 'funds unavailable; not entering'
            else:
                now = time.monotonic()
                _RECENT_BUYS[:] = [(t, c) for t, c in _RECENT_BUYS if now - t < RECENT_BUY_SECONDS]
                in_flight = sum(c for _, c in _RECENT_BUYS)
                reserve = max(BALANCE_RESERVE_MIN, BALANCE_RESERVE_FRACTION * available)
                allowed = cost + in_flight + reserve <= available
                reason = f'available={available:.2f} in_flight={in_flight:.2f} reserve={reserve:.2f} new={cost:.2f}'
                if allowed:
                    _RECENT_BUYS.append((now, cost))
            with self.runtime_state_lock:
                self._selection.update(balance_available=available, balance_checked_at=datetime.now(IST).isoformat(timespec='seconds'))
        else:
            open_count = sum(1 for c in others if self._is_open(c))
            allowed = open_count < int(row['capture_max_entries'])
            reason = f'open={open_count} max={row["capture_max_entries"]}'
        if not allowed:
            self.limit_refusals += 1
            child.logger.info('[CAPTURE LIMIT] %s entry skipped; %s', child.symbol, reason)
        return allowed

    @staticmethod
    def _available_cash(child):
        """Available cash from OpenAlgo funds (sandbox funds in Analyze mode); None if unavailable."""
        try:
            response = child.read_client.funds()
        except Exception:
            return None
        if not isinstance(response, dict) or response.get('status') != 'success':
            return None
        data = response.get('data') or {}
        try:
            value = float(data.get('availablecash'))
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value >= 0 else None

    # ----- children -----------------------------------------------------------
    def _make_child(self, selected):
        import strategy
        child = self._child(selected)
        # Each strike keeps its own lock so one slow order call cannot stall the others.
        child.runtime_state_lock = threading.RLock()
        place = child._place_limit_buy
        owner = self

        def guarded_buy(ltp, now, _place=place, _child=child):
            limit = ltp + getattr(strategy, 'BUY_LIMIT_BUFFER_POINTS', 0.0)
            with owner.runtime_state_lock:
                if owner.configuration['capture_limit'] == 'balance':
                    with BALANCE_LOCK:
                        if owner.permit(_child, limit):
                            _place(ltp, now)
                    return
                if owner.permit(_child, limit):
                    _place(ltp, now)
        child._place_limit_buy = guarded_buy
        return child

    @staticmethod
    def _run_child(child):
        try:
            child.run()
        except Exception:
            child.state.halted = True
            child.logger.exception('[CAPTURE] strike worker ended unexpectedly')

    def _journal(self):
        with self.pool_lock:
            children = [entry['selected'] for entry in self.pool.values()]
        save_selected(self.worker_name, {'configuration': self.configuration, 'settings': self.settings,
                                         'mode': 'all', 'children': children} if children else None)

    def _spawn(self, selected, recovering=False):
        strike = int(selected['strike_price'])
        with self.pool_lock:
            if strike in self.pool or len(self.pool) >= CAPTURE_HARD_CAP:
                return False
            child = self._make_child(selected)
            if recovering or self.runtime_pause.is_set():
                child.runtime_pause.set()
            thread = threading.Thread(target=self._run_child, args=(child,), daemon=True,
                                      name=f"{self.worker_name}-{strike}-{selected['option_type']}-worker")
            self.pool[strike] = {'child': child, 'thread': thread, 'selected': selected}
        self._journal()  # claim is persisted before the worker can place an order
        self.logger.info('[CAPTURE ADD] %s strike=%s%s', self.worker_name, strike, ' (recovering)' if recovering else '')
        thread.start()
        return True

    def _reap(self):
        changed = False
        with self.pool_lock:
            for strike, entry in list(self.pool.items()):
                if entry['thread'].is_alive():
                    continue
                child = entry['child']
                if child.runtime_retired_safely:
                    del self.pool[strike]
                    self.released_at[strike] = time.monotonic()
                    changed = True
                    self.logger.info('[CAPTURE RELEASE] %s strike=%s flat; claim released', self.worker_name, strike)
                elif not self.stop_event.is_set() and not self._state.halted:
                    # Unknown position or order state: keep the claim and journal entry, stop new entries.
                    self._state.halted = True
                    self.logger.error('[CAPTURE HALT] %s strike=%s ended without a confirmed flat exit; '
                                      'side halted for reconciliation', self.worker_name, strike)
                    for other in self._children():
                        other.runtime_pause.set()
        if changed:
            self._journal()

    # ----- scanner ------------------------------------------------------------
    def _candidates(self):
        """Fresh in-band strikes in the existing ATM-outward order (may be empty)."""
        row = self.configuration
        scan = self._scan
        if scan is None:
            chain, anchor = self._load_chain()
            if self.scan_client is None:
                self.scan_client = self.feed_hub.acquire(('scanner', id(self)), self._on_tick)
            if not self.scan_client.connect():
                raise RuntimeError('Live scanner feed unavailable')
            scan = self._scan = {'chain': chain, 'anchor': anchor, 'refresh_at': time.monotonic() + 60,
                                 'subscriptions': set(), 'window': None, 'last_spot_at': time.monotonic()}
        client = self.scan_client
        spot = self._price(row['underlying_exchange'], row['underlying'])
        if scan['window'] is None:
            initial = spot or scan['anchor']
            scan['window'] = strike_search_order(scan['chain'], initial, row['option_type'], row['max_levels'])
        desired = {(row['underlying_exchange'], row['underlying'])}
        desired.update((row['option_exchange'], scan['chain'][s][row['option_type'].lower()]['symbol']) for s in scan['window'])
        if desired != scan['subscriptions'] or not client.connected or not client.authenticated:
            if not client.set_instruments([{'exchange': ex, 'symbol': sy} for ex, sy in sorted(desired)], replay=True):
                raise RuntimeError('Scanner subscriptions rejected')
            scan['subscriptions'] = desired
            scan['settle_until'] = time.monotonic() + 2
        now = datetime.now(IST)
        minutes = now.hour * 60 + now.minute
        if now.weekday() >= 5 or not (555 <= minutes < 915):
            cutoff = now.weekday() < 5 and 915 <= minutes < 930
            self._set_status('entry_cutoff' if cutoff else 'market_closed',
                             'Entry cutoff reached · no new strikes' if cutoff else 'Outside market hours · waiting for next session')
            return []
        if spot is None:
            self._set_status('waiting_spot', 'Waiting for fresh spot price')
            if time.monotonic() - scan['last_spot_at'] >= 20:
                client.set_instruments([{'exchange': ex, 'symbol': sy} for ex, sy in sorted(desired)], refresh=True)
                scan['last_spot_at'] = time.monotonic()
                raise RuntimeError('No fresh index spot tick for 20 seconds')
            return []
        scan['last_spot_at'] = time.monotonic()
        order = strike_search_order(scan['chain'], spot, row['option_type'], row['max_levels'])
        listed = sorted(scan['chain'])
        original_atm = min(listed, key=lambda strike: (abs(strike - scan['anchor']), -strike))
        recenter = abs(listed.index(order[0]) - listed.index(original_atm)) > row['max_levels']
        if recenter or time.monotonic() >= scan['refresh_at']:
            self._set_status('refreshing_chain', 'Refreshing metadata on the existing live feed')
            scan['chain'], scan['anchor'] = self._load_chain(force=recenter)
            scan['refresh_at'] = time.monotonic() + 60
            scan['window'] = strike_search_order(scan['chain'], spot, row['option_type'], row['max_levels'])
            return []
        if set(order) != set(scan['window']):
            scan['window'] = order
            return []
        if time.monotonic() < scan.get('settle_until', 0):
            self._set_status('receiving_prices', 'Waiting for candidate quotes')
            return []
        quotes = {s: self._price(row['option_exchange'], scan['chain'][s][row['option_type'].lower()]['symbol']) for s in order}
        inside = [s for s in order if quotes[s] is not None and row['premium_min'] <= quotes[s] <= row['premium_max']]
        with self.runtime_state_lock:
            self._selection.update(spot=spot, atm=order[0], scanned=len(order),
                                   fresh_candidates=sum(q is not None for q in quotes.values()),
                                   in_band=[{'strike': s, 'ltp': quotes[s]} for s in inside])
        return inside

    def _fill_pool(self, inside):
        row = self.configuration
        now = time.monotonic()
        added = 0
        for strike in inside:
            with self.pool_lock:
                if strike in self.pool or len(self.pool) >= CAPTURE_HARD_CAP:
                    continue
            if now - self.released_at.get(strike, -1e9) < CAPTURE_RESPAWN_SECONDS:
                continue
            if self._spawn({**row, 'strike_price': strike}):
                added += 1
        with self.pool_lock:
            monitoring = len(self.pool)
        self._set_status('capturing' if monitoring else 'no_match',
                         f'Monitoring {monitoring} strike(s) in band' if monitoring else 'No fresh premium inside the configured band',
                         monitoring=monitoring)
        return added

    # ----- main loop ----------------------------------------------------------
    def run(self):
        row = self.configuration
        try:
            self.logger.info('[AUTO START] %s capture=all index=%s expiry=%s premium=[%.2f, %.2f] lots=%s limit=%s max_positions=%s',
                             self.worker_name, row['underlying'], row['expiry_date'], row['premium_min'], row['premium_max'],
                             row['lots'], row['capture_limit'], row['capture_max_entries'])
            saved = journal_entries().get(self.worker_name)
            if saved:
                if saved.get('configuration') != self.configuration or saved.get('mode') != 'all':
                    raise RuntimeError('Automatic recovery configuration changed; restore it before starting')
                self.settings = copy.deepcopy(saved['settings'])
                self._set_status('recovering', 'Recovering previous strikes and checking orders')
                for selected in saved.get('children', []):
                    self._spawn(selected, recovering=True)
            while not self.stop_event.is_set():
                self._reap()
                if not self._state.halted and any(c.state.halted for c in self._children()):
                    self._state.halted = True
                    self.logger.error('[CAPTURE HALT] %s a strike worker needs reconciliation; no new entries on this side', self.worker_name)
                if self._state.halted:
                    for child in self._children():
                        child.runtime_pause.set()   # open strikes keep their exit rules
                if self.runtime_pause.is_set() or self._state.halted:
                    with self.pool_lock:
                        empty = not self.pool
                    if empty:
                        if not self._state.halted:
                            self.runtime_retired_safely = True
                        return
                    self._set_status('stopping', 'Stopping after exit · open strikes keep their exit rules')
                    self.stop_event.wait(0.5)
                    continue
                try:
                    inside = self._candidates()
                    self._fill_pool(inside)
                    self._retry_count = 0
                except Exception as exc:
                    self._retry_count += 1
                    self._scan = None
                    reason = str(exc).replace('\n', ' ')[:160]
                    delay = min(30, 5 * self._retry_count)
                    self._set_status('rate_limited' if '429' in reason else 'scanner_error', 'Scanner unavailable · retrying',
                                     last_error=reason, retry_seconds=delay)
                    self.logger.warning('[AUTO SCAN] %s %s; retry in %ss', self.worker_name, reason, delay)
                    self.stop_event.wait(delay)
                    continue
                self.wake.wait(0.5)
                self.wake.clear()
        except Exception:
            self._state.halted = True
            self._set_status('halted', 'Capture all failed · check strategy logs')
            self.logger.exception('[AUTO HALTED] Capture all failed')
            # Open strikes keep managing their exits; no new entries while halted.
            for child in self._children():
                child.runtime_pause.set()
            while not self.stop_event.is_set():
                self._reap()
                with self.pool_lock:
                    if not self.pool:
                        break
                self.stop_event.wait(0.5)
        finally:
            if self.stop_event.is_set():
                for child in self._children():
                    child.stop()
                for entry in list(self.pool.values()):
                    entry['thread'].join(timeout=5)
            if self.scan_client is not None:
                try:
                    self.scan_client.disconnect()
                except Exception:
                    self.logger.exception('[AUTO FEED] Could not release scanner feed')
                self.scan_client = None

    def runtime_details(self):
        row = self.configuration
        children = []
        open_count, capital_used = 0, 0.0
        for strike, entry in sorted(self.pool.items()):
            c = entry['child']
            st = c.state
            age = time.monotonic() - c.last_ws_tick_monotonic if c.last_ws_tick_monotonic is not None else None
            is_open = self._is_open(c)
            open_count += is_open
            if st.position_quantity:
                capital_used += float(st.entry_average_price or 0) * int(st.position_quantity)
            elif st.pending is not None and st.pending.kind == 'BUY':
                capital_used += float(st.pending.price or 0) * int(st.pending.requested_quantity)
            children.append({'strike': strike, 'symbol': c.symbol, 'ltp': st.last_ltp,
                             'k': st.last_k if math.isfinite(st.last_k) else None, 'phase': st.phase,
                             'quantity': int(st.position_quantity or 0), 'pending': st.pending is not None,
                             'entry_price': st.entry_average_price or None,
                             'stop_price': (st.entry_average_price - c.stop_loss_points) if st.entry_average_price else None,
                             'halted': st.halted, 'ready': c.runtime_ready,
                             'stopping': c.runtime_pause.is_set(),
                             'tick_age_seconds': round(age, 1) if age is not None else None,
                             'feed_stale': st.last_ltp is None or age is None or age > 15,
                             'order_quantity': c.order_quantity})
        with self.runtime_state_lock:
            info = dict(self._selection)
        info.update(mode='all', underlying=row['underlying'], expiry_date=row['expiry_date'], lots=row['lots'],
                    candle_minutes=row['candle_minutes'], children=children, monitoring=len(children),
                    open_positions=open_count, capital_used=round(capital_used, 2),
                    capture_limit=row['capture_limit'], capture_max_entries=row['capture_max_entries'],
                    limit_refusals=self.limit_refusals,
                    confirmed=bool(children), rejected_ticks=self._rejected_ticks, retries=self._retry_count,
                    updated_at=datetime.now(IST).isoformat(timespec='seconds'))
        info.update(self.feed_hub.metrics())
        if time.monotonic() - self._status_saved_at >= 15:
            self._status_saved_at = time.monotonic()
            try:
                import json
                with config_lock():
                    rows = json.loads(STATUS_FILE.read_text()) if STATUS_FILE.exists() else {}
                    rows[self.worker_name] = info
                    atomic_json(STATUS_FILE, rows)
            except Exception:
                self.logger.warning('[AUTO STATUS] Could not persist status snapshot')
        return info
