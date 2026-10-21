import datetime
from database.strategy_module_db import create_strategy, list_strategies

strategies_to_add = [
    {
        'name': 'RSI_Mean_Reversion_Neutral_CE',
        'strategy_kind': 'batch',
        'direction': 'both',
        'universe_tab': 'weekly_monthly',
        'underlying': 'NIFTY',
        'underlying_exchange': 'NSE_INDEX',
        'strategy_type': 'intraday',
        'entry_time': datetime.time(9, 20),
        'exit_time': datetime.time(15, 25),
        'product': 'MIS',
        'pricetype': 'MARKET',
        'legs': [
            {
                'id': 1,
                'segment': 'options',
                'position': 'B',
                'lots': 1,
                'option_type': 'CE',
                'strike_mode': 'atm',
                'expiry': 'weekly',
                'target_value': 5.0,
                'target_unit': 'pts',
                'sl_value': 3.0,
                'sl_unit': 'pts'
            }
        ]
    },
    {
        'name': 'RSI_Mean_Reversion_Risk_CE',
        'strategy_kind': 'batch',
        'direction': 'both',
        'universe_tab': 'weekly_monthly',
        'underlying': 'NIFTY',
        'underlying_exchange': 'NSE_INDEX',
        'strategy_type': 'intraday',
        'entry_time': datetime.time(9, 20),
        'exit_time': datetime.time(15, 25),
        'product': 'MIS',
        'pricetype': 'MARKET',
        'legs': [
            {
                'id': 1,
                'segment': 'options',
                'position': 'B',
                'lots': 1,
                'option_type': 'CE',
                'strike_mode': 'atm',
                'expiry': 'weekly',
                'target_value': 10.0,
                'target_unit': 'pts',
                'sl_value': 5.0,
                'sl_unit': 'pts'
            }
        ]
    },
    {
        'name': 'RSI_Mean_Reversion_Risky_CE',
        'strategy_kind': 'batch',
        'direction': 'both',
        'universe_tab': 'weekly_monthly',
        'underlying': 'NIFTY',
        'underlying_exchange': 'NSE_INDEX',
        'strategy_type': 'intraday',
        'entry_time': datetime.time(9, 20),
        'exit_time': datetime.time(15, 25),
        'product': 'MIS',
        'pricetype': 'MARKET',
        'legs': [
            {
                'id': 1,
                'segment': 'options',
                'position': 'B',
                'lots': 1,
                'option_type': 'CE',
                'strike_mode': 'atm',
                'expiry': 'weekly',
                'target_value': 5.0,
                'target_unit': 'pts',
                'sl_value': 5.0,
                'sl_unit': 'pts'
            }
        ]
    }
]

for strat in strategies_to_add:
    payload, err = create_strategy('Nandeesh', strat)
    sname = strat['name']
    if payload:
        print(f"Created Strategy: {sname} (Token: {payload.get('webhook_token')})")
    else:
        print(f"Notice for {sname}: {err}")

existing = list_strategies('Nandeesh')
print(f"Total Registered Strategies in OpenAlgo: {len(existing)}")
for s in existing:
    print(f" - {s.get('name')} [{s.get('underlying')} {s.get('product')}]")
