"""
Strategy Helper Functions for DirectIOCBox
Contains all static/utility functions that don't depend on strategy execution state
"""

import os
import redis
import json
import orjson
import time
import threading
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from APIConnect.APIConnect import APIConnect
from .order_class import Orders
from constants.exchange import ExchangeEnum
from constants.action import ActionEnum
from constants.order_type import OrderTypeEnum
from constants.product_code import ProductCodeENum
from constants.duration import DurationEnum


class StrategyHelpers:
    """Static helper functions for strategy operations"""
    
    @staticmethod
    def format_limit_price(price):
        """Format price to ensure it's never negative and rounded properly."""
        return str(round(max(0.05, abs(price)) * 20) / 20)
    
    @staticmethod
    def analyze_price_trend(price_list):
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


class StrategyDataHelpers:
    """Helper functions for data operations and Redis interactions"""
    
    def __init__(self, redis_client):
        self.r = redis_client
    
    def depth_from_redis(self, streaming_symbol: str):
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

    def create_depth_key(self, leg_info):
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

    def load_leg_data(self, leg_key, leg_info):
        """Load and validate leg data from Redis."""
        depth_key = self.create_depth_key(leg_info)
        leg_data = self.depth_from_redis(depth_key)
        
        if leg_data is None:
            print(f"ERROR: {leg_key} depth not found: {depth_key}")
            raise RuntimeError(f"{leg_key} depth missing in redis")
        
        return {'data': leg_data, 'info': leg_info, 'depth_key': depth_key}


class StrategyPricingHelpers:
    """Helper functions for pricing calculations"""
    
    def __init__(self, params):
        self.params = params
    
    def safe_get_price(self, data, side_key):
        """Safe price extraction with proper None checking."""
        try:
            if not (data and data.get("response", {}).get("data", {}).get(side_key)):
                return 0.0
            
            pricing_method = self.params.get("pricing_method", "average")
            
            if pricing_method == "depth":
                depth_index = self.params.get("depth_index", 1)
                return self.depth_price(data, side_key, depth_index)
            
            no_of_average = self.params.get("no_of_bidask_average", 1)
            if no_of_average > 1:
                return self.avg_price(data, side_key, no_of_average)
            
            return float(data["response"]["data"][side_key][0]["price"])
        except (KeyError, IndexError, TypeError, ValueError):
            return 0.0

    def depth_price(self, data, side_key, depth_index):
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

    def avg_price(self, data, side_key, n):
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


class StrategyCalculationHelpers:
    """Helper functions for spread and quantity calculations"""
    
    def __init__(self, params, legs, global_action):
        self.params = params
        self.legs = legs
        self.global_action = global_action
    
    def calculate_spread(self, leg_keys, prices, action_factor=1):
        """Generic spread calculation method."""
        spread = 0
        for leg_key in leg_keys:
            leg_action = self.legs[leg_key]['info'].get('action', self.global_action).upper()
            leg_price = prices.get(leg_key, 0)
            spread += leg_price * action_factor if leg_action == "BUY" else -leg_price * action_factor
        return abs(spread)

    def calculate_pair_spread(self, leg1_key, leg2_key, prices):
        """Calculate spread for a pair of legs considering their actions."""
        return self.calculate_spread([leg1_key, leg2_key], prices)

    def calculate_box_spread(self, prices):
        """Calculate the full box spread for all legs."""
        return self.calculate_spread(list(self.legs.keys()), prices)

    def calculate_bidding_leg_price(self, current_prices, bidding_leg_key):
        """Calculate the bidding leg price based on desired spread formula."""
        try:
            desired_spread = self.params.get("desired_spread", 405)
            
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

    def calculate_leg_as_bidding_price(self, current_prices, target_leg_key):
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


class StrategyOrderHelpers:
    """Helper functions for order creation and management"""
    
    def __init__(self, params, option_mapper, exchange):
        self.params = params
        self.option_mapper = option_mapper
        self.exchange = exchange
    
    def make_order_template(self, leg_obj, buy_if="BUY", user_id=None, leg_key=None, quantity=None):
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


