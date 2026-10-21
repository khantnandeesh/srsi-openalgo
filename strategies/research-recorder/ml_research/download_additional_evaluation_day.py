#!/usr/bin/env python3
"""Download one additional evaluation day to VM files; no order APIs."""
from pathlib import Path
import sys,time,json
import numpy as np,pandas as pd
ROOT=Path("/home/ubuntu/openalgo")
RESEARCH=ROOT/"strategies/research-recorder/ml_research"
OUT=RESEARCH/"results/prospective_20261001"
OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(RESEARCH))
from backfill_intraday_6otm import key
from openalgo import api
client=api(api_key=key(),host="http://127.0.0.1:5000")
DAY="2026-10-01"
def fetch(symbol,exchange):
    response=client.history(symbol=symbol,exchange=exchange,interval="1m",start_date=DAY,end_date=DAY,source="api")
    if not isinstance(response,pd.DataFrame) or response.empty:
        raise RuntimeError(str(response.get("message","No historical bars"))[:180] if isinstance(response,dict) else "Empty frame")
    df=response.copy()
    if not isinstance(df.index,pd.DatetimeIndex):
        if "timestamp" in df:
            df=df.set_index("timestamp")
        df.index=pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index=df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    df.index.name="timestamp"
    return df
state={"day":DAY,"completed":0,"successes":0,"failures":[],"records":0}
def save():
    (OUT/"download_status.json").write_text(json.dumps(state,indent=2))
if (OUT/"NIFTY.csv").exists():
    spot=pd.read_csv(OUT/"NIFTY.csv",parse_dates=["timestamp"]).set_index("timestamp")
else:
    spot=fetch("NIFTY","NSE_INDEX");spot.to_csv(OUT/"NIFTY.csv")
atms=(np.floor(pd.to_numeric(spot.close)/50+.5)*50).astype(int)
contracts=sorted({f"NIFTY06OCT26{int(atm+offset*50)}CE" for atm in atms for offset in range(7)}|
                 {f"NIFTY06OCT26{int(atm-offset*50)}PE" for atm in atms for offset in range(7)})
state["contract_count"]=len(contracts);save()
print("ADDITIONAL_DAY",DAY,"spot_bars",len(spot),"contracts",len(contracts),flush=True)
for i,symbol in enumerate(contracts,1):
    path=OUT/f"{symbol}.csv"
    try:
        if path.exists():
            frame=pd.read_csv(path)
        else:
            frame=fetch(symbol,"NFO");frame.to_csv(path)
        state["records"]+=len(frame);state["successes"]+=1
        if i%10==0 or i==len(contracts):
            print("DOWNLOAD",i,"/",len(contracts),"success",state["successes"],"rows",state["records"],flush=True)
    except Exception as exc:
        state["failures"].append({"symbol":symbol,"message":str(exc)[:200]})
        print("DOWNLOAD_FAIL",symbol,type(exc).__name__,str(exc)[:120],flush=True)
    state["completed"]=i;save()
    time.sleep(.8)
state["finished"]=True;save()
print("ADDITIONAL_DAY_DONE",json.dumps(state),flush=True)

