import os
from APIConnect.APIConnect import APIConnect
import redis
import json
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
        self.lot_sizes = json.loads(self.r.get("lotsizes"))
        users = self.r.keys("user:*")
        data = [json.loads(self.r.get(user)) for user in users]
        self.user_obj_dict = {}
        
        for item in data:
            if self.r.exists(f"reqid:{item.get('userid')}"):
                self.user_obj_dict[item.get("userid")] = APIConnect(item.get("apikey"), "", "", False, "", False)

        self.order = Orders(self.user_obj_dict)
        # lock used when updating shared per-user templates/qtys from worker threads
        self.templates_lock = threading.Lock()
        
        # Initialize fixed thread pool executor with 2 workers
        self.executor = ThreadPoolExecutor(max_workers=2)

        # load params and basic state (updated to match API format)
        self.params_key = f"4_leg:{paramsid}"
        raw_params = self.r.get(self.params_key)
        if raw_params is None:
            raise RuntimeError(f"params key missing in redis: {self.params_key}")
        self.params = orjson.loads(raw_params.decode())
        self.global_action = self.params.get('action', 'BUY').upper()
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
            except Exception as e:
                print(f"ERROR: failed to update live params: {e}")
                time.sleep(1)

    # --- initialization helpers -------------------------------------------------
    def _depth_from_redis(self, streaming_symbol: str):
        """Load depth JSON from redis and return parsed object."""
        try:
            raw = self.r.get(streaming_symbol)
            if raw is None:
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

    def _create_depth_key(self, leg_info):
        """Create Redis depth key from leg info, handling both numeric and date expiry formats."""
        expiry_value = leg_info['expiry']
        strike = leg_info['strike']
        
        # Ensure strike is formatted correctly (remove any existing decimal if it's a whole number)
        if isinstance(strike, (int, float)):
            strike_str = f"{strike:.1f}" if strike % 1 != 0 else f"{int(strike)}.0"
        else:
            # If strike is already a string, use it as-is but ensure .0 format for whole numbers
            strike_str = str(strike)
            if '.' not in strike_str:
                strike_str += '.0'
        
        if isinstance(expiry_value, (int, str)) and str(expiry_value).isdigit():
            # Numeric expiry: use as-is
            return f"depth:{leg_info['symbol'].upper()}_{strike_str}_{leg_info['type']}-{expiry_value}"
        else:
            # Legacy date format: keep existing behavior
            return f"depth:{leg_info['symbol'].upper()}_{strike_str}_{leg_info['type']}-{leg_info['expiry']}"

    def _load_leg_data(self, leg_key, leg_info):
        """Load and validate leg data from Redis."""
        depth_key = self._create_depth_key(leg_info)
        leg_data = self._depth_from_redis(depth_key)
        
        if leg_data is None:
            print(f"ERROR: {leg_key} depth not found: {depth_key}")
            raise RuntimeError(f"{leg_key} depth missing in redis")
        
        return {
            'data': leg_data,
            'info': leg_info,
            'depth_key': depth_key
        }

    def _calculate_action_based_price_sum(self, leg_keys, leg_prices, debug_label=""):
        """Calculate price sum considering BUY/SELL actions for each leg, weighted by quantity/lot_size.
        
        This method is used ONLY for spread calculation, not for order placement.
        The weighted pricing ensures accurate spread calculation when legs have different quantities.
        """
        total_sum = 0
        debug_parts = []
        lot_size = self._get_lot_size()
        
        for leg_key in leg_keys:
            leg_action = self.legs[leg_key]['info'].get('action', self.global_action).upper()
            leg_price = leg_prices.get(leg_key, 0)
            leg_quantity = self.legs[leg_key]['info'].get('quantity', self.params.get("bidding_leg", {}).get("quantity", 75))
            
            # Calculate weighted price: (price * quantity) / lot_size
            weighted_price = (leg_price * leg_quantity) / lot_size
            
            if leg_action == "BUY":
                total_sum += weighted_price
                debug_parts.append(f"+{weighted_price:.2f}({leg_key}:BUY:{leg_price}*{leg_quantity}/{lot_size})")
            else:  # SELL
                total_sum -= weighted_price
                debug_parts.append(f"-{weighted_price:.2f}({leg_key}:SELL:{leg_price}*{leg_quantity}/{lot_size})")
        
        if debug_label:
            pass
        
        return total_sum, debug_parts

    def _calculate_simple_price_sum(self, leg_keys, leg_prices, debug_label=""):
        """Calculate simple price sum considering BUY/SELL actions for each leg (for order placement).
        
        This method is used for calculating order prices. It uses actual market prices
        without quantity weighting since orders are placed at market prices.
        """
        total_sum = 0
        debug_parts = []
        
        for leg_key in leg_keys:
            leg_action = self.legs[leg_key]['info'].get('action', self.global_action).upper()
            leg_price = leg_prices.get(leg_key, 0)
            
            if leg_action == "BUY":
                total_sum += leg_price
                debug_parts.append(f"+{leg_price:.2f}({leg_key}:BUY)")
            else:  # SELL
                total_sum -= leg_price
                debug_parts.append(f"-{leg_price:.2f}({leg_key}:SELL)")
        
        if debug_label:
            pass
        
        return total_sum, debug_parts

    def _calculate_exit_price_with_gap(self, base_price, action, exit_price_gap):
        """Calculate exit price with gap adjustment, ensuring minimum price of 0.05."""
        if action.upper() == "BUY":
            exit_price = base_price + exit_price_gap
        else:  # SELL
            exit_price = base_price - exit_price_gap
        
        # Ensure minimum price of 0.05
        return max(exit_price, 0.05)

    def _format_limit_price(self, price):
        """Format price to ensure it's never negative and rounded properly."""
        return str(round(max(0.05, abs(price)) * 20) / 20)

    def _get_lot_size(self):
        """Get the lot size for the trading symbol. Default to 75 for NIFTY.
        
        Priority:
        1. Explicit lot_size in params
        2. Symbol-based default lot sizes
        3. Fallback to 75
        """
        # Check if lot_size is explicitly provided in params
        # if 'lot_size' in self.params:
        #     return int(self.params['lot_size'])
        
        symbol = None
        if hasattr(self, 'legs') and self.legs:
            # Get symbol from first available leg
            first_leg_key = next(iter(self.legs.keys()))
            symbol = self.legs[first_leg_key]['info'].get('symbol', 'NIFTY').upper()
        
        # Default lot sizes for different symbols
        lot_sizes = {
            'NIFTY': 75,
            'BANKNIFTY': 15,
            'FINNIFTY': 40,
            'MIDCPNIFTY': 75,
            'SENSEX': 20,
            'BANKEX': 15
        }

        return self.lot_sizes.get(symbol, 75)

    def _calculate_slice_quantity(self, total_quantity, slice_multiplier):
        """Calculate slice quantity based on slice multiplier.
        
        Example: 
        - Total quantity = 300 (after quantity_multiplier applied)
        - slice_multiplier = 2 (divide orders into 2 slices)
        - slice_quantity = 300 / 2 = 150
        - This will create: 2 orders of 150 qty each
        """
        if slice_multiplier <= 0:
            slice_multiplier = 1
        
        # Divide total quantity by slice multiplier
        slice_quantity = total_quantity / slice_multiplier
        
        # If slice quantity is 0, use at least 1 unit
        if slice_quantity <= 0:
            slice_quantity = total_quantity
        
        return slice_quantity

    def _adjust_quantity_for_slicing(self, order_template, slice_multiplier):
        """Adjust slice quantity based on lot size and slice multiplier."""
        total_quantity = order_template["Quantity"]
        slice_quantity = self._calculate_slice_quantity(total_quantity, slice_multiplier)
        order_template["Slice_Quantity"] = slice_quantity

    def _update_base_leg_quantities(self, base_leg_templates, remaining_qty, slice_multiplier):
        """Update quantities for all base leg templates."""
        for base_leg_key in self.base_leg_keys:
            original_qty = base_leg_templates[base_leg_key]["Quantity"]
            actual_qty = min(original_qty, remaining_qty)
            base_leg_templates[base_leg_key]["Quantity"] = actual_qty
            self._adjust_quantity_for_slicing(base_leg_templates[base_leg_key], slice_multiplier)

    def _place_all_orders(self, main_order, base_leg_orders_dict):
        """Place orders for all legs and execute IOC."""
        
        # Get slice multiplier to handle multiple order placements
        slice_multiplier = self.params.get("slice_multiplier", 1)
        
        if slice_multiplier <= 1:
            # Single order placement (default behavior)
            main_order = self.order.place_order(main_order)
            self.order.IOC_order(main_order, *base_leg_orders_dict.values())
            # self.order._place_base_leg_orders_basket(base_leg_orders_dict.values(),75,"70249886")
            return main_order
        else:
            # Multiple order placement based on slice multiplier
            slice_quantity = main_order.get("Slice_Quantity", main_order["Quantity"])
            total_quantity = main_order["Quantity"]
            
            placed_orders = []
            remaining_quantity = total_quantity
            
            # Place orders in slices
            while remaining_quantity > 0 and len(placed_orders) < slice_multiplier:
                # Calculate quantity for this slice
                current_slice_qty = min(slice_quantity, remaining_quantity)
                
                # Create order copies for this slice
                slice_main_order = main_order.copy()
                slice_main_order["Quantity"] = current_slice_qty
                
                slice_base_orders = {}
                for leg_key, base_order in base_leg_orders_dict.items():
                    slice_base_orders[leg_key] = base_order.copy()
                    # Calculate proportional quantity for base leg
                    base_slice_qty = int((base_order["Quantity"] * current_slice_qty) / total_quantity)
                    slice_base_orders[leg_key]["Quantity"] = base_slice_qty
                
                # Place this slice
                placed_main_order = self.order.place_order(slice_main_order)
                self.order.IOC_order(placed_main_order, *slice_base_orders.values())
                placed_orders.append(placed_main_order)
                
                remaining_quantity -= current_slice_qty
            
            # Return the first placed order for backward compatibility
            return placed_orders[0] if placed_orders else main_order

    def _update_filled_quantities(self, order, uid, is_entry=True):
        """Update filled quantities and templates after order execution."""
        last_key = f"order:{order['user_id']}" + f"{order['remark']}" + f"{order.get('order_id', '')}"
        last_raw = self.r.get(last_key)
        if last_raw:
            order_data = orjson.loads(last_raw.decode())
            filled = int(order_data["response"]["data"]["fQty"])
            
            with self.templates_lock:
                if is_entry:
                    self.entry_qtys[uid] += filled
                    if self.order_templates[uid].get("Quantity", 0) > 0:
                        self.order_templates[uid]["Quantity"] = max(0, self.order_templates[uid]["Quantity"] - filled)
                else:
                    self.exit_qtys[uid] += filled
            
            return filled
        return 0

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
            self.legs[leg_key] = self._load_leg_data(leg_key, leg_info)
        
        # Load bidding leg dynamically
        bidding_leg_key = self.params.get("bidding_leg_key", "bidding_leg")
        self.bidding_leg_key = bidding_leg_key
        
        bidding_leg_info = self.params.get(bidding_leg_key)
        if not bidding_leg_info:
            raise RuntimeError(f"Missing bidding leg info for {bidding_leg_key}")
        
        self.legs[bidding_leg_key] = self._load_leg_data(bidding_leg_key, bidding_leg_info)

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

        # Global action for backward compatibility
        self.global_action = self.params.get('action', 'BUY').upper()

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
        
        self.completed_straddles = {uid: False for uid in uids}
        self.completed_exits = {uid: False for uid in uids}

        # Create templates for each user
        # Note: Global action still used for backward compatibility, but individual leg actions take precedence
        for uid in uids:
            # Initialize dictionaries for this user
            self.order_templates[uid] = {}
            self.base_leg_templates[uid] = {}
            self.exit_order_templates[uid] = {}
            self.exit_base_leg_templates[uid] = {}
            
            # Bidding leg templates (main trading leg)
            bidding_leg_data = self.legs[self.bidding_leg_key]['data']
            bidding_leg_info = self.legs[self.bidding_leg_key]['info']
            bidding_leg_action = bidding_leg_info.get('action', self.global_action).upper()
            
            self.order_templates[uid] = self._make_order_template(
                bidding_leg_data, 
                buy_if=bidding_leg_action, 
                user_id=uid,
                leg_key=self.bidding_leg_key
            )
            
            self.exit_order_templates[uid] = self._make_order_template(
                bidding_leg_data, 
                buy_if=("SELL" if bidding_leg_action == "BUY" else "BUY"), 
                quantity=0, 
                user_id=uid,
                leg_key=self.bidding_leg_key
            )
            
            # Base legs templates
            for base_leg_key in self.base_leg_keys:
                base_leg_data = self.legs[base_leg_key]['data']
                base_leg_info = self.legs[base_leg_key]['info']
                base_leg_action = base_leg_info.get('action', self.global_action).upper()
                
                self.base_leg_templates[uid][base_leg_key] = self._make_order_template(
                    base_leg_data, 
                    buy_if=base_leg_action, 
                    user_id=uid,
                    leg_key=base_leg_key
                )
                
                self.exit_base_leg_templates[uid][base_leg_key] = self._make_order_template(
                    base_leg_data, 
                    buy_if=("SELL" if base_leg_action == "BUY" else "BUY"), 
                    quantity=0, 
                    user_id=uid,
                    leg_key=base_leg_key
                )

        # keep single-template attributes for backward compatibility (use first user if present)
        first_uid = uids[0] if uids else None
        if first_uid is not None:
            self.order_details = self.order_templates[first_uid]
            self.base_leg_details = self.base_leg_templates[first_uid]
            self.exit_order_details = self.exit_order_templates[first_uid]
            self.exit_base_leg_details = self.exit_base_leg_templates[first_uid]

    def _make_order_template(self, leg_obj, buy_if="BUY", quantity=None, user_id=None, leg_key=None):
        """Return a dict template for orders built from a depth/leg object.

        - buy_if: the action string to use as 'BUY' for entry templates, 'SELL' for exit templates.
        - quantity: optional override for Quantity.
        - leg_key: the key of the leg (e.g., 'leg1', 'bidding_leg') to get quantity from params
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

        # Get quantity from leg-specific data or use override
        if quantity is not None:
            qty = quantity
        elif leg_key and leg_key in self.params and 'quantity' in self.params[leg_key]:
            qty = int(self.params[leg_key]['quantity'])
        else:
            # Fallback to bidding_leg quantity if available
            qty = int(self.params.get("bidding_leg", {}).get("quantity", 75))

        # Apply quantity multiplier (e.g., 2x means 75 -> 150, 150 -> 300)
        quantity_multiplier = self.params.get("quantity_multiplier", 1)
        qty = qty * int(quantity_multiplier)

        # Calculate slices based on slice_multiplier and lot size
        slice_multiplier = self.params.get("slice_multiplier", 1)
        slice_quantity = self._calculate_slice_quantity(qty, slice_multiplier)
       
        return {
            "user_id": user_id,
            "Trading_Symbol": trading_symbol,
            "Exchange": self.exchange,
            "Action": action,
            "Order_Type": order_type,
            "Quantity": qty,
            "Slice_Quantity": slice_quantity,
            "Streaming_Symbol": streaming_symbol,
            "Limit_Price": "0",
            "Disclosed_Quantity": 0,
            "TriggerPrice": 0,
            "ProductCode": ProductCodeENum.NRML,
            "remark": self.params.get("notes", "Lord_Shreeji"),
            "IOC": self.params['IOC_timeout'],
            "exit_price_gap": float(self.params.get('exit_price_gap', 0))
        }

    # --- runtime helpers --------------------------------------------------------
    def _avg_price(self, data, side_key, n):
        # compute average of the first n bid/ask prices
        if data is None:
            return 0.0
        
        try:
            entries = data["response"]["data"][side_key]
            n = int(n)
            if n <= 1 or len(entries) == 0:
                return float(entries[0]["price"]) if entries else 0.0
            s = 0.0
            count = min(n, len(entries))
            for i in range(0,count):
                s += float(entries[i]["price"])
            return s / count
        except (KeyError, IndexError, TypeError, ValueError) as e:
            print(f"ERROR: _avg_price failed for side {side_key}: {e}")
            return 0.0

    def _depth_price(self, data, side_key, depth_index):
        # get the price at specific depth index (1-based)
        if data is None:
            return 0.0
        
        try:
            entries = data["response"]["data"][side_key]
            depth_index = int(depth_index)
            if depth_index <= 0 or len(entries) == 0:
                return float(entries[0]["price"]) if entries else 0.0
            
            # Convert to 0-based index
            index = depth_index - 1
            if index >= len(entries):
                # If requested depth is not available, use the last available entry
                index = len(entries) - 1
            
            return float(entries[index]["price"])
        except (KeyError, IndexError, TypeError, ValueError) as e:
            print(f"ERROR: _depth_price failed for side {side_key} at depth {depth_index}: {e}")
            return 0.0

    def _safe_get_price(self, data, side_key):
        """Safe price extraction with proper None checking - uses pricing method from params"""
        try:
            if data and data.get("response", {}).get("data", {}).get(side_key):
                pricing_method = self.params.get("pricing_method", "average")
                
                if pricing_method == "depth":
                    depth_index = self.params.get("depth_index", 3)
                    return self._depth_price(data, side_key, depth_index)
                else:
                    # Default to average method using no_of_bidask_average
                    no_of_average = self.params.get("no_of_bidask_average", 1)
                    if no_of_average > 1:
                        return self._avg_price(data, side_key, no_of_average)
                    else:
                        return float(data["response"]["data"][side_key][0]["price"])
            return 0.0
        except (KeyError, IndexError, TypeError, ValueError):
            return 0.0

    def _get_leg_prices_with_actions(self, is_exit=False):
        """Get current prices for all legs based on individual leg actions.
        
        For entry: BUY legs use askValues, SELL legs use bidValues
        For exit: Opposite of entry (BUY legs use bidValues, SELL legs use askValues)
        """
        prices = {}
        direction_debug = []
        
        # Get base leg prices dynamically based on their individual actions
        for base_leg_key in self.base_leg_keys:
            try:
                leg_data = self._depth_from_redis(self.legs[base_leg_key]['depth_key'])
                leg_action = self.legs[base_leg_key]['info'].get('action', self.global_action).upper()
                
                # Determine bid_or_ask based on leg action and entry/exit
                if is_exit:
                    # For exit: opposite of entry direction
                    bid_or_ask = "bidValues" if leg_action == "BUY" else "askValues"
                else:
                    # For entry: BUY uses ask, SELL uses bid
                    bid_or_ask = "askValues" if leg_action == "BUY" else "bidValues"
                
                direction_debug.append(f"{base_leg_key}({leg_action}:{bid_or_ask[:3]})")
                
                pricing_method = self.params.get("pricing_method", "average")
                
                if pricing_method == "depth":
                    depth_index = self.params.get("depth_index", 3)
                    prices[base_leg_key] = self._depth_price(leg_data, bid_or_ask, depth_index)
                elif self.params.get("no_of_bidask_average", 1) > 1:
                    prices[base_leg_key] = self._avg_price(leg_data, bid_or_ask, self.params["no_of_bidask_average"])
                else:
                    prices[base_leg_key] = self._safe_get_price(leg_data, bid_or_ask)
            except (KeyError, TypeError) as e:
                print(f"ERROR: Failed to get price for {base_leg_key}: {e}")
                prices[base_leg_key] = 0.0
        
        # Get bidding leg price based on its individual action
        try:
            bidding_leg_data = self._depth_from_redis(self.legs[self.bidding_leg_key]['depth_key'])
            bidding_leg_action = self.legs[self.bidding_leg_key]['info'].get('action', self.global_action).upper()
            
            # Determine bid_or_ask based on bidding leg action and entry/exit
            if is_exit:
                # For exit: opposite of entry direction
                bid_or_ask = "bidValues" if bidding_leg_action == "BUY" else "askValues"
            else:
                # For entry: BUY uses ask, SELL uses bid
                bid_or_ask = "askValues" if bidding_leg_action == "BUY" else "bidValues"
            
            direction_debug.append(f"{self.bidding_leg_key}({bidding_leg_action}:{bid_or_ask[:3]})")
            
            pricing_method = self.params.get("pricing_method", "average")
            
            if pricing_method == "depth":
                # depth_index = self.params.get("depth_index", 3)
                depth_index = 1
                prices[self.bidding_leg_key] = self._depth_price(bidding_leg_data, bid_or_ask, depth_index)
            elif self.params.get("no_of_bidask_average", 1) > 1:
                prices[self.bidding_leg_key] = self._avg_price(bidding_leg_data, bid_or_ask, self.params["no_of_bidask_average"])
            else:
                prices[self.bidding_leg_key] = self._safe_get_price(bidding_leg_data, bid_or_ask)
        except (KeyError, TypeError) as e:
            print(f"ERROR: Failed to get price for bidding leg {self.bidding_leg_key}: {e}")
            prices[self.bidding_leg_key] = 0.0
        
        # Debug log showing direction for each leg
        context = "EXIT" if is_exit else "ENTRY"
        
        return prices

    def _get_leg_prices(self, bid_or_ask):
        """Get current prices for all legs (supports dynamic number of legs)"""
        prices = {}
        
        # Get base leg prices dynamically
        for base_leg_key in self.base_leg_keys:
            try:
                leg_data = self._depth_from_redis(self.legs[base_leg_key]['depth_key'])
                pricing_method = self.params.get("pricing_method", "average")
                
                if pricing_method == "depth":
                    depth_index = self.params.get("depth_index", 3)
                    prices[base_leg_key] = self._depth_price(leg_data, bid_or_ask, depth_index)
                elif self.params.get("no_of_bidask_average", 1) > 1:
                    prices[base_leg_key] = self._avg_price(leg_data, bid_or_ask, self.params["no_of_bidask_average"])
                else:
                    prices[base_leg_key] = self._safe_get_price(leg_data, bid_or_ask)
            except (KeyError, TypeError) as e:
                print(f"ERROR: Failed to get price for {base_leg_key}: {e}")
                prices[base_leg_key] = 0.0
        
        # Get bidding leg price (for display/validation purposes)
        try:
            bidding_leg_data = self._depth_from_redis(self.legs[self.bidding_leg_key]['depth_key'])
            pricing_method = self.params.get("pricing_method", "average")
            
            if pricing_method == "depth":
                depth_index = self.params.get("depth_index", 3)
                prices[self.bidding_leg_key] = self._depth_price(bidding_leg_data, bid_or_ask, depth_index)
            elif self.params.get("no_of_bidask_average", 1) > 1:
                prices[self.bidding_leg_key] = self._avg_price(bidding_leg_data, bid_or_ask, self.params["no_of_bidask_average"])
            else:
                prices[self.bidding_leg_key] = self._safe_get_price(bidding_leg_data, bid_or_ask)
        except (KeyError, TypeError) as e:
            print(f"ERROR: Failed to get price for bidding leg {self.bidding_leg_key}: {e}")
            prices[self.bidding_leg_key] = 0.0
        
        return prices

    def _execute_entry_orders(self, uid, spread, od, od_base_legs, remaining_qty, is_buy_action):
        """Execute entry orders for all legs if conditions are met."""
        start_price = self.params["start_price"]
        run_state = int(self.params['run_state'])
        
        # Check entry conditions based on action type
        price_condition = (spread < start_price) if is_buy_action else (spread > start_price)
        if (price_condition and od["Quantity"] > 0 and run_state == 0 and remaining_qty > 0):
            # Only place order for remaining quantity
            actual_order_qty = min(od["Quantity"], remaining_qty)
            od["Quantity"] = actual_order_qty
            
            # Execute entry orders for all legs
            slice_multiplier = self.params.get("slice_multiplier", 1)
            self._adjust_quantity_for_slicing(od, slice_multiplier)
            self._update_base_leg_quantities(od_base_legs, remaining_qty, slice_multiplier)
            
            od = self._place_all_orders(od, od_base_legs)
            
            # Update quantities
            desired_total_qty = int(self.params.get("bidding_leg", {}).get("quantity", 75))
            self._update_filled_quantities(od, uid, is_entry=True)
            
            return {"uid": uid, "action": "entry"}
        
        return None

    def _execute_exit_orders(self, uid, exit_spread, ex, ex_base_legs, is_buy_action):
        """Execute exit orders for all legs if conditions are met."""
        exit_start = self.params["exit_start"]
        run_state = int(self.params['run_state'])
        
        # Check exit conditions based on action type using exit_spread
        exit_condition_1 = (exit_spread > exit_start) if is_buy_action else (exit_spread < exit_start)
        exit_condition_1 = exit_condition_1 or (run_state == 2)
        exit_condition_2 = self.entry_qtys.get(uid, 0) > self.exit_qtys.get(uid, 0)
        
        if exit_condition_1 and exit_condition_2:
            action_type = "BUY" if is_buy_action else "SELL"
            
            # Calculate remaining quantity to exit
            remaining_exit_qty = self.entry_qtys.get(uid, 0) - self.exit_qtys.get(uid, 0)
            
            if remaining_exit_qty > 0:
                ex["Quantity"] = remaining_exit_qty
                
                slice_multiplier = self.params.get("slice_multiplier", 1)
                self._adjust_quantity_for_slicing(ex, slice_multiplier)
                
                # Update base leg exit quantities to match
                for base_leg_key in self.base_leg_keys:
                    ex_base_legs[base_leg_key]["Quantity"] = remaining_exit_qty
                    self._adjust_quantity_for_slicing(ex_base_legs[base_leg_key], slice_multiplier)
                
                # Place all exit orders
                ex = self._place_all_orders(ex, ex_base_legs)
                
                # Update exit quantities
                self._update_filled_quantities(ex, uid, is_entry=False)
                
                return {"uid": uid, "action": "exit"}
            else:
                return {"uid": uid, "action": "no_exit_needed"}
        
        return None

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

            # Calculate prices considering BUY/SELL actions for each leg
            desired_spread = self.params.get("desired_spread", 0)
            exit_desired_spread = self.params.get("exit_desired_spread", 0)
            
            # Entry prices - calculate base legs sum using SIMPLE pricing (for order placement)
            base_legs_price_sum, base_legs_price_debug = self._calculate_simple_price_sum(
                self.base_leg_keys, leg_prices, f"Base legs order price calculation for {uid}")
            
            # Calculate bidding leg price based on its action
            bidding_leg_action = self.legs[self.bidding_leg_key]['info'].get('action', self.global_action).upper()
            if bidding_leg_action == "BUY":
                bidding_leg_entry_price = desired_spread - abs(base_legs_price_sum)
            else:  # SELL
                bidding_leg_entry_price = desired_spread - abs(base_legs_price_sum)
                
            
            lots = self.legs[self.bidding_leg_key]['info'].get('quantity', 0) / self.lot_sizes.get(self.params.get("bidding_leg", {}).get("symbol", "NIFTY"), 75)
            bidding_leg_entry_price = bidding_leg_entry_price / lots
           
          
            # Exit prices - calculate base legs exit sum using SIMPLE pricing (for order placement)
            
            base_legs_exit_price_sum, _ = self._calculate_simple_price_sum(
                self.base_leg_keys, leg_prices_exit)
            
            od["Limit_Price"] = self._format_limit_price(bidding_leg_entry_price)
           
            # Set base leg prices using ACTUAL leg prices (not weighted)
            for base_leg_key in self.base_leg_keys:
                od_base_legs[base_leg_key]["Limit_Price"] = self._format_limit_price(leg_prices.get(base_leg_key, 0))
            
            # Handle exit pricing
            if int(self.params['run_state']) == 2:
                # Market exit with price gap adjustment
                exit_price_gap = float(self.params.get('exit_price_gap', 0))
                action = self.params.get("action", "").upper()
                
                # Bidding leg exit price
                bidding_exit_price = self._calculate_exit_price_with_gap(
                    leg_prices_exit.get(self.bidding_leg_key, 0), action, exit_price_gap)
                ex['Limit_Price'] = self._format_limit_price(bidding_exit_price/lots)
                
                # Base legs exit prices
                for base_leg_key in self.base_leg_keys:
                    base_exit_price = self._calculate_exit_price_with_gap(
                        leg_prices_exit.get(base_leg_key, 0), action, exit_price_gap)
                    ex_base_legs[base_leg_key]['Limit_Price'] = self._format_limit_price(base_exit_price)
            else:
                # Normal exit using exit_desired_spread
                # For exit: bidding_leg_price = exit_desired_spread - base_legs_exit_price_sum (for both BUY and SELL)
                bidding_leg_exit_price = exit_desired_spread - base_legs_exit_price_sum
                ex["Limit_Price"] = self._format_limit_price(bidding_leg_exit_price/lots)
                for base_leg_key in self.base_leg_keys:
                    ex_base_legs[base_leg_key]["Limit_Price"] = self._format_limit_price(leg_prices_exit.get(base_leg_key, 0))

            # Calculate exit spread using exit prices and exit logic
            # Calculate exit spread considering BUY/SELL actions for each leg, weighted by quantity/lot_size
            lot_size = self._get_lot_size()
            
            # Process bidding leg with quantity weighting for exit spread
            bidding_leg_exit_price_for_spread = leg_prices_exit.get(self.bidding_leg_key, 0)
            bidding_leg_quantity = self.legs[self.bidding_leg_key]['info'].get('quantity', self.params.get("bidding_leg", {}).get("quantity", 75))
            
            # Calculate weighted bidding leg exit price: (price * quantity) / lot_size
            weighted_bidding_exit_price = (bidding_leg_exit_price_for_spread * bidding_leg_quantity) / lot_size
            bidding_exit_spread_part = weighted_bidding_exit_price if bidding_leg_action == "BUY" else -weighted_bidding_exit_price
            
            # Process base legs for exit spread (uses WEIGHTED pricing for spread calculation)
            base_legs_exit_sum, _ = self._calculate_action_based_price_sum(self.base_leg_keys, leg_prices_exit)
            
            total_exit_price_sum = bidding_exit_spread_part + base_legs_exit_sum
            exit_spread = abs(round(total_exit_price_sum * 20) / 20)

            # ENTRY/EXIT LOGIC
            if self.params["action"].upper() == "BUY":
                # Check if user still has quantity to fill
                current_entry_qty = self.entry_qtys.get(uid, 0)
                bidding_leg_qty = int(self.params.get("bidding_leg", {}).get("quantity", 75))
                remaining_qty = (bidding_leg_qty - current_entry_qty) * self.params.get("quantity_multiplier", 1)
                
                # Try entry orders
                entry_result = self._execute_entry_orders(uid, spread, od, od_base_legs, remaining_qty, is_buy_action=True)
                if entry_result:
                    return entry_result

                # Try exit orders
                exit_result = self._execute_exit_orders(uid, exit_spread, ex, ex_base_legs, is_buy_action=True)
                if exit_result:
                    return exit_result

            else:  # SELL behaviour
                # Check if user still has quantity to fill
                current_entry_qty = self.entry_qtys.get(uid, 0)
                bidding_leg_qty = int(self.params.get("bidding_leg", {}).get("quantity", 75))
                remaining_qty = (bidding_leg_qty - current_entry_qty) * self.params.get("quantity_multiplier", 1)
                
                # Try entry orders
                entry_result = self._execute_entry_orders(uid, spread, od, od_base_legs, remaining_qty, is_buy_action=False)
                if entry_result:
                    return entry_result

                # Try exit orders
                exit_result = self._execute_exit_orders(uid, exit_spread, ex, ex_base_legs, is_buy_action=False)
                if exit_result:
                    return exit_result

        except (redis.RedisError, KeyError, IndexError, TypeError, ValueError) as e:
            print(f"ERROR: per-user ({uid}) dynamic {len(self.base_leg_keys) if hasattr(self, 'base_leg_keys') else 'multi'}-leg flow failed: {e}")
        except Exception as e:
            print(traceback.format_exc())
            print(f"ERROR: unexpected error in per-user ({uid}) dynamic {len(self.base_leg_keys) if hasattr(self, 'base_leg_keys') else 'multi'}-leg worker: {e}")
        return {"uid": uid, "error": True}

    def _safe_get_total_quantities(self):
        """Safely get total entry and exit quantities with error handling."""
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
            
        return total_entry, total_exit, run_state_val

    
    # --- main logic (kept behavior identical) ----------------------------------
    def main_logic(self):
        # run until both conditions are met: total entry == total exit AND run_state == 2
        while True:
            # t1 = time.time()
            try:
                if int(self.params['run_state']) == 1:
                    continue # pause
                
                # Get current prices for all legs based on individual leg actions
                leg_prices = self._get_leg_prices_with_actions()
                leg_prices_exit = self._get_leg_prices_with_actions(is_exit=True)
                
                # Validate that we have valid price data before proceeding
                base_leg_sum = sum(leg_prices.get(key, 0) for key in self.base_leg_keys)
                if base_leg_sum <= 0:
                    print(f"ERROR: Invalid base leg prices: {leg_prices}")
                    time.sleep(0.1)
                    continue

                # Calculate spread considering BUY/SELL actions for each leg, weighted by quantity/lot_size
                # NOTE: This uses WEIGHTED pricing for accurate spread calculation
                # BUY legs are added, SELL legs are subtracted
                lot_size = self._get_lot_size()
                
                # Process bidding leg with quantity weighting
                bidding_leg_action = self.legs[self.bidding_leg_key]['info'].get('action', self.global_action).upper()
                bidding_leg_price = leg_prices.get(self.bidding_leg_key, 0)
                bidding_leg_quantity = self.legs[self.bidding_leg_key]['info'].get('quantity', self.params.get("bidding_leg", {}).get("quantity", 75))
                
                # Calculate weighted bidding leg price: (price * quantity) / lot_size
                weighted_bidding_price = (bidding_leg_price * bidding_leg_quantity) / lot_size
                bidding_spread_part = weighted_bidding_price if bidding_leg_action == "BUY" else -weighted_bidding_price
                bidding_debug = f"+{weighted_bidding_price:.2f}({self.bidding_leg_key}:BUY:{bidding_leg_price}*{bidding_leg_quantity}/{lot_size})" if bidding_leg_action == "BUY" else f"-{weighted_bidding_price:.2f}({self.bidding_leg_key}:SELL:{bidding_leg_price}*{bidding_leg_quantity}/{lot_size})"
                
                # Process base legs (uses WEIGHTED pricing for spread calculation)
                base_legs_sum, base_legs_debug = self._calculate_action_based_price_sum(self.base_leg_keys, leg_prices)
                
                total_price_sum = bidding_spread_part + base_legs_sum
                spread = abs(round(total_price_sum * 20) / 20)  # Remove abs() to preserve sign

                # Enhanced debug logging showing the complete calculation with quantities
                all_debug_parts = [bidding_debug] + base_legs_debug
                calculation_str = " ".join(all_debug_parts)
                print("Calculation spread : ", spread," : " ,calculation_str,end="  \r\r")

                # If per-user templates are configured, run per-user logic in parallel
                if getattr(self, "uids", None):
                    futures = {
                        self.executor.submit(
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
                        except Exception as e:
                            print(f"ERROR: Dynamic {len(self.base_leg_keys)}-leg per-user task failed: {e}")
                    
                    # Check exit condition only after processing all users
                    total_entry, total_exit, run_state_val = self._safe_get_total_quantities()
                    
                    if total_entry == total_exit and run_state_val == 2:
                        break
                    
                    # t2 = time.time()
                    # print("Time Taken : ",t2-t1)
                    # breakpoint()
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