class StrategyTrackingHelpers:
    """Helper functions for order tracking and quantity management"""
    
    def __init__(self, redis_client, templates_lock):
        self.r = redis_client
        self.templates_lock = templates_lock
    
    def check_and_reset_cancelled_orders(self, uid, pair1_orders_placed, pair2_orders_placed, 
                                       pair1_executed, pair2_executed,
                                       pair1_bidding_leg, pair1_base_leg, 
                                       pair2_bidding_leg, pair2_base_leg):
        """Check for cancelled orders and reset placement flags accordingly."""
        try:
            pattern = f"order_tracking:{uid}:*"
            tracking_keys = self.r.keys(pattern)
            
            for key in tracking_keys:
                tracking_data = self.r.get(key)
                if tracking_data:
                    data = orjson.loads(tracking_data.decode())
                    if data.get('status') == 'CANCELLED':
                        bidding_leg = data.get('bidding_leg')
                        self.handle_cancelled_order(uid, bidding_leg, pair1_orders_placed, 
                                                  pair2_orders_placed, pair1_bidding_leg, 
                                                  pair2_bidding_leg)
            
            # Check for stale placement flags
            self.check_stale_flags(uid, pair1_executed, pair1_orders_placed, 
                                 pair2_executed, pair2_orders_placed,
                                 pair1_bidding_leg, pair1_base_leg,
                                 pair2_bidding_leg, pair2_base_leg)
            
        except Exception as e:
            print(f"ERROR: Failed to check cancelled orders for user {uid}: {e}")

    def handle_cancelled_order(self, uid, bidding_leg, pair1_orders_placed, pair2_orders_placed,
                             pair1_bidding_leg, pair2_bidding_leg):
        """Handle specific cancelled order by resetting appropriate flags."""
        if bidding_leg == pair1_bidding_leg and pair1_orders_placed.get(uid, False):
            pair1_orders_placed[uid] = False
            print(f"INFO: Reset pair1 placement flag for user {uid} due to cancellation")
        elif bidding_leg == pair2_bidding_leg and pair2_orders_placed.get(uid, False):
            pair2_orders_placed[uid] = False
            print(f"INFO: Reset pair2 placement flag for user {uid} due to cancellation")

    def check_stale_flags(self, uid, pair1_executed, pair1_orders_placed, 
                         pair2_executed, pair2_orders_placed,
                         pair1_bidding_leg, pair1_base_leg,
                         pair2_bidding_leg, pair2_base_leg):
        """Check and reset stale order placement flags."""
        pairs_to_check = [
            (pair1_executed, pair1_orders_placed, pair1_bidding_leg, pair1_base_leg),
            (pair2_executed, pair2_orders_placed, pair2_bidding_leg, pair2_base_leg)
        ]
        
        for executed_dict, orders_placed_dict, bidding_leg, base_leg in pairs_to_check:
            if orders_placed_dict.get(uid, False) and not executed_dict.get(uid, False):
                if not self.has_active_tracking(uid, bidding_leg):
                    orders_placed_dict[uid] = False
                    print(f"INFO: Reset stale placement flag for {bidding_leg}")

    def has_active_tracking(self, uid, bidding_leg):
        """Check if there are active tracking keys for a specific pair."""
        active_keys = self.r.keys(f"order_tracking:{uid}:*")
        for key in active_keys:
            tracking_data = self.r.get(key)
            if tracking_data:
                data = orjson.loads(tracking_data.decode())
                if data.get('bidding_leg') == bidding_leg and data.get('status') == 'ACTIVE':
                    return True
        return False

    def update_executed_prices(self, uid, leg_key, fill_price, pair1_executed_prices, 
                             exit_qtys, is_entry=True):
        """Update executed prices for tracking."""
        try:
            if is_entry:
                if uid not in pair1_executed_prices:
                    pair1_executed_prices[uid] = {}
                pair1_executed_prices[uid][leg_key] = fill_price
            else:
                if uid not in exit_qtys:
                    exit_qtys[uid] = {}
                exit_qtys[uid][leg_key] = fill_price
        except Exception as e:
            print(f"ERROR: Failed to update executed prices: {e}")

    def update_filled_quantities(self, order, uid, leg_key, entry_qtys, exit_qtys, is_entry=True):
        """Update filled quantities for a specific leg."""
        order_id = order.get('order_id', '')
        last_key = f"order:{order['user_id']}{order['remark']}{order_id}"
        last_raw = self.r.get(last_key)
        
        if last_raw:
            order_data = orjson.loads(last_raw.decode())
            filled = int(order_data["response"]["data"]["fQty"])
            
            with self.templates_lock:
                if is_entry:
                    if uid not in entry_qtys:
                        entry_qtys[uid] = {}
                    entry_qtys[uid][leg_key] = filled
                else:
                    if uid not in exit_qtys:
                        exit_qtys[uid] = {}
                    exit_qtys[uid][leg_key] = filled
            
            return filled
        return 0

    def check_pair_filled_quantities(self, uid, leg1_key, leg2_key, entry_qtys):
        """Check if both legs of a pair have filled quantities."""
        leg1_qty = entry_qtys[uid].get(leg1_key, 0)
        leg2_qty = entry_qtys[uid].get(leg2_key, 0)
        both_filled = leg1_qty > 0 and leg2_qty > 0
        return both_filled, leg1_qty, leg2_qty


