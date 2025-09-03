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

from constants.duration import DurationEnum

# Direct IOC Box Strategy - Execute legs in pairs with direct IOC orders using bid/ask pricing


class StratergyDirectIOCBox:
    def __init__(self, paramsid) -> None:
        # Redis connection
        self.r = redis.Redis(host="localhost", port=6379, db=0)
        
        # Load configuration data
        self.lot_sizes = json.loads(self.r.get("lotsizes"))
        self._init_user_connections()
        
        # Thread management
        self.templates_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=5)

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
                    bid_or_ask = "askValues" if leg_action == "BUY" else "bidValues"
                else:
                    bid_or_ask = "bidValues" if leg_action == "BUY" else "askValues"
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
    
    def _analyze_price_trend(self, price_list):
        """Analyze price trend from list of prices to determine if increasing or decreasing."""
        if len(price_list) < 2:
            return "STABLE", 0
        
        # Calculate overall trend from first to last
        total_change = price_list[-1] - price_list[0]
        
        # Calculate weighted trend (recent prices have more weight)
        weighted_sum = 0
        weight_total = 0
        for i in range(1, len(price_list)):
            change = price_list[i] - price_list[i-1]
            weight = i  # Later changes get higher weight
            weighted_sum += change * weight
            weight_total += weight
        
        weighted_trend = weighted_sum / weight_total if weight_total > 0 else 0
        
        # Determine trend direction
        if weighted_trend > 0.02:  # More than 2 paise increase
            return "INCREASING", weighted_trend
        elif weighted_trend < -0.02:  # More than 2 paise decrease
            return "DECREASING", weighted_trend
        else:
            return "STABLE", weighted_trend

    def _observe_market_for_pair1(self, uid, leg1_key, leg2_key, observation_duration=5):
        """Observe market for a specified duration and collect price data for analysis."""
        print(f"INFO: Starting {observation_duration}-second market observation for pair1: {leg1_key}, {leg2_key}")

        leg1_prices = []
        leg2_prices = []
        start_time = time.time()
        # observation_duration = 5  # 5 seconds
        
        # Collect prices in a loop for 5 seconds
        while time.time() - start_time < observation_duration:
            current_prices = self._get_leg_prices([leg1_key, leg2_key])
            leg1_price = current_prices.get(leg1_key, 0)
            leg2_price = current_prices.get(leg2_key, 0)
            
            leg1_prices.append(leg1_price)
            leg2_prices.append(leg2_price)
            
            elapsed = time.time() - start_time
            print(f"\rObserving [{elapsed:.1f}s]: {leg1_key}={leg1_price:.2f}, {leg2_key}={leg2_price:.2f}", end='')
            time.sleep(0.2)  # Check every 200ms
        
        print()  # New line after observation
        
        # Analyze trends
        leg1_trend, leg1_change = self._analyze_price_trend(leg1_prices)
        leg2_trend, leg2_change = self._analyze_price_trend(leg2_prices)
        
        print(f"INFO: Market analysis complete:")
        print(f"  - {leg1_key}: {leg1_prices[0]:.2f} → {leg1_prices[-1]:.2f} [{leg1_trend}] (change: {leg1_change:+.4f})")
        print(f"  - {leg2_key}: {leg2_prices[0]:.2f} → {leg2_prices[-1]:.2f} [{leg2_trend}] (change: {leg2_change:+.4f})")
        
        # Determine order sequence - place increasing leg first
        if leg1_change > leg2_change:
            first_leg, second_leg = leg1_key, leg2_key
            print(f"INFO: {leg1_key} showing stronger upward movement, placing it FIRST")
        elif leg2_change > leg1_change:
            first_leg, second_leg = leg2_key, leg1_key
            print(f"INFO: {leg2_key} showing stronger upward movement, placing it FIRST")
        else:
            # Equal trends, use original order
            first_leg, second_leg = leg1_key, leg2_key
            print(f"INFO: Equal trends, using original order")
        
        return {
            'first_leg': first_leg,
            'second_leg': second_leg,
            'price_data': {
                leg1_key: leg1_prices,
                leg2_key: leg2_prices
            },
            'trends': {
                leg1_key: {'trend': leg1_trend, 'change': leg1_change},
                leg2_key: {'trend': leg2_trend, 'change': leg2_change}
            },
            'final_prices': {
                leg1_key: leg1_prices[-1],
                leg2_key: leg2_prices[-1]
            }
        }
    
    def _place_single_order(self, uid, leg_key, leg_price):
        """Place a single order for a leg."""
        try:
            new_price = self._get_leg_prices([leg_key])
            print(new_price)
            order = self.order_templates[uid][leg_key].copy()
            tick = 0.05 if order.get("Action") == ActionEnum.BUY else -0.05
            order["Limit_Price"] = self._format_limit_price(float(new_price[leg_key]) + tick)
            
            print(f"INFO: Placing order for {leg_key} at price {order['Limit_Price']}")
            result = self.order.place_order(order)
            
            if result:
                # Start modification monitoring
                success = self.modify_till_complete(leg_key, result, uid, tick)
                if success:
                    print(f"SUCCESS: Order for {leg_key} completed successfully")
                    return True
                else:
                    print(f"WARNING: Order modification failed for {leg_key}")
                    return False
            else:
                print(f"ERROR: Failed to place order for {leg_key}")
                return False
                
        except Exception as e:
            print(f"ERROR: Failed to place single order for {leg_key}: {e}")
            return False

    def _execute_both_pairs(self, uid, prices):
        """Execute both pairs - pair1 with market observation, then pair2 directly."""
        try:
            print(f"\n=== EXECUTING BOTH PAIRS FOR USER {uid} ===")
            
            # Step 1: Market observation for pair1 (both BUY orders)
            observation_result1 = self._observe_market_for_pair1(uid, self.pair1_bidding_leg, self.pair1_base_leg,10)
            
            first_leg = observation_result1['first_leg']
            second_leg = observation_result1['second_leg']
            final_prices = observation_result1['final_prices']
            
            observation_result2 = self._observe_market_for_pair1(uid, self.pair1_bidding_leg, self.pair1_base_leg,2)
            first_leg_2 = observation_result2['first_leg']
            second_leg_2 = observation_result2['second_leg']
            final_prices_2 = observation_result2['final_prices']
            # Step 2: Place pair1 orders sequentially based on observation
            
            print(f"\n--- PAIR 1 EXECUTION (Sequential based on market observation) ---")
            print(f"Order sequence: FIRST={first_leg} , prices : {final_prices[first_leg]}, SECOND={second_leg}")
            print(f"Order sequence 2: FIRST={first_leg_2} , prices : {final_prices_2[first_leg_2]}, SECOND={second_leg_2}")

           # Place first leg of pair1
            if first_leg != first_leg_2 or second_leg != second_leg_2:
                print(f"WARNING: Market conditions changed for pair1, aborting execution.")
                # breakpoint()
                return False
            
            # breakpoint()
            first_success = self._place_single_order(uid, first_leg, final_prices[first_leg])
            
            if first_success:
                print(f"SUCCESS: First leg ({first_leg}) completed, placing second leg...")
                
                # Place second leg of pair1
                second_success = self._place_single_order(uid, second_leg, final_prices[second_leg])
                
                if second_success:
                    print(f"SUCCESS: Pair1 completely executed")
                    
                    # Update pair1 tracking
                    self.pair1_orders_placed[uid] = True
                    self.pair1_executed[uid] = True
                    
                    # Calculate pair1 executed spread using filled prices
                    filled_bidding_price = self.pair1_executed_prices.get(uid, {}).get(self.pair1_bidding_leg, final_prices[self.pair1_bidding_leg])
                    filled_base_price = self.pair1_executed_prices.get(uid, {}).get(self.pair1_base_leg, final_prices[self.pair1_base_leg])
                    self.pair1_executed_spread[uid] = filled_bidding_price + filled_base_price
                    
                    print(f"INFO: Pair1 executed spread: {self.pair1_executed_spread[uid]:.2f}")
                    
                    # Step 3: Execute pair2 directly (no market observation needed for SELL orders)
                    print(f"\n--- PAIR 2 EXECUTION (Direct execution - SELL orders) ---")
                    
                    # Calculate pair2 prices
                    desired_spread = self.params.get("desired_spread", 405)
                    executed_pair1_spread = self.pair1_executed_spread[uid]
                    remaining_spread = executed_pair1_spread - desired_spread
                    
                    prices = self._get_leg_prices([self.pair2_bidding_leg, self.pair2_base_leg])
                    pair2_base_price = prices.get(self.pair2_base_leg, 0)
                    pair2_bidding_price = remaining_spread - pair2_base_price
                    pair2_bidding_price = max(0.05, abs(pair2_bidding_price))  # Ensure positive price
                    
                    print(f"INFO: Pair2 pricing - Remaining spread: {remaining_spread:.2f}")
                    print(f"INFO: Pair2 base price: {pair2_base_price:.2f}, calculated bidding price: {pair2_bidding_price:.2f}")
                    breakpoint()
                    # Place pair2 orders simultaneously (SELL orders don't need market observation)
                    pair2_success = self._place_pair_orders_original(uid, self.pair2_bidding_leg, self.pair2_base_leg, 
                                                                   pair2_bidding_price, pair2_base_price)
                    
                    if pair2_success:
                        print(f"SUCCESS: Pair2 orders placed and executed")
                        self.pair2_orders_placed[uid] = True
                        self.pair2_executed[uid] = True
                        self.all_legs_executed[uid] = True
                        
                        print(f"SUCCESS: ALL LEGS EXECUTED FOR USER {uid}")
                        return True
                    else:
                        print(f"ERROR: Pair2 execution failed")
                        return False
                else:
                    print(f"ERROR: Second leg of pair1 failed")
                    return False
            else:
                print(f"ERROR: First leg of pair1 failed")
                return False
                
        except Exception as e:
            print(f"ERROR: Failed to execute both pairs for user {uid}: {e}")
            traceback.print_exc()
            return False

    def _place_pair_orders_original(self, uid, bidding_leg_key, base_leg_key, bidding_leg_price, base_leg_price):
        """Original simultaneous pair order placement method for pair2."""
        try:
            # Prepare orders
            bidding_order = self.order_templates[uid][bidding_leg_key].copy()
            base_order = self.order_templates[uid][base_leg_key].copy()
            
            # Determine tick based on action (ActionEnum comparison)
            bidding_tick = 0.05 if bidding_order.get("Action") == ActionEnum.BUY else -0.05
            base_tick = 0.05 if base_order.get("Action") == ActionEnum.BUY else -0.05
            
            # Set prices with tick adjustment
            bidding_order["Limit_Price"] = self._format_limit_price(bidding_leg_price + bidding_tick)
            base_order["Limit_Price"] = self._format_limit_price(base_leg_price + base_tick)

            # Place both orders
            print(f"INFO: Placing simultaneous orders for pair2")
            print(f"  - Bidding order limit price: {bidding_order['Limit_Price']}")
            print(f"  - Base order limit price: {base_order['Limit_Price']}")
           
            bidding_result = self.order.place_order(bidding_order)
            base_result = self.order.place_order(base_order)
            
            if not bidding_result or not base_result:
                print(f"ERROR: Failed to place one or both orders")
                return False

            # Submit both orders for modification monitoring
            futures = {
                self.executor.submit(self.modify_till_complete, bidding_leg_key, bidding_result, uid, bidding_tick),
                self.executor.submit(self.modify_till_complete, base_leg_key, base_result, uid, base_tick)
            }

            # Wait for both orders to complete
            success_count = 0
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        success_count += 1
                        print(f"INFO: Order modification completed successfully")
                    else:
                        print(f"WARNING: Order modification failed")
                except Exception as e:
                    print(f"ERROR: Failed to modify order: {e}")

            # Return True only if both orders completed successfully
            return success_count == 2
            
        except Exception as e:
            print(f"ERROR: Failed to place original pair orders for user {uid}: {e}")
            return False
        
    def modify_till_complete(self, leg_key, result, uid, tick):
        """Modify orders until they are complete."""
        prev_limit_price = result.get('Limit_Price', '0')
        
        while True: 
            order_details = self.order.get_order_status(result.get("order_id", ""),uid, result.get("remark", "Lord_Shreeji"))
            if order_details.get("order_status") == "complete":
                print(f"INFO: Order {order_details.get('order_id')} completed.")
                # Update fill quantities and prices
                filled_qty = int(order_details.get('filled_qty', 0))
                avg_price = float(order_details.get('filled_price', 0.0))
                
                if filled_qty > 0:
                    with self.templates_lock:
                        self.entry_qtys[uid][leg_key] += filled_qty
                    # Store executed price
                    self._update_executed_prices(uid, leg_key, avg_price, is_entry=True)
                
                return True
            else:
                prices = self._get_leg_prices(list(self.legs.keys()))
                new_price = self._format_limit_price(prices.get(leg_key, 0) + tick)
                print("New price while update : ",new_price)
                if new_price != prev_limit_price:
                    # Prepare modification parameters
                    modify_details = {
                        "user_id": uid,
                        "Trading_Symbol": result.get("Trading_Symbol"),
                        "Exchange": result.get("Exchange"),
                        "Action": result.get("Action"),
                        "Duration": DurationEnum.DAY,  # Use DAY for modify
                        "Order_Type": result.get("Order_Type"),
                        "Quantity": result.get("Quantity"),
                        "CurrentQuantity": result.get("Quantity"),
                        "Streaming_Symbol": result.get("Streaming_Symbol"),
                        "Limit_Price": new_price,
                        "Order_ID": result.get("order_id"),
                        "Disclosed_Quantity": result.get("Disclosed_Quantity", "0"),
                        "TriggerPrice": result.get("TriggerPrice", "0"),
                        "ProductCode": result.get("ProductCode")
                    }
                    
                    modify_result = self.order.modify_order(modify_details)
                    if modify_result.get("status") == "success":
                        result['Limit_Price'] = new_price
                        prev_limit_price = new_price
                        print(f"INFO: Modified order {result.get('order_id')} to price {new_price}")
                    else:
                        print(f"WARNING: Failed to modify order: {modify_result.get('message', 'Unknown error')}")
            time.sleep(0.25)

    def _place_pair_orders(self, uid, bidding_leg_key, base_leg_key, bidding_leg_price, base_leg_price):
        """Redirect to new execution approach - this method should not be called directly."""
        print(f"WARNING: _place_pair_orders called directly - use _execute_both_pairs instead")
        print(f"INFO: Executing both pairs using new market observation logic")
        
        # Get current prices for all legs
        all_leg_keys = list(self.legs.keys())
        current_prices = self._get_leg_prices(all_leg_keys)
        
        # Execute using the new combined approach
        return self._execute_both_pairs(uid, current_prices)

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
        """Process sequential box strategy for a single user - simplified version."""
        try:
            run_state = int(self.params.get('run_state', 0))
            
            # If exit state, execute full exit
            if run_state == 2:
                return self._execute_full_exit(uid)
            
            # If both pairs not executed and entry condition met
            if not self.all_legs_executed[uid] and run_state == 0:
                success = self._execute_both_pairs(uid, prices)
                if success:
                    return {"uid": uid, "action": "all_pairs_executed"}
                else:
                    return {"uid": uid, "action": "pairs_execution_failed"}
            
            # If all legs executed, monitor for exit
            elif self.all_legs_executed[uid]:
                # return self._process_exit_monitoring(uid, prices, run_state)
                pass
            
            return {"uid": uid, "action": "waiting"}
            
        except Exception as e:
            print(f"ERROR: Sequential box processing failed for user {uid}: {e}")
            traceback.print_exc()
            return {"uid": uid, "error": True}

    def _execute_full_exit(self, uid):
        """Execute exit orders for all legs using current market prices."""
        try:
            print(f"INFO: Executing full exit for user {uid}")
            
            all_leg_keys = list(self.legs.keys())
            current_exit_prices = self._get_leg_prices(all_leg_keys, is_exit=True)
            
            exit_orders = []
            for leg_key in all_leg_keys:
                entry_qty = self.entry_qtys[uid].get(leg_key, 0)
                exit_qty = self.exit_qtys[uid].get(leg_key, 0)
                remaining_qty = entry_qty - exit_qty
                
                if remaining_qty > 0:
                    exit_order = self.exit_order_templates[uid][leg_key].copy()
                    exit_order["Quantity"] = remaining_qty
                    exit_price = current_exit_prices.get(leg_key, 0)
                    exit_order["Limit_Price"] = self._format_limit_price(exit_price)
                    exit_orders.append(exit_order)
                    print(f"INFO: Exit {leg_key}: qty={remaining_qty}, price={exit_price:.2f}")
            
            if not exit_orders:
                print(f"INFO: No remaining positions to exit for user {uid}")
                return {"uid": uid, "action": "no_positions_to_exit"}
            
            # Place all exit orders
            for order in exit_orders:
                result = self.order.place_order(order)
                if result:
                    print(f"INFO: Exit order placed successfully for {order.get('Trading_Symbol')}")
                else:
                    print(f"WARNING: Failed to place exit order for {order.get('Trading_Symbol')}")
            
            # Update exit quantities (simplified approach)
            for leg_key in all_leg_keys:
                entry_qty = self.entry_qtys[uid].get(leg_key, 0)
                if entry_qty > 0:
                    with self.templates_lock:
                        self.exit_qtys[uid][leg_key] = entry_qty
            
            return {"uid": uid, "action": "full_exit_completed"}
            
        except Exception as e:
            print(f"ERROR: Failed to execute full exit for user {uid}: {e}")
            traceback.print_exc()
            return {"uid": uid, "error": True}

    def _process_exit_monitoring(self, uid, prices, run_state):
        """Monitor exit conditions for completed boxes."""
        try:
            box_spread = self._calculate_box_spread(prices)
            exit_start = self.params.get("exit_start", 1)
            exit_condition = ((box_spread > exit_start) if self.global_action == "BUY" 
                             else (box_spread < exit_start))
            
            if exit_condition or run_state == 2:
                return self._execute_full_exit(uid)
            
            return {"uid": uid, "action": "monitoring", "box_spread": box_spread}
        except Exception as e:
            print(f"ERROR: Failed to process exit monitoring for user {uid}: {e}")
            return {"uid": uid, "error": True}

    def _safe_get_total_quantities(self):
        """Get total quantities across all users and legs."""
        try:
            total_entry = sum(sum(user_qtys.values()) for user_qtys in self.entry_qtys.values())
            total_exit = sum(sum(user_qtys.values()) for user_qtys in self.exit_qtys.values())
            run_state_val = int(self.params.get('run_state', 0))
            return total_entry, total_exit, run_state_val
        except Exception as e:
            print(f"ERROR: Failed to calculate total quantities: {e}")
            return 0, 0, 0

    # --- main logic -----------------------------------
        """Handle pair1 execution phase."""
        if not self.pair1_orders_placed[uid]:
            return self._attempt_pair1_entry(uid, prices, run_state)
        else:
            return self._check_pair1_fills(uid, prices)

    def _attempt_pair1_entry(self, uid, prices, run_state):
        """Attempt to enter pair1 if conditions are met."""
        
        # if not (entry_condition and run_state == 0):
        if not run_state == 0:
            return {"uid": uid, "action": "waiting_entry_condition"}
        
        bidding_price = prices.get(self.pair1_bidding_leg, 0)
        base_price = prices.get(self.pair1_base_leg, 0)
        
        bidding_price_p2 = prices.get(self.pair2_bidding_leg,0)
        base_price_p2 = prices.get(self.pair2_base_leg,0)
        success = self._place_pair_orders(uid, self.pair1_bidding_leg, self.pair1_base_leg, 
                                           bidding_price, base_price)
        success_pair_2 = self._place_pair_orders(uid, self.pair2_bidding_leg, self.pair2_base_leg, 
                                                  bidding_price_p2, base_price_p2)
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
            return {"uid": uid, "action": "pair1_orders_placed"}
        else:
            self.pair1_orders_placed[uid] = False
            print(f"WARNING: Pair1 orders failed for user {uid}")
            return {"uid": uid, "action": "pair1_orders_failed"}

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

    def _process_exit_monitoring(self, uid, prices, run_state):
        """Monitor exit conditions for completed boxes."""
        try:
            box_spread = self._calculate_box_spread(prices)
            exit_start = self.params.get("exit_start", 1)
            exit_condition = ((box_spread > exit_start) if self.global_action == "BUY" 
                             else (box_spread < exit_start))
            
            if exit_condition or run_state == 2:
                return self._execute_full_exit(uid)
            
            return {"uid": uid, "action": "monitoring", "box_spread": box_spread}
        except Exception as e:
            print(f"ERROR: Failed to process exit monitoring for user {uid}: {e}")
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

    def _safe_get_total_quantities(self):
        """Safely calculate total quantities across all users."""
        try:
            total_entry = sum(sum(user_qtys.values()) for user_qtys in self.entry_qtys.values())
            total_exit = sum(sum(user_qtys.values()) for user_qtys in self.exit_qtys.values())
            run_state_val = int(self.params.get('run_state', 0))
            return total_entry, total_exit, run_state_val
        except Exception as e:
            print(f"ERROR: Failed to calculate total quantities: {e}")
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
                
                if sum(leg_prices.values()) <= 0:
                    print(f"ERROR: Invalid leg prices: {leg_prices}")
                    time.sleep(0.1)
                    continue
                
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
