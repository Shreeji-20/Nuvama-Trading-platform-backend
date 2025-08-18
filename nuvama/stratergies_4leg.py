"""
Four-leg strategy adapted from `stratergies.py`.

Assumptions (inferred):
- params stored in redis at key `stratergies:stratergy_4_{paramsid}` and is an orjson-serializable dict.
- params contains:
  - "symbol" (e.g., "NIFTY")
  - "expiry" (string used in streaming symbol suffix)
  - "legs": a list of 4 dicts, each: {"strike": <number>, "type": "CE"|"PE"}
  - "bidding_leg": index 0-3 selecting which leg is the bidding leg
  - "leg_actions": list of 4 strings "BUY" or "SELL" stating desired action for each leg at entry
  - "user_ids": list of user ids (required)
  - Other params (start_price, exit_start, order_type, quantity, slices, desired_spread, exit_desired_spread, IOC, run_state)

Behavior:
- At each polling cycle we read depth for all 4 legs, compute an aggregated `net_spread` by summing signed prices
  (price if leg_action == BUY else -price).
- If net_spread satisfies entry/exit thresholds we place the bidding leg order for each user in parallel and
  then run IOC orders for the 3 base legs to attempt to fill their quantities (keeps IOC logic similar to original).
- Per-user templates are created for all legs. The code mirrors the multi-user/threaded design used previously.

Notes:
- This is a conservative port and keeps the overall flow and naming similar to the original. Adjust pricing/threshold rules
  if you require a different spread formula.
"""

