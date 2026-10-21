#!/usr/bin/env python3
"""Research-only NIFTY SRSI + ML/DL evaluation. Never submits orders.

Inputs are VM historical_option_data 1m CSVs plus the local Historify DuckDB.
Signals use a provisional current interval close sampled at each 1m bar close.
Because these are 1m bars, they cannot verify the live 5-second limit fill.
"""
from __future__ import annotations
import json, math, re, sys, time
from pathlib import Path
from datetime import datetime, time as dtime
import duckdb
import numpy as np
import pandas as pd

ROOT=Path('/home/ubuntu/openalgo/strategies')
SCRIPTS=ROOT/'scripts'
DB=Path('/home/ubuntu/openalgo/db/historify.duckdb')
OUT=ROOT/'research-recorder/ml_research/results'
OUT.mkdir(parents=True,exist_ok=True)
R_PERIOD=4; S_PERIOD=4; K_SMOOTH=2; D_SMOOTH=2
ENTRY_K=0.0; EXIT_K=80.0; STOP=15.0
MARKET_OPEN=dtime(9,15); ENTRY_END=dtime(15,15); MARKET_END=dtime(15,30)
SLIPPAGES=(0.25,1.0)
FEATURES=['premium','dte_days','moneyness_pct','moneyness_signed_pct','is_ce','minute_sin','minute_cos','k','d','option_ret_1m','option_ret_5m','option_ret_15m','option_ret_30m','option_vol_15m','option_range_5m_pct','option_range_15m_pct','log_volume','volume_rel_30m','spot_ret_5m','spot_ret_15m','spot_ret_30m','spot_vol_15m','spot_distance_ema15_pct']

def parse_symbol(symbol):
    m=re.fullmatch(r'NIFTY(\d{2}[A-Z]{3}\d{2})(\d+)(CE|PE)',str(symbol))
    if not m:return None
    expiry=pd.to_datetime(m.group(1),format='%d%b%y',errors='coerce')
    if pd.isna(expiry):return None
    return expiry.date(),int(m.group(2)),m.group(3)

def load_all():
    parts=[]
    old=SCRIPTS/'historical_option_data/NIFTY/29SEP26/1m'
    for p in old.glob('*.csv'):
        try:
            d=pd.read_csv(p)
            if not {'timestamp','open','high','low','close'}.issubset(d.columns):continue
            d['symbol']=p.stem; d['timestamp']=pd.to_datetime(d.timestamp,errors='coerce')
            for c in ['open','high','low','close','volume']:
                if c in d:d[c]=pd.to_numeric(d[c],errors='coerce')
            if 'volume' not in d:d['volume']=0
            parts.append(d[['symbol','timestamp','open','high','low','close','volume']])
        except Exception as e: print('SKIP',p.name,str(e)[:120])
    con=duckdb.connect(str(DB),read_only=True)
    hist=con.execute("SELECT symbol, timestamp, open, high, low, close, volume FROM market_data WHERE exchange='NFO' AND interval='1m' AND symbol LIKE 'NIFTY%' ").df()
    spot=con.execute("SELECT timestamp, open, high, low, close, volume FROM market_data WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'").df()
    con.close()
    if len(hist):
        hist['timestamp']=pd.to_datetime(hist.timestamp,unit='s',utc=True).dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
        parts.append(hist)
    if not parts:raise RuntimeError('No NIFTY option bars found')
    opt=pd.concat(parts,ignore_index=True)
    opt=opt.dropna(subset=['timestamp','close']).sort_values(['symbol','timestamp'])
    opt=opt.drop_duplicates(['symbol','timestamp'],keep='last')
    opt=opt[(opt.close>0)&(opt.open>0)&(opt.high>0)&(opt.low>0)].copy()
    if len(spot):
        spot['timestamp']=pd.to_datetime(spot.timestamp,unit='s',utc=True).dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
        for c in ['open','high','low','close']:
            spot[c]=pd.to_numeric(spot[c],errors='coerce')
        spot=spot.dropna(subset=['timestamp','close']).drop_duplicates('timestamp',keep='last').sort_values('timestamp')
    else:
        spot=pd.DataFrame(columns=['timestamp','open','high','low','close','volume'])
    meta=[]
    for s in opt.symbol.unique():
        z=parse_symbol(s)
        if z:meta.append((s,*z))
    m=pd.DataFrame(meta,columns=['symbol','expiry','strike','option_type'])
    opt=opt.merge(m,on='symbol',how='inner').sort_values(['symbol','timestamp'])
    opt['session']=opt.timestamp.dt.date
    opt['sample_time']=opt.timestamp+pd.Timedelta(seconds=59)
    opt['minute_of_day']=opt.sample_time.dt.hour*60+opt.sample_time.dt.minute
    opt=opt[(opt.sample_time.dt.time>=MARKET_OPEN)&(opt.sample_time.dt.time<MARKET_END)]
    if len(spot):
        spot=spot[['timestamp','close']].rename(columns={'close':'spot'})
        opt=opt.merge(spot,on='timestamp',how='left')
    else:opt['spot']=np.nan
    opt['spot']=opt.groupby('session').spot.transform(lambda x:x.ffill().bfill())
    opt['spot_distance']=opt.strike-opt.spot
    opt['moneyness_signed_pct']=np.where(opt.option_type.eq('CE'),(opt.spot-opt.strike)/opt.spot*100,(opt.strike-opt.spot)/opt.spot*100)
    opt['moneyness_pct']=opt.spot_distance/opt.spot*100
    opt['dte_days']=(pd.to_datetime(opt.expiry)-pd.to_datetime(opt.session)).dt.days
    return opt.reset_index(drop=True),spot

