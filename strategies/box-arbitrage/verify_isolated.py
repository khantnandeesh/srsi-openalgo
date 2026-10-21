"""Isolated verification. Imports a fake SDK; no network or broker access."""
import copy
import json
import sys
import tempfile
import time
import types
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

SOURCE = Path("/home/ubuntu/openalgo/strategies/box-arbitrage/box_arbitrage.py")
fake_sdk = types.ModuleType("openalgo")
fake_sdk.api = lambda **kwargs: None
sys.modules["openalgo"] = fake_sdk
ns = {"__name__": "box_isolated_review", "__file__": str(SOURCE)}
exec(compile(SOURCE.read_text(), str(SOURCE), "exec"), ns)
root = Path(tempfile.mkdtemp(prefix="box-verification-"))
ns["load_env"] = lambda: None
ns["os"].environ["OPENALGO_API_KEY"] = "ISOLATED_FAKE_ACCOUNT_NO_NETWORK"
clock = [datetime(2026, 10, 7, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))]
ns["now"] = lambda: clock[0]

def ok(data):
    return {"status": "success", "data": data}

class Fake:
    def __init__(self):
        self.mode = False
        self.orders = []
        self.trades = []
        self.pos = {}
        self.next = "full"
        self.place_calls = []
        self.position_override = None
        self.cancel_extra = 0
        self.cash = 1e6
        self.market_prices = {}
        self.margin_baskets = []
        self.before_submit = None
    def analyzerstatus(self):
        return ok({"analyze_mode": self.mode})
    def positionbook(self):
        pos = self.pos if self.position_override is None else self.position_override
        return ok([{"symbol": s, "exchange": "NFO", "product": "NRML", "quantity": q}
                   for s, q in pos.items()])
    def orderbook(self):
        return ok({"orders": copy.deepcopy(self.orders)})
    def tradebook(self):
        return ok(copy.deepcopy(self.trades))
    def orderstatus(self, order_id, strategy):
        return ok(copy.deepcopy(next(o for o in self.orders if o["orderid"] == order_id)))
    def margin(self, positions):
        self.margin_baskets.append(copy.deepcopy(positions))
        return ok({"total_margin_required": 1000})
    def funds(self):
        return ok({"availablecash": self.cash})
    def placeorder(self, **kwargs):
        if self.before_submit:
            self.before_submit()
        self.place_calls.append(copy.deepcopy(kwargs))
        outcome, self.next = self.next, "full"
        if outcome == "absent":
            return {"status": "error"}
        order = dict(kwargs, orderid=str(len(self.orders)+1), order_status="open",
                     filled_quantity=0, average_price=0)
        self.orders.append(order)
        if outcome == "reject":
            order["order_status"] = "rejected"
        else:
            qty = 30 if outcome == "partial" else kwargs["quantity"]
            self.fill(order, qty, kwargs.get("price",
                                           self.market_prices.get(kwargs["symbol"], 50)))
            if outcome != "partial":
                order["order_status"] = "complete"
        if outcome == "timeout":
            raise TimeoutError("simulated accepted request, lost response")
        return {"status": "success", "orderid": order["orderid"]}
    def fill(self, order, qty, price):
        order["filled_quantity"] += qty
        old = order["filled_quantity"] - qty
        order["average_price"] = (old * order["average_price"] + qty * price) / (old+qty)
        sign = 1 if order["action"] == "BUY" else -1
        self.pos[order["symbol"]] = self.pos.get(order["symbol"], 0) + sign*qty
        self.trades.append({"orderid": order["orderid"], "tradeid": str(len(self.trades)+1),
                            "symbol": order["symbol"], "exchange": "NFO",
                            "product": "NRML", "action": order["action"],
                            "quantity": qty, "average_price": price})
    def cancelorder(self, order_id, strategy):
        order = next(o for o in self.orders if o["orderid"] == order_id)
        if self.cancel_extra:
            self.fill(order, self.cancel_extra, order["average_price"] or 50)
            self.cancel_extra = 0
        order["order_status"] = "cancelled"
        return ok({})
    def connect(self):
        return True
    def subscribe_depth(self, **kwargs):
        return True
    def disconnect(self):
        pass

