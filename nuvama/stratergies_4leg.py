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
from concurrent.futures import ThreadPoolExecutor, as_completed

# Removed logger setup - using print statements instead


class Stratergy4Leg:
    def __init__(self, paramsid) -> None:
        self.r = redis.Redis(host="localhost", port=6379, db=0)
        self.order = Orders()
        # lock used when updating shared per-user templates/qtys from worker threads
        self.templates_lock = threading.Lock()

        # load params and basic state (updated to match API format)
        self.params_key = f"4_leg:{paramsid}"
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
        """
        Initialize dynamic legs: base legs + 1 bidding leg
        Expected params structure (updated to match API):
        {
            "leg1": {"symbol": "NIFTY", "strike": 20000, "type": "CE", "expiry": 0},  # 0=current week
            "leg2": {"symbol": "NIFTY", "strike": 20100, "type": "PE", "expiry": 1},  # 1=next week
            "leg3": {"symbol": "NIFTY", "strike": 20200, "type": "CE", "expiry": 2},  # 2=following week
            "legN": {...},  # Any number of legs can be added dynamically
            "bidding_leg": {"symbol": "NIFTY", "strike": 20150, "type": "PE", "expiry": 0},
            "base_legs": ["leg1", "leg2", "leg3"],  # Which legs are base legs
            "bidding_leg_key": "bidding_leg",       # Which leg is bidding leg
            "notes": "Strategy description...",     # Optional notes field
            ... other params
        }
        Note: expiry field now accepts numeric values (0,1,2,3,4,5...) instead of date strings
        """
        
        # Load all dynamic legs from Redis
        self.legs = {}
        
        # Get base legs dynamically from params
        base_leg_keys = self.params.get("base_legs", [])
        if not base_leg_keys:
            # Fallback: try to find leg1, leg2, leg3 for backward compatibility
            possible_legs = ["leg1", "leg2", "leg3", "leg4", "leg5"]
            base_leg_keys = [leg for leg in possible_legs if leg in self.params and self.params[leg]]
        
        self.base_leg_keys = base_leg_keys
        
        # Validate we have at least one base leg
        if not base_leg_keys:
            raise RuntimeError("No base legs found in params. Please specify 'base_legs' array or provide leg1, leg2, etc.")
        
        # Load base legs dynamically
        for leg_key in base_leg_keys:
            leg_info = self.params.get(leg_key)
            if not leg_info:
                raise RuntimeError(f"Missing leg info for {leg_key}")
            
            # Handle numeric expiry (0,1,2,3,4,5 etc.) instead of date format
            expiry_value = leg_info['expiry']
            if isinstance(expiry_value, (int, str)) and str(expiry_value).isdigit():
                # Numeric expiry: use as-is
                depth_key = f"depth:{leg_info['symbol'].upper()}_{leg_info['strike']}.0_{leg_info['type']}-{expiry_value}"
            else:
                # Legacy date format: keep existing behavior
                depth_key = f"depth:{leg_info['symbol'].upper()}_{leg_info['strike']}.0_{leg_info['type']}-{leg_info['expiry']}"
            
            leg_data = self._depth_from_redis(depth_key)
            
            if leg_data is None:
                print(f"ERROR: {leg_key} depth not found: {depth_key}")
                raise RuntimeError(f"{leg_key} depth missing in redis")
            
            self.legs[leg_key] = {
                'data': leg_data,
                'info': leg_info,
                'depth_key': depth_key
            }
        
        # Load bidding leg dynamically
        bidding_leg_key = self.params.get("bidding_leg_key", "bidding_leg")
        self.bidding_leg_key = bidding_leg_key
        
        bidding_leg_info = self.params.get(bidding_leg_key)
        if not bidding_leg_info:
            raise RuntimeError(f"Missing bidding leg info for {bidding_leg_key}")
        
        # Handle numeric expiry (0,1,2,3,4,5 etc.) for bidding leg
        expiry_value = bidding_leg_info['expiry']
        if isinstance(expiry_value, (int, str)) and str(expiry_value).isdigit():
            # Numeric expiry: use as-is
            bidding_depth_key = f"depth:{bidding_leg_info['symbol'].upper()}_{bidding_leg_info['strike']}.0_{bidding_leg_info['type']}-{expiry_value}"
        else:
            # Legacy date format: keep existing behavior
            bidding_depth_key = f"depth:{bidding_leg_info['symbol'].upper()}_{bidding_leg_info['strike']}.0_{bidding_leg_info['type']}-{bidding_leg_info['expiry']}"
        
        bidding_leg_data = self._depth_from_redis(bidding_depth_key)
        
        if bidding_leg_data is None:
            print(f"ERROR: bidding leg depth not found: {bidding_depth_key}")
            raise RuntimeError("bidding leg depth missing in redis")
        
        self.legs[bidding_leg_key] = {
            'data': bidding_leg_data,
            'info': bidding_leg_info,
            'depth_key': bidding_depth_key
        }

        # Log strategy info including notes if available
        notes = self.params.get("notes", "")
        print(f"INFO: Initializing dynamic strategy with {len(base_leg_keys)} base legs: {base_leg_keys}")
        print(f"INFO: Bidding leg: {bidding_leg_key}")
        
        # Log expiry format being used
        sample_leg = self.params.get(base_leg_keys[0]) if base_leg_keys else None
        if sample_leg:
            expiry_val = sample_leg.get('expiry')
            if isinstance(expiry_val, (int, str)) and str(expiry_val).isdigit():
                print(f"INFO: Using numeric expiry format (0=current, 1=next week, etc.)")
            else:
                print(f"INFO: Using date expiry format")
        
        if notes:
            print(f"INFO: Strategy notes: {notes}")

        # determine exchange from the first base leg streaming symbol
        first_base_leg = self.legs[base_leg_keys[0]]['data']
        symbol_text = first_base_leg["response"]["data"]["symbol"]
        if "BFO" in symbol_text:
            self.exchange = ExchangeEnum.BFO
        elif "NFO" in symbol_text:
            self.exchange = ExchangeEnum.NFO
        elif "NSE" in symbol_text:
            self.exchange = ExchangeEnum.NSE
        else:
            self.exchange = ExchangeEnum.BSE

        # create per-user order templates used during runtime
        # params may contain a list of user ids; normalize to list (updated for API format)
        uids = self.params.get("user_ids", [])
        if isinstance(uids, (int, str)):
            uids = [str(uids)]  # Convert to string for consistency
        if not isinstance(uids, list):
            uids = list(uids) if uids is not None else []
        
        # Convert all user IDs to strings for consistency
        uids = [str(uid) for uid in uids]

        # dictionaries keyed by user_id -> template for each leg
        self.order_templates = {}  # For bidding leg entry orders
        self.base_leg_templates = {}  # For base legs entry orders
        self.exit_order_templates = {}  # For bidding leg exit orders
        self.exit_base_leg_templates = {}  # For base legs exit orders

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

        # Create templates for each user
        action = self.params['action'].upper()
        for uid in uids:
            # Initialize dictionaries for this user
            self.order_templates[uid] = {}
            self.base_leg_templates[uid] = {}
            self.exit_order_templates[uid] = {}
            self.exit_base_leg_templates[uid] = {}
            
            # Bidding leg templates (main trading leg)
            bidding_leg_data = self.legs[bidding_leg_key]['data']
            self.order_templates[uid] = self._make_order_template(
                bidding_leg_data, 
                buy_if=action, 
                user_id=uid
            )
            self.exit_order_templates[uid] = self._make_order_template(
                bidding_leg_data, 
                buy_if=("SELL" if action == "BUY" else "BUY"), 
                quantity=0, 
                user_id=uid
            )
            
            # Base legs templates
            for base_leg_key in base_leg_keys:
                base_leg_data = self.legs[base_leg_key]['data']
                self.base_leg_templates[uid][base_leg_key] = self._make_order_template(
                    base_leg_data, 
                    buy_if=action, 
                    user_id=uid
                )
                self.exit_base_leg_templates[uid][base_leg_key] = self._make_order_template(
                    base_leg_data, 
                    buy_if=("SELL" if action == "BUY" else "BUY"), 
                    quantity=0, 
                    user_id=uid
                )

        # keep single-template attributes for backward compatibility (use first user if present)
        first_uid = uids[0] if uids else None
        if first_uid is not None:
            self.order_details = self.order_templates[first_uid]
            self.base_leg_details = self.base_leg_templates[first_uid]
            self.exit_order_details = self.exit_order_templates[first_uid]
            self.exit_base_leg_details = self.exit_base_leg_templates[first_uid]

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
            "IOC": self.params['IOC_timeout'],
            "exit_price_gap": float(self.params.get('exit_price_gap', 0))
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

    def _safe_get_price(self, data, side_key):
        """Safe price extraction with proper None checking"""
        try:
            if data and data.get("response", {}).get("data", {}).get(side_key):
                return float(data["response"]["data"][side_key][0]["price"])
            return 0.0
        except (KeyError, IndexError, TypeError, ValueError):
            return 0.0

    def _get_leg_prices(self, bid_or_ask):
        """Get current prices for all legs (supports dynamic number of legs)"""
        prices = {}
        
        # Get base leg prices dynamically
        for base_leg_key in self.base_leg_keys:
            try:
                leg_data = self._depth_from_redis(self.legs[base_leg_key]['depth_key'])
                if self.params.get("no_of_bidask_average", 1) > 1:
                    prices[base_leg_key] = self._avg_price(leg_data, bid_or_ask, self.params["no_of_bidask_average"])
                else:
                    prices[base_leg_key] = self._safe_get_price(leg_data, bid_or_ask)
            except (KeyError, TypeError) as e:
                print(f"ERROR: Failed to get price for {base_leg_key}: {e}")
                prices[base_leg_key] = 0.0
        
        # Get bidding leg price (for display/validation purposes)
        try:
            bidding_leg_data = self._depth_from_redis(self.legs[self.bidding_leg_key]['depth_key'])
            if self.params.get("no_of_bidask_average", 1) > 1:
                prices[self.bidding_leg_key] = self._avg_price(bidding_leg_data, bid_or_ask, self.params["no_of_bidask_average"])
            else:
                prices[self.bidding_leg_key] = self._safe_get_price(bidding_leg_data, bid_or_ask)
        except (KeyError, TypeError) as e:
            print(f"ERROR: Failed to get price for bidding leg {self.bidding_leg_key}: {e}")
            prices[self.bidding_leg_key] = 0.0
        
        return prices

    def _process_user(self, uid, spread, leg_prices, leg_prices_exit=None):
        """Worker that runs ENTRY/EXIT logic for a single user (uid).

        This handles dynamic-leg strategy where bidding_leg_price = desired_spread - sum(base_legs_prices)
        Supports any number of base legs as defined in params["base_legs"]
        """
        try:
            # Validate price data
            base_leg_sum = sum(leg_prices.get(key, 0) for key in self.base_leg_keys)
            if base_leg_sum <= 0:
                print(f"ERROR: Invalid base leg prices for user {uid}: {leg_prices}")
                return {"uid": uid, "error": True}
            
            # Set defaults for exit prices if None
            if leg_prices_exit is None:
                leg_prices_exit = leg_prices.copy()
                
            # prepare local copies of templates so a thread can mutate them safely
            # Bidding leg templates
            od = self.order_templates[uid].copy()
            ex = self.exit_order_templates[uid].copy()
            
            # Base legs templates
            od_base_legs = {}
            ex_base_legs = {}
            for base_leg_key in self.base_leg_keys:
                od_base_legs[base_leg_key] = self.base_leg_templates[uid][base_leg_key].copy()
                ex_base_legs[base_leg_key] = self.exit_base_leg_templates[uid][base_leg_key].copy()

            # Calculate prices: bidding_leg_price = desired_spread - sum(base_legs_prices)
            desired_spread = self.params.get("desired_spread", 0)
            exit_desired_spread = self.params.get("exit_desired_spread", 0)
            
            # Entry prices
            base_legs_price_sum = sum(leg_prices.get(key, 0) for key in self.base_leg_keys)
            bidding_leg_entry_price = desired_spread - base_legs_price_sum
            
            # Exit prices
            base_legs_exit_price_sum = sum(leg_prices_exit.get(key, 0) for key in self.base_leg_keys)
            
            # Update limit prices
            od["Limit_Price"] = str(round(bidding_leg_entry_price * 20) / 20)
            
            # Set base leg prices
            for base_leg_key in self.base_leg_keys:
                price = leg_prices.get(base_leg_key, 0)
                od_base_legs[base_leg_key]["Limit_Price"] = str(round(price * 20) / 20)
            
            # Handle exit pricing
            if int(self.params['run_state']) == 2:
                # Market exit with price gap adjustment
                exit_price_gap = float(self.params.get('exit_price_gap', 0))
                action = self.params.get("action", "").upper()
                
                # Bidding leg exit price
                bidding_exit_price = leg_prices_exit.get(self.bidding_leg_key, 0)
                if action == "BUY":
                    bidding_exit_price = bidding_exit_price - ((bidding_exit_price * exit_price_gap) / 100)
                else:
                    bidding_exit_price = bidding_exit_price + ((bidding_exit_price * exit_price_gap) / 100)
                ex['Limit_Price'] = str(round(bidding_exit_price * 20) / 20)
                
                # Base legs exit prices
                for base_leg_key in self.base_leg_keys:
                    base_exit_price = leg_prices_exit.get(base_leg_key, 0)
                    if action == "BUY":
                        base_exit_price = base_exit_price - ((base_exit_price * exit_price_gap) / 100)
                    else:
                        base_exit_price = base_exit_price + ((base_exit_price * exit_price_gap) / 100)
                    ex_base_legs[base_leg_key]['Limit_Price'] = str(round(base_exit_price * 20) / 20)
            else:
                # Normal exit using exit_desired_spread
                bidding_leg_exit_price = exit_desired_spread - base_legs_exit_price_sum
                ex["Limit_Price"] = str(round(bidding_leg_exit_price * 20) / 20)
                
                for base_leg_key in self.base_leg_keys:
                    price = leg_prices_exit.get(base_leg_key, 0)
                    ex_base_legs[base_leg_key]["Limit_Price"] = str(round(price * 20) / 20)

            # ENTRY LOGIC
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
                    
                    # Execute entry orders for all legs
                    if not od["Quantity"] >= self.params["slices"]:
                        od["Slice_Quantity"] = od["Quantity"]
                    
                    # Update base leg quantities to match
                    for base_leg_key in self.base_leg_keys:
                        od_base_legs[base_leg_key]["Quantity"] = actual_order_qty
                        if not od_base_legs[base_leg_key]["Quantity"] >= self.params["slices"]:
                            od_base_legs[base_leg_key]["Slice_Quantity"] = od_base_legs[base_leg_key]["Quantity"]
                    
                    # Place bidding leg order
                    od = self.order.place_order(od)
                    
                    # Place base leg orders
                    base_leg_orders = []
                    for base_leg_key in self.base_leg_keys:
                        base_leg_order = self.order.place_order(od_base_legs[base_leg_key])
                        base_leg_orders.append(base_leg_order)
                    
                    # Execute IOC for all legs
                    self.order.IOC_order(od, *base_leg_orders)
                    
                    # Update quantities
                    last_key = f"order:{od['user_id']}" + f"{od['remark']}" + f"{od.get('order_id', '')}"
                    last_raw = self.r.get(last_key)
                    if last_raw:
                        lastest_order_data = orjson.loads(last_raw.decode())
                        filled = int(lastest_order_data["response"]["data"]["fQty"])
                        with self.templates_lock:
                            self.entry_qtys[uid] += filled
                            if self.order_templates[uid].get("Quantity", 0) > 0:
                                self.order_templates[uid]["Quantity"] = max(0, self.order_templates[uid]["Quantity"] - filled)
                            print(f"INFO: Dynamic {len(self.base_leg_keys)}-leg entry order filled for user {uid}: {filled} (total entry: {sum(self.entry_qtys.values())}) (user entry: {self.entry_qtys[uid]}/{desired_total_qty})")
                    
                    return {"uid": uid, "action": "entry"}

                # EXIT LOGIC
                exit_condition_1 = spread > self.params["exit_start"] or int(self.params['run_state']) == 2
                exit_condition_2 = self.entry_qtys.get(uid, 0) > self.exit_qtys.get(uid, 0)
                
                if exit_condition_1 and exit_condition_2:
                    print(f"Dynamic {len(self.base_leg_keys)}-leg BUY Exit!")
                    print(f"INFO: Dynamic {len(self.base_leg_keys)}-leg EXIT TRIGGERED for user {uid}: spread={spread}, exit_start={self.params.get('exit_start')}, run_state={self.params.get('run_state')}, entry_qty={self.entry_qtys.get(uid, 0)}, exit_qty={self.exit_qtys.get(uid, 0)}")
                    
                    # Calculate remaining quantity to exit (should not exceed entry quantity)
                    remaining_exit_qty = self.entry_qtys.get(uid, 0) - self.exit_qtys.get(uid, 0)
                    
                    if remaining_exit_qty > 0:
                        ex["Quantity"] = remaining_exit_qty
                        
                        if not ex["Quantity"] >= self.params["slices"]:
                            ex["Slice_Quantity"] = ex["Quantity"]
                        
                        # Update base leg exit quantities to match
                        for base_leg_key in self.base_leg_keys:
                            ex_base_legs[base_leg_key]["Quantity"] = remaining_exit_qty
                            if not ex_base_legs[base_leg_key]["Quantity"] >= self.params["slices"]:
                                ex_base_legs[base_leg_key]["Slice_Quantity"] = ex_base_legs[base_leg_key]["Quantity"]
                        
                        # Place exit orders for all legs
                        ex = self.order.place_order(ex)
                        
                        # Place base leg exit orders
                        base_leg_exit_orders = []
                        for base_leg_key in self.base_leg_keys:
                            base_leg_exit_order = self.order.place_order(ex_base_legs[base_leg_key])
                            base_leg_exit_orders.append(base_leg_exit_order)
                        
                        # Execute IOC for all exit legs
                        self.order.IOC_order(ex, *base_leg_exit_orders)
                        
                        # Update exit quantities
                        last_key = f"order:{ex['user_id']}" + f"{ex['remark']}" + f"{ex.get('order_id', '')}"
                        last_raw = self.r.get(last_key)
                        if last_raw:
                            lastest_order_data_exit = orjson.loads(last_raw.decode())
                            filled = int(lastest_order_data_exit["response"]["data"]["fQty"])
                            with self.templates_lock:
                                self.exit_qtys[uid] += filled
                                print(f"INFO: Dynamic {len(self.base_leg_keys)}-leg exit order filled for user {uid}: {filled} (total exit: {sum(self.exit_qtys.values())}) (user exit: {self.exit_qtys[uid]}/{self.entry_qtys.get(uid, 0)})")
                        
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
                    
                    # Execute entry orders for all legs
                    if not od["Quantity"] >= self.params["slices"]:
                        od["Slice_Quantity"] = od["Quantity"]
                    
                    # Update base leg quantities to match
                    for base_leg_key in self.base_leg_keys:
                        od_base_legs[base_leg_key]["Quantity"] = actual_order_qty
                        if not od_base_legs[base_leg_key]["Quantity"] >= self.params["slices"]:
                            od_base_legs[base_leg_key]["Slice_Quantity"] = od_base_legs[base_leg_key]["Quantity"]
                    
                    # Place bidding leg order
                    od = self.order.place_order(od)
                    
                    # Place base leg orders
                    base_leg_orders = []
                    for base_leg_key in self.base_leg_keys:
                        base_leg_order = self.order.place_order(od_base_legs[base_leg_key])
                        base_leg_orders.append(base_leg_order)
                    
                    # Execute IOC for all legs
                    self.order.IOC_order(od, *base_leg_orders)
                    
                    # Update quantities
                    last_key = f"order:{od['user_id']}" + f"{od['remark']}" + f"{od.get('order_id', '')}"
                    last_raw = self.r.get(last_key)
                    if last_raw:
                        lastest_order_data = orjson.loads(last_raw.decode())
                        filled = int(lastest_order_data["response"]["data"]["fQty"])
                        with self.templates_lock:
                            self.entry_qtys[uid] += filled
                            if self.order_templates[uid].get("Quantity", 0) > 0:
                                self.order_templates[uid]["Quantity"] = max(0, self.order_templates[uid]["Quantity"] - filled)
                            print(f"INFO: Dynamic {len(self.base_leg_keys)}-leg SELL Entry order filled for user {uid}: {filled} (total entry: {sum(self.entry_qtys.values())}) (user entry: {self.entry_qtys[uid]}/{desired_total_qty})")
                    
                    return {"uid": uid, "action": "entry"}

                # EXIT for SELL behavior  
                exit_condition_1_sell = spread < self.params["exit_start"] or int(self.params['run_state']) == 2
                exit_condition_2_sell = self.entry_qtys.get(uid, 0) > self.exit_qtys.get(uid, 0)
                
                if exit_condition_1_sell and exit_condition_2_sell:
                    print(f"Dynamic {len(self.base_leg_keys)}-leg SELL Exit!")
                    print(f"INFO: Dynamic {len(self.base_leg_keys)}-leg SELL EXIT TRIGGERED for user {uid}: spread={spread}, exit_start={self.params.get('exit_start')}, run_state={self.params.get('run_state')}, entry_qty={self.entry_qtys.get(uid, 0)}, exit_qty={self.exit_qtys.get(uid, 0)}")
                    
                    # Calculate remaining quantity to exit (should not exceed entry quantity)
                    remaining_exit_qty = self.entry_qtys.get(uid, 0) - self.exit_qtys.get(uid, 0)
                    
                    if remaining_exit_qty > 0:
                        ex["Quantity"] = remaining_exit_qty
                        
                        if not ex["Quantity"] >= self.params["slices"]:
                            ex["Slice_Quantity"] = ex["Quantity"]
                        
                        # Update base leg exit quantities to match
                        for base_leg_key in self.base_leg_keys:
                            ex_base_legs[base_leg_key]["Quantity"] = remaining_exit_qty
                            if not ex_base_legs[base_leg_key]["Quantity"] >= self.params["slices"]:
                                ex_base_legs[base_leg_key]["Slice_Quantity"] = ex_base_legs[base_leg_key]["Quantity"]
                        
                        # Place exit orders for all legs
                        ex = self.order.place_order(ex)
                        
                        # Place base leg exit orders
                        base_leg_exit_orders = []
                        for base_leg_key in self.base_leg_keys:
                            base_leg_exit_order = self.order.place_order(ex_base_legs[base_leg_key])
                            base_leg_exit_orders.append(base_leg_exit_order)
                        
                        # Execute IOC for all exit legs
                        self.order.IOC_order(ex, *base_leg_exit_orders)
                        
                        # Update exit quantities
                        last_key = f"order:{ex['user_id']}" + f"{ex['remark']}" + f"{ex.get('order_id', '')}"
                        last_raw = self.r.get(last_key)
                        if last_raw:
                            lastest_order_data_exit = orjson.loads(last_raw.decode())
                            filled = int(lastest_order_data_exit["response"]["data"]["fQty"])
                            with self.templates_lock:
                                self.exit_qtys[uid] += filled
                                print(f"INFO: Dynamic {len(self.base_leg_keys)}-leg SELL exit order filled for user {uid}: {filled} (total exit: {sum(self.exit_qtys.values())}) (user exit: {self.exit_qtys[uid]}/{self.entry_qtys.get(uid, 0)})")
                        
                        return {"uid": uid, "action": "exit"}
                    else:
                        print(f"INFO: No remaining quantity to exit for user {uid}")
                        return {"uid": uid, "action": "no_exit_needed"}

        except (redis.RedisError, KeyError, IndexError, TypeError, ValueError) as e:
            print(f"ERROR: per-user ({uid}) dynamic {len(self.base_leg_keys) if hasattr(self, 'base_leg_keys') else 'multi'}-leg flow failed: {e}")
        except Exception as e:
            print(traceback.format_exc())
            print(f"ERROR: unexpected error in per-user ({uid}) dynamic {len(self.base_leg_keys) if hasattr(self, 'base_leg_keys') else 'multi'}-leg worker: {e}")
        return {"uid": uid, "error": True}

    
    # --- main logic (kept behavior identical) ----------------------------------
    def main_logic(self):
        # run until both conditions are met: total entry == total exit AND run_state == 2
        while True:
            try:
                if int(self.params['run_state']) == 1:
                    continue # pause
                
                # Get bid/ask direction
                bid_or_ask = "bidValues" if self.params["action"].upper() == "SELL" else "askValues"
                bid_ask_exit = "askValues" if self.params["action"].upper() == "SELL" else "bidValues"

                # Get current prices for all legs
                leg_prices = self._get_leg_prices(bid_or_ask)
                leg_prices_exit = self._get_leg_prices(bid_ask_exit)
                
                # Validate that we have valid price data before proceeding
                base_leg_sum = sum(leg_prices.get(key, 0) for key in self.base_leg_keys)
                if base_leg_sum <= 0:
                    print(f"ERROR: Invalid base leg prices: {leg_prices}")
                    time.sleep(0.1)
                    continue

                # Calculate spread as sum of all legs (supports dynamic number of legs)
                total_price_sum = sum(leg_prices.values())
                spread = round(abs(total_price_sum) * 20) / 20
                
                # Enhanced debug logging for dynamic legs
                base_legs_debug = {k: leg_prices.get(k, 0) for k in self.base_leg_keys}
                print(f"DEBUG: Dynamic {len(self.base_leg_keys)}-leg spread: {spread}")
                print(f"DEBUG: Base legs prices: {base_legs_debug}")
                print(f"DEBUG: Bidding leg ({self.bidding_leg_key}): {leg_prices.get(self.bidding_leg_key, 0)}")

                # If per-user templates are configured, run per-user logic in parallel
                if getattr(self, "uids", None):
                    max_workers = min(8, max(1, len(self.uids)))
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = {
                            executor.submit(
                                self._process_user,
                                uid,
                                spread,
                                leg_prices,
                                leg_prices_exit,
                            ): uid
                            for uid in self.uids
                        }
                        for fut in as_completed(futures):
                            try:
                                res = fut.result()
                                print(f"INFO: Dynamic {len(self.base_leg_keys)}-leg per-user result: {res}")
                            except Exception as e:
                                print(f"ERROR: Dynamic {len(self.base_leg_keys)}-leg per-user task failed: {e}")
                    
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
                        print(f"INFO: Dynamic {len(self.base_leg_keys)}-leg Quantities - Entry: {total_entry}, Exit: {total_exit}, Run State: {run_state_val}")
                    
                    if total_entry == total_exit and run_state_val == 2:
                        print(f"INFO: Dynamic {len(self.base_leg_keys)}-leg Exit condition met: total_entry={total_entry}, total_exit={total_exit}, run_state={run_state_val}")
                        break
                    
                    continue

                # No single-template fallback: per-user tasks handled above.
                
            except redis.RedisError as e:
                print(f"ERROR: redis error in dynamic {len(self.base_leg_keys) if hasattr(self, 'base_leg_keys') else 'multi'}-leg main loop: {e}")
                time.sleep(0.5)
                continue
            except (KeyError, IndexError, TypeError, AttributeError, ValueError, orjson.JSONDecodeError) as e:
                # expected parsing/access errors - log and continue polling
                print(f"WARNING: transient data error in dynamic {len(self.base_leg_keys) if hasattr(self, 'base_leg_keys') else 'multi'}-leg main loop: {e}")
                time.sleep(0.1)
                continue
            except Exception as e:
                print(f"ERROR: unexpected error in dynamic {len(self.base_leg_keys) if hasattr(self, 'base_leg_keys') else 'multi'}-leg main loop: {e}")
                print(traceback.format_exc())
                time.sleep(0.1)
                continue
            except KeyboardInterrupt:
                print("INFO: KeyboardInterrupt received, exiting dynamic multi-leg main loop")
                os._exit(0)