class StochState:
    """Wilder RSI + OpenAlgo backend 0..100 StochRSI and SMA K/D."""
    def __init__(self):
        self.prev=None; self.seed_g=[];self.seed_l=[];self.ag=None;self.al=None
        self.rsi=[];self.raw=[];self.k=[];self.n=0
    def _rsi_for(self,price,commit=False):
        if self.prev is None:return np.nan
        delta=float(price)-self.prev; g=max(delta,0.0);l=max(-delta,0.0)
        if self.ag is None:
            gs=self.seed_g+[g];ls=self.seed_l+[l]
            if len(gs)<R_PERIOD:
                if commit:self.seed_g.append(g);self.seed_l.append(l)
                return np.nan
            ag=float(np.mean(gs[-R_PERIOD:]));al=float(np.mean(ls[-R_PERIOD:]))
            val=100.0 if al==0 else 100.0-100.0/(1.0+ag/al)
            if commit:self.ag=ag;self.al=al;self.seed_g=[];self.seed_l=[]
            return val
        ag=(self.ag*(R_PERIOD-1)+g)/R_PERIOD;al=(self.al*(R_PERIOD-1)+l)/R_PERIOD
        val=100.0 if al==0 else 100.0-100.0/(1.0+ag/al)
        if commit:self.ag=ag;self.al=al
        return val
    @staticmethod
    def _raw(window):
        w=np.asarray(window,dtype=float);w=w[np.isfinite(w)]
        if not len(w):return np.nan
        hi=w.max();lo=w.min()
        return 50.0 if hi==lo else (float(w[-1])-lo)/(hi-lo)*100.0
    @staticmethod
    def _mean_valid(window):
        w=np.asarray(window,dtype=float);w=w[np.isfinite(w)]
        return float(w.mean()) if len(w) else np.nan
    def provisional(self,price):
        if self.prev is None:return np.nan,np.nan
        r=self._rsi_for(price,False)
        ri=self.rsi+[r]
        raw=self._raw(ri[-S_PERIOD:]) if len(ri)>=S_PERIOD else np.nan
        raw_i=self.raw+[raw];idx=self.n
        kv=self._mean_valid(raw_i[-K_SMOOTH:]) if idx>=K_SMOOTH-1 else np.nan
        kvals=self.k+[kv]
        dv=self._mean_valid(kvals[-D_SMOOTH:]) if idx>=D_SMOOTH-1 else np.nan
        return kv,dv
    def finalize(self,price):
        if self.prev is None:
            self.prev=float(price);self.n+=1;self.rsi.append(np.nan);self.raw.append(np.nan);self.k.append(np.nan);return
        r=self._rsi_for(price,True);self.prev=float(price);self.rsi.append(r);idx=self.n
        raw=self._raw(self.rsi[-S_PERIOD:]) if idx>=S_PERIOD-1 else np.nan
        self.raw.append(raw);kv=self._mean_valid(self.raw[-K_SMOOTH:]) if idx>=K_SMOOTH-1 else np.nan
        self.k.append(kv);self.n+=1