counter = 0
def engine(fake=None, directory=None):
    global counter
    counter += 1
    f = fake or Fake()
    ns["api"] = lambda **kwargs: f
    ns["STATE_DIR"] = directory or root / str(counter)
    clock[0] = datetime(2026, 10, 7, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    return ns["BoxStrategy"](), f

def candidate():
    legs = []
    for strike, kind, action, limit in (
        (22500,"CE","BUY",50), (22600,"PE","BUY",50),
        (22600,"CE","SELL",20), (22500,"PE","SELL",20)):
        legs.append({"symbol": f"NIFTY13OCT26{strike}{kind}", "strike": strike,
                     "type": kind, "action": action, "limit": limit, "tick": .05,
                     "lot_size": 65, "expiry": "13OCT26"})
    return {"legs": legs, "qty": 65, "width": 100, "expiry": "13OCT26",
            "debit": 60, "costs": 500, "funding": 100, "buffer": 26, "edge": 1974}

def feed(e, box):
    for l in box["legs"]:
        p = l["limit"]
        e.quotes[l["symbol"]] = {"bid": p, "ask": p, "bid_qty": 1000,
                                 "ask_qty": 1000, "received": time.monotonic(),
                                 "timestamp": time.time()}

def start(e):
    b = candidate()
    feed(e,b)
    e.start_box(b)
    return b

def full_entry(e, legs=4):
    for _ in range(legs):
        e.entry_step()
        e.poll_pending()
    if legs == 4:
        e.entry_step()
        assert e.state["phase"] == "HOLDING"

def flatten(e):
    e.state["phase"] = "EXITING"
    for _ in range(12):
        if e.state["box"] is None:
            return
        e.exit_step()
        if e.state["pending"]:
            e.poll_pending()
    raise AssertionError("Could not flatten fake inventory")

results = []
def scenario(name, fn):
    fn()
    results.append(name)
    print("PASS",name)

def full_cycle():
    e,f = engine(); start(e); full_entry(e); flatten(e)
    assert not any(f.pos.values())
    assert [o["action"] for o in f.place_calls] == [
        "BUY","BUY","SELL","SELL","BUY","BUY","SELL","SELL"]
    assert e.state["pending"] is None
scenario("full box: equal fills, shorts close before longs", full_cycle)

def partial_short():
    e,f = engine(); start(e); full_entry(e,2)
    f.next="partial"; e.entry_step(); p=e.state["pending"]
    p["submitted"] -= 6; e.poll_pending()
    assert e.state["pending"] is not None
    e.poll_pending()
    assert e.state["phase"] == "EXITING"
    assert e.state["box"]["inventory"][p["symbol"]] == -30
    flatten(e)
    assert not any(f.pos.values())
    assert f.place_calls[3]["action"] == "BUY" and f.place_calls[3]["quantity"] == 30
scenario("partial short fill: confirmed cancellation and exact remainder unwind", partial_short)

def late_cancel_fill():
    e,f = engine(); start(e)
    f.next="partial"; e.entry_step()
    e.state["pending"]["submitted"] -= 6
    f.cancel_extra=35; e.poll_pending(); e.poll_pending()
    assert e.state["box"]["inventory"][candidate()["legs"][0]["symbol"]] == 65
    assert e.state["phase"] == "EXITING"
    flatten(e); assert not any(f.pos.values())
scenario("fill races cancellation: all late fills accounted", late_cancel_fill)

def reject_second():
    e,f=engine(); start(e); full_entry(e,1)
    f.next="reject"; e.entry_step(); e.poll_pending()
    assert e.state["phase"]=="EXITING"
    flatten(e)
    assert [x["action"] for x in f.place_calls] == ["BUY","BUY","SELL"]
scenario("second long rejected: no short submitted, first long unwound",reject_second)

def timeout():
    e,f=engine(); start(e); f.next="timeout"; e.entry_step()
    assert e.state["pending"]["order_id"] is None
    e.poll_pending()
    assert e.state["phase"]=="EXITING"
    e.poll_pending(); flatten(e)
    assert len(f.place_calls)==2 and not any(f.pos.values())
scenario("accepted order with lost response: recovered without duplicate",timeout)

def absent():
    e,f=engine(); start(e); f.next="absent"; e.entry_step()
    for _ in range(3): e.poll_pending()
    assert len(f.place_calls)==1 and e.state["pending"]["order_id"] is None
scenario("unknown absent response: no blind resubmission",absent)

def ambiguous():
    e,f=engine(); start(e); f.next="timeout"; e.entry_step()
    other=copy.deepcopy(f.orders[0]); other["orderid"]="other"; f.orders.append(other)
    e.poll_pending()
    assert len(f.place_calls)==1 and e.state["pending"]["order_id"] is None
scenario("ambiguous recovered order: blocked instead of guessed",ambiguous)

def restart():
    e,f=engine(); start(e); full_entry(e,1)
    directory=ns["STATE_DIR"]; e.lock_file.close()
    restored,_=engine(f,directory)
    assert restored.state["phase"]=="EXITING"
    flatten(restored)
    assert not any(f.pos.values()) and len(f.place_calls)==2
scenario("restart after fill: journal recovered and exits only known inventory",restart)

def restart_pending():
    e,f=engine(); start(e); e.entry_step()
    directory=ns["STATE_DIR"]; e.lock_file.close()
    restored,_=engine(f,directory); restored.poll_pending(); flatten(restored)
    assert len(f.place_calls)==2 and not any(f.pos.values())
scenario("restart before fill accounting: pending order reconciled once",restart_pending)

def book_lag():
    e,f=engine(); start(e); e.entry_step()
    f.position_override={}; e.poll_pending()
    assert e.state["pending"] is not None
    qty_before=e.state["box"]["inventory"].copy()
    f.position_override=None; e.poll_pending()
    assert e.state["pending"] is None and qty_before==e.state["box"]["inventory"]
scenario("terminal position-book lag: pending retained, fills not counted twice",book_lag)

def duplicates():
    e,f=engine(); start(e); e.entry_step()
    f.trades.append(copy.deepcopy(f.trades[0]))
    e.poll_pending()
    assert e.state["box"]["inventory"][candidate()["legs"][0]["symbol"]] == 65
scenario("duplicate trade ID: deduplicated fill accounting",duplicates)

def ownership():
    e,f=engine(); b=start(e); f.pos[b["legs"][0]["symbol"]]=65
    try: e.entry_step()
    except ns["NeedsAttention"]: pass
    else: raise AssertionError("Shared inventory was adopted")
    assert not f.place_calls
scenario("foreign contract position: no order or adoption",ownership)

def route():
    e,f=engine(); start(e); f.mode=True
    try: e.step()
    except ns["NeedsAttention"]: pass
    else: raise AssertionError("Mode switch not blocked")
    assert not f.place_calls
scenario("routing mode changed: new orders blocked",route)

def stale():
    e,f=engine(); b=candidate(); feed(e,b)
    for q in e.quotes.values(): q["received"]-=10
    try: e.fresh([l["symbol"] for l in b["legs"]])
    except ns["RetryLater"]: pass
    else: raise AssertionError("Stale depth accepted")
scenario("stale quotes: rejected",stale)

def skew():
    e,f=engine(); b=candidate(); feed(e,b)
    e.quotes[b["legs"][0]["symbol"]]["received"]-=1.5
    try: e.fresh([l["symbol"] for l in b["legs"]])
    except ns["RetryLater"]: pass
    else: raise AssertionError("Unsynchronized quotes accepted")
scenario("unsynchronized quote basket: rejected",skew)

def capital():
    e,f=engine(); f.cash=100; b=candidate(); feed(e,b)
    try: e.start_box(b)
    except ns["RetryLater"]: pass
    else: raise AssertionError("Insufficient funds ignored")
    assert not f.place_calls and not e.state["box"]
scenario("insufficient staged capital: entry blocked",capital)

def shutdown():
    e,f=engine(); start(e); full_entry(e); e.state["box"]["reconcile_after"]=0
    e.stop.set(); e.step()
    assert e.state["phase"]=="EXITING" and e.state["pending"]["action"]=="BUY"
scenario("shutdown: closes shorts before long hedges",shutdown)

def endofday():
    e,f=engine(); start(e); full_entry(e); e.state["box"]["reconcile_after"]=0
    clock[0]=datetime(2026,10,7,15,21,tzinfo=ZoneInfo("Asia/Kolkata")); e.step()
    assert e.state["phase"]=="EXITING" and e.state["pending"]["action"]=="BUY"
scenario("15:20 deadline: starts coordinated exit",endofday)

def feed_failure():
    e,f=engine(); start(e); full_entry(e); e.state["box"]["reconcile_after"]=0
    e.state["box"]["last_depth_ok"]=time.time()-30; e.quotes.clear()
    try: e.step()
    except ns["RetryLater"]: pass
    assert e.state["phase"]=="EXITING"
    e.step(); assert e.state["pending"]["action"]=="BUY"
scenario("feed outage: unwind starts without pretending stale prices are current",feed_failure)

def accounting():
    e,f=engine(); start(e); full_entry(e)
    b=e.state["box"]
    for leg in b["legs"]:
        f.market_prices[leg["symbol"]]=50 if leg["action"]=="BUY" else 15
        price=f.market_prices[leg["symbol"]]
        e.quotes[leg["symbol"]].update(bid=price,ask=price,received=time.monotonic())
    expected=650-(8*30+(9100+8450)*.003)-26
    assert abs(e.close_value()-expected)<.00001
    flatten(e)
    archived=json.loads((e.path.parent/(e.path.stem+".completed.jsonl")).read_text())
    assert archived["cashflow"]==650 and archived["turnover"]==17550
scenario("gross/net cashflow, turnover and conservative close buffer calculations",accounting)

def scanner():
    e,f=engine(); b=candidate(); feed(e,b)
    e.expiry="13OCT26"
    e.strikes={}
    for leg in b["legs"]:
        e.strikes.setdefault(leg["strike"],{})[leg["type"].lower()]=leg
    found=e.candidates()
    assert len(found)==1 and found[0]["qty"]==65 and found[0]["debit"]==60
    assert [l["action"] for l in found[0]["legs"]]==["BUY","BUY","SELL","SELL"]
    for q in e.quotes.values(): q["ask_qty"]=1
    assert not e.candidates()
scenario("scanner formula, same-expiry legs, lot sizing and depth capacity",scanner)

def stage_margin():
    e,f=engine(); start(e)
    assert [len(x) for x in f.margin_baskets]==[1,2,3,4]
scenario("all four intermediate baskets receive margin preflight",stage_margin)

def durable_intent():
    e,f=engine(); start(e)
    def inspect():
        journal=json.loads(e.path.read_text())
        assert journal["pending"]["order_id"] is None
        assert journal["pending"]["qty"]==65 and journal["pending"]["intent"]
        assert "ISOLATED_FAKE_ACCOUNT_NO_NETWORK" not in e.path.read_text()
    f.before_submit=inspect; e.entry_step()
scenario("submission intent durably journaled before network call; key absent",durable_intent)

def working_order():
    e,f=engine(); b=start(e)
    f.orders.append(dict(b["legs"][0], exchange="NFO",product="NRML",
                         orderid="foreign",order_status="open",quantity=65))
    try: e.entry_step()
    except ns["NeedsAttention"]: pass
    else: raise AssertionError("Working foreign order ignored")
    assert not f.place_calls
scenario("foreign working order: submission blocked",working_order)

def process_lock():
    e,f=engine()
    try: engine(f,ns["STATE_DIR"])
    except BlockingIOError: pass
    else: raise AssertionError("Second process acquired existing account lock")
scenario("second process cannot acquire the same account lock",process_lock)

def rejected_exits():
    e,f=engine(); start(e); full_entry(e,1)
    e.state["phase"]="EXITING"
    for _ in range(3):
        f.next="reject"; e.exit_step(); e.poll_pending()
    assert e.state["halt"] and e.state["box"]["inventory"][candidate()["legs"][0]["symbol"]]==65
    before=len(f.place_calls); e.step()
    assert len(f.place_calls)==before
scenario("repeated exit rejections: halt with inventory retained; no duplicate loop",rejected_exits)

def interrupted_run():
    e,f=engine(); start(e); full_entry(e,1)
    e.subscription=set(e.contracts)
    saved_step=e.step
    count=[0]
    def fail_then_exit():
        count[0]+=1
        if count[0]==1:
            raise RuntimeError("simulated interruption between entry legs")
        assert e.state["phase"]=="EXITING"
        flatten(e); e.stop.set()
    e.step=fail_then_exit
    e.run()
    assert not any(f.pos.values()) and len(f.place_calls)==2
scenario("run-loop exception between legs switches to unwind before further entry",interrupted_run)

def sdk_depth_payload():
    e,f=engine(); symbol=candidate()["legs"][0]["symbol"]
    e.on_depth({"exchange":"NFO","symbol":symbol,"mode":3,"data":{
        "timestamp":time.time()*1000,"depth":{
            "buy":[{"price":"49.95","quantity":"130","orders":2}],
            "sell":[{"price":"50.00","quantity":"195","orders":3}]}}})
    q=e.fresh([symbol])[0]
    assert q["bid"]==49.95 and q["ask"]==50 and q["bid_qty"]==130 and q["ask_qty"]==195
scenario("exact installed SDK nested depth callback parsed correctly",sdk_depth_payload)

def funding():
    e,f=engine(); start(e); full_entry(e)
    b=e.state["box"]
    b["opened_at"]="2026-10-06T10:00:00+05:30"
    b["funding_rate"]=.10; b["capital_reserved"]=10000
    baseline=(-3900+3900)-(8*30+(9100+9100)*.003)-26
    assert abs(e.close_value()-(baseline-10000*.10/365))<.000001
scenario("elapsed capital funding is included in close P&L",funding)

report={"passed":results,"count":len(results),"broker":"fake",
        "network_access":False,"real_orders":0,"strategy_file":str(SOURCE)}
(root/"results.json").write_text(json.dumps(report,indent=2))
(SOURCE.parent/"isolated-verification-results.json").write_text(json.dumps(report,indent=2))
print("ISOLATED_SCENARIOS_PASSED",len(results))
print("RESULTS",str(root/"results.json"))
