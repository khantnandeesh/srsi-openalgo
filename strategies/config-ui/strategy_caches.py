"""Immutable, candle-aware API history cache; no worker position state is shared."""
from collections import OrderedDict
from datetime import date
import threading
import time

_HISTORY = OrderedDict()
_LOCK = threading.Lock()
MAX_CONTRACTS = 64


def cached_history(key, bucket, start_date, end_date, fetch, normalize):
    # Serialize history requests so CE/PE cannot duplicate a request for one key.
    with _LOCK:
        entry = _HISTORY.get(key)
        if entry and entry['bucket'] == bucket and time.monotonic() - entry['at'] < 60:
            _HISTORY.move_to_end(key)
            return entry['frame'].copy(deep=True), 'hit'
        # Fetch overlapping days to update recent bars and fill any missed candles.
        incremental = bool(entry and len(entry['frame']) and entry['start'] <= start_date)
        request_start = max(start_date, entry['frame'].index[-1].date().isoformat()) if incremental else start_date
        frame = normalize(fetch(request_start, end_date))
        if incremental:
            import pandas as pd
            frame = pd.concat([entry['frame'], frame]).sort_index()
            frame = frame[~frame.index.duplicated(keep='last')]
            frame = frame.loc[frame.index.date >= date.fromisoformat(start_date)]
        _HISTORY[key] = {'frame': frame.copy(deep=True), 'bucket': bucket,
                         'start': start_date, 'at': time.monotonic()}
        _HISTORY.move_to_end(key)
        while len(_HISTORY) > MAX_CONTRACTS:
            _HISTORY.popitem(last=False)
        return frame.copy(deep=True), 'incremental' if incremental else 'miss'


def clear_history_cache():
    with _LOCK:
        _HISTORY.clear()