def interval_kd(g,minutes):
    state=StochState();last_bucket=None;last_price=None;ks=[];ds=[]
    for r in g.itertuples(index=False):
        t=r.sample_time;session_start=t.normalize()+pd.Timedelta(hours=9,minutes=15)
        bucket=int((t-session_start).total_seconds()//(minutes*60));key=(t.date(),bucket)
        if last_bucket is not None and key!=last_bucket and last_price is not None:state.finalize(last_price)
        last_bucket=key;last_price=float(r.close)
        k,d=state.provisional(last_price);ks.append(k);ds.append(d)
    out=g.copy();out['k']=ks;out['d']=ds
    return out

def add_features(g):
    g=g.copy().sort_values('sample_time').reset_index(drop=True)
    g['premium']=g.close.astype(float)
    for n in [1,5,15,30]:
        prev=g.close.shift(n);prev_t=g.sample_time.shift(n);ret=g.close/prev-1
        ret[(g.sample_time-prev_t)!=pd.Timedelta(minutes=n)]=np.nan;g[f'option_ret_{n}m']=ret
    for n in [5,15]:
        g[f'option_range_{n}m_pct']=(g.high.rolling(n,min_periods=n).max()-g.low.rolling(n,min_periods=n).min())/g.close
    g['option_vol_15m']=g.option_ret_1m.rolling(15,min_periods=10).std()
    g['log_volume']=np.log1p(g.volume.clip(lower=0));med=g.volume.rolling(30,min_periods=10).median().replace(0,np.nan)
    g['volume_rel_30m']=g.volume/med
    g['minute_sin']=np.sin(2*np.pi*(g.minute_of_day-555)/375);g['minute_cos']=np.cos(2*np.pi*(g.minute_of_day-555)/375)
    g['is_ce']=float(g.option_type.iloc[0]=='CE')
    g['spot_ret_1m']=g.spot.pct_change()
    for n in [5,15,30]:
        p=g.spot.shift(n);pt=g.sample_time.shift(n);x=g.spot/p-1
        x[(g.sample_time-pt)!=pd.Timedelta(minutes=n)]=np.nan;g[f'spot_ret_{n}m']=x
    g['spot_vol_15m']=g.spot_ret_1m.rolling(15,min_periods=10).std()
    g['spot_ema15']=g.spot.ewm(span=15,adjust=False,min_periods=15).mean();g['spot_distance_ema15_pct']=g.spot/g.spot_ema15-1
    return g

def trade_outcome(g,i,slip):
    entry=float(g.close.iloc[i]);stop=entry-STOP;exit_i=None;price=None;reason=None
    for j in range(i+1,len(g)):
        row=g.iloc[j]
        if float(row.low)<stop:
            op=float(row.open);price=op if op<stop else stop;exit_i=j;reason='STOP';break
        if np.isfinite(row.k) and float(row.k)>=EXIT_K and float(row.close)>=entry:
            price=float(row.close);exit_i=j;reason='SRSI_EXIT';break
        if row.sample_time.time()>=dtime(15,25):
            if g.sample_time.iloc[-1].time()<dtime(15,24):return None
            price=float(row.close);exit_i=j;reason='RESEARCH_EOD';break
    if exit_i is None:
        if g.sample_time.iloc[-1].time()<dtime(15,24):return None
        exit_i=len(g)-1;price=float(g.close.iloc[-1]);reason='RESEARCH_EOD'
    return {'entry_price':entry,'exit_price':price,'exit_i':exit_i,'exit_time':g.sample_time.iloc[exit_i],
            'reason':reason,'gross_points':price-entry,'net_points':price-entry-slip}

def make_events(opt,minutes):
    events=[]
    # Keep StochRSI state continuous across sessions, as the live worker does.
    # Then create features and simulated trades within each individual session.
    for symbol,whole in opt.groupby('symbol',sort=True):
        whole=interval_kd(whole.sort_values('sample_time'),minutes)
        for day,g0 in whole.groupby('session',sort=True):
            g=g0.sort_values('sample_time').copy()
            if g.spot.notna().any():g=g[g.spot.notna() & (g.spot_distance.abs()<=300)].copy()
            if g.empty:continue
            g=add_features(g);g['bar_i']=np.arange(len(g))
            zero=np.isfinite(g.k.to_numpy()) & (np.abs(g.k.to_numpy()-ENTRY_K)<1e-10);prev=np.r_[False,zero[:-1]]
            for i in np.flatnonzero(zero & ~prev):
                row=g.iloc[i]
                if row.sample_time.time()>=ENTRY_END or row.close<=0:continue
                outcome=trade_outcome(g,int(i),SLIPPAGES[0])
                if outcome is None:continue
                rec={c:row[c] for c in FEATURES}
                rec.update({'symbol':symbol,'session':day,'sample_time':row.sample_time,'option_type':row.option_type,
                            'strike':int(row.strike),'expiry':row.expiry,'bar_i':int(i),'score_label':int(outcome['net_points']>0),
                            **outcome})
                rec['net_points_stress1']=outcome['gross_points']-SLIPPAGES[1];events.append(rec)
    if not events:return pd.DataFrame(),pd.DataFrame()
    return pd.DataFrame(events),pd.DataFrame()

def sigmoid(z):return 1/(1+np.exp(-np.clip(z,-30,30)))
def logloss(y,p):
    p=np.clip(p,1e-7,1-1e-7);return float(-np.mean(y*np.log(p)+(1-y)*np.log(1-p)))
def prep_data(train,val,test):
    xtr=train[FEATURES].to_numpy(float);med=np.nanmedian(xtr,axis=0);med=np.where(np.isfinite(med),med,0)
    def fill(a):return np.where(np.isfinite(a),a,med)
    xtr=fill(xtr);mu=xtr.mean(0);sd=xtr.std(0);sd=np.where(sd<1e-8,1,sd)
    return (np.clip((fill(train[FEATURES].to_numpy(float))-mu)/sd,-8,8),
            np.clip((fill(val[FEATURES].to_numpy(float))-mu)/sd,-8,8),
            np.clip((fill(test[FEATURES].to_numpy(float))-mu)/sd,-8,8))
def fit_logistic(x,y):
    y=y.astype(float);w=np.zeros(x.shape[1]);b=0.;m=np.zeros_like(w);v=np.zeros_like(w);mb=vb=0.;lr=.025
    pos=max(y.mean(),1e-4);weights=np.where(y>0.5,.5/pos,.5/(1-pos))
    for t in range(1,801):
        p=sigmoid(x@w+b);err=(p-y)*weights/len(y);gw=x.T@err+0.01*w;gb=err.sum()
        m=.9*m+.1*gw;v=.999*v+.001*gw*gw;mb=.9*mb+.1*gb;vb=.999*vb+.001*gb*gb
        w-=lr*(m/(1-.9**t))/(np.sqrt(v/(1-.999**t))+1e-8);b-=lr*(mb/(1-.9**t))/(np.sqrt(vb/(1-.999**t))+1e-8)
    return lambda z:sigmoid(z@w+b)
def fit_mlp(x,y,xv,yv,seed=21):
    rng=np.random.default_rng(seed);dims=[x.shape[1],24,12,1]
    W=[rng.normal(0,np.sqrt(2/dims[i]),(dims[i],dims[i+1])) for i in range(3)];B=[np.zeros((1,dims[i+1])) for i in range(3)]
    mw=[np.zeros_like(a) for a in W];vw=[np.zeros_like(a) for a in W];mb=[np.zeros_like(a) for a in B];vb=[np.zeros_like(a) for a in B]
    pos=max(y.mean(),1e-4);cw=np.where(y>.5,.5/pos,.5/(1-pos));best=None;bestloss=1e9;wait=0;step=0
    def forward(z):
        a1=np.tanh(z@W[0]+B[0]);a2=np.tanh(a1@W[1]+B[1]);p=sigmoid(a2@W[2]+B[2]);return a1,a2,p
    n=len(y)
    for epoch in range(120):
        ix=rng.permutation(n)
        for st in range(0,n,256):
            ids=ix[st:st+256];xb=x[ids];yb=y[ids,None];ww=cw[ids,None];a1,a2,p=forward(xb)
            dz3=(p-yb)*ww/len(ids);gw2=a2.T@dz3+.005*W[2];gb2=dz3.sum(0,keepdims=True)
            da2=dz3@W[2].T;dz2=da2*(1-a2*a2);gw1=a1.T@dz2+.005*W[1];gb1=dz2.sum(0,keepdims=True)
            da1=dz2@W[1].T;dz1=da1*(1-a1*a1);gw0=xb.T@dz1+.005*W[0];gb0=dz1.sum(0,keepdims=True)
            step+=1;gs=[gw0,gw1,gw2];bs=[gb0,gb1,gb2]
            for j in range(3):
                mw[j]=.9*mw[j]+.1*gs[j];vw[j]=.999*vw[j]+.001*gs[j]**2
                mb[j]=.9*mb[j]+.1*bs[j];vb[j]=.999*vb[j]+.001*bs[j]**2
                W[j]-=.001*(mw[j]/(1-.9**step))/(np.sqrt(vw[j]/(1-.999**step))+1e-8)
                B[j]-=.001*(mb[j]/(1-.9**step))/(np.sqrt(vb[j]/(1-.999**step))+1e-8)
        pv=forward(xv)[2].ravel();ll=logloss(yv,pv)
        if ll<bestloss-1e-4:bestloss=ll;best=([a.copy() for a in W],[a.copy() for a in B]);wait=0
        else:wait+=1
        if wait>=12:break
    W,B=best
    def predict(z):
        a=np.tanh(z@W[0]+B[0]);a=np.tanh(a@W[1]+B[1]);return sigmoid(a@W[2]+B[2]).ravel()
    return predict,epoch+1,bestloss

def replay(events,score=None,threshold=None,nearest=False):
    if events.empty:return []
    chosen=[]
    for (day,ts,side),g in events.groupby(['session','sample_time','option_type'],sort=True):
        if nearest:g=g.assign(_near=g.moneyness_signed_pct.abs()).sort_values(['_near','premium']).head(1)
        else:
            if score is not None:g=g.assign(_score=np.asarray(score)[g.index]).sort_values('_score',ascending=False)
            g=g.head(1)
            if threshold is not None and score is not None and np.asarray(score)[g.index[0]]<threshold:continue
        chosen.append(g.iloc[0].to_dict())
    chosen.sort(key=lambda r:(r['session'],r['sample_time'],r['option_type']))
    busy={};out=[]
    for r in chosen:
        key=(r['session'],r['option_type'])
        if key in busy and r['sample_time']<=busy[key]:continue
        busy[key]=r['exit_time'];out.append(r)
    return out

def metric(rows,net_col='net_points'):
    if not rows:return {'trades':0,'win_rate_pct':None,'mean_points':None,'median_points':None,'total_points':0.,'profit_factor':None,'worst_daily_points':None}
    d=pd.DataFrame(rows);x=d[net_col].astype(float);wins=x[x>0].sum();loss=-x[x<0].sum()
    return {'trades':len(d),'win_rate_pct':round(float((x>0).mean()*100),2),'mean_points':round(float(x.mean()),3),'median_points':round(float(x.median()),3),'total_points':round(float(x.sum()),3),'profit_factor':round(float(wins/loss),3) if loss else None,'worst_daily_points':round(float(d.groupby('session')[net_col].sum().min()),3)}
def select_threshold(events,score):
    vals=np.asarray(score);candidates=np.unique(np.quantile(vals,[.35,.45,.55,.65,.75,.85,.9,.95]));best=None
    for t in candidates:
        rows=replay(events,vals,float(t))
        if len(rows)<10:continue
        m=metric(rows)
        if best is None or m['total_points']>best['metric']['total_points']:best={'threshold':float(t),'metric':m}
    return best

def main():
    started=time.time();print('Loading VM option bars + Historify DB…',flush=True)
    opt,spot=load_all()
    print(f'Loaded {len(opt):,} positive option minute bars; {opt.symbol.nunique()} symbols; dates {opt.session.min()} to {opt.session.max()}',flush=True)
    print(f'NIFTY spot bars in archive: {len(spot):,}',flush=True)
    coverage=opt.groupby(['expiry','option_type']).agg(symbols=('symbol','nunique'),bars=('close','size'),first=('sample_time','min'),last=('sample_time','max')).reset_index()
    coverage.to_csv(OUT/'data_coverage.csv',index=False)
    try:
        from openalgo import ta
        vals=np.array([100,101,99,100,98,97,98,96,96,97,99,98,100,102,101,99,98,100],dtype=float)
        k,d=ta.stochrsi(vals,rsi_period=R_PERIOD,stoch_period=S_PERIOD,k_period=K_SMOOTH,d_period=D_SMOOTH)
        print('OpenAlgo TA import sample final K/D:',float(np.asarray(k)[-1]),float(np.asarray(d)[-1]),flush=True)
    except Exception as e:print('TA import note:',repr(e),flush=True)
    summaries=[];all_ev={}
    for mins in [1,5,15]:
        print(f'Computing {mins}m provisional SRSI candidates…',flush=True)
        ev,bars=make_events(opt,mins)
        if ev.empty:print('No eligible signals at',mins);continue
        ev['time_split']=np.where(ev.session<pd.Timestamp('2026-09-17').date(),'train',np.where(ev.session<pd.Timestamp('2026-09-23').date(),'validation','test'))
        ev.to_csv(OUT/f'events_{mins}m.csv',index=False);all_ev[mins]=ev
        for split,g in ev.groupby('time_split'):
            summaries.append({'interval_min':mins,'split':split,'selection':'all_event_diagnostic',**metric(g.to_dict('records'))})
        for split in ['train','validation','test']:
            part=ev[ev.time_split==split];rr=replay(part,nearest=True)
            summaries.append({'interval_min':mins,'split':split,'selection':'nearest_ATM_one_CE_PE',**metric(rr)})
        print(f'{mins}m signals={len(ev):,}; split counts={ev.time_split.value_counts().to_dict()}',flush=True)
    pd.DataFrame(summaries).to_csv(OUT/'baseline_summary.csv',index=False)
    ev=all_ev.get(5)
    if ev is None:raise RuntimeError('5m event frame empty; no model run')
    tr=ev[ev.time_split=='train'].copy().reset_index(drop=True);va=ev[ev.time_split=='validation'].copy().reset_index(drop=True);te=ev[ev.time_split=='test'].copy().reset_index(drop=True)
    if min(len(tr),len(va),len(te))<30:raise RuntimeError(f'Not enough events per split for ML: train={len(tr)} val={len(va)} test={len(te)}')
    xtr,xva,xte=prep_data(tr,va,te);ytr=tr.score_label.to_numpy(float);yva=va.score_label.to_numpy(float)
    p_log=fit_logistic(xtr,ytr);tr['p_log']=p_log(xtr);va['p_log']=p_log(xva);te['p_log']=p_log(xte)
    pred_mlp,epochs,val_loss=fit_mlp(xtr,ytr,xva,yva);tr['p_mlp']=pred_mlp(xtr);va['p_mlp']=pred_mlp(xva);te['p_mlp']=pred_mlp(xte)
    mlrows=[]
    for name,train,valid,test,col in [('logistic',tr,va,te,'p_log'),('two_hidden_layer_MLP',tr,va,te,'p_mlp')]:
        valpro=valid[col].to_numpy();thr=select_threshold(valid,valpro);threshold=thr['threshold'] if thr else 1.1
        test=test.copy().reset_index(drop=True);testpro=test[col].to_numpy();allrows=replay(test,testpro,threshold)
        mlrows.append({'model':name,'train_n':len(train),'val_n':len(valid),'test_n':len(test),'train_positive_rate':round(float(train.score_label.mean()),4),'val_positive_rate':round(float(valid.score_label.mean()),4),'test_positive_rate':round(float(test.score_label.mean()),4),'val_logloss':round(logloss(valid.score_label.to_numpy(float),valid[col].to_numpy()),4),'selected_threshold_from_validation':threshold,'val_selected':thr['metric'] if thr else None,'test_selected':metric(allrows),'test_selected_stress_1pt_sell':metric(allrows,'net_points_stress1'),'epochs_mlp':epochs if name!='logistic' else None,'mlp_best_val_logloss':round(val_loss,4) if name!='logistic' else None})
    base_test=replay(te.copy().reset_index(drop=True),nearest=True)
    mlrows.append({'model':'baseline_nearest_ATM','train_n':len(tr),'val_n':len(va),'test_n':len(te),'test_selected':metric(base_test),'test_selected_stress_1pt_sell':metric(base_test,'net_points_stress1')})
    (OUT/'model_comparison.json').write_text(json.dumps(mlrows,indent=2,default=str))
    pd.DataFrame(summaries).to_csv(OUT/'baseline_summary.csv',index=False)
    print('\nMODEL RESULTS (1m-bar proxy; 5m SRSI; one CE and one PE max):');print(json.dumps(mlrows,indent=2,default=str))
    print('\nBASELINES:');print(pd.DataFrame(summaries).to_string(index=False))
    print('\nCOVERAGE BY EXPIRY / TYPE:');print(coverage.to_string(index=False))
    print('Output:',OUT,'elapsed_s=',round(time.time()-started,1),flush=True)

if __name__=='__main__':main()