class StrategyLoggingHelpers:
    """Enhanced logging with colors and formatting for strategy execution"""
    
    # Color constants
    COLORS = {
        'ERROR': '\033[91m',      # Red
        'WARNING': '\033[93m',    # Yellow
        'INFO': '\033[94m',       # Blue
        'DEBUG': '\033[96m',      # Cyan
        'SUCCESS': '\033[92m',    # Green
        'RESET': '\033[0m',       # Reset
        'BOLD': '\033[1m',        # Bold
    }
    
    @staticmethod
    def _get_timestamp():
        """Get formatted timestamp for logging"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    
    @staticmethod
    def _format_message(level, message, details=None):
        """Format message with timestamp and color"""
        timestamp = StrategyLoggingHelpers._get_timestamp()
        color = StrategyLoggingHelpers.COLORS.get(level, '')
        reset = StrategyLoggingHelpers.COLORS['RESET']
        bold = StrategyLoggingHelpers.COLORS['BOLD']
        
        formatted = f"{color}{bold}[{timestamp}] [{level}]{reset} {color}{message}{reset}"
        if details:
            formatted += f"\n{color}  └─ Details: {details}{reset}"
        
        return formatted
    
    @staticmethod
    def error(message, details=None, exception=None):
        """Log error message in red"""
        if exception:
            details = f"{details} | Exception: {str(exception)}" if details else f"Exception: {str(exception)}"
        print(StrategyLoggingHelpers._format_message('ERROR', message, details))
    
    @staticmethod
    def warning(message, details=None):
        """Log warning message in yellow"""
        print(StrategyLoggingHelpers._format_message('WARNING', message, details))
    
    @staticmethod
    def info(message, details=None):
        """Log info message in blue"""
        print(StrategyLoggingHelpers._format_message('INFO', message, details))
    
    @staticmethod
    def debug(message, details=None):
        """Log debug message in cyan"""
        print(StrategyLoggingHelpers._format_message('DEBUG', message, details))
    
    @staticmethod
    def success(message, details=None):
        """Log success message in green"""
        print(StrategyLoggingHelpers._format_message('SUCCESS', message, details))
    
    @staticmethod
    def separator(title=None):
        """Print a separator line with optional title"""
        line = "=" * 80
        color = StrategyLoggingHelpers.COLORS['INFO']
        reset = StrategyLoggingHelpers.COLORS['RESET']
        
        if title:
            print(f"\n{color}{line}")
            print(f"{'=' * 30} {title.upper()} {'=' * (49 - len(title))}")
            print(f"{line}{reset}\n")
        else:
            print(f"{color}{line}{reset}")


class StrategyExecutionTracker:
    """Track strategy execution details and store in Redis with datetime keys"""
    
    def __init__(self, redis_connection, strategy_name="DirectIOCBox"):
        self.redis_conn = redis_connection
        self.strategy_name = strategy_name
        self.execution_id = None
        self.execution_key = None
        self.start_time = None
        self.execution_data = {}
        
    def start_execution(self, params_id, case_type=None):
        """Start a new execution tracking session"""
        self.start_time = datetime.now()
        self.execution_id = self.start_time.strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self.execution_key = f"strategy_execution:{self.strategy_name}:{self.execution_id}"
        
        self.execution_data = {
            "execution_id": self.execution_id,
            "strategy_name": self.strategy_name,
            "params_id": params_id,
            "case_type": case_type,
            "start_time": self.start_time.isoformat(),
            "status": "STARTED",
            "milestones": [],
            "errors": [],
            "orders": {},
            "observations": {},
            "final_result": None,
            "end_time": None,
            "duration": None
        }
        
        self._save_to_redis()
        StrategyLoggingHelpers.success(
            f"Started execution tracking for {self.strategy_name}",
            f"ID: {self.execution_id} | Params: {params_id} | Case: {case_type}"
        )
        return self.execution_id
    
    def add_milestone(self, milestone, details=None):
        """Add a milestone to the execution tracking"""
        if not self.execution_data:
            return
            
        milestone_data = {
            "timestamp": datetime.now().isoformat(),
            "milestone": milestone,
            "details": details or {}
        }
        self.execution_data["milestones"].append(milestone_data)
        self._save_to_redis()
        
        StrategyLoggingHelpers.info(
            f"Milestone reached: {milestone}",
            details if isinstance(details, str) else json.dumps(details, indent=2) if details else None
        )
    
    def add_error(self, error_msg, error_details=None, exception=None):
        """Add an error to the execution tracking"""
        if not self.execution_data:
            return
            
        error_data = {
            "timestamp": datetime.now().isoformat(),
            "error_message": error_msg,
            "error_details": error_details,
            "exception": str(exception) if exception else None,
            "traceback": traceback.format_exc() if exception else None
        }
        self.execution_data["errors"].append(error_data)
        self.execution_data["status"] = "ERROR"
        self._save_to_redis()
        
        StrategyLoggingHelpers.error(
            f"Error recorded: {error_msg}",
            error_details,
            exception
        )
    
    def add_order(self, order_id, order_data):
        """Add order information to tracking"""
        if not self.execution_data:
            return
            
        self.execution_data["orders"][order_id] = {
            "timestamp": datetime.now().isoformat(),
            "order_data": order_data
        }
        self._save_to_redis()
    
    def add_observation(self, observation_type, observation_data):
        """Add observation data to tracking"""
        if not self.execution_data:
            return
            
        if observation_type not in self.execution_data["observations"]:
            self.execution_data["observations"][observation_type] = []
            
        self.execution_data["observations"][observation_type].append({
            "timestamp": datetime.now().isoformat(),
            "data": observation_data
        })
        self._save_to_redis()
    
    def complete_execution(self, final_result, status="COMPLETED"):
        """Complete the execution tracking"""
        if not self.execution_data:
            return
            
        end_time = datetime.now()
        duration = (end_time - self.start_time).total_seconds()
        
        self.execution_data.update({
            "status": status,
            "final_result": final_result,
            "end_time": end_time.isoformat(),
            "duration": duration
        })
        
        self._save_to_redis()
        
        StrategyLoggingHelpers.success(
            f"Execution completed with status: {status}",
            f"Duration: {duration:.2f}s | Result: {final_result}"
        )
    
    def handle_crash(self, exception):
        """Handle unexpected crashes and save state"""
        if not self.execution_data:
            return
            
        self.add_error("Strategy execution crashed", "Unexpected termination", exception)
        self.complete_execution("CRASHED", "CRASHED")
        
        StrategyLoggingHelpers.error(
            "Strategy execution crashed - state saved to Redis",
            f"Execution ID: {self.execution_id}",
            exception
        )
    
    def _save_to_redis(self):
        """Save execution data to Redis"""
        try:
            self.redis_conn.set(
                self.execution_key,
                json.dumps(self.execution_data, indent=2),
                ex=86400 * 7  # Expire after 7 days
            )
        except Exception as e:
            print(f"Failed to save execution data to Redis: {e}")
    
    def get_execution_summary(self):
        """Get a summary of the current execution"""
        if not self.execution_data:
            return "No active execution"
            
        milestones_count = len(self.execution_data["milestones"])
        errors_count = len(self.execution_data["errors"])
        orders_count = len(self.execution_data["orders"])
        
        return f"Execution {self.execution_id}: {milestones_count} milestones, {errors_count} errors, {orders_count} orders"


class StrategyExecutionHelpers:
    """Helper class for managing Live and Simulation execution modes"""
    
    # Execution modes
    LIVE_MODE = "LIVE"
    SIMULATION_MODE = "SIMULATION"
    
    def __init__(self, redis_connection, execution_mode=LIVE_MODE):
        self.r = redis_connection
        self.execution_mode = execution_mode
        self.simulation_orders = {}  # Store simulated orders
        self.simulation_counter = 1
        
    def set_execution_mode(self, mode):
        """Set execution mode (LIVE or SIMULATION)"""
        if mode not in [self.LIVE_MODE, self.SIMULATION_MODE]:
            raise ValueError(f"Invalid execution mode: {mode}. Must be LIVE or SIMULATION")
        self.execution_mode = mode
        StrategyLoggingHelpers.info(f"Execution mode set to: {mode}")
    
    def execute_order(self, order_instance, order_data, uid, leg_key):
        """Execute order based on current mode (Live or Simulation)"""
        if self.execution_mode == self.LIVE_MODE:
            return self._execute_live_order(order_instance, order_data, uid, leg_key)
        else:
            return self._execute_simulation_order(order_data, uid, leg_key)
    
    def _execute_live_order(self, order_instance, order_data, uid, leg_key):
        """Execute actual order in live mode"""
        try:
            StrategyLoggingHelpers.info(
                f"LIVE ORDER: Placing real order for {leg_key}",
                f"User: {uid}, Qty: {order_data.get('Quantity')}, Price: {order_data.get('Limit_Price')}"
            )
            result = order_instance.place_order(order_data)
            
            if 'data' in result and 'oid' in result['data']:
                StrategyLoggingHelpers.success(f"LIVE ORDER: Successfully placed for {leg_key}")
                return True, result
            else:
                StrategyLoggingHelpers.error(f"LIVE ORDER: Failed to place for {leg_key}")
                return False, result

        except Exception as e:
            StrategyLoggingHelpers.error(f"LIVE ORDER: Exception placing order for {leg_key}", exception=e)
            return False, str(e)

    def _execute_simulation_order(self, order_data, uid, leg_key):
        """Execute simulated order - assume execution at specified price"""
        try:
            # Generate simulation order ID
            sim_order_id = f"SIM_{self.simulation_counter:06d}"
            self.simulation_counter += 1
            
            # Create simulated execution
            simulated_execution = {
                "order_id": sim_order_id,
                "user_id": uid,
                "leg_key": leg_key,
                "symbol": order_data.get("TradingSymbol", "UNKNOWN"),
                "action": order_data.get("Action", "UNKNOWN"),
                "quantity": order_data.get("Quantity", 0),
                "executed_price": order_data.get("Limit_Price", 0),
                "execution_time": datetime.now().isoformat(),
                "status": "FILLED",
                "mode": "SIMULATION"
            }
            
            # Store simulation data
            sim_key = f"simulation_order:{sim_order_id}"
            self.simulation_orders[sim_order_id] = simulated_execution
            
            # Save to Redis for persistence
            self.r.setex(sim_key, 86400, json.dumps(simulated_execution))  # 1 day expiry
            
            StrategyLoggingHelpers.success(
                f"SIMULATION: Order executed for {leg_key}",
                f"OrderID: {sim_order_id}, User: {uid}, Qty: {order_data.get('Quantity')}, Price: {order_data.get('Limit_Price')}"
            )
            
            return True
            
        except Exception as e:
            StrategyLoggingHelpers.error(f"SIMULATION: Exception executing order for {leg_key}", exception=e)
            return False
    
    def get_simulation_orders(self, uid=None, leg_key=None):
        """Get simulation orders with optional filtering"""
        filtered_orders = {}
        
        for order_id, order_data in self.simulation_orders.items():
            if uid and order_data.get("user_id") != uid:
                continue
            if leg_key and order_data.get("leg_key") != leg_key:
                continue
            filtered_orders[order_id] = order_data
        
        return filtered_orders
    
    def get_simulation_summary(self):
        """Get summary of all simulation orders"""
        total_orders = len(self.simulation_orders)
        if total_orders == 0:
            return "No simulation orders executed"
        
        # Calculate summary statistics
        total_quantity = sum(order.get("quantity", 0) for order in self.simulation_orders.values())
        actions = {}
        for order in self.simulation_orders.values():
            action = order.get("action", "UNKNOWN")
            actions[action] = actions.get(action, 0) + 1
        
        summary = f"Simulation Summary: {total_orders} orders, {total_quantity} total qty"
        if actions:
            action_summary = ", ".join(f"{action}: {count}" for action, count in actions.items())
            summary += f" ({action_summary})"
        
        return summary
    
    def clear_simulation_data(self):
        """Clear all simulation data"""
        # Clear in-memory data
        cleared_count = len(self.simulation_orders)
        self.simulation_orders.clear()
        self.simulation_counter = 1
        
        # Clear Redis simulation keys
        try:
            sim_keys = self.r.keys("simulation_order:*")
            if sim_keys:
                self.r.delete(*sim_keys)
        except Exception as e:
            StrategyLoggingHelpers.warning(f"Failed to clear Redis simulation data: {e}")
        
        StrategyLoggingHelpers.info(f"Cleared {cleared_count} simulation orders")
        return cleared_count


class StrategyQuantityHelpers:
    """Helper class for quantity-related calculations and validations"""
    
    @staticmethod
    def get_remaining_quantity_for_leg(params, entry_qtys, uid, leg_key):
        """Get the remaining quantity that can be executed for a specific leg."""
        try:
            # Get desired quantity for this leg
            if leg_key in params and 'quantity' in params[leg_key]:
                desired_qty = int(params[leg_key]['quantity'])
            else:
                desired_qty = int(params.get("default_quantity", 75))
            
            # Apply quantity multiplier
            desired_qty *= int(params.get("quantity_multiplier", 1))
            
            # Get current executed quantity
            current_qty = entry_qtys[uid].get(leg_key, 0)
            
            # Return remaining quantity
            remaining_qty = max(0, desired_qty - current_qty)
            return remaining_qty, desired_qty, current_qty
            
        except Exception as e:
            StrategyLoggingHelpers.error(f"Failed to get remaining quantity for {leg_key}", exception=e)
            return 0, 0, 0
    
    @staticmethod
    def check_desired_quantity_reached(legs_dict, params, entry_qtys, uid):
        """Check if the desired quantities have been reached for all legs."""
        try:
            all_leg_keys = list(legs_dict.keys())
            
            for leg_key in all_leg_keys:
                remaining_qty, desired_qty, current_qty = StrategyQuantityHelpers.get_remaining_quantity_for_leg(
                    params, entry_qtys, uid, leg_key
                )
                if remaining_qty > 0:
                    return False, leg_key, current_qty, desired_qty
            
            # All legs have reached desired quantities
            return True, None, 0, 0
            
        except Exception as e:
            StrategyLoggingHelpers.error("Failed to check desired quantities", exception=e)
            return False, None, 0, 0


class StrategyValidationHelpers:
    """Helper class for common validation functions"""
    
    @staticmethod
    def validate_spread_condition(current_spread, required_spread, condition_type="BUY"):
        """
        Validate spread conditions for BUY or SELL legs
        
        Args:
            current_spread: Current market spread
            required_spread: Required spread for favorable execution
            condition_type: "BUY" or "SELL" - determines comparison logic
            
        Returns:
            bool: True if condition is favorable, False otherwise
        """
        if condition_type.upper() == "BUY":
            # For BUY legs: current spread should be <= required spread
            is_favorable = current_spread <= required_spread
            comparison = f"{current_spread:.2f} <= {required_spread:.2f}"
        else:  # SELL
            # For SELL legs: current spread should be >= required spread  
            is_favorable = current_spread >= required_spread
            comparison = f"{current_spread:.2f} >= {required_spread:.2f}"
        
        StrategyLoggingHelpers.info(
            f"{condition_type} spread validation",
            f"{comparison} = {'FAVORABLE' if is_favorable else 'UNFAVORABLE'}"
        )
        
        return is_favorable
    
    @staticmethod
    def calculate_profit(executed_spread, current_spread, leg_type="SELL"):
        """
        Calculate profit for executed legs
        
        Args:
            executed_spread: Spread at which legs were executed
            current_spread: Current market spread
            leg_type: "SELL" or "BUY" - determines profit calculation logic
            
        Returns:
            float: Calculated profit
        """
        if leg_type.upper() == "SELL":
            # For SELL legs: profit = executed_spread - current_spread
            profit = executed_spread - current_spread
        else:  # BUY
            # For BUY legs: profit = current_spread - executed_spread
            profit = current_spread - executed_spread
        
        return profit
    
    @staticmethod
    def check_profit_threshold(profit, threshold, leg_type="SELL"):
        """Check if profit exceeds threshold"""
        exceeds_threshold = profit >= threshold
        
        if exceeds_threshold:
            StrategyLoggingHelpers.success(
                f"{leg_type} profit threshold reached",
                f"Profit: {profit:.2f} >= Threshold: {threshold:.2f}"
            )
        else:
            StrategyLoggingHelpers.debug(
                f"{leg_type} profit below threshold",
                f"Profit: {profit:.2f} < Threshold: {threshold:.2f}"
            )
        
        return exceeds_threshold
