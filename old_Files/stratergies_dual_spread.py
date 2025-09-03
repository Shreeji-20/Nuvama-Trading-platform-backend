import os
from APIConnect.APIConnect import APIConnect
import redis
import json
import orjson
import importlib.util, sys, pathlib, traceback
import time
from ..nuvama.order_class import Orders
from constants.exchange import ExchangeEnum
from constants.action import ActionEnum
from constants.order_type import OrderTypeEnum
from constants.product_code import ProductCodeENum
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Removed logger setup - using print statements instead


class StratergyDualSpread:
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
        
        # Initialize fixed thread pool executor with 4 workers for dual spread management
        self.executor = ThreadPoolExecutor(max_workers=4)

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
        
        # Initialize tracking for both call and put sides
        self.call_entry_qtys = {}
        self.call_exit_qtys = {}
        self.put_entry_qtys = {}
        self.put_exit_qtys = {}
        
        # Track executed prices for interdependent calculation
        self.call_executed_prices = {}
        self.put_executed_prices = {}
        
        # Track which side was executed first per user
        self.first_executed_side = {}  # 'call' or 'put'

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
                    current_call_entry = getattr(self, 'call_entry_qtys', {}).copy()
                    current_call_exit = getattr(self, 'call_exit_qtys', {}).copy()
                    current_put_entry = getattr(self, 'put_entry_qtys', {}).copy()
                    current_put_exit = getattr(self, 'put_exit_qtys', {}).copy()
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

    def _calculate_action_based_price_sum(self, leg_keys, leg_prices, legs_dict, debug_label=""):
        """Calculate price sum considering BUY/SELL actions for each leg, weighted by quantity/lot_size.
        
        This method is used ONLY for spread calculation, not for order placement.
        The weighted pricing ensures accurate spread calculation when legs have different quantities.
        """
        total_sum = 0
        debug_parts = []
        lot_size = self._get_lot_size()
        
        for leg_key in leg_keys:
            leg_action = legs_dict[leg_key]['info'].get('action', self.global_action).upper()
            leg_price = leg_prices.get(leg_key, 0)
            leg_quantity = legs_dict[leg_key]['info'].get('quantity', self.params.get("bidding_leg", {}).get("quantity", 75))
            
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

    def _calculate_simple_price_sum(self, leg_keys, leg_prices, legs_dict, debug_label=""):
        """Calculate simple price sum considering BUY/SELL actions for each leg (for order placement).
        
        This method is used for calculating order prices. It uses actual market prices
        without quantity weighting since orders are placed at market prices.
        """
        total_sum = 0
        debug_parts = []
        
        for leg_key in leg_keys:
            leg_action = legs_dict[leg_key]['info'].get('action', self.global_action).upper()
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
        """Get the lot size for the trading symbol. Default to 75 for NIFTY."""
        symbol = None
        if hasattr(self, 'call_legs') and self.call_legs:
            # Get symbol from first available call leg
            first_leg_key = next(iter(self.call_legs.keys()))
            symbol = self.call_legs[first_leg_key]['info'].get('symbol', 'NIFTY').upper()
        
        return self.lot_sizes.get(symbol, 75)

    def _calculate_slice_quantity(self, total_quantity, slice_multiplier):
        """Calculate slice quantity based on slice multiplier."""
        if slice_multiplier <= 0:
            slice_multiplier = 1
        
        slice_quantity = total_quantity / slice_multiplier
        
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
        for base_leg_key in base_leg_templates:
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

    def _update_filled_quantities(self, order, uid, side, is_entry=True):
        """Update filled quantities for call or put side."""
        last_key = f"order:{order['user_id']}" + f"{order['remark']}" + f"{order.get('order_id', '')}"
        last_raw = self.r.get(last_key)
        if last_raw:
            order_data = orjson.loads(last_raw.decode())
            filled = int(order_data["response"]["data"]["fQty"])
            
            with self.templates_lock:
                if side == 'call':
                    if is_entry:
                        self.call_entry_qtys[uid] += filled
                    else:
                        self.call_exit_qtys[uid] += filled
                else:  # put
                    if is_entry:
                        self.put_entry_qtys[uid] += filled
                    else:
                        self.put_exit_qtys[uid] += filled
            
            return filled
        return 0

    def _auto_select_bidding_legs(self):
        """
        Automatically select call and put bidding legs from available legs.
        If bidding_leg is call, find a put leg as put_bidding_leg and vice versa.
        """
        # Get the main bidding leg from params
        main_bidding_leg = self.params.get("bidding_leg", {})
        main_bidding_type = main_bidding_leg.get("type", "CE").upper()
        
        # Initialize bidding legs
        self.call_bidding_leg_info = None
        self.put_bidding_leg_info = None
        self.call_bidding_leg_key = None
        self.put_bidding_leg_key = None
        
        # If main bidding leg is CE (Call), use it as call bidding leg
        if main_bidding_type == "CE":
            self.call_bidding_leg_info = main_bidding_leg
            self.call_bidding_leg_key = "bidding_leg"
            
            # Find a PE (Put) leg from other legs to use as put bidding leg
            for leg_key in ["leg1", "leg2", "leg3", "leg4", "leg5"]:
                if leg_key in self.params:
                    leg_info = self.params[leg_key]
                    if leg_info.get("type", "").upper() == "PE":
                        self.put_bidding_leg_info = leg_info
                        self.put_bidding_leg_key = leg_key
                        break
        
        # If main bidding leg is PE (Put), use it as put bidding leg
        elif main_bidding_type == "PE":
            self.put_bidding_leg_info = main_bidding_leg
            self.put_bidding_leg_key = "bidding_leg"
            
            # Find a CE (Call) leg from other legs to use as call bidding leg
            for leg_key in ["leg1", "leg2", "leg3", "leg4", "leg5"]:
                if leg_key in self.params:
                    leg_info = self.params[leg_key]
                    if leg_info.get("type", "").upper() == "CE":
                        self.call_bidding_leg_info = leg_info
                        self.call_bidding_leg_key = leg_key
                        break
        
        # Validate that we have both call and put bidding legs
        if not self.call_bidding_leg_info:
            raise RuntimeError("No Call (CE) leg found for call bidding leg")
        if not self.put_bidding_leg_info:
            raise RuntimeError("No Put (PE) leg found for put bidding leg")
        
        print(f"Auto-selected bidding legs - Call: {self.call_bidding_leg_key}, Put: {self.put_bidding_leg_key}")

    def _auto_select_base_legs(self):
        """
        Automatically separate remaining legs into call and put base legs.
        """
        base_leg_keys = self.params.get("base_legs", [])
        
        self.call_base_leg_keys = []
        self.put_base_leg_keys = []
        
        for leg_key in base_leg_keys:
            if leg_key in self.params and leg_key != self.call_bidding_leg_key and leg_key != self.put_bidding_leg_key:
                leg_info = self.params[leg_key]
                leg_type = leg_info.get("type", "").upper()
                
                if leg_type == "CE":
                    self.call_base_leg_keys.append(leg_key)
                elif leg_type == "PE":
                    self.put_base_leg_keys.append(leg_key)
        
        print(f"Auto-selected base legs - Call: {self.call_base_leg_keys}, Put: {self.put_base_leg_keys}")

    def _init_legs_and_orders(self):
        """
        Initialize dual spread legs: automatically select call and put bidding legs from params
        """
        
        # Auto-select bidding legs based on the main bidding leg type
        self._auto_select_bidding_legs()
        
        # Auto-select base legs from remaining legs
        self._auto_select_base_legs()
        
        # Load call legs
        self.call_legs = {}
        
        # Load call bidding leg
        self.call_legs[self.call_bidding_leg_key] = self._load_leg_data(self.call_bidding_leg_key, self.call_bidding_leg_info)
        
        # Load call base legs
        for leg_key in self.call_base_leg_keys:
            leg_info = self.params.get(leg_key)
            if not leg_info:
                raise RuntimeError(f"Missing call leg info for {leg_key}")
            self.call_legs[leg_key] = self._load_leg_data(leg_key, leg_info)
        
        # Load put legs
        self.put_legs = {}
        
        # Load put bidding leg
        self.put_legs[self.put_bidding_leg_key] = self._load_leg_data(self.put_bidding_leg_key, self.put_bidding_leg_info)
        
        # Load put base legs
        for leg_key in self.put_base_leg_keys:
            leg_info = self.params.get(leg_key)
            if not leg_info:
                raise RuntimeError(f"Missing put leg info for {leg_key}")
            self.put_legs[leg_key] = self._load_leg_data(leg_key, leg_info)

        # determine exchange from the first available leg
        first_leg_data = None
        if self.call_base_leg_keys:
            first_leg_data = self.call_legs[self.call_base_leg_keys[0]]['data']
        elif self.call_bidding_leg_key:
            first_leg_data = self.call_legs[self.call_bidding_leg_key]['data']
        elif self.put_base_leg_keys:
            first_leg_data = self.put_legs[self.put_base_leg_keys[0]]['data']
        elif self.put_bidding_leg_key:
            first_leg_data = self.put_legs[self.put_bidding_leg_key]['data']
        
        if first_leg_data:
            symbol_text = first_leg_data["response"]["data"]["symbol"]
            if "BFO" in symbol_text:
                self.exchange = ExchangeEnum.BFO
            elif "NFO" in symbol_text:
                self.exchange = ExchangeEnum.NFO
            elif "NSE" in symbol_text:
                self.exchange = ExchangeEnum.NSE
            else:
                self.exchange = ExchangeEnum.BSE
        else:
            self.exchange = ExchangeEnum.NFO  # Default fallback

        # create per-user order templates used during runtime
        uids = self.params.get("user_ids", [])
        if isinstance(uids, (int, str)):
            uids = [str(uids)]
        if not isinstance(uids, list):
            uids = list(uids) if uids is not None else []
        
        uids = [str(uid) for uid in uids]

        # Global action for backward compatibility
        self.global_action = self.params.get('action', 'BUY').upper()

        # dictionaries keyed by user_id -> template for each leg
        self.call_order_templates = {}  # For call bidding leg entry orders
        self.call_base_leg_templates = {}  # For call base legs entry orders
        self.call_exit_order_templates = {}  # For call bidding leg exit orders
        self.call_exit_base_leg_templates = {}  # For call base legs exit orders
        
        self.put_order_templates = {}  # For put bidding leg entry orders
        self.put_base_leg_templates = {}  # For put base legs entry orders
        self.put_exit_order_templates = {}  # For put bidding leg exit orders
        self.put_exit_base_leg_templates = {}  # For put base legs exit orders

        # save normalized user list and create per-user tracking structures
        self.uids = uids
        
        # Initialize quantities for both call and put sides
        if not hasattr(self, 'call_entry_qtys') or set(self.call_entry_qtys.keys()) != set(uids):
            old_call_entry_qtys = getattr(self, 'call_entry_qtys', {})
            old_call_exit_qtys = getattr(self, 'call_exit_qtys', {})
            old_put_entry_qtys = getattr(self, 'put_entry_qtys', {})
            old_put_exit_qtys = getattr(self, 'put_exit_qtys', {})
            
            self.call_entry_qtys = {uid: old_call_entry_qtys.get(uid, 0) for uid in uids}
            self.call_exit_qtys = {uid: old_call_exit_qtys.get(uid, 0) for uid in uids}
            self.put_entry_qtys = {uid: old_put_entry_qtys.get(uid, 0) for uid in uids}
            self.put_exit_qtys = {uid: old_put_exit_qtys.get(uid, 0) for uid in uids}
        
        # Initialize execution tracking
        self.call_executed_prices = {uid: None for uid in uids}
        self.put_executed_prices = {uid: None for uid in uids}
        self.first_executed_side = {uid: None for uid in uids}

        # Create templates for each user
        for uid in uids:
            # Initialize dictionaries for this user
            self.call_order_templates[uid] = {}
            self.call_base_leg_templates[uid] = {}
            self.call_exit_order_templates[uid] = {}
            self.call_exit_base_leg_templates[uid] = {}
            
            self.put_order_templates[uid] = {}
            self.put_base_leg_templates[uid] = {}
            self.put_exit_order_templates[uid] = {}
            self.put_exit_base_leg_templates[uid] = {}
            
            # Call bidding leg templates
            call_bidding_leg_data = self.call_legs[self.call_bidding_leg_key]['data']
            call_bidding_leg_action = self.call_bidding_leg_info.get('action', self.global_action).upper()
            
            self.call_order_templates[uid] = self._make_order_template(
                call_bidding_leg_data, 
                buy_if=call_bidding_leg_action, 
                user_id=uid,
                leg_key=self.call_bidding_leg_key
            )
            
            self.call_exit_order_templates[uid] = self._make_order_template(
                call_bidding_leg_data, 
                buy_if=("SELL" if call_bidding_leg_action == "BUY" else "BUY"), 
                quantity=0, 
                user_id=uid,
                leg_key=self.call_bidding_leg_key
            )
            
            # Call base legs templates
            for base_leg_key in self.call_base_leg_keys:
                base_leg_data = self.call_legs[base_leg_key]['data']
                base_leg_info = self.call_legs[base_leg_key]['info']
                base_leg_action = base_leg_info.get('action', self.global_action).upper()
                
                self.call_base_leg_templates[uid][base_leg_key] = self._make_order_template(
                    base_leg_data, 
                    buy_if=base_leg_action, 
                    user_id=uid,
                    leg_key=base_leg_key
                )
                
                self.call_exit_base_leg_templates[uid][base_leg_key] = self._make_order_template(
                    base_leg_data, 
                    buy_if=("SELL" if base_leg_action == "BUY" else "BUY"), 
                    quantity=0, 
                    user_id=uid,
                    leg_key=base_leg_key
                )
            
            # Put bidding leg templates
            put_bidding_leg_data = self.put_legs[self.put_bidding_leg_key]['data']
            put_bidding_leg_action = self.put_bidding_leg_info.get('action', self.global_action).upper()
            
            self.put_order_templates[uid] = self._make_order_template(
                put_bidding_leg_data, 
                buy_if=put_bidding_leg_action, 
                user_id=uid,
                leg_key=self.put_bidding_leg_key
            )
            
            self.put_exit_order_templates[uid] = self._make_order_template(
                put_bidding_leg_data, 
                buy_if=("SELL" if put_bidding_leg_action == "BUY" else "BUY"), 
                quantity=0, 
                user_id=uid,
                leg_key=self.put_bidding_leg_key
            )
            
            # Put base legs templates
            for base_leg_key in self.put_base_leg_keys:
                base_leg_data = self.put_legs[base_leg_key]['data']
                base_leg_info = self.put_legs[base_leg_key]['info']
                base_leg_action = base_leg_info.get('action', self.global_action).upper()
                
                self.put_base_leg_templates[uid][base_leg_key] = self._make_order_template(
                    base_leg_data, 
                    buy_if=base_leg_action, 
                    user_id=uid,
                    leg_key=base_leg_key
                )
                
                self.put_exit_base_leg_templates[uid][base_leg_key] = self._make_order_template(
                    base_leg_data, 
                    buy_if=("SELL" if base_leg_action == "BUY" else "BUY"), 
                    quantity=0, 
                    user_id=uid,
                    leg_key=base_leg_key
                )

    def _make_order_template(self, leg_obj, buy_if="BUY", quantity=None, user_id=None, leg_key=None):
        """Return a dict template for orders built from a depth/leg object."""
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
            # Fallback to main bidding_leg quantity if available
            qty = int(self.params.get("bidding_leg", {}).get("quantity", 75))

        # Apply quantity multiplier
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
            "remark": self.params.get("notes", "Lord_Shreeji_Dual"),
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

    def _get_leg_prices_with_actions(self, legs_dict, base_leg_keys, bidding_leg_key, is_exit=False):
        """Get current prices for legs based on individual leg actions."""
        prices = {}
        direction_debug = []
        
        # Get base leg prices dynamically based on their individual actions
        for base_leg_key in base_leg_keys:
            try:
                leg_data = self._depth_from_redis(legs_dict[base_leg_key]['depth_key'])
                leg_action = legs_dict[base_leg_key]['info'].get('action', self.global_action).upper()
                
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
            bidding_leg_data = self._depth_from_redis(legs_dict[bidding_leg_key]['depth_key'])
            bidding_leg_action = legs_dict[bidding_leg_key]['info'].get('action', self.global_action).upper()
            
            # Determine bid_or_ask based on bidding leg action and entry/exit
            if is_exit:
                # For exit: opposite of entry direction
                bid_or_ask = "bidValues" if bidding_leg_action == "BUY" else "askValues"
            else:
                # For entry: BUY uses ask, SELL uses bid
                bid_or_ask = "askValues" if bidding_leg_action == "BUY" else "bidValues"
            
            direction_debug.append(f"{bidding_leg_key}({bidding_leg_action}:{bid_or_ask[:3]})")
            
            pricing_method = self.params.get("pricing_method", "average")
            
            if pricing_method == "depth":
                depth_index = 1
                prices[bidding_leg_key] = self._depth_price(bidding_leg_data, bid_or_ask, depth_index)
            elif self.params.get("no_of_bidask_average", 1) > 1:
                prices[bidding_leg_key] = self._avg_price(bidding_leg_data, bid_or_ask, self.params["no_of_bidask_average"])
            else:
                prices[bidding_leg_key] = self._safe_get_price(bidding_leg_data, bid_or_ask)
        except (KeyError, TypeError) as e:
            print(f"ERROR: Failed to get price for bidding leg {bidding_leg_key}: {e}")
            prices[bidding_leg_key] = 0.0
        
        return prices

    def _calculate_dual_spreads(self, call_prices, put_prices):
        """
        Calculate call and put spreads using the dual spread strategy:
        - call_desired_spread = desired_spread - put_spread
        - put_desired_spread = desired_spread - call_spread
        """
        desired_spread = self.params.get("desired_spread", 0)
        
        # Calculate call spread (sum of call legs with weighted pricing)
        call_spread_sum, _ = self._calculate_action_based_price_sum(
            self.call_base_leg_keys + [self.call_bidding_leg_key], 
            call_prices, 
            self.call_legs
        )
        call_spread = abs(round(call_spread_sum * 20) / 20)
        
        # Calculate put spread (sum of put legs with weighted pricing)
        put_spread_sum, _ = self._calculate_action_based_price_sum(
            self.put_base_leg_keys + [self.put_bidding_leg_key], 
            put_prices, 
            self.put_legs
        )
        put_spread = abs(round(put_spread_sum * 20) / 20)
        
        # Calculate interdependent desired spreads
        call_desired_spread = desired_spread - put_spread
        put_desired_spread = desired_spread - call_spread
        
        return call_spread, put_spread, call_desired_spread, put_desired_spread

    def _calculate_bidding_leg_prices(self, call_prices, put_prices, call_desired_spread, put_desired_spread):
        """
        Calculate bidding leg prices for both call and put sides:
        - call_bidding_leg_price = call_desired_spread - call_base_leg_price_sum
        - put_bidding_leg_price = put_desired_spread - put_base_leg_price_sum
        """
        
        # Calculate call base legs sum (for order placement)
        call_base_legs_price_sum, _ = self._calculate_simple_price_sum(
            self.call_base_leg_keys, call_prices, self.call_legs)
        
        # Calculate put base legs sum (for order placement)
        put_base_legs_price_sum, _ = self._calculate_simple_price_sum(
            self.put_base_leg_keys, put_prices, self.put_legs)
        
        # Calculate bidding leg prices
        call_bidding_leg_price = call_desired_spread - call_base_legs_price_sum
        put_bidding_leg_price = put_desired_spread - put_base_legs_price_sum
        
        # Adjust for lot size
        call_lots = self.call_bidding_leg_info.get('quantity', 75) / self._get_lot_size()
        put_lots = self.put_bidding_leg_info.get('quantity', 75) / self._get_lot_size()
        
        call_bidding_leg_price = call_bidding_leg_price / call_lots if call_lots > 0 else call_bidding_leg_price
        put_bidding_leg_price = put_bidding_leg_price / put_lots if put_lots > 0 else put_bidding_leg_price
        
        return call_bidding_leg_price, put_bidding_leg_price

    def _execute_dual_entry_orders(self, uid, call_prices, put_prices, call_desired_spread, put_desired_spread):
        """Execute entry orders for both call and put sides in parallel."""
        start_price = self.params["start_price"]
        run_state = int(self.params['run_state'])
        
        if run_state != 0:
            return None
        
        # Calculate bidding leg prices
        call_bidding_leg_price, put_bidding_leg_price = self._calculate_bidding_leg_prices(
            call_prices, put_prices, call_desired_spread, put_desired_spread)
        
        # Check if either side meets entry conditions
        call_condition = call_desired_spread < start_price if self.params["action"].upper() == "BUY" else call_desired_spread > start_price
        put_condition = put_desired_spread < start_price if self.params["action"].upper() == "BUY" else put_desired_spread > start_price
        
        # Check remaining quantities
        call_current_entry_qty = self.call_entry_qtys.get(uid, 0)
        put_current_entry_qty = self.put_entry_qtys.get(uid, 0)
        call_bidding_leg_qty = int(self.call_bidding_leg_info.get("quantity", 75))
        put_bidding_leg_qty = int(self.put_bidding_leg_info.get("quantity", 75))
        call_remaining_qty = (call_bidding_leg_qty - call_current_entry_qty) * self.params.get("quantity_multiplier", 1)
        put_remaining_qty = (put_bidding_leg_qty - put_current_entry_qty) * self.params.get("quantity_multiplier", 1)
        
        results = []
        
        # Execute call side if conditions are met
        if call_condition and call_remaining_qty > 0:
            call_od = self.call_order_templates[uid].copy()
            call_od_base_legs = {k: v.copy() for k, v in self.call_base_leg_templates[uid].items()}
            
            # Set call bidding leg price
            call_od["Limit_Price"] = self._format_limit_price(call_bidding_leg_price)
            
            # Set call base leg prices
            for base_leg_key in self.call_base_leg_keys:
                call_od_base_legs[base_leg_key]["Limit_Price"] = self._format_limit_price(call_prices.get(base_leg_key, 0))
            
            # Adjust quantities
            actual_call_qty = min(call_od["Quantity"], call_remaining_qty)
            call_od["Quantity"] = actual_call_qty
            
            slice_multiplier = self.params.get("slice_multiplier", 1)
            self._adjust_quantity_for_slicing(call_od, slice_multiplier)
            self._update_base_leg_quantities(call_od_base_legs, call_remaining_qty, slice_multiplier)
            
            # Place call orders
            print("Call order : ",json.dumps(call_od,indent=2), json.dumps(call_od_base_legs,indent=2))
            # breakpoint()
            call_order = self._place_all_orders(call_od, call_od_base_legs)
            self._update_filled_quantities(call_order, uid, 'call', is_entry=True)
            
            # Track execution
            self.call_executed_prices[uid] = call_bidding_leg_price
            if self.first_executed_side[uid] is None:
                self.first_executed_side[uid] = 'call'
            
            results.append({"uid": uid, "side": "call", "action": "entry"})
        
        # Execute put side if conditions are met
        if put_condition and put_remaining_qty > 0:
            put_od = self.put_order_templates[uid].copy()
            put_od_base_legs = {k: v.copy() for k, v in self.put_base_leg_templates[uid].items()}
            
            # Set put bidding leg price
            put_od["Limit_Price"] = self._format_limit_price(put_bidding_leg_price)
            
            # Set put base leg prices
            for base_leg_key in self.put_base_leg_keys:
                put_od_base_legs[base_leg_key]["Limit_Price"] = self._format_limit_price(put_prices.get(base_leg_key, 0))
            
            # Adjust quantities
            actual_put_qty = min(put_od["Quantity"], put_remaining_qty)
            put_od["Quantity"] = actual_put_qty
            
            slice_multiplier = self.params.get("slice_multiplier", 1)
            self._adjust_quantity_for_slicing(put_od, slice_multiplier)
            self._update_base_leg_quantities(put_od_base_legs, put_remaining_qty, slice_multiplier)
            
            # Place put orders
            print("Put orders : ",json.dumps(put_od,indent=2), json.dumps(put_od_base_legs,indent=2))
            # breakpoint()
            put_order = self._place_all_orders(put_od, put_od_base_legs)
            self._update_filled_quantities(put_order, uid, 'put', is_entry=True)
            
            # Track execution
            self.put_executed_prices[uid] = put_bidding_leg_price
            if self.first_executed_side[uid] is None:
                self.first_executed_side[uid] = 'put'
            
            results.append({"uid": uid, "side": "put", "action": "entry"})
        
        return results if results else None

    def _execute_dual_exit_orders(self, uid, call_prices, put_prices):
        """Execute exit orders for both call and put sides."""
        exit_start = self.params["exit_start"]
        run_state = int(self.params['run_state'])
        
        # Calculate current spreads for exit conditions
        call_spread, put_spread, _, _ = self._calculate_dual_spreads(call_prices, put_prices)
        
        # Check exit conditions
        is_buy_action = self.params["action"].upper() == "BUY"
        call_exit_condition = (call_spread > exit_start) if is_buy_action else (call_spread < exit_start)
        put_exit_condition = (put_spread > exit_start) if is_buy_action else (put_spread < exit_start)
        call_exit_condition = call_exit_condition or (run_state == 2)
        put_exit_condition = put_exit_condition or (run_state == 2)
        
        # Check if there are quantities to exit
        call_exit_condition_2 = self.call_entry_qtys.get(uid, 0) > self.call_exit_qtys.get(uid, 0)
        put_exit_condition_2 = self.put_entry_qtys.get(uid, 0) > self.put_exit_qtys.get(uid, 0)
        
        results = []
        
        # Execute call exit if conditions are met
        if call_exit_condition and call_exit_condition_2:
            call_remaining_exit_qty = self.call_entry_qtys.get(uid, 0) - self.call_exit_qtys.get(uid, 0)
            
            if call_remaining_exit_qty > 0:
                call_ex = self.call_exit_order_templates[uid].copy()
                call_ex_base_legs = {k: v.copy() for k, v in self.call_exit_base_leg_templates[uid].items()}
                
                call_ex["Quantity"] = call_remaining_exit_qty
                
                slice_multiplier = self.params.get("slice_multiplier", 1)
                self._adjust_quantity_for_slicing(call_ex, slice_multiplier)
                
                # Update call base leg exit quantities
                for base_leg_key in self.call_base_leg_keys:
                    call_ex_base_legs[base_leg_key]["Quantity"] = call_remaining_exit_qty
                    self._adjust_quantity_for_slicing(call_ex_base_legs[base_leg_key], slice_multiplier)
                
                # Place call exit orders
                call_exit_order = self._place_all_orders(call_ex, call_ex_base_legs)
                self._update_filled_quantities(call_exit_order, uid, 'call', is_entry=False)
                
                results.append({"uid": uid, "side": "call", "action": "exit"})
        
        # Execute put exit if conditions are met
        if put_exit_condition and put_exit_condition_2:
            put_remaining_exit_qty = self.put_entry_qtys.get(uid, 0) - self.put_exit_qtys.get(uid, 0)
            
            if put_remaining_exit_qty > 0:
                put_ex = self.put_exit_order_templates[uid].copy()
                put_ex_base_legs = {k: v.copy() for k, v in self.put_exit_base_leg_templates[uid].items()}
                
                put_ex["Quantity"] = put_remaining_exit_qty
                
                slice_multiplier = self.params.get("slice_multiplier", 1)
                self._adjust_quantity_for_slicing(put_ex, slice_multiplier)
                
                # Update put base leg exit quantities
                for base_leg_key in self.put_base_leg_keys:
                    put_ex_base_legs[base_leg_key]["Quantity"] = put_remaining_exit_qty
                    self._adjust_quantity_for_slicing(put_ex_base_legs[base_leg_key], slice_multiplier)
                
                # Place put exit orders
                put_exit_order = self._place_all_orders(put_ex, put_ex_base_legs)
                self._update_filled_quantities(put_exit_order, uid, 'put', is_entry=False)
                
                results.append({"uid": uid, "side": "put", "action": "exit"})
        
        return results if results else None

    def _process_user_dual_spread(self, uid, call_prices, put_prices, call_prices_exit=None, put_prices_exit=None):
        """Worker that runs dual spread ENTRY/EXIT logic for a single user."""
        try:
            # Validate price data
            call_base_leg_sum = sum(call_prices.get(key, 0) for key in self.call_base_leg_keys)
            put_base_leg_sum = sum(put_prices.get(key, 0) for key in self.put_base_leg_keys)
            
            if call_base_leg_sum <= 0 or put_base_leg_sum <= 0:
                print(f"ERROR: Invalid leg prices for user {uid} - Call: {call_prices}, Put: {put_prices}")
                return {"uid": uid, "error": True}
            
            # Set defaults for exit prices if None
            if call_prices_exit is None:
                call_prices_exit = call_prices.copy()
            if put_prices_exit is None:
                put_prices_exit = put_prices.copy()
            
            # Calculate dual spreads
            call_spread, put_spread, call_desired_spread, put_desired_spread = self._calculate_dual_spreads(call_prices, put_prices)
            
            # Try entry orders (both call and put in parallel)
            entry_results = self._execute_dual_entry_orders(uid, call_prices, put_prices, call_desired_spread, put_desired_spread)
            if entry_results:
                return {"uid": uid, "action": "entry", "results": entry_results}
            
            # Try exit orders (both call and put)
            exit_results = self._execute_dual_exit_orders(uid, call_prices_exit, put_prices_exit)
            if exit_results:
                return {"uid": uid, "action": "exit", "results": exit_results}
            
            return {"uid": uid, "action": "no_action"}

        except (redis.RedisError, KeyError, IndexError, TypeError, ValueError) as e:
            print(f"ERROR: per-user ({uid}) dual spread flow failed: {e}")
        except Exception as e:
            print(traceback.format_exc())
            print(f"ERROR: unexpected error in per-user ({uid}) dual spread worker: {e}")
        return {"uid": uid, "error": True}

    def _safe_get_total_quantities_dual(self):
        """Safely get total entry and exit quantities for both call and put sides."""
        try:
            total_call_entry = sum(self.call_entry_qtys.values())
            total_call_exit = sum(self.call_exit_qtys.values())
            total_put_entry = sum(self.put_entry_qtys.values())
            total_put_exit = sum(self.put_exit_qtys.values())
        except Exception as e:
            print(f"ERROR: Error calculating total quantities: {e}")
            total_call_entry = total_call_exit = total_put_entry = total_put_exit = 0
        
        try:
            run_state_val = int(self.params.get('run_state', 0))
        except Exception:
            run_state_val = 0
        
        return total_call_entry, total_call_exit, total_put_entry, total_put_exit, run_state_val

    # --- main logic for dual spread strategy -----------------------------------
    def main_logic(self):
        """Main logic for dual spread strategy - runs both call and put spreads in parallel."""
        while True:
            try:
                if int(self.params['run_state']) == 1:
                    continue # pause
                
                # Get current prices for both call and put legs
                call_prices = self._get_leg_prices_with_actions(
                    self.call_legs, self.call_base_leg_keys, self.call_bidding_leg_key)
                put_prices = self._get_leg_prices_with_actions(
                    self.put_legs, self.put_base_leg_keys, self.put_bidding_leg_key)
                
                call_prices_exit = self._get_leg_prices_with_actions(
                    self.call_legs, self.call_base_leg_keys, self.call_bidding_leg_key, is_exit=True)
                put_prices_exit = self._get_leg_prices_with_actions(
                    self.put_legs, self.put_base_leg_keys, self.put_bidding_leg_key, is_exit=True)
                
                # Validate price data
                call_base_leg_sum = sum(call_prices.get(key, 0) for key in self.call_base_leg_keys)
                put_base_leg_sum = sum(put_prices.get(key, 0) for key in self.put_base_leg_keys)
                
                if call_base_leg_sum <= 0 or put_base_leg_sum <= 0:
                    print(f"ERROR: Invalid leg prices - Call: {call_prices}, Put: {put_prices}")
                    time.sleep(0.1)
                    continue

                # Calculate dual spreads for display
                call_spread, put_spread, call_desired_spread, put_desired_spread = self._calculate_dual_spreads(call_prices, put_prices)
                
                print(f"Call Spread: {call_spread:.2f} | Put Spread: {put_spread:.2f} | "
                      f"Call Desired: {call_desired_spread:.2f} | Put Desired: {put_desired_spread:.2f}", end="  \r\r")
                # Process users in parallel for dual spread strategy
                if getattr(self, "uids", None):
                    futures = {
                        self.executor.submit(
                            self._process_user_dual_spread,
                            uid,
                            call_prices,
                            put_prices,
                            call_prices_exit,
                            put_prices_exit,
                        ): uid
                        for uid in self.uids
                    }
                    
                    for fut in as_completed(futures):
                        try:
                            res = fut.result()
                        except Exception as e:
                            print(f"ERROR: Dual spread per-user task failed: {e}")
                    
                    # Check exit condition for both call and put sides
                    total_call_entry, total_call_exit, total_put_entry, total_put_exit, run_state_val = self._safe_get_total_quantities_dual()
                    
                    # Exit when both call and put sides are fully closed and run_state is 2
                    if (total_call_entry == total_call_exit and total_put_entry == total_put_exit and run_state_val == 2):
                        break
                    
                    continue
                
            except redis.RedisError as e:
                print(f"ERROR: redis error in dual spread main loop: {e}")
                time.sleep(0.5)
                continue
            except (KeyError, IndexError, TypeError, AttributeError, ValueError, orjson.JSONDecodeError) as e:
                print(f"WARNING: transient data error in dual spread main loop: {e}")
                time.sleep(0.1)
                continue
            except Exception as e:
                print(f"ERROR: unexpected error in dual spread main loop: {e}")
                print(traceback.format_exc())
                time.sleep(0.1)
                continue
            except KeyboardInterrupt:
                print("INFO: KeyboardInterrupt received, exiting dual spread main loop")
                os._exit(0)