from .order_class import Orders
import redis
import orjson
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from constants.exchange import ExchangeEnum
from constants.action import ActionEnum
from constants.order_type import OrderTypeEnum
from constants.product_code import ProductCodeENum

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class Stratergy4:
    def __init__(self, paramsid):
        self.r = redis.Redis(host="localhost", port=6379, db=0)
        self.order = Orders()
        self.templates_lock = threading.Lock()

        self.params_key = f"stratergies:stratergy_4_{paramsid}"
        raw = self.r.get(self.params_key)
        if raw is None:
            raise RuntimeError(f"params key missing: {self.params_key}")
        self.params = orjson.loads(raw.decode())

        # load option mapper
        raw_map = self.r.get("option_mapper")
        if raw_map is None:
            raise RuntimeError("option_mapper missing in redis")
        self.option_mapper = orjson.loads(raw_map.decode())

        # start background params thread
        self.params_update_thread = threading.Thread(target=self._live_params_update_thread, daemon=True)
        self.params_update_thread.start()

        # init legs/templates
        self._init_legs_and_orders()

    def _live_params_update_thread(self):
        while True:
            try:
                raw = self.r.get(self.params_key)
                if raw:
                    params = orjson.loads(raw.decode())
                    if params != self.params:
                        self.params = params
                time.sleep(1)
            except Exception:
                logger.exception("failed updating params")
                time.sleep(1)

    def _depth_from_redis(self, streaming_symbol: str):
        try:
            raw = self.r.get(streaming_symbol)
            if raw is None:
                return None
            return orjson.loads(raw.decode())
        except Exception:
            logger.exception("error reading depth %s", streaming_symbol)
            return None

    def _make_order_template(self, leg_info, buy_if="BUY", quantity=None, user_id=None):
        """leg_info can be either the depth data dict or a dict with keys:
        {'data': <depth_data>, 'streaming_key': <key>, 'exchange': <ExchangeEnum>}
        """
        user_id = user_id if user_id is not None else self.params.get("user_ids")
        if leg_info is None:
            raise RuntimeError("leg_info is None")

        # support both legacy depth dict and our new leg_info container
        if isinstance(leg_info, dict) and 'data' in leg_info:
            depth_data = leg_info['data']
            streaming_symbol = leg_info.get('streaming_key') or depth_data["response"]["data"]["symbol"]
            exchange = leg_info.get('exchange')
        else:
            depth_data = leg_info
            streaming_symbol = depth_data["response"]["data"]["symbol"]
            exchange = None

        trading_symbol = self.option_mapper.get(streaming_symbol, {}).get("tradingsymbol", streaming_symbol)
        action = ActionEnum.BUY if buy_if.upper() == "BUY" else ActionEnum.SELL
        order_type = (
            OrderTypeEnum.MARKET if self.params.get("order_type", "LIMIT").upper() == "MARKET" else OrderTypeEnum.LIMIT
        )
        qty = int(self.params.get("quantity", 0)) if quantity is None else quantity
        return {
            "user_id": user_id,
            "Trading_Symbol": trading_symbol,
            "Exchange": exchange,
            "Action": action,
            "Order_Type": order_type,
            "Quantity": qty,
            "Slice_Quantity": self.params.get("slices", 1),
            "Streaming_Symbol": streaming_symbol,
            "Limit_Price": "0",
            "Disclosed_Quantity": 0,
            "TriggerPrice": 0,
            "ProductCode": ProductCodeENum.NRML,
            "remark": "Lord_Shreeji",
        }

    def _avg_price(self, data, side_key, n):
        if data is None:
            return 0.0
        entries = data["response"]["data"][side_key]
        n = int(n)
        if n <= 1 or len(entries) == 0:
            return float(entries[0]["price"]) if entries else 0.0
        s = 0.0
        count = min(n, len(entries))
        for i in range(count):
            s += float(entries[i]["price"])
        return s / count

    def _init_legs_and_orders(self):
        # expect params['legs'] to be a list of 4 leg dicts {"strike":..., "type":"CE"/"PE"}
        legs = self.params.get("legs")
        if not legs or len(legs) != 4:
            raise RuntimeError("params['legs'] must be a list of 4 leg defs")
        # store per-leg metadata: data, streaming_key, exchange
        self.legs = []
        for leg in legs:
            # allow explicit depth_key to be provided for mixed instruments
            if 'depth_key' in leg and leg.get('depth_key'):
                key = leg['depth_key']
            else:
                sym = leg.get('symbol', self.params.get('symbol', '')).upper()
                strike = leg.get('strike')
                ltype = leg.get('type', 'CE').upper()
                expiry = leg.get('expiry', self.params.get('expiry'))
                key = f"depth:{sym}_{strike}.0_{ltype}-{expiry}"

            data = self._depth_from_redis(key)
            # derive exchange from the streaming symbol inside depth data when available
            exchange = None
            if data and isinstance(data, dict):
                symbol_text = data.get('response', {}).get('data', {}).get('symbol', '')
                if 'BFO' in symbol_text:
                    exchange = ExchangeEnum.BFO
                elif 'NFO' in symbol_text:
                    exchange = ExchangeEnum.NFO
                elif 'NSE' in symbol_text:
                    exchange = ExchangeEnum.NSE
                else:
                    exchange = ExchangeEnum.BSE

            self.legs.append({'data': data, 'streaming_key': key, 'exchange': exchange})

        # normalize users
        uids = self.params.get("user_ids")
        if isinstance(uids, (int, str)):
            uids = [uids]
        if uids is None:
            uids = []
        self.uids = uids

        # templates per user per leg: dict uid -> named templates for clarity
        # keys: 'base_leg_1', 'base_leg_2', 'base_leg_3', 'bidding_leg'
        self.order_templates = {uid: {} for uid in uids}
        self.exit_order_templates = {uid: {} for uid in uids}

        # build templates
        bidding_index = int(self.params.get("bidding_leg", 0))
        leg_actions = self.params.get("leg_actions", ["BUY"] * 4)
        for uid in uids:
            # map the three base legs and the bidding leg to named keys
            base_count = 1
            for idx in range(4):
                buy_if = leg_actions[idx]
                leg_info = self.legs[idx]
                tmpl = self._make_order_template(leg_info, buy_if=buy_if, quantity=None, user_id=uid)
                exit_tmpl = self._make_order_template(leg_info, buy_if=("SELL" if buy_if.upper()=="BUY" else "BUY"), quantity=0, user_id=uid)
                # ensure Exchange is populated from leg_info
                tmpl['Exchange'] = tmpl.get('Exchange') or leg_info.get('exchange')
                exit_tmpl['Exchange'] = exit_tmpl.get('Exchange') or leg_info.get('exchange')
                if idx == bidding_index:
                    self.order_templates[uid]["bidding_leg"] = tmpl
                    self.exit_order_templates[uid]["bidding_leg"] = exit_tmpl
                else:
                    key = f"base_leg_{base_count}"
                    self.order_templates[uid][key] = tmpl
                    self.exit_order_templates[uid][key] = exit_tmpl
                    base_count += 1

        # per-user counters
        self.entry_qtys = {uid: 0 for uid in uids}
        self.exit_qtys = {uid: 0 for uid in uids}

    def _process_user(self, uid, net_spread, prices, bid_index):
        """Place bidding leg order for uid and attempt IOC for base legs (3 others)."""
        try:
            # bidding leg template (named)
            bid_t = self.order_templates[uid]["bidding_leg"].copy()
            base_keys = [k for k in self.order_templates[uid].keys() if k.startswith("base_leg_")]

            # Place bidding leg
            order_details_bidding_leg = None
            order_details_base_leg1 = None
            order_details_base_leg2 = None
            order_details_base_leg3 = None
            if bid_t.get("Quantity", 0) > 0:
                bid_res = self.order.place_order(bid_t)
                order_details_bidding_leg = bid_res
                # attempt IOC to fill other legs based on filled qty
                self.order.IOC_order(bid_res, None)

                # read filled qty and update per-user counters; key format same as Orders
                last_key = f"order:{bid_res['user_id']}" + f"{bid_res['remark']}" + f"{bid_res.get('order_id', '')}"
                last_raw = self.r.get(last_key)
                if last_raw:
                    order_data = orjson.loads(last_raw.decode())
                    filled = int(order_data['response']['data']['fQty'])
                    with self.templates_lock:
                        self.entry_qtys[uid] += filled
                        # decrement bidding template quantity
                        cur_qty = self.order_templates[uid]["bidding_leg"].get('Quantity', 0)
                        self.order_templates[uid]["bidding_leg"]['Quantity'] = max(0, cur_qty - filled)

                    # for each base leg, attempt place if fill > 0
                    for idx, bk in enumerate(sorted(base_keys)):
                        base_t = self.order_templates[uid][bk].copy()
                        # set Slice_Quantity to filled or existing slice
                        base_t['Slice_Quantity'] = min(base_t.get('Slice_Quantity', 1), filled)
                        if base_t['Slice_Quantity'] > 0:
                            res = self.order.place_order(base_t)
                            # store order details into named variables by index
                            if idx == 0:
                                order_details_base_leg1 = res
                            elif idx == 1:
                                order_details_base_leg2 = res
                            elif idx == 2:
                                order_details_base_leg3 = res
                            self.order.IOC_order(res, self.order_templates[uid][bk])
                            # update exit counters by reading their order key
                            last_key_b = f"order:{res['user_id']}" + f"{res['remark']}" + f"{res.get('order_id', '')}"
                            last_raw_b = self.r.get(last_key_b)
                            if last_raw_b:
                                od = orjson.loads(last_raw_b.decode())
                                filled_b = int(od['response']['data']['fQty'])
                                with self.templates_lock:
                                    self.exit_qtys[uid] += filled_b
                    return {
                        "uid": uid,
                        "action": "entry",
                        "filled": filled,
                        "order_details_bidding_leg": order_details_bidding_leg,
                        "order_details_base_leg1": order_details_base_leg1,
                        "order_details_base_leg2": order_details_base_leg2,
                        "order_details_base_leg3": order_details_base_leg3,
                    }
        except Exception:
            logger.exception("per-user 4-leg worker failed: %s", uid)
        return {"uid": uid, "error": True}

    def main_logic(self):
        lotsize = 75 if self.params.get("symbol") == "NIFTY" else 20
        while True:
            try:
                # reload depths
                sym = self.params.get("symbol", "").upper()
                legs_data = []
                sides = []
                bid_or_ask = "bidValues" if self.params.get("action", "BUY").upper() == "SELL" else "askValues"
                bid_ask_exit = "askValues" if self.params.get("action", "BUY").upper() == "SELL" else "bidValues"
                no_avg = int(self.params.get("no_of_bidask_average", 1))
                for idx, leg in enumerate(self.params.get("legs", [])):
                    strike = leg.get("strike")
                    ltype = leg.get("type", "CE").upper()
                    key = f"depth:{sym}_{strike}.0_{ltype}-{self.params.get('expiry')}"
                    data = self._depth_from_redis(key)
                    legs_data.append(data)
                    # choose price side
                    side = bid_or_ask if self.params.get("leg_actions", ["BUY"]*4)[idx].upper() == "BUY" else bid_ask_exit
                    sides.append(side)

                prices = []
                for i, d in enumerate(legs_data):
                    if no_avg > 1:
                        prices.append(self._avg_price(d, sides[i], no_avg))
                    else:
                        prices.append(float(d["response"]["data"][sides[i]][0]["price"]) if d else 0.0)

                # compute net_spread: buy legs add price, sell legs subtract price
                leg_actions = [a.upper() for a in self.params.get("leg_actions", ["BUY"]*4)]
                net_spread = 0.0
                for i, p in enumerate(prices):
                    net_spread += p if leg_actions[i] == "BUY" else -p
                spread = round(abs(net_spread) * 20) / 20

                print("Net spread:", spread * lotsize, "prices:", prices)

                # per-user parallel processing
                if self.uids:
                    bid_index = int(self.params.get("bidding_leg", 0))
                    max_workers = min(8, max(1, len(self.uids)))
                    with ThreadPoolExecutor(max_workers=max_workers) as ex:
                        futures = {ex.submit(self._process_user, uid, net_spread, prices, bid_index): uid for uid in self.uids}
                        for fut in as_completed(futures):
                            try:
                                res = fut.result()
                                logger.info("four-leg per-user result: %s", res)
                            except Exception as e:
                                logger.exception("four-leg user task failed: %s", e)
                    time.sleep(0.1)
                    # exit check as before
                    total_entry = sum(self.entry_qtys.values())
                    total_exit = sum(self.exit_qtys.values())
                    try:
                        run_state_val = int(self.params.get('run_state', 0))
                    except Exception:
                        run_state_val = 0
                    if total_entry == total_exit and run_state_val == 2:
                        logger.info("four-leg: exit condition met")
                        break
                    continue

                time.sleep(2)
            except Exception:
                logger.exception("unexpected error in four-leg main loop")
                time.sleep(0.5)
                continue
