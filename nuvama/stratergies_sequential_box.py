import os
import redis
import json
import orjson
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from APIConnect.APIConnect import APIConnect
from .order_class import Orders
from constants.exchange import ExchangeEnum
from constants.action import ActionEnum
from constants.order_type import OrderTypeEnum
from constants.product_code import ProductCodeENum

# Sequential Box Strategy - Execute legs in pairs with spread monitoring


class StratergySequentialBox:
    def __init__(self, paramsid) -> None:
        # Redis connection
        self.r = redis.Redis(host="localhost", port=6379, db=0)
        
        # Load configuration data
        self.lot_sizes = json.loads(self.r.get("lotsizes"))
        self._init_user_connections()
        
        # Thread management
        self.templates_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=2)

        # Load and validate parameters
        self.params_key = f"4_leg:{paramsid}"
        self._load_params()
        self.global_action = self.params.get('action', 'BUY').upper()
        
        # Load option mapper
        self._load_option_mapper()

        # Initialize tracking dictionaries
        self._init_tracking_data()

        # Start background parameter update thread
        self.params_update_thread = threading.Thread(target=self._live_params_update_thread, daemon=True)
        self.params_update_thread.start()

        # Initialize legs and order templates
        self._init_legs_and_orders()

    def _init_user_connections(self):
        """Initialize user API connections."""
        users = self.r.keys("user:*")
        data = [json.loads(self.r.get(user)) for user in users]
        self.user_obj_dict = {}
        
        for item in data:
            if self.r.exists(f"reqid:{item.get('userid')}"):
                self.user_obj_dict[item.get("userid")] = APIConnect(
                    item.get("apikey"), "", "", False, "", False)
        
        self.order = Orders(self.user_obj_dict)

    def _load_params(self):
        """Load parameters from Redis with validation."""
        raw_params = self.r.get(self.params_key)
        if raw_params is None:
            raise RuntimeError(f"params key missing in redis: {self.params_key}")
        self.params = orjson.loads(raw_params.decode())

    def _load_option_mapper(self):
        """Load option mapper from Redis."""
        try:
            raw_map = self.r.get("option_mapper")
            if raw_map is None:
                raise RuntimeError("option_mapper key missing in redis")
            self.option_mapper = orjson.loads(raw_map.decode())
        except Exception as e:
            print(f"ERROR: failed to load option_mapper from redis: {e}")
            raise

    def _init_tracking_data(self):
        """Initialize all tracking dictionaries."""
        # Sequential execution tracking
        self.pair1_executed = {}
        self.pair1_executed_prices = {}
        self.pair1_executed_spread = {}
        self.pair2_executed = {}
        self.all_legs_executed = {}
        
        # Order placement tracking
        self.pair1_orders_placed = {}
        self.pair2_orders_placed = {}
        
        # Quantity tracking
        self.entry_qtys = {}
        self.exit_qtys = {}

    def _live_params_update_thread(self):
        while True:
            try:
                params = orjson.loads(self.r.get(self.params_key).decode())
                if self.params != params:
                    self.params = params
                    self._init_legs_and_orders()
                time.sleep(1)
            except Exception as e:
                print(f"ERROR: failed to update live params: {e}")
                time.sleep(1)

    # --- initialization helpers -------------------------------------------------
    def _depth_from_redis(self, streaming_symbol: str):
        """Load depth JSON from redis and return parsed object."""
        try:
            raw = self.r.get(streaming_symbol)
            return orjson.loads(raw.decode()) if raw else None
        except (orjson.JSONDecodeError, redis.RedisError) as e:
            print(f"ERROR: redis/JSON error for {streaming_symbol}: {e}")
            return None
        except Exception as e:
            print(f"ERROR: unexpected error reading {streaming_symbol}: {e}")
            return None

    def _create_depth_key(self, leg_info):
        """Create Redis depth key from leg info."""
        expiry_value = leg_info['expiry']
        strike = leg_info['strike']
        
        # Format strike with proper decimal handling
        if isinstance(strike, (int, float)):
            strike_str = f"{strike:.1f}" if strike % 1 != 0 else f"{int(strike)}.0"
        else:
            strike_str = f"{strike}.0" if '.' not in str(strike) else str(strike)
        
        # Format expiry
        if isinstance(expiry_value, (int, str)) and str(expiry_value).isdigit():
            expiry_part = expiry_value
        else:
            expiry_part = leg_info['expiry']
        
        return f"depth:{leg_info['symbol'].upper()}_{strike_str}_{leg_info['type']}-{expiry_part}"

    def _load_leg_data(self, leg_key, leg_info):
        """Load and validate leg data from Redis."""
        depth_key = self._create_depth_key(leg_info)
        leg_data = self._depth_from_redis(depth_key)
        
        if leg_data is None:
            print(f"ERROR: {leg_key} depth not found: {depth_key}")
            raise RuntimeError(f"{leg_key} depth missing in redis")
        
        return {'data': leg_data, 'info': leg_info, 'depth_key': depth_key}

    @staticmethod
    def _format_limit_price(price):
        """Format price to ensure it's never negative and rounded properly."""
        return str(round(max(0.05, abs(price)) * 20) / 20)

    def _get_lot_size(self):
        """Get the lot size for the trading symbol."""
        if hasattr(self, 'legs') and self.legs:
            first_leg = next(iter(self.legs.values()))
            symbol = first_leg['info'].get('symbol', 'NIFTY').upper()
            return self.lot_sizes.get(symbol, 75)
        return 75

    def _determine_leg_pairs(self, all_leg_keys, bidding_leg_key):
        """Automatically determine pairs based on BUY/SELL actions."""
        buy_legs = []
        sell_legs = []
        
        for leg_key in all_leg_keys:
            leg_action = self.legs[leg_key]['info'].get('action', self.global_action).upper()
            if leg_action == "BUY":
                buy_legs.append(leg_key)
            else:
                sell_legs.append(leg_key)
        
        print(f"INFO: Auto-detected BUY legs: {buy_legs}")
        print(f"INFO: Auto-detected SELL legs: {sell_legs}")
        
        # Assign pairs based on available legs
        pair1_bidding, pair1_base = self._assign_pair(buy_legs, bidding_leg_key, sell_legs, all_leg_keys)
        pair2_bidding, pair2_base = self._assign_remaining_pair(
            sell_legs, buy_legs, bidding_leg_key, all_leg_keys, pair1_bidding, pair1_base)
        
        return pair1_bidding, pair1_base, pair2_bidding, pair2_base

    def _assign_pair(self, primary_legs, bidding_leg_key, secondary_legs, all_leg_keys):
        """Assign first pair from primary legs."""
        if len(primary_legs) >= 2:
            return primary_legs[0], primary_legs[1]
        elif len(primary_legs) == 1:
            return primary_legs[0], (bidding_leg_key if bidding_leg_key in primary_legs else secondary_legs[0])
        else:
            return bidding_leg_key, (all_leg_keys[1] if len(all_leg_keys) > 1 else bidding_leg_key)

    def _assign_remaining_pair(self, sell_legs, buy_legs, bidding_leg_key, all_leg_keys, pair1_bidding, pair1_base):
        """Assign second pair from remaining legs."""
        if len(sell_legs) >= 2:
            return sell_legs[0], sell_legs[1]
        elif len(sell_legs) == 1:
            return sell_legs[0], (bidding_leg_key if bidding_leg_key in sell_legs else buy_legs[0])
        else:
            remaining_legs = [leg for leg in all_leg_keys if leg not in [pair1_bidding, pair1_base]]
            return (remaining_legs[0] if remaining_legs else bidding_leg_key,
                   remaining_legs[1] if len(remaining_legs) > 1 else bidding_leg_key)

    def _init_legs_and_orders(self):
        """Initialize 4-leg sequential box strategy with optimized leg pairing."""
        # Load legs data
        self.legs = {}
        bidding_leg_key = self.params.get("bidding_leg_key", "bidding_leg")
        bidding_leg_info = self.params.get("bidding_leg")
        if not bidding_leg_info:
            raise RuntimeError("Missing bidding leg info")
        
        self.legs[bidding_leg_key] = self._load_leg_data(bidding_leg_key, bidding_leg_info)
        
        # Load base legs
        base_leg_keys = self.params.get("base_legs", ["leg1", "leg2", "leg3", "leg4"])
        all_leg_keys = [bidding_leg_key] + base_leg_keys
        
        for leg_key in base_leg_keys:
            leg_info = self.params.get(leg_key)
            if not leg_info:
                raise RuntimeError(f"Missing leg info for {leg_key}")
            self.legs[leg_key] = self._load_leg_data(leg_key, leg_info)
        
        # Determine pairs automatically
        (self.pair1_bidding_leg, self.pair1_base_leg, 
         self.pair2_bidding_leg, self.pair2_base_leg) = self._determine_leg_pairs(all_leg_keys, bidding_leg_key)
        
        print(f"INFO: Pair 1 - Bidding: {self.pair1_bidding_leg}, Base: {self.pair1_base_leg}")
        print(f"INFO: Pair 2 - Bidding: {self.pair2_bidding_leg}, Base: {self.pair2_base_leg}")
        
        # Validate leg assignments
        required_legs = [self.pair1_bidding_leg, self.pair1_base_leg, self.pair2_bidding_leg, self.pair2_base_leg]
        for leg in required_legs:
            if leg not in self.legs:
                raise RuntimeError(f"Required leg {leg} not found in legs definition")

        # Determine exchange
        self._determine_exchange()
        
        # Setup user data
        self._setup_user_data(all_leg_keys)
        
        # Create order templates
        self._create_order_templates(all_leg_keys)

    def _determine_exchange(self):
        """Determine exchange from first leg symbol."""
        first_leg = list(self.legs.values())[0]['data']
        symbol_text = first_leg["response"]["data"]["symbol"]
        
        exchange_map = {
            "BFO": ExchangeEnum.BFO,
            "NFO": ExchangeEnum.NFO,
            "NSE": ExchangeEnum.NSE
        }
        
        self.exchange = next(
            (enum_val for key, enum_val in exchange_map.items() if key in symbol_text),
            ExchangeEnum.BSE
        )

    def _setup_user_data(self, all_leg_keys):
        """Setup per-user tracking data."""
        uids = self.params.get("user_ids", [])
        if isinstance(uids, (int, str)):
            uids = [str(uids)]
        if not isinstance(uids, list):
            uids = list(uids) if uids is not None else []
        
        self.uids = [str(uid) for uid in uids]

        # Initialize per-user tracking
        for uid in self.uids:
            self.pair1_executed[uid] = False
            self.pair1_executed_prices[uid] = {}
            self.pair1_executed_spread[uid] = 0.0
            self.pair2_executed[uid] = False
            self.all_legs_executed[uid] = False
            self.pair1_orders_placed[uid] = False
            self.pair2_orders_placed[uid] = False
            self.entry_qtys[uid] = {leg: 0 for leg in all_leg_keys}
            self.exit_qtys[uid] = {leg: 0 for leg in all_leg_keys}

    def _create_order_templates(self, all_leg_keys):
        """Create order templates for all legs and users."""
        self.order_templates = {}
        self.exit_order_templates = {}

        for uid in self.uids:
            self.order_templates[uid] = {}
            self.exit_order_templates[uid] = {}
            
            for leg_key in all_leg_keys:
                leg_data = self.legs[leg_key]['data']
                leg_info = self.legs[leg_key]['info']
                leg_action = leg_info.get('action', self.global_action).upper()
                
                # Entry template
                self.order_templates[uid][leg_key] = self._make_order_template(
                    leg_data, leg_action, uid, leg_key)
                
                # Exit template (opposite action)
                exit_action = "SELL" if leg_action == "BUY" else "BUY"
                self.exit_order_templates[uid][leg_key] = self._make_order_template(
                    leg_data, exit_action, uid, leg_key, quantity=0)

    def _make_order_template(self, leg_obj, buy_if="BUY", user_id=None, leg_key=None, quantity=None):
        """Return a dict template for orders built from a depth/leg object."""
        if leg_obj is None:
            raise RuntimeError("leg_obj is None when building order template")
        
        streaming_symbol = leg_obj["response"]["data"]["symbol"]
        trading_symbol = self.option_mapper[streaming_symbol]["tradingsymbol"]
        action = ActionEnum.BUY if buy_if.upper() == "BUY" else ActionEnum.SELL
        order_type = (OrderTypeEnum.MARKET if self.params["order_type"].upper() == "MARKET" 
                     else OrderTypeEnum.LIMIT)

        # Calculate quantity
        if quantity is not None:
            qty = quantity
        elif leg_key and leg_key in self.params and 'quantity' in self.params[leg_key]:
            qty = int(self.params[leg_key]['quantity'])
        else:
            qty = int(self.params.get("default_quantity", 75))

        qty *= int(self.params.get("quantity_multiplier", 1))

        return {
            "user_id": user_id or self.params.get("user_ids"),
            "Trading_Symbol": trading_symbol,
            "Exchange": self.exchange,
            "Action": action,
            "Order_Type": order_type,
            "Quantity": qty,
            "Slice_Quantity": qty,
            "Streaming_Symbol": streaming_symbol,
            "Limit_Price": "0",
            "Disclosed_Quantity": 0,
            "TriggerPrice": 0,
            "ProductCode": ProductCodeENum.NRML,
            "remark": self.params.get("notes", "SequentialBox"),
            "IOC": self.params.get('IOC_timeout', 0.5),
            "exit_price_gap": float(self.params.get('exit_price_gap', 0))
        }

    # --- runtime helpers --------------------------------------------------------
    def _safe_get_price(self, data, side_key):
        """Safe price extraction with proper None checking."""
        try:
            if not (data and data.get("response", {}).get("data", {}).get(side_key)):
                return 0.0
            
            pricing_method = self.params.get("pricing_method", "average")
            
            if pricing_method == "depth":
                return self._depth_price(data, side_key, self.params.get("depth_index", 3))
            
            no_of_average = self.params.get("no_of_bidask_average", 1)
            if no_of_average > 1:
                return self._avg_price(data, side_key, no_of_average)
            
            return float(data["response"]["data"][side_key][0]["price"])
        except (KeyError, IndexError, TypeError, ValueError):
            return 0.0

    def _depth_price(self, data, side_key, depth_index):
        """Get the price at specific depth index (1-based)."""
        try:
            entries = data["response"]["data"][side_key]
            if not entries:
                return 0.0
            
            depth_index = max(1, int(depth_index))
            index = min(depth_index - 1, len(entries) - 1)
            return float(entries[index]["price"])
        except (KeyError, IndexError, TypeError, ValueError) as e:
            print(f"ERROR: _depth_price failed for side {side_key}: {e}")
            return 0.0

    def _avg_price(self, data, side_key, n):
        """Compute average of the first n bid/ask prices."""
        try:
            entries = data["response"]["data"][side_key]
            if not entries:
                return 0.0
            
            n = max(1, int(n))
            count = min(n, len(entries))
            return sum(float(entries[i]["price"]) for i in range(count)) / count
        except (KeyError, IndexError, TypeError, ValueError) as e:
            print(f"ERROR: _avg_price failed for side {side_key}: {e}")
            return 0.0

    def _get_leg_prices(self, leg_keys, is_exit=False):
        """Get current prices for specified legs based on their actions."""
        prices = {}
        for leg_key in leg_keys:
            try:
                leg_data = self._depth_from_redis(self.legs[leg_key]['depth_key'])
                leg_action = self.legs[leg_key]['info'].get('action', self.global_action).upper()
                
                # Determine bid_or_ask based on leg action and entry/exit
                if is_exit:
                    bid_or_ask = "bidValues" if leg_action == "BUY" else "askValues"
                else:
                    bid_or_ask = "askValues" if leg_action == "BUY" else "bidValues"
                
                prices[leg_key] = self._safe_get_price(leg_data, bid_or_ask)
            except (KeyError, TypeError) as e:
                print(f"ERROR: Failed to get price for {leg_key}: {e}")
                prices[leg_key] = 0.0
        return prices

    def _calculate_spread(self, leg_keys, prices, action_factor=1):
        """Generic spread calculation method."""
        spread = 0
        for leg_key in leg_keys:
            leg_action = self.legs[leg_key]['info'].get('action', self.global_action).upper()
            leg_price = prices.get(leg_key, 0)
            spread += leg_price * action_factor if leg_action == "BUY" else -leg_price * action_factor
        return abs(spread)

    def _calculate_pair_spread(self, leg1_key, leg2_key, prices):
        """Calculate spread for a pair of legs considering their actions."""
        return self._calculate_spread([leg1_key, leg2_key], prices)

    def _calculate_box_spread(self, prices):
        """Calculate the full box spread for all legs."""
        return self._calculate_spread(list(self.legs.keys()), prices)

    def _calculate_bidding_leg_price(self, current_prices):
        """Calculate the bidding leg price based on desired spread formula."""
        try:
            desired_spread = self.params.get("desired_spread", 405)
            bidding_leg_key = self.params.get("bidding_leg_key", "bidding_leg")
            
            # Get all leg keys except bidding leg
            other_leg_keys = [leg_key for leg_key in self.legs.keys() if leg_key != bidding_leg_key]
            
            # Calculate sum of other legs considering their actions
            other_legs_sum = 0
            for leg_key in other_leg_keys:
                leg_action = self.legs[leg_key]['info'].get('action', self.global_action).upper()
                leg_price = current_prices.get(leg_key, 0)
                other_legs_sum += leg_price if leg_action == "BUY" else -leg_price
            
            # Calculate bidding leg price
            bidding_leg_action = self.legs[bidding_leg_key]['info'].get('action', self.global_action).upper()
            
            if bidding_leg_action == "BUY":
                calculated_price = desired_spread - other_legs_sum
            else:
                calculated_price = other_legs_sum - desired_spread
            
            calculated_price = max(0.05, abs(calculated_price))
            
            print(f"INFO: Bidding leg ({bidding_leg_key}) calculated price: {calculated_price:.2f}")
            print(f"INFO: Other legs sum: {other_legs_sum:.2f}, Desired spread: {desired_spread}")
            
            return calculated_price
            
        except Exception as e:
            print(f"ERROR: Failed to calculate bidding leg price: {e}")
            return current_prices.get(bidding_leg_key, 0)

    def _calculate_leg_as_bidding_price(self, current_prices, target_leg_key):
        """Calculate any leg's price as if it were the designated bidding leg."""
        try:
            desired_spread = self.params.get("desired_spread", 405)
            
            # Get all leg keys except the target leg
            other_leg_keys = [leg_key for leg_key in self.legs.keys() if leg_key != target_leg_key]
            
            # Calculate sum of other legs considering their actions
            other_legs_sum = 0
            for leg_key in other_leg_keys:
                leg_action = self.legs[leg_key]['info'].get('action', self.global_action).upper()
                leg_price = current_prices.get(leg_key, 0)
                other_legs_sum += leg_price if leg_action == "BUY" else -leg_price
            
            # Calculate target leg price as if it were the bidding leg
            target_leg_action = self.legs[target_leg_key]['info'].get('action', self.global_action).upper()
            
            if target_leg_action == "BUY":
                calculated_price = desired_spread - other_legs_sum
            else:
                calculated_price = other_legs_sum - desired_spread
            
            calculated_price = max(0.05, abs(calculated_price))
            
            print(f"INFO: Leg ({target_leg_key}) calculated as bidding price: {calculated_price:.2f}")
            print(f"INFO: Other legs sum: {other_legs_sum:.2f}, Desired spread: {desired_spread}")
            
            return calculated_price
            
        except Exception as e:
            print(f"ERROR: Failed to calculate {target_leg_key} as bidding leg price: {e}")
            return current_prices.get(target_leg_key, 0)

    def _check_and_reset_cancelled_orders(self, uid):
        """Check for cancelled orders and reset placement flags accordingly."""
        try:
            pattern = f"order_tracking:{uid}:*"
            tracking_keys = self.r.keys(pattern)
            
            for key in tracking_keys:
                tracking_data = self.r.get(key)
                if not tracking_data:
                    continue
                    
                order_info = orjson.loads(tracking_data.decode())
                if order_info.get("status") != "cancelled":
                    continue
                
                bidding_leg = order_info.get("bidding_leg")
                self._handle_cancelled_order(uid, bidding_leg)
                self.r.delete(key)
            
            # Check for stale placement flags
            self._check_stale_flags(uid)
            
        except Exception as e:
            print(f"ERROR: Failed to check cancelled orders for user {uid}: {e}")

    def _handle_cancelled_order(self, uid, bidding_leg):
        """Handle specific cancelled order by resetting appropriate flags."""
        pairs_to_check = [
            (self.pair1_bidding_leg, self.pair1_base_leg, self.pair1_orders_placed),
            (self.pair2_bidding_leg, self.pair2_base_leg, self.pair2_orders_placed)
        ]
        
        for pair_bidding, pair_base, orders_placed_dict in pairs_to_check:
            if bidding_leg == pair_bidding and orders_placed_dict.get(uid, False):
                both_filled, _, _ = self._check_pair_filled_quantities(uid, pair_bidding, pair_base)
                if not both_filled:
                    orders_placed_dict[uid] = False
                    print(f"INFO: Reset orders_placed for user {uid} due to cancellation")

    def _check_stale_flags(self, uid):
        """Check and reset stale order placement flags."""
        pairs_to_check = [
            (self.pair1_executed, self.pair1_orders_placed, self.pair1_bidding_leg, self.pair1_base_leg),
            (self.pair2_executed, self.pair2_orders_placed, self.pair2_bidding_leg, self.pair2_base_leg)
        ]
        
        for executed_dict, orders_placed_dict, bidding_leg, base_leg in pairs_to_check:
            if orders_placed_dict.get(uid, False) and not executed_dict.get(uid, False):
                both_filled, _, _ = self._check_pair_filled_quantities(uid, bidding_leg, base_leg)
                if not both_filled and not self._has_active_tracking(uid, bidding_leg):
                    orders_placed_dict[uid] = False
                    print(f"INFO: Reset stale orders_placed for user {uid}")

    def _has_active_tracking(self, uid, bidding_leg):
        """Check if there are active tracking keys for a specific pair."""
        active_keys = self.r.keys(f"order_tracking:{uid}:*")
        for key in active_keys:
            tracking_data = self.r.get(key)
            if tracking_data:
                order_info = orjson.loads(tracking_data.decode())
                if order_info.get("bidding_leg") == bidding_leg:
                    return True
        return False

    def _update_executed_prices(self, uid, leg_key, fill_price, is_entry=True):
        """Update executed prices for tracking."""
        try:
            if is_entry:
                if uid not in self.pair1_executed_prices:
                    self.pair1_executed_prices[uid] = {}
                self.pair1_executed_prices[uid][leg_key] = fill_price
                print(f"INFO: Updated executed price for {leg_key}: {fill_price:.2f}")
        except Exception as e:
            print(f"ERROR: Failed to update executed prices: {e}")

    def _update_filled_quantities(self, order, uid, leg_key, is_entry=True):
        """Update filled quantities for a specific leg."""
        order_id = order.get('order_id', '')
        last_key = f"order:{order['user_id']}{order['remark']}{order_id}"
        last_raw = self.r.get(last_key)
        
        if last_raw:
            order_data = orjson.loads(last_raw.decode())
            filled = int(order_data["response"]["data"]["fQty"])
            
            with self.templates_lock:
                target_dict = self.entry_qtys if is_entry else self.exit_qtys
                target_dict[uid][leg_key] += filled
            
            return filled
        return 0

    def _check_pair_filled_quantities(self, uid, leg1_key, leg2_key):
        """Check if both legs of a pair have filled quantities."""
        leg1_qty = self.entry_qtys[uid].get(leg1_key, 0)
        leg2_qty = self.entry_qtys[uid].get(leg2_key, 0)
        both_filled = leg1_qty > 0 and leg2_qty > 0
        return both_filled, leg1_qty, leg2_qty

    def _place_pair_orders(self, uid, bidding_leg_key, base_leg_key, bidding_leg_price, base_leg_price):
        """Place orders for a pair of legs with proper order tracking."""
        try:
            # Prepare orders
            bidding_order = self.order_templates[uid][bidding_leg_key].copy()
            bidding_order["Limit_Price"] = self._format_limit_price(bidding_leg_price)
            
            base_order = self.order_templates[uid][base_leg_key].copy()
            base_order["Limit_Price"] = self._format_limit_price(base_leg_price)
            
            # Place bidding leg first
            bidding_result = self.order.place_order(bidding_order)
            # Create order tracking
            order_id = bidding_result.get('order_id', '')
            tracking_key = f"order_tracking:{uid}:{order_id}"
            tracking_data = {
                "bidding_leg": bidding_leg_key,
                "base_leg": base_leg_key,
                "order_id": order_id,
                "placed_time": bidding_result.get('placed_time'),
                "status": "placed"
            }
            self.r.set(tracking_key, orjson.dumps(tracking_data), ex=300)
            
            # Execute IOC
            base_order['Order_Type'] = OrderTypeEnum.MARKET
            ioc_success, ioc_result = self.order.IOC_order(bidding_result, *[base_order], use_basket=False)
            
            # Update tracking and quantities based on result
            return self._handle_ioc_result(uid, bidding_leg_key, base_leg_key, 
                                         bidding_result, ioc_success, ioc_result, tracking_key)

            # ioc_success = self.order._place_base_leg_orders_basket([base_order, bidding_order], bidding_order['Slice_Quantity'], uid)
            # return True
        except Exception as e:
            print(f"ERROR: Failed to place pair orders for user {uid}: {e}")
            return False

    def _handle_ioc_result(self, uid, bidding_leg_key, base_leg_key, bidding_result, ioc_success, ioc_result, tracking_key):
        """Handle IOC result and update tracking accordingly."""
        if ioc_success:
            # Order completed successfully
            bidding_leg_data = ioc_result.get('bidding_leg', {})
            base_legs_data = ioc_result.get('base_legs', [])
            
            # Store fill prices in tracking
            tracking_data = {
                "bidding_leg": bidding_leg_key,
                "base_leg": base_leg_key,
                "order_id": bidding_result.get('order_id'),
                "status": "completed",
                "fill_prices": {
                    "bidding_leg": bidding_leg_data.get('filled_price', 0),
                    "base_legs": base_legs_data
                }
            }
            self.r.set(tracking_key, orjson.dumps(tracking_data), ex=300)
            
            # Update quantities using actual fill data
            bidding_filled_qty = bidding_leg_data.get('filled_qty', 0)
            bidding_fill_price = bidding_leg_data.get('filled_price', 0)
            if bidding_filled_qty > 0:
                with self.templates_lock:
                    self.entry_qtys[uid][bidding_leg_key] += bidding_filled_qty
                # Store executed price
                self._update_executed_prices(uid, bidding_leg_key, bidding_fill_price, is_entry=True)
            
            # Update base leg quantities and prices
            for base_leg_data in base_legs_data:
                base_filled_qty = base_leg_data.get('filled_qty', 0)
                base_fill_price = base_leg_data.get('filled_price', 0)
                if base_filled_qty > 0:
                    with self.templates_lock:
                        self.entry_qtys[uid][base_leg_key] += base_filled_qty
                    # Store executed price for base leg
                    self._update_executed_prices(uid, base_leg_key, base_fill_price, is_entry=True)
            
            print(f"INFO: Pair orders COMPLETED for user {uid}")
            print(f"  - Bidding ({bidding_leg_key}): Qty={bidding_filled_qty}, Price={bidding_leg_data.get('filled_price', 0):.2f}")
            print(f"  - Base ({base_leg_key}): Fill data={base_legs_data}")
            
            return True
        else:
            # Order was cancelled or failed
            error_msg = ioc_result.get('error', 'Unknown error') if isinstance(ioc_result, dict) else str(ioc_result)
            
            self.r.set(tracking_key, orjson.dumps({
                "bidding_leg": bidding_leg_key,
                "base_leg": base_leg_key,
                "order_id": bidding_result.get('order_id'),
                "status": "cancelled",
                "error": error_msg
            }), ex=300)
            
            # Check for partial fills only on bidding leg using old method as fallback
            partial_fills = self._update_filled_quantities(bidding_result, uid, bidding_leg_key, is_entry=True)
            if partial_fills > 0:
                print(f"INFO: Partial fills received: {partial_fills} for {bidding_leg_key}")
            
            print(f"WARNING: Pair orders CANCELLED/FAILED for user {uid}: {error_msg}")
            return False

    def _check_pair1_spread_tolerance(self, uid, current_prices):
        """Check if pair1 spread has moved beyond tolerance."""
        if not self.pair1_executed[uid]:
            return False
        
        spread_tolerance = self.params.get("spread_tolerance", 5)
        executed_spread = self.pair1_executed_spread[uid]
        current_pair1_spread = self._calculate_pair_spread(
            self.pair1_bidding_leg, self.pair1_base_leg, current_prices)
        
        spread_movement = current_pair1_spread - executed_spread
        
        if spread_movement > spread_tolerance:
            print(f"WARNING: Pair1 spread moved {spread_movement:.2f} points beyond tolerance for user {uid}")
            return True
        return False

    def _execute_pair1_exit(self, uid):
        """Execute exit orders for pair1 legs using current market prices."""
        try:
            print(f"INFO: Executing pair1 exit for user {uid}")
            
            exit_leg_keys = [self.pair1_bidding_leg, self.pair1_base_leg]
            current_exit_prices = self._get_leg_prices(exit_leg_keys, is_exit=True)
            
            # Create exit orders
            exit_orders = []
            for leg_key in exit_leg_keys:
                exit_order = self.exit_order_templates[uid][leg_key].copy()
                exit_order["Quantity"] = self.entry_qtys[uid][leg_key]
                exit_price = current_exit_prices.get(leg_key, 0)
                exit_order["Limit_Price"] = self._format_limit_price(exit_price)
                exit_orders.append(exit_order)
                print(f"INFO: Exit {leg_key}: qty={exit_order['Quantity']}, price={exit_price:.2f}")
            
            # Place exit orders
            if exit_orders:
                bidding_exit_result = self.order.place_order(exit_orders[0])
                remaining_orders = exit_orders[1:] if len(exit_orders) > 1 else []
                
                # Execute remaining orders using IOC with proper return value check
                if remaining_orders:
                    exit_success, exit_result = self.order.IOC_order(bidding_exit_result, *remaining_orders)
                    if exit_success:
                        print(f"SUCCESS: Exit orders completed successfully for user {uid}")
                        
                        # Update exit quantities using fill data
                        bidding_leg_data = exit_result.get('bidding_leg', {})
                        base_legs_data = exit_result.get('base_legs', [])
                        
                        bidding_filled_qty = bidding_leg_data.get('filled_qty', 0)
                        if bidding_filled_qty > 0:
                            with self.templates_lock:
                                self.exit_qtys[uid][self.pair1_bidding_leg] += bidding_filled_qty
                        
                        # Update base leg exit quantities
                        for base_leg_data in base_legs_data:
                            base_filled_qty = base_leg_data.get('filled_qty', 0)
                            if base_filled_qty > 0:
                                with self.templates_lock:
                                    self.exit_qtys[uid][self.pair1_base_leg] += base_filled_qty
                        
                        print(f"INFO: Exit fills - Bidding: {bidding_filled_qty}, Base legs: {base_legs_data}")
                    else:
                        print(f"WARNING: Exit orders failed or partially filled for user {uid}")
                        error_msg = exit_result.get('error', 'Unknown error') if isinstance(exit_result, dict) else str(exit_result)
                        print(f"INFO: Exit error: {error_msg}")
                        
                        # Check for partial fills on exit using fallback method
                        partial_fills = self._update_filled_quantities(bidding_exit_result, uid, self.pair1_bidding_leg, is_entry=False)
                        if partial_fills > 0:
                            print(f"INFO: Partial exit fills received: {partial_fills}")
                        return False
                else:
                    # Only bidding order, update directly
                    self._update_filled_quantities(bidding_exit_result, uid, self.pair1_bidding_leg, is_entry=False)
            
            return True
            
        except Exception as e:
            print(f"ERROR: Failed to execute pair1 exit for user {uid}: {e}")
            return False

    def _process_user_sequential_box(self, uid, prices):
        """Process sequential box strategy for a single user."""
        try:
            # self._check_and_reset_cancelled_orders(uid)
            
            run_state = int(self.params.get('run_state', 0))
            if run_state == 2:
                return self._execute_full_exit(uid)
            
            # Phase routing based on execution state
            if not self.pair1_executed[uid]:
                return self._process_pair1_phase(uid, prices, run_state)
            elif not self.pair2_executed[uid]:
                return self._process_pair2_phase(uid, prices)
            elif self.all_legs_executed[uid]:
                return self._process_exit_monitoring(uid, prices, run_state)
            
            return {"uid": uid, "action": "monitoring"}
            
        except Exception as e:
            print(f"ERROR: Sequential box processing failed for user {uid}: {e}")
            return {"uid": uid, "error": True}

    def _process_pair1_phase(self, uid, prices, run_state):
        """Handle pair1 execution phase."""
        if not self.pair1_orders_placed[uid]:
            return self._attempt_pair1_entry(uid, prices, run_state)
        else:
            return self._check_pair1_fills(uid, prices)

    def _attempt_pair1_entry(self, uid, prices, run_state):
        """Attempt to enter pair1 if conditions are met."""
        start_price = self.params.get("start_price", 403)
        full_box_spread = self._calculate_box_spread(prices)
        entry_condition = ((full_box_spread < start_price) if self.global_action == "BUY" 
                          else (full_box_spread > start_price))
        
        if not (entry_condition and run_state == 0):
            return {"uid": uid, "action": "waiting_entry_condition", "box_spread": full_box_spread}
        
        # Calculate prices for pair1
        bidding_leg_key = self.params.get("bidding_leg_key", "bidding_leg")
        if self.pair1_bidding_leg == bidding_leg_key:
            bidding_price = self._calculate_bidding_leg_price(prices)
            base_price = prices.get(self.pair1_base_leg, 0)
        else:
            # Calculate pair1_bidding_leg price as if it were the designated bidding leg
            bidding_price = self._calculate_leg_as_bidding_price(prices, self.pair1_bidding_leg)
            base_price = prices.get(self.pair1_base_leg, 0)
        
        print('pair 1 : ',self.pair2_base_leg,self.pair2_bidding_leg)
        success = self._place_pair_orders(uid, self.pair1_bidding_leg, self.pair1_base_leg, 
                                        bidding_price, base_price)
        # breakpoint()
        
        if success:
            self.pair1_orders_placed[uid] = True
            self.pair1_executed[uid] = True
            
            # Use filled prices for spread calculation
            filled_bidding_price = self.pair1_executed_prices.get(uid, {}).get(self.pair1_bidding_leg, bidding_price)
            filled_base_price = self.pair1_executed_prices.get(uid, {}).get(self.pair1_base_leg, base_price)
            self.pair1_executed_spread[uid] = filled_bidding_price + filled_base_price
            
            print("INFO: Pair1 execution details:")
            print(f" - User ID: {uid}")
            print(f" - Calculated Bidding Price: {bidding_price}")
            print(f" - Calculated Base Price: {base_price}")
            print(f" - Filled Bidding Price: {filled_bidding_price}")
            print(f" - Filled Base Price: {filled_base_price}")
            print(f" - Executed Spread (using filled prices): {self.pair1_executed_spread[uid]}")
            breakpoint()
            return {"uid": uid, "action": "pair1_orders_placed", "box_spread": full_box_spread}
        else:
            self.pair1_orders_placed[uid] = False
            print(f"WARNING: Pair1 orders failed for user {uid}")
            return {"uid": uid, "action": "pair1_orders_failed", "box_spread": full_box_spread}

    def _check_pair1_fills(self, uid, prices):
        """Check if pair1 orders have been filled."""
        # breakpoint()
        both_filled, bidding_qty, base_qty = self._check_pair_filled_quantities(
            uid, self.pair1_bidding_leg, self.pair1_base_leg)
        print("Filled quantities : ", bidding_qty, base_qty , both_filled)
        if both_filled:
            self.pair1_executed[uid] = True
            self.pair1_executed_prices[uid] = {
                self.pair1_bidding_leg: prices.get(self.pair1_bidding_leg, 0),
                self.pair1_base_leg: prices.get(self.pair1_base_leg, 0)
            }
            pair1_spread = self._calculate_pair_spread(
                self.pair1_bidding_leg, self.pair1_base_leg, prices)
            self.pair1_executed_spread[uid] = pair1_spread
            
            print(f"INFO: Pair1 FILLED for user {uid} - Bidding: {bidding_qty}, Base: {base_qty}")
            return {"uid": uid, "action": "pair1_filled", "spread": pair1_spread}
        else:
            if bidding_qty > 0 or base_qty > 0:
                print(f"INFO: Partial fill for pair1 - Bidding: {bidding_qty}, Base: {base_qty}")
            return {"uid": uid, "action": "waiting_for_pair1_fills"}

    def _process_pair2_phase(self, uid, prices):
        """Handle pair2 execution phase."""
        if not self.pair2_orders_placed[uid]:
            return self._attempt_pair2_entry(uid, prices)
        else:
            return self._check_pair2_fills(uid, prices)

    def _attempt_pair2_entry(self, uid, prices):
        """Attempt to enter pair2 if conditions are met."""
        # Check tolerance first
        if self._check_pair1_spread_tolerance(uid, prices):
            self._execute_pair1_exit(uid)
            return {"uid": uid, "action": "pair1_exit_tolerance"}
        
        # Calculate pair2 prices
        bidding_leg_key = self.params.get("bidding_leg_key", "bidding_leg")
        desired_spread = self.params.get("desired_spread", 405)
        executed_pair1_spread = self.pair1_executed_spread[uid]
        remaining_spread = executed_pair1_spread - desired_spread
        print("remaining spread : ", remaining_spread)
        # breakpoint()
        # if self.pair2_bidding_leg == bidding_leg_key:
        #     pair2_bidding_price = self._calculate_bidding_leg_price(prices)
        #     pair2_base_price = prices.get(self.pair2_base_leg, 0)
        #     success = self._place_pair_orders(uid, self.pair2_bidding_leg, self.pair2_base_leg,
        #                                     pair2_bidding_price, pair2_base_price)
        # else:
        #     # Calculate pair2 prices using remaining spread formula
        pair2_base_price = prices.get(self.pair2_base_leg, 0)
        pair2_bidding_price = remaining_spread - pair2_base_price
        pair2_bidding_price = max(0.05, abs(pair2_bidding_price))  # Ensure positive price
            
        print(f"INFO: Pair2 pricing - Remaining spread: {remaining_spread:.2f}, Base price: {pair2_base_price:.2f}, Calculated bidding price: {pair2_bidding_price:.2f}")
            
        success = self._place_pair_orders(uid, self.pair2_bidding_leg, self.pair2_base_leg,
                                            pair2_bidding_price, pair2_base_price)
        
        if success:
            self.pair2_orders_placed[uid] = True
            self.pair2_executed[uid] = True
            print(f"INFO: Pair2 orders placed for user {uid}")
            return {"uid": uid, "action": "pair2_orders_placed"}
        else:
            self.pair2_orders_placed[uid] = False
            return {"uid": uid, "action": "pair2_orders_failed"}

    def _calculate_pair2_target_prices(self, uid, prices, remaining_spread):
        """Calculate target prices for pair2 based on remaining spread."""
        pair2_base_price = prices.get(self.pair2_base_leg, 0)
        bidding_action = self.legs[self.pair2_bidding_leg]['info'].get('action', self.global_action).upper()
        base_action = self.legs[self.pair2_base_leg]['info'].get('action', self.global_action).upper()
        
        # Calculate target bidding price based on actions
        if bidding_action == "SELL" and base_action in ["SELL", "BUY"]:
            target_bidding_price = remaining_spread - pair2_base_price
        elif bidding_action == "BUY" and base_action == "SELL":
            target_bidding_price = pair2_base_price - remaining_spread
        else:  # BUY and BUY
            target_bidding_price = -(remaining_spread + pair2_base_price)
        
        return target_bidding_price, pair2_base_price

    def _check_pair2_fills(self, uid, prices):
        """Check if pair2 orders have been filled."""
        both_filled, bidding_qty, base_qty = self._check_pair_filled_quantities(
            uid, self.pair2_bidding_leg, self.pair2_base_leg)
        
        if both_filled:
            self.pair2_executed[uid] = True
            self.all_legs_executed[uid] = True
            box_spread = self._calculate_box_spread(prices)
            print(f"INFO: Pair2 FILLED for user {uid} - Full box COMPLETED")
            return {"uid": uid, "action": "pair2_filled", "box_spread": box_spread}
        else:
            if bidding_qty > 0 or base_qty > 0:
                print(f"INFO: Partial fill for pair2 - Bidding: {bidding_qty}, Base: {base_qty}")
            return {"uid": uid, "action": "waiting_for_pair2_fills"}

    def _process_exit_monitoring(self, uid, prices, run_state):
        """Monitor exit conditions for completed boxes."""
        box_spread = self._calculate_box_spread(prices)
        exit_start = self.params.get("exit_start", 1)
        exit_condition = ((box_spread > exit_start) if self.global_action == "BUY" 
                         else (box_spread < exit_start))
        
        if exit_condition or run_state == 2:
            return self._execute_full_exit(uid)
        
        return {"uid": uid, "action": "monitoring", "box_spread": box_spread}

    def _execute_full_exit(self, uid):
        """Execute exit orders for all legs using current market prices."""
        try:
            print(f"INFO: Executing full exit for user {uid}")
            
            all_leg_keys = list(self.legs.keys())
            current_exit_prices = self._get_leg_prices(all_leg_keys, is_exit=True)
            
            exit_orders = []
            for leg_key in all_leg_keys:
                entry_qty = self.entry_qtys[uid][leg_key]
                exit_qty = self.exit_qtys[uid][leg_key]
                remaining_qty = entry_qty - exit_qty
                
                if remaining_qty > 0:
                    exit_order = self.exit_order_templates[uid][leg_key].copy()
                    exit_order["Quantity"] = remaining_qty
                    exit_price = current_exit_prices.get(leg_key, 0)
                    exit_order["Limit_Price"] = self._format_limit_price(exit_price)
                    exit_orders.append(exit_order)
                    print(f"INFO: Exit {leg_key}: qty={remaining_qty}, price={exit_price:.2f}")
            
            # Place all exit orders
            for order in exit_orders:
                self.order.place_order(order)
            
            return {"uid": uid, "action": "full_exit"}
            
        except Exception as e:
            print(f"ERROR: Failed to execute full exit for user {uid}: {e}")
            return {"uid": uid, "error": True}

    def _safe_get_total_quantities(self):
        """Get total quantities across all users and legs."""
        try:
            total_entry = sum(sum(user_qtys.values()) for user_qtys in self.entry_qtys.values())
            total_exit = sum(sum(user_qtys.values()) for user_qtys in self.exit_qtys.values())
            run_state_val = int(self.params.get('run_state', 0))
            return total_entry, total_exit, run_state_val
        except Exception as e:
            print(f"ERROR: Error calculating total quantities: {e}")
            return 0, 0, 0

    # --- main logic -----------------------------------
    def main_logic(self):
        """Main logic for sequential box strategy."""
        while True:
            try:
                if int(self.params.get('run_state', 0)) == 1:
                    time.sleep(0.1)
                    continue
                
                # Get current prices and validate
                all_leg_keys = list(self.legs.keys())
                leg_prices = self._get_leg_prices(all_leg_keys)
                # print("leg prices : ",leg_prices)
                if sum(leg_prices.values()) <= 0:
                    print(f"ERROR: Invalid leg prices: {leg_prices}")
                    time.sleep(0.1)
                    continue
                # breakpoint()
                # Display current state
                box_spread = self._calculate_box_spread(leg_prices)
                print(f"Box Spread: {round(box_spread, 2)} | Leg Prices: {leg_prices} \r", end="")
               
                # Process users if available
                if hasattr(self, "uids") and self.uids:
                    self._process_all_users(leg_prices)
                    
                    # Check exit condition
                    total_entry, total_exit, run_state_val = self._safe_get_total_quantities()
                    if total_entry == total_exit and run_state_val == 2:
                        break
                
            except redis.RedisError as e:
                print(f"ERROR: redis error in main loop: {e}")
                time.sleep(0.5)
            except (KeyError, IndexError, TypeError, AttributeError, ValueError, orjson.JSONDecodeError) as e:
                print(f"WARNING: transient data error in main loop: {e}")
                time.sleep(0.1)
            except KeyboardInterrupt:
                print("INFO: KeyboardInterrupt received, exiting")
                os._exit(0)
            except Exception as e:
                print(f"ERROR: unexpected error in main loop: {e}")
                print(traceback.format_exc())
                time.sleep(0.1)

    def _process_all_users(self, leg_prices):
        """Process all users concurrently."""
        futures = {
            self.executor.submit(self._process_user_sequential_box, uid, leg_prices): uid
            for uid in self.uids
        }
        
        for fut in as_completed(futures):
            try:
                result = fut.result()
                # Optional: Process result if needed
            except Exception as e:
                uid = futures[fut]
                print(f"ERROR: Sequential box task failed for user {uid}: {e}")
