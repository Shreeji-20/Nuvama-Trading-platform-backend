import os
from APIConnect.APIConnect import APIConnect
import redis
import orjson
import importlib.util, sys, pathlib, traceback
import time
from .order_class import Orders
from constants.exchange import ExchangeEnum
from constants.action import ActionEnum
from constants.order_type import OrderTypeEnum
from constants.product_code import ProductCodeENum
import threading
from concurrent.futures import ThreadPoolExecutor ,as_completed

# Removed logger setup - using print statements instead


class Stratergy1:
    def __init__(self, paramsid) -> None:
        self.r = redis.Redis(host="localhost", port=6379, db=0)
        self.order = Orders()
        # lock used when updating shared per-user templates/qtys from worker threads
        self.templates_lock = threading.Lock()

        # load params and basic state
        self.params_key = f"stratergies:stratergy_1_{paramsid}"
        raw_params = self.r.get(self.params_key)
        if raw_params is None:
            raise RuntimeError(f"params key missing in redis: {self.params_key}")
        self.params = orjson.loads(raw_params.decode())
        self.executed = False

        # cached mapping used to translate streaming symbol -> trading symbol
        try:
            raw_map = self.r.get("option_mapper")
            if raw_map is None:
                raise RuntimeError("option_mapper key missing in redis")
            self.option_mapper = orjson.loads(raw_map.decode())
        except Exception as e:
            print(f"ERROR: failed to load option_mapper from redis: {e}")
            raise

        self.completed_straddle = False
        self.completed_exit = False
        
        # Initialize empty dictionaries - will be populated in _init_legs_and_orders
        # These will only be reset if user list changes, preserving quantities otherwise
        self.entry_qtys = {}
        self.exit_qtys = {}

        # start background thread to watch params updates
        self.params_update_thread = threading.Thread(target=self.live_params_update_thread, daemon=True)
        self.params_update_thread.start()

        # initialise legs/templates
        self._init_legs_and_orders()
        # main logic is started externally when desired

    def live_params_update_thread(self):
        while True:
            try:
                params = orjson.loads(self.r.get(self.params_key).decode())
                if self.params != params:
                    # Store current quantities before update
                    current_entry = getattr(self, 'entry_qtys', {}).copy()
                    current_exit = getattr(self, 'exit_qtys', {}).copy()
                    self.params = params
                    self._init_legs_and_orders()
                    # Log if quantities were preserved
                    print(f"INFO: Quantities after param update - Entry: {self.entry_qtys}, Exit: {self.exit_qtys}")
                    print(f"INFO: Previous quantities were - Entry: {current_entry}, Exit: {current_exit}")
            except Exception as e:
                print(f"ERROR: failed to update live params: {e}")
                time.sleep(1)

    # --- initialization helpers -------------------------------------------------
    def _depth_from_redis(self, streaming_symbol: str):
        """Load depth JSON from redis and return parsed object."""
        try:
            raw = self.r.get(streaming_symbol)
            if raw is None:
                print(f"DEBUG: redis key not found: {streaming_symbol}")
                return None
            return orjson.loads(raw.decode())
        except orjson.JSONDecodeError as e:
            print(f"ERROR: invalid JSON for redis key {streaming_symbol}: {e}")
            return None
        except redis.RedisError as e:
            print(f"ERROR: redis error while fetching {streaming_symbol}: {e}")
            return None
        except Exception as e:
            print(f"ERROR: unexpected error reading {streaming_symbol} from redis: {e}")
            return None

    def _init_legs_and_orders(self):
        # choose which leg is base vs other depending on requested base_leg
        sym = self.params["symbol"].upper()
        if self.params["base_leg"].upper() == "CE":
            self.base_leg = self._depth_from_redis(
                f"depth:{sym}_{self.params['call_strike']}.0_CE-{self.params['expiry']}"
            )
            self.other_leg = self._depth_from_redis(
                f"depth:{sym}_{self.params['put_strike']}.0_PE-{self.params['expiry']}"
            )
        else:
            self.base_leg = self._depth_from_redis(
                f"depth:{sym}_{self.params['put_strike']}.0_PE-{self.params['expiry']}"
            )
            self.other_leg = self._depth_from_redis(
                f"depth:{sym}_{self.params['call_strike']}.0_CE-{self.params['expiry']}"
            )

        # determine exchange from the streaming symbol
        if self.base_leg is None:
            print(f"ERROR: base_leg depth not found for params={self.params}")
            raise RuntimeError("base_leg depth missing in redis")
        symbol_text = self.base_leg["response"]["data"]["symbol"]
        if "BFO" in symbol_text:
            self.exchange = ExchangeEnum.BFO
        elif "NFO" in symbol_text:
            self.exchange = ExchangeEnum.NFO
        elif "NSE" in symbol_text:
            self.exchange = ExchangeEnum.NSE
        else:
            self.exchange = ExchangeEnum.BSE


        # create per-user order templates used during runtime
        # params may contain a list of user ids; normalize to list
        uids = self.params.get("user_ids") or self.params.get("user_ids")
        if isinstance(uids, (int, str)):
            uids = [uids]
        if not isinstance(uids, list):
            uids = list(uids) if uids is not None else []

        # dictionaries keyed by user_id -> template
        self.order_templates = {}
        self.order_templates_base_leg = {}
        self.exit_order_templates = {}
        self.exit_order_templates_base_leg = {}

        # save normalized user list and create per-user tracking structures
        self.uids = uids
        
        # Only initialize entry/exit quantities if they don't exist or if user list changed
        if not hasattr(self, 'entry_qtys') or set(self.entry_qtys.keys()) != set(uids):
            # Preserve existing quantities for users that still exist
            old_entry_qtys = getattr(self, 'entry_qtys', {})
            old_exit_qtys = getattr(self, 'exit_qtys', {})
            
            self.entry_qtys = {uid: old_entry_qtys.get(uid, 0) for uid in uids}
            self.exit_qtys = {uid: old_exit_qtys.get(uid, 0) for uid in uids}
            print(f"INFO: Quantity tracking updated - Entry: {self.entry_qtys}, Exit: {self.exit_qtys}")
        
        self.completed_straddles = {uid: False for uid in uids}
        self.completed_exits = {uid: False for uid in uids}

        if self.params['action'].upper() == "BUY":
            for uid in uids:
                self.order_templates[uid] = self._make_order_template(self.other_leg, buy_if="BUY", user_id=uid)
                self.order_templates_base_leg[uid] = self._make_order_template(self.base_leg, buy_if="BUY", user_id=uid)
                self.exit_order_templates[uid] = self._make_order_template(self.other_leg, buy_if="SELL", quantity=0, user_id=uid)
                self.exit_order_templates_base_leg[uid] = self._make_order_template(self.base_leg, buy_if="SELL", quantity=0, user_id=uid)
        else:
            for uid in uids:
                self.order_templates[uid] = self._make_order_template(self.other_leg, buy_if="SELL", user_id=uid)
                self.order_templates_base_leg[uid] = self._make_order_template(self.base_leg, buy_if="SELL", user_id=uid)
                self.exit_order_templates[uid] = self._make_order_template(self.other_leg, buy_if="BUY", quantity=0, user_id=uid)
                self.exit_order_templates_base_leg[uid] = self._make_order_template(self.base_leg, buy_if="BUY", quantity=0, user_id=uid)

        # keep single-template attributes for backward compatibility (use first user if present)
        first_uid = uids[0] if uids else None
        if first_uid is not None:
            self.order_details = self.order_templates[first_uid]
            self.order_details_base_leg = self.order_templates_base_leg[first_uid]
            self.exit_order_details = self.exit_order_templates[first_uid]
            self.exit_order_details_base_leg = self.exit_order_templates_base_leg[first_uid]
        else:
            # fallback to single templates without user_id
            self.order_details = self._make_order_template(self.other_leg, buy_if=("BUY" if self.params['action'].upper() == "BUY" else "SELL"))
            self.order_details_base_leg = self._make_order_template(self.base_leg, buy_if=("BUY" if self.params['action'].upper() == "BUY" else "SELL"))
            self.exit_order_details = self._make_order_template(self.other_leg, buy_if=("SELL" if self.params['action'].upper() == "BUY" else "BUY"), quantity=0)
            self.exit_order_details_base_leg = self._make_order_template(self.base_leg, buy_if=("SELL" if self.params['action'].upper() == "BUY" else "BUY"), quantity=0)

    def _make_order_template(self, leg_obj, buy_if="BUY", quantity=None, user_id=None):
        """Return a dict template for orders built from a depth/leg object.

        - buy_if: the action string to use as 'BUY' for entry templates, 'SELL' for exit templates.
        - quantity: optional override for Quantity.
        """
        user_id = user_id if user_id is not None else self.params.get("user_ids")
        if leg_obj is None:
            raise RuntimeError("leg_obj is None when building order template")
        streaming_symbol = leg_obj["response"]["data"]["symbol"]
        trading_symbol = self.option_mapper[streaming_symbol]["tradingsymbol"]

        action = ActionEnum.BUY if buy_if.upper() == "BUY" else ActionEnum.SELL
        order_type = (
            OrderTypeEnum.MARKET if self.params["order_type"].upper() == "MARKET" else OrderTypeEnum.LIMIT
        )

        qty = int(self.params["quantity"]) if quantity is None else quantity

        return {
            "user_id": user_id,
            "Trading_Symbol": trading_symbol,
            "Exchange": self.exchange,
            "Action": action,
            "Order_Type": order_type,
            "Quantity": qty,
            "Slice_Quantity": self.params["slices"],
            "Streaming_Symbol": streaming_symbol,
            "Limit_Price": "0",
            "Disclosed_Quantity": 0,
            "TriggerPrice": 0,
            "ProductCode": ProductCodeENum.NRML,
            "remark": "Lord_Shreeji",
            "IOC":self.params['IOC_timeout'],
            "exit_price_gap": float(self.params['exit_price_gap'])
        }

    # --- runtime helpers --------------------------------------------------------
    def _avg_price(self, data, side_key, n):
        # compute average of the first n bid/ask prices
        if data is None:
            print(f"DEBUG: _avg_price called with None data for side {side_key}")
            return 0.0
        
        try:
            entries = data["response"]["data"][side_key]
            n = int(n)
            if n <= 1 or len(entries) == 0:
                return float(entries[0]["price"]) if entries else 0.0
            s = 0.0
            count = min(n, len(entries))
            for i in range(count):
                s += float(entries[i]["price"])
            return s / count
        except (KeyError, IndexError, TypeError, ValueError) as e:
            print(f"ERROR: _avg_price failed for side {side_key}: {e}")
            return 0.0

    def _process_user(self, uid, spread, call_price, put_price, call_price_exit=None, put_price_exit=None, call=None, put=None, bid_or_ask=None, bid_ask_exit=None):
        """Worker that runs ENTRY/EXIT logic for a single user (uid).

        This mirrors the original entry/exit logic but operates on per-user templates
        and updates per-user quantities under a lock.
        """
        try:
            # Check if uid exists in templates
            if uid not in self.order_templates:
                print(f"ERROR: User {uid} not found in order templates")
                return {"uid": uid, "error": True}
                
            # Check for None values that could cause multiplication errors
            if call_price is None or put_price is None:
                print(f"ERROR: Invalid price data for user {uid} - call_price: {call_price}, put_price: {put_price}")
                return {"uid": uid, "error": True}
            
            # Set defaults for exit prices if None
            if call_price_exit is None:
                call_price_exit = call_price
            if put_price_exit is None:
                put_price_exit = put_price
                
            # Debug logging for price values
            # print(f"DEBUG: Processing user {uid} - call_price: {call_price}, put_price: {put_price}, call_price_exit: {call_price_exit}, put_price_exit: {put_price_exit}")
            
            # prepare local copies of templates so a thread can mutate them safely
            try:
                # print(f"DEBUG: Copying templates for user {uid}")
                od = self.order_templates[uid].copy()
                od_base = self.order_templates_base_leg[uid].copy()
                ex = self.exit_order_templates[uid].copy()
                ex_base = self.exit_order_templates_base_leg[uid].copy()
                # print(f"DEBUG: Templates copied successfully for user {uid}")
            except Exception as e:
                print(f"ERROR: Failed to copy templates for user {uid}: {type(e).__name__}: {e}")
                return {"uid": uid, "error": True}

            # update limit prices the same way original code did
            try:
                # print(f"DEBUG: Starting price calculations for user {uid}, base_leg: {self.params['base_leg']}")
                if self.params["base_leg"] == "CE":
                    od["Limit_Price"] = str(round((self.params.get("desired_spread") - call_price) * 20) / 20)
                    od_base["Limit_Price"] = str(round(call_price * 20) / 20)
                    exit_call = call_price_exit or call_price
                    exit_price_gap = float(self.params.get('exit_price_gap', 0))
                    
                    # print(f"DEBUG: CE calculations - desired_spread: {self.params.get('desired_spread')}, exit_price_gap: {exit_price_gap}, run_state: {self.params.get('run_state')}")
                    
                    if int(self.params['run_state']) == 2:
                        ex['Limit_Price'] = (round(put_price_exit * 20) / 20) 
                        ex['Limit_Price'] = ex['Limit_Price'] - ((ex['Limit_Price'] * exit_price_gap) / 100) if self.params.get("action").upper() == "BUY" else ex['Limit_Price'] + ((ex['Limit_Price'] * exit_price_gap) / 100)
                        ex['Limit_Price'] = str(round(ex['Limit_Price'] * 20)/20)
                        
                        ex_base['Limit_Price'] = (round(exit_call * 20) / 20)
                        ex_base['Limit_Price'] = ex_base['Limit_Price'] - ((ex_base['Limit_Price'] * exit_price_gap) / 100) if self.params.get("action").upper() == "BUY" else ex_base['Limit_Price'] + ((ex_base['Limit_Price'] * exit_price_gap) / 100)
                        ex_base['Limit_Price'] = str(round(ex_base['Limit_Price'] * 20)/20)
                    else:
                        ex["Limit_Price"] = str(round((self.params.get("exit_desired_spread") - exit_call) * 20) / 20)
                        ex_base["Limit_Price"] = str(round(exit_call * 20) / 20)
                else:
                    exit_put = put_price_exit or put_price
                    od["Limit_Price"] = str(round((self.params.get("desired_spread") - exit_put) * 20) / 20)
                    od_base["Limit_Price"] = str(round(exit_put * 20) / 20)
                    exit_price_gap = float(self.params.get('exit_price_gap', 0))
                    
                    if int(self.params['run_state']) == 2:
                        ex['Limit_Price'] = (round(call_price_exit * 20) / 20)
                        ex['Limit_Price'] = ex['Limit_Price'] - ((ex['Limit_Price'] * exit_price_gap) / 100) if self.params.get("action").upper() == "BUY" else ex['Limit_Price'] + ((ex['Limit_Price'] * exit_price_gap) / 100)
                        ex['Limit_Price'] = str(round(ex['Limit_Price'] * 20)/20)
                        
                        ex_base['Limit_Price'] = (round(exit_put * 20) / 20)
                        ex_base['Limit_Price'] = ex_base['Limit_Price'] - ((ex_base['Limit_Price'] * exit_price_gap) / 100) if self.params.get("action").upper() == "BUY" else ex_base['Limit_Price'] + ((ex_base['Limit_Price'] * exit_price_gap) / 100)
                        ex_base['Limit_Price'] = str(round(ex_base['Limit_Price'] * 20)/20)
                    else:
                        ex["Limit_Price"] = str(round((self.params.get("desired_spread") - exit_put) * 20) / 20)
                        ex_base["Limit_Price"] = str(round(exit_put * 20) / 20)
                        
                # print(f"DEBUG: Price calculations completed for user {uid}")
            except Exception as e:
                print(f"ERROR: Price calculation failed for user {uid}: {type(e).__name__}: {e}")
                return {"uid": uid, "error": True}

            # ENTRY
            if self.params["action"].upper() == "BUY":
                # Check if user still has quantity to fill (entry quantity should not exceed total desired quantity)
                current_entry_qty = self.entry_qtys.get(uid, 0)
                desired_total_qty = int(self.params["quantity"])
                remaining_qty = desired_total_qty - current_entry_qty
                
                if (spread < self.params["start_price"] and 
                    od["Quantity"] > 0 and 
                    int(self.params['run_state']) == 0 and 
                    remaining_qty > 0):
                    
                    # Only place order for remaining quantity
                    actual_order_qty = min(od["Quantity"], remaining_qty)
                    od["Quantity"] = actual_order_qty
                    
                    if not od["Quantity"] >= self.params["slices"]:
                        od["Slice_Quantity"] = od["Quantity"]
                    od = self.order.place_order(od)
                    self.order.IOC_order(od, *[od_base])
                    # read latest order data and update per-user counters
                    last_key = f"order:{od['user_id']}" + f"{od['remark']}" + f"{od.get('order_id', '')}"
                    last_raw = self.r.get(last_key)
                    if last_raw:
                        lastest_order_data = orjson.loads(last_raw.decode())
                        filled = int(lastest_order_data["response"]["data"]["fQty"])
                        with self.templates_lock:
                            self.entry_qtys[uid] += filled
                            # decrement stored template quantity if present
                            if self.order_templates[uid].get("Quantity", 0) > 0:
                                self.order_templates[uid]["Quantity"] = max(0, self.order_templates[uid]["Quantity"] - filled)
                            print(f"INFO: Entry order filled for user {uid}: {filled} (total entry: {sum(self.entry_qtys.values())}) (user entry: {self.entry_qtys[uid]}/{desired_total_qty})")
                            print("Order placed: ", sum(self.entry_qtys.values()), " : ", type(sum(self.entry_qtys.values())))
                            # breakpoint()
                    return {"uid": uid, "action": "entry"}

                # EXIT
                # Debug the exit condition
                exit_condition_1 = spread > self.params["exit_start"] or int(self.params['run_state']) == 2
                exit_condition_2 = self.entry_qtys.get(uid, 0) > self.exit_qtys.get(uid, 0)
                
                if exit_condition_1 and exit_condition_2:
                    print("Buying Exit!")
                    print(f"INFO: EXIT TRIGGERED for user {uid}: spread={spread}, exit_start={self.params.get('exit_start')}, run_state={self.params.get('run_state')}, entry_qty={self.entry_qtys.get(uid, 0)}, exit_qty={self.exit_qtys.get(uid, 0)}")
                    
                    # Calculate remaining quantity to exit (should not exceed entry quantity)
                    remaining_exit_qty = self.entry_qtys.get(uid, 0) - self.exit_qtys.get(uid, 0)
                    
                    if remaining_exit_qty > 0:
                        ex["Quantity"] = remaining_exit_qty
                        if not ex["Quantity"] >= self.params["slices"]:
                            ex["Slice_Quantity"] = ex["Quantity"]
                        ex = self.order.place_order(ex)
                        self.order.IOC_order(ex, *[ex_base])
                        last_key = f"order:{ex['user_id']}" + f"{ex['remark']}" + f"{ex.get('order_id', '')}"
                        last_raw = self.r.get(last_key)
                        if last_raw:
                            lastest_order_data_exit = orjson.loads(last_raw.decode())
                            filled = int(lastest_order_data_exit["response"]["data"]["fQty"])
                            with self.templates_lock:
                                self.exit_qtys[uid] += filled
                                print(f"INFO: Exit order filled for user {uid}: {filled} (total exit: {sum(self.exit_qtys.values())}) (user exit: {self.exit_qtys[uid]}/{self.entry_qtys.get(uid, 0)})")
                        return {"uid": uid, "action": "exit"}
                    else:
                        print(f"INFO: No remaining quantity to exit for user {uid}")
                        return {"uid": uid, "action": "no_exit_needed"}

            else:  # SELL behaviour mirrored
                # Check if user still has quantity to fill (entry quantity should not exceed total desired quantity)
                current_entry_qty = self.entry_qtys.get(uid, 0)
                desired_total_qty = int(self.params["quantity"])
                remaining_qty = desired_total_qty - current_entry_qty
                
                if (spread > self.params["start_price"] and 
                    od["Quantity"] > 0 and 
                    int(self.params['run_state']) == 0 and 
                    remaining_qty > 0):
                    
                    # Only place order for remaining quantity
                    actual_order_qty = min(od["Quantity"], remaining_qty)
                    od["Quantity"] = actual_order_qty
                    
                    if not od["Quantity"] >= self.params["slices"]:
                        od["Slice_Quantity"] = od["Quantity"]
                    od = self.order.place_order(od)
                    self.order.IOC_order(od, *[od_base])
                    last_key = f"order:{od['user_id']}" + f"{od['remark']}" + f"{od.get('order_id', '')}"
                    last_raw = self.r.get(last_key)
                    if last_raw:
                        lastest_order_data = orjson.loads(last_raw.decode())
                        filled = int(lastest_order_data["response"]["data"]["fQty"])
                        with self.templates_lock:
                            self.entry_qtys[uid] += filled
                            if self.order_templates[uid].get("Quantity", 0) > 0:
                                self.order_templates[uid]["Quantity"] = max(0, self.order_templates[uid]["Quantity"] - filled)
                            print(f"INFO: SELL Entry order filled for user {uid}: {filled} (total entry: {sum(self.entry_qtys.values())}) (user entry: {self.entry_qtys[uid]}/{desired_total_qty})")
                    return {"uid": uid, "action": "entry"}

                # EXIT for SELL behavior  
                exit_condition_1_sell = spread < self.params["exit_start"] or int(self.params['run_state']) == 2
                exit_condition_2_sell = self.entry_qtys.get(uid, 0) > self.exit_qtys.get(uid, 0)
                
                if exit_condition_1_sell and exit_condition_2_sell:
                    print("SELL Exiting !")
                    print(f"INFO: SELL EXIT TRIGGERED for user {uid}: spread={spread}, exit_start={self.params.get('exit_start')}, run_state={self.params.get('run_state')}, entry_qty={self.entry_qtys.get(uid, 0)}, exit_qty={self.exit_qtys.get(uid, 0)}")
                    
                    # Calculate remaining quantity to exit (should not exceed entry quantity)
                    remaining_exit_qty = self.entry_qtys.get(uid, 0) - self.exit_qtys.get(uid, 0)
                    
                    if remaining_exit_qty > 0:
                        ex["Quantity"] = remaining_exit_qty
                        if not ex["Quantity"] >= self.params["slices"]:
                            ex["Slice_Quantity"] = ex["Quantity"]
                        ex = self.order.place_order(ex)
                        self.order.IOC_order(ex, *[ex_base])
                        last_key = f"order:{ex['user_id']}" + f"{ex['remark']}" + f"{ex.get('order_id', '')}"
                        last_raw = self.r.get(last_key)
                        if last_raw:
                            lastest_order_data_exit = orjson.loads(last_raw.decode())
                            filled = int(lastest_order_data_exit["response"]["data"]["fQty"])
                            with self.templates_lock:
                                self.exit_qtys[uid] += filled
                                print(f"INFO: SELL Exit order filled for user {uid}: {filled} (total exit: {sum(self.exit_qtys.values())}) (user exit: {self.exit_qtys[uid]}/{self.entry_qtys.get(uid, 0)})")
                        return {"uid": uid, "action": "exit"}
                    else:
                        print(f"INFO: No remaining quantity to exit for user {uid}")
                        return {"uid": uid, "action": "no_exit_needed"}

        except (redis.RedisError, KeyError, IndexError, TypeError, ValueError) as e:
            print(traceback.format_exc())
            print(f"ERROR: per-user ({uid}) flow failed with specific error: {type(e).__name__}: {e}")
        except Exception as e:
            print(traceback.format_exc())
            print(f"ERROR: unexpected error in per-user ({uid}) worker: {type(e).__name__}: {e}")
            return {"uid": uid, "error": True}

    
    # --- main logic (kept behavior identical) ----------------------------------
    def main_logic(self):
        # run until both conditions are met: total entry == total exit AND run_state == 2
        while True:
            try:
                if int(self.params['run_state']) == 1:
                    continue # pause
                # reload live depths each loop
                sym = self.params["symbol"].upper()
                call = self._depth_from_redis(f"depth:{sym}_{self.params['call_strike']}.0_CE-{self.params['expiry']}")
                put = self._depth_from_redis(f"depth:{sym}_{self.params['put_strike']}.0_PE-{self.params['expiry']}")

                bid_or_ask = "bidValues" if self.params["action"].upper() == "SELL" else "askValues"
                bid_ask_exit = "askValues" if self.params["action"].upper() == "SELL" else "bidValues"

                # Safe price extraction with proper None checking
                def safe_get_price(data, side_key):
                    try:
                        if data and data.get("response", {}).get("data", {}).get(side_key):
                            return float(data["response"]["data"][side_key][0]["price"])
                        return 0.0
                    except (KeyError, IndexError, TypeError, ValueError):
                        return 0.0
                
                if int(self.params.get("no_of_bidask_average", 1)) > 1:
                    if self.params["base_leg"] == "CE":
                        call_price = self._avg_price(call, bid_or_ask, self.params["no_of_bidask_average"])
                        put_price = safe_get_price(put, bid_or_ask)
                        call_price_exit = self._avg_price(call, bid_ask_exit, self.params["no_of_bidask_average"])
                        put_price_exit = safe_get_price(put, bid_ask_exit)
                        # print("Exit prices : ",call_price_exit,put_price_exit)
                    else:
                        put_price = self._avg_price(put, bid_or_ask, self.params["no_of_bidask_average"])
                        call_price = safe_get_price(call, bid_or_ask)
                        put_price_exit = self._avg_price(put, bid_ask_exit, self.params["no_of_bidask_average"])
                        call_price_exit = safe_get_price(call, bid_ask_exit)
                else:
                    call_price = safe_get_price(call, bid_or_ask)
                    put_price = safe_get_price(put, bid_or_ask)
                    call_price_exit = safe_get_price(call, bid_ask_exit)
                    put_price_exit = safe_get_price(put, bid_ask_exit)

                # spread calculation preserved from original code
                spread = round(abs(call_price + put_price) * 20) / 20
                
                # Validate that we have valid price data before proceeding
                if call_price is None or put_price is None:
                    print(f"ERROR: Invalid price data - call_price: {call_price}, put_price: {put_price}")
                    print(f"       call data: {call is not None}, put data: {put is not None}")
                    time.sleep(0.1)
                    continue

                # If per-user templates are configured, run per-user logic in parallel
                if getattr(self, "uids", None):
                    max_workers = min(8, max(1, len(self.uids)))
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = {
                            executor.submit(
                                self._process_user,
                                uid,
                                spread,
                                call_price,
                                put_price,
                                locals().get("call_price_exit"),
                                locals().get("put_price_exit"),
                                call,
                                put,
                                bid_or_ask,
                                bid_ask_exit,
                            ): uid
                            for uid in self.uids
                        }
                        for fut in as_completed(futures):
                            try:
                                res = fut.result()
                                # print(f"INFO: per-user result: {res}")
                            except Exception as e:
                                print(f"ERROR: per-user task failed: {e}")
                    # throttle a tiny bit before next polling cycle
                    time.sleep(0.1)
                    
                    # Check exit condition only after processing all users
                    try:
                        total_entry = sum(self.entry_qtys.values())
                    except Exception as e:
                        print(f"ERROR: Error calculating total_entry: {e}")
                        total_entry = 0
                    try:
                        total_exit = sum(self.exit_qtys.values())
                    except Exception as e:
                        print(f"ERROR: Error calculating total_exit: {e}")
                        total_exit = 0
                    try:
                        run_state_val = int(self.params.get('run_state', 0))
                    except Exception:
                        run_state_val = 0
                    
                    # Log quantities for debugging
                    if total_entry > 0 or total_exit > 0:
                        print(f"INFO: Quantities - Entry: {total_entry}, Exit: {total_exit}, Run State: {run_state_val}")
                    
                    if total_entry == total_exit and run_state_val == 2:
                        print(f"INFO: Exit condition met: total_entry={total_entry}, total_exit={total_exit}, run_state={run_state_val}")
                        break
                    # Log current state for debugging
                    # print(f"DEBUG: Current state: total_entry={total_entry}, total_exit={total_exit}, run_state={run_state_val}")
                    continue

                # No single-template fallback: per-user tasks handled above.
                # Sleep to maintain the original polling cadence.
                
            except redis.RedisError as e:
                print(f"ERROR: redis error in main loop: {e}")
                time.sleep(0.5)
                continue
            except (KeyError, IndexError, TypeError, AttributeError, ValueError, orjson.JSONDecodeError) as e:
                # expected parsing/access errors - log and continue polling
                print(f"WARNING: transient data error in main loop: {e}")
                time.sleep(0.1)
                continue
            except Exception as e:
                print(f"ERROR: unexpected error in main loop: {e}")
                time.sleep(0.1)
                continue
            except KeyboardInterrupt:
                print("INFO: KeyboardInterrupt received, exiting main loop")
                os._exit(0)