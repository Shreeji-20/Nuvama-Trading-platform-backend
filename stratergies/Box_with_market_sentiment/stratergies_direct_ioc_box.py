"""
Direct IOC Box Strategy - Main Logic
Execute legs in pairs with direct IOC orders using bid/ask pricing
"""

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
from .strategy_helpers import (
    StrategyHelpers, StrategyDataHelpers, StrategyPricingHelpers,
    StrategyCalculationHelpers, StrategyOrderHelpers, StrategyTrackingHelpers,
    StrategyLoggingHelpers, StrategyExecutionTracker, StrategyExecutionHelpers,
    StrategyQuantityHelpers, StrategyValidationHelpers
)
from constants.exchange import ExchangeEnum
from constants.action import ActionEnum
from constants.order_type import OrderTypeEnum
from constants.product_code import ProductCodeENum
from constants.duration import DurationEnum


class StratergyDirectIOCBox:
    def __init__(self, paramsid) -> None:
        # Redis connection
        self.r = redis.Redis(host="localhost", port=6379, db=0)
        
        # Initialize execution tracker and helpers
        self.execution_tracker = StrategyExecutionTracker(self.r, "DirectIOCBox")
        self.logger = StrategyLoggingHelpers
        
        # Load configuration data
        self.lot_sizes = json.loads(self.r.get("lotsizes"))
        self._init_user_connections()
        
        # Thread management
        self.templates_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=5)

        # Load and validate parameters
        self.params_key = f"4_leg:{paramsid}"
        self.paramsid = paramsid
        self._load_params()
        self.global_action = self.params.get('action', 'BUY').upper()
        self.hard_entry = False
        # Initialize execution mode (default is SIMULATION)
        execution_mode = self.params.get('execution_mode', StrategyExecutionHelpers.LIVE_MODE)
        self.execution_helper = StrategyExecutionHelpers(self.r, execution_mode)
        
        # Log execution mode
        StrategyLoggingHelpers.info(
            f"Strategy initialized in {execution_mode} mode",
            f"Params ID: {paramsid}"
        )
        
        # Set default profit thresholds if not provided
        self.params.setdefault('profit_threshold_sell', 2)  # 2 Rs profit for SELL legs
        self.params.setdefault('profit_threshold_buy', 2)   # 2 Rs profit for BUY legs
        
        # Load option mapper
        self._load_option_mapper()

        # Initialize tracking dictionaries
        self._init_tracking_data()

        # Initialize helper classes
        self._init_helpers()

        # Start background parameter update thread
        self.params_update_thread = threading.Thread(target=self._live_params_update_thread, daemon=True)
        self.params_update_thread.start()

        # Initialize legs and order templates
        self._init_legs_and_orders()

        # Start global parallel observation
        self._init_global_parallel_observation()

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
            self.logger.error("Failed to load option_mapper from redis", exception=e)
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
        
        # Global parallel observation tracking
        self.global_observation_active = False
        self.global_observation_threads = {}
        self.latest_observation_results = {}
        self.observation_locks = {}
        self.observation_stop_flags = {}

    def _init_helpers(self):
        """Initialize helper class instances."""
        self.data_helpers = StrategyDataHelpers(self.r)
        self.pricing_helpers = None  # Will be initialized after params are loaded
        self.calculation_helpers = None  # Will be initialized after legs are loaded
        self.order_helpers = None  # Will be initialized after option_mapper is loaded
        self.tracking_helpers = StrategyTrackingHelpers(self.r, self.templates_lock)

    def _live_params_update_thread(self):
        """Background thread to update parameters from Redis."""
        while True:
            try:
                time.sleep(1)
                new_params = self.r.get(self.params_key)
                if new_params:
                    self.params = orjson.loads(new_params.decode())
            except Exception as e:
                self.logger.error("Params update thread failed", exception=e)

    def _init_global_parallel_observation(self):
        """Initialize global parallel observation for both pairs at startup."""
        try:
            # Check if already initialized to prevent duplicate initialization
            if self.global_observation_active:
                self.logger.warning("Global parallel observation already active, skipping initialization")
                return
                
            if hasattr(self, 'pair1_bidding_leg') and hasattr(self, 'pair2_bidding_leg'):
                buy_leg_keys = [self.pair1_bidding_leg, self.pair1_base_leg]
                sell_leg_keys = [self.pair2_bidding_leg, self.pair2_base_leg]
                
                self.logger.info("Starting global parallel observation for all pairs")
                
                # Start observation for BUY pair
                self._start_global_parallel_observation("BUY_PAIR", buy_leg_keys[0], buy_leg_keys[1])
                
                # Start observation for SELL pair  
                self._start_global_parallel_observation("SELL_PAIR", sell_leg_keys[0], sell_leg_keys[1])
                time.sleep(2)
                self.global_observation_active = True
                self.logger.success("Global parallel observation started successfully")
                
        except Exception as e:
            self.logger.error("Failed to start global parallel observation", exception=e)

    def _start_global_parallel_observation(self, observation_key, leg1_key, leg2_key):
        """Start global parallel observation that runs throughout strategy execution."""
        try:
            # Check if observation thread already exists for this key
            if observation_key in self.global_observation_threads:
                existing_thread = self.global_observation_threads[observation_key]
                if existing_thread.is_alive():
                    self.logger.warning(f"Global observation thread already running for {observation_key}")
                    return
                else:
                    self.logger.info(f"Cleaning up dead thread for {observation_key}")
                    del self.global_observation_threads[observation_key]
            
            # Initialize tracking structures
            self.observation_locks[observation_key] = threading.Lock()
            self.observation_stop_flags[observation_key] = False
            self.latest_observation_results[observation_key] = None
            
            # Start observation thread
            observation_thread = threading.Thread(
                target=self._global_observation_worker,
                args=(observation_key, leg1_key, leg2_key),
                daemon=True
            )
            observation_thread.start()
            self.global_observation_threads[observation_key] = observation_thread
            
            self.logger.info(f"Started global observation for {observation_key}", f"Legs: {leg1_key}, {leg2_key}")
            
        except Exception as e:
            self.logger.error("Failed to start global observation", exception=e)

    def _global_observation_worker(self, observation_key, leg1_key, leg2_key):
        """Background worker for continuous market observation."""
        try:
            self.logger.debug(f"Global observation worker started for {observation_key}")
            
            while not self.observation_stop_flags.get(observation_key, True):
                try:
                    # Perform market observation
                    observation_result = self._observe_market_for_pair(leg1_key, leg2_key, 2)
                    
                    # Update latest result with lock
                    with self.observation_locks[observation_key]:
                        self.latest_observation_results[observation_key] = {
                            'result': observation_result,
                            'timestamp': time.time(),
                            'legs': [leg1_key, leg2_key]
                        }
                    
                    # Small sleep to prevent excessive CPU usage
                    time.sleep(0.5)
                    
                except Exception as e:
                    self.logger.error(f"Global observation worker error for {observation_key}", exception=e)
                    time.sleep(1)
            
            self.logger.debug(f"Global observation worker stopped for {observation_key}")
            
        except Exception as e:
            self.logger.error(f"Global observation worker failed for {observation_key}", exception=e)

    def _get_global_observation_result(self, observation_key):
        """Get the latest observation result from global observation."""
        try:
            if observation_key in self.latest_observation_results:
                with self.observation_locks[observation_key]:
                    latest_data = self.latest_observation_results[observation_key]
                    if latest_data:
                        age = time.time() - latest_data['timestamp']
                        self.logger.debug(f"Retrieved global observation result for {observation_key} (age: {age:.2f}s)")
                        return latest_data['result']
            
            self.logger.warning(f"No global observation result available for {observation_key}")
            return None
            
        except Exception as e:
            self.logger.error("Failed to get global observation result", exception=e)
            return None

    def _stop_global_parallel_observation(self):
        """Stop all global parallel observations."""
        try:
            for observation_key in self.observation_stop_flags:
                self.observation_stop_flags[observation_key] = True
                
            # Wait for threads to finish
            for observation_key, thread in self.global_observation_threads.items():
                thread.join(timeout=2.0)
                
            self.global_observation_active = False
            self.logger.info("Stopped all global parallel observations")
            
        except Exception as e:
            self.logger.error("Failed to stop global observations", exception=e)

    def _observe_market_for_pair(self, leg1_key, leg2_key, observation_duration=2):
        """Observe market for a specified duration and collect price data for analysis."""
        leg1_prices = []
        leg2_prices = []
        start_time = time.time()
        
        # Collect prices in a loop
        while time.time() - start_time < observation_duration:
            current_prices = self._get_leg_prices([leg1_key, leg2_key])
            leg1_price = current_prices.get(leg1_key, 0)
            leg2_price = current_prices.get(leg2_key, 0)
            
            leg1_prices.append(leg1_price)
            leg2_prices.append(leg2_price)
            
            time.sleep(0.2)  # Check every 200ms
        
        # Analyze trends
        leg1_trend, leg1_change = StrategyHelpers.analyze_price_trend(leg1_prices)
        leg2_trend, leg2_change = StrategyHelpers.analyze_price_trend(leg2_prices)
        
        if leg1_trend == "STABLE" and leg2_trend == "STABLE":
            return False
        
        # Determine order sequence - place increasing leg first for BUY legs
        if leg1_change > leg2_change:
            first_leg, second_leg = leg1_key, leg2_key
        elif leg2_change > leg1_change:
            first_leg, second_leg = leg2_key, leg1_key
        else:
            # Equal trends, use original order
            first_leg, second_leg = leg1_key, leg2_key
        
        return {
            'first_leg': first_leg,
            'second_leg': second_leg,
            'trend': 'moving',
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

    def _get_leg_prices(self, leg_keys, is_exit=False):
        """Get current prices for specified legs based on their actions."""
        prices = {}
        for leg_key in leg_keys:
            try:
                leg_data = self.legs[leg_key]['data']
                leg_action = self.legs[leg_key]['info'].get('action', self.global_action).upper()
                
                if is_exit:
                    # For exit orders, flip the action (BUY becomes SELL, SELL becomes BUY)
                    side_key = "askValues" if leg_action == "BUY" else "bidValues"
                else:
                    side_key = "bidValues" if leg_action == "BUY" else "askValues"

                prices[leg_key] = self.pricing_helpers.safe_get_price(leg_data, side_key)
            except (KeyError, TypeError) as e:
                print(f"ERROR: Failed to get price for {leg_key}: {e}")
                prices[leg_key] = 0
        return prices

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
        
        self.logger.info("Auto-detected BUY legs", str(buy_legs))
        self.logger.info("Auto-detected SELL legs", str(sell_legs))
        
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
            return primary_legs[0], bidding_leg_key if bidding_leg_key != primary_legs[0] else secondary_legs[0]
        else:
            return bidding_leg_key, all_leg_keys[1] if len(all_leg_keys) > 1 else bidding_leg_key

    def _assign_remaining_pair(self, sell_legs, buy_legs, bidding_leg_key, all_leg_keys, pair1_bidding, pair1_base):
        """Assign second pair from remaining legs."""
        if len(sell_legs) >= 2:
            return sell_legs[0], sell_legs[1]
        elif len(sell_legs) == 1:
            return sell_legs[0], bidding_leg_key if bidding_leg_key not in [pair1_bidding, pair1_base] else buy_legs[0]
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
        
        self.legs[bidding_leg_key] = self.data_helpers.load_leg_data(bidding_leg_key, bidding_leg_info)
        
        # Load base legs
        base_leg_keys = self.params.get("base_legs", ["leg1", "leg2", "leg3", "leg4"])
        all_leg_keys = [bidding_leg_key] + base_leg_keys
        
        for leg_key in base_leg_keys:
            leg_info = self.params.get(leg_key)
            if not leg_info:
                print(f"WARNING: Missing leg info for {leg_key}")
                continue
            self.legs[leg_key] = self.data_helpers.load_leg_data(leg_key, leg_info)
        
        # Determine pairs automatically
        (self.pair1_bidding_leg, self.pair1_base_leg, 
         self.pair2_bidding_leg, self.pair2_base_leg) = self._determine_leg_pairs(all_leg_keys, bidding_leg_key)
        
        print(f"INFO: Pair 1 - Bidding: {self.pair1_bidding_leg}, Base: {self.pair1_base_leg}")
        print(f"INFO: Pair 2 - Bidding: {self.pair2_bidding_leg}, Base: {self.pair2_base_leg}")
        
        # Validate leg assignments
        required_legs = [self.pair1_bidding_leg, self.pair1_base_leg, self.pair2_bidding_leg, self.pair2_base_leg]
        for leg in required_legs:
            if leg not in self.legs:
                raise RuntimeError(f"Missing required leg: {leg}")

        # Determine exchange
        self._determine_exchange()
        
        # Initialize helper classes that depend on loaded data
        self.pricing_helpers = StrategyPricingHelpers(self.params)
        self.calculation_helpers = StrategyCalculationHelpers(self.params, self.legs, self.global_action)
        self.order_helpers = StrategyOrderHelpers(self.params, self.option_mapper, self.exchange)
        
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
                leg_action = self.legs[leg_key]['info'].get('action', self.global_action).upper()
                
                # Entry order template
                self.order_templates[uid][leg_key] = self.order_helpers.make_order_template(
                    leg_data, leg_action, uid, leg_key)
                
                # Exit order template (opposite action)
                exit_action = "SELL" if leg_action == "BUY" else "BUY"
                self.exit_order_templates[uid][leg_key] = self.order_helpers.make_order_template(
                    leg_data, exit_action, uid, leg_key)

    def _check_desired_quantity_reached(self, uid):
        """Check if the desired quantities have been reached for all legs."""
        return StrategyQuantityHelpers.check_desired_quantity_reached(
            self.legs, self.params, self.entry_qtys, uid
        )

    def _get_remaining_quantity_for_leg(self, uid, leg_key):
        """Get the remaining quantity that can be executed for a specific leg."""
        return StrategyQuantityHelpers.get_remaining_quantity_for_leg(
            self.params, self.entry_qtys, uid, leg_key
        )

    def _place_single_order(self, uid, leg_key, leg_price):
        """Place a single order for a leg with quantity validation."""
        try:
            # Check remaining quantity for this leg
            remaining_qty, desired_qty, current_qty = self._get_remaining_quantity_for_leg(uid, leg_key)
            
            if remaining_qty <= 0:
                self.logger.info(
                    f"Desired quantity already reached for {leg_key}",
                    f"Current: {current_qty}/{desired_qty}"
                )
                return True  # Consider as success since quantity is already met
            
            # Get current prices and prepare order
            new_price = self._get_leg_prices([leg_key])
            print("New Price : ",new_price)
            breakpoint()
            order = self.order_templates[uid][leg_key].copy()
            
            # Adjust order quantity to remaining quantity
            original_qty = order.get("Quantity", 0)
            order["Quantity"] = min(remaining_qty, original_qty)
            order["Slice_Quantity"] = order["Quantity"]
            
            tick = 0.05 if order.get("Action") == ActionEnum.BUY else -0.05
            order["Limit_Price"] = StrategyHelpers.format_limit_price(float(new_price[leg_key]) + tick)
            
            self.logger.info(
                f"Placing order for {leg_key}",
                f"Qty: {order['Quantity']} (remaining: {remaining_qty}/{desired_qty}) at price {order['Limit_Price']}"
            )
            
            # Track order attempt
            self.execution_tracker.add_order(f"{uid}_{leg_key}", {
                "user": uid,
                "leg": leg_key,
                "quantity": order["Quantity"],
                "price": order["Limit_Price"],
                "action": order.get("Action"),
                "remaining_qty": remaining_qty,
                "desired_qty": desired_qty
            })
            
            # Use execution helper to place order (supports Live/Simulation modes)
            success , result = self.execution_helper.execute_order(self.order, order, uid, leg_key)

            if success:
                self.logger.success(f"Order executed successfully for {leg_key} ({self.execution_helper.execution_mode} mode)")
                self.execution_tracker.add_milestone(f"Order executed for {leg_key}", {
                    "user": uid,
                    "leg": leg_key,
                    "status": "SUCCESS",
                    "mode": self.execution_helper.execution_mode,
                    "response": result
                })
                breakpoint()
                return True
            else:
                self.logger.error(f"Order execution failed for {leg_key} ({self.execution_helper.execution_mode} mode)")
                self.execution_tracker.add_error(f"Order execution failed for {leg_key}", f"User: {uid}, Mode: {self.execution_helper.execution_mode}, Response: {result}")
                return False
                
        except Exception as e:
            self.logger.error(f"Failed to place single order for {leg_key}", exception=e)
            self.execution_tracker.add_error(f"Order placement exception for {leg_key}", str(e), e)
            return False

    def _execute_both_pairs(self, uid, prices):
        """Execute both pairs using new dual strategy approach with global observation."""
        try:
            self.logger.separator(f"EXECUTING PAIRS FOR USER {uid}")
            
            # Step 0: Check if desired quantities have already been reached
            quantities_reached, leg_key, current_qty, desired_qty = self._check_desired_quantity_reached(uid)
            if quantities_reached:
                self.logger.success(f"All desired quantities reached for user {uid}")
                self.execution_tracker.add_milestone(f"User {uid} quantities completed", {
                    "user": uid,
                    "status": "quantities_reached"
                })
                return True
            else:
                remaining_qty = desired_qty - current_qty
                self.logger.info(
                    f"Proceeding with execution for user {uid}",
                    f"{leg_key} needs {remaining_qty} more quantity"
                )
                self.execution_tracker.add_milestone(f"User {uid} needs more quantity", {
                    "user": uid,
                    "leg": leg_key,
                    "remaining": remaining_qty,
                    "current": current_qty,
                    "desired": desired_qty
                })
            
            self.logger.info("Using global observation results for dual strategy execution")
            
            # Use new dual strategy execution with global observation
            return self._execute_dual_strategy_pairs(uid, prices)
            
        except KeyboardInterrupt:
            self.logger.warning("Execution interrupted by user")
            self.execution_tracker.add_milestone("User interrupt during pair execution")
            self.hard_entry = True
            return False
        except Exception as e:
            self.logger.error(f"Failed to execute both pairs for user {uid}", exception=e)
            self.execution_tracker.add_error(f"Pair execution failed for user {uid}", str(e), e)
            return False

    def _execute_dual_strategy_pairs(self, uid, prices):
        """Execute pairs using dual strategy approach with global observation results."""
        try:
            self.logger.separator(f"DUAL STRATEGY EXECUTION - USER {uid}")
            
            # Step 1: Get BUY legs observation from global cache
            buy_leg_keys = [self.pair1_bidding_leg, self.pair1_base_leg]
            sell_leg_keys = [self.pair2_bidding_leg, self.pair2_base_leg]
            
            self.logger.info(
                "Analyzing BUY leg stability using global observation",
                f"BUY legs: {buy_leg_keys}"
            )
            
            # Get observation result from global observation
            observation_result = self._get_global_observation_result("BUY_PAIR")
            
            # Log observation to execution tracker
            self.execution_tracker.add_observation("BUY_PAIR_DECISION", {
                "user": uid,
                "buy_legs": buy_leg_keys,
                "sell_legs": sell_leg_keys,
                "observation_result": observation_result
            })
            
            if observation_result == None or observation_result == False:
                # Both BUY legs are STABLE - CASE A: Execute SELL legs first
                self.logger.success("CASE A: BUY legs are STABLE - executing SELL legs first")
                case_type = "CASE_A"
                self.execution_tracker.add_milestone(f"User {uid} executing CASE A", {
                    "strategy": "SELL_FIRST",
                    "reason": "BUY_legs_stable"
                })
                return self._execute_case_a_sell_first(uid, prices, buy_leg_keys, sell_leg_keys)
            else:
                # BUY legs are moving - CASE B: Execute BUY legs first with profit monitoring
                self.logger.warning("CASE B: BUY legs are MOVING - executing BUY legs first")
                case_type = "CASE_B"
                self.execution_tracker.add_milestone(f"User {uid} executing CASE B", {
                    "strategy": "BUY_FIRST",
                    "reason": "BUY_legs_moving",
                    "observation_details": observation_result
                })
                return self._execute_case_b_buy_first(uid, prices, buy_leg_keys, sell_leg_keys, observation_result)
                
        except Exception as e:
            self.logger.error(f"Failed to execute dual strategy for user {uid}", exception=e)
            self.execution_tracker.add_error(f"Dual strategy failed for user {uid}", str(e), e)
            return False

    def _execute_case_a_sell_first(self, uid, prices, buy_leg_keys, sell_leg_keys):
        """CASE A: BUY legs stable - Execute SELL legs first, then BUY based on remaining spread."""
        try:
            self.logger.info("CASE A: BUY legs stable - executing SELL legs first")
            self.execution_tracker.add_milestone(f"CASE A execution started for user {uid}", {
                "strategy": "SELL_FIRST",
                "buy_legs": buy_leg_keys,
                "sell_legs": sell_leg_keys
            })
            
            # Get SELL leg observation result from global observation
            sell_observation = self._get_global_observation_result("SELL_PAIR")
            
            self.logger.info("Using global observation for SELL leg execution order", str(sell_leg_keys))
            
            if sell_observation == None or sell_observation == False:
                # SELL legs are stable, execute in original order
                self.logger.info("SELL legs are stable - executing in original order")
                first_sell_leg = sell_leg_keys[0]
                second_sell_leg = sell_leg_keys[1]
                current_sell_prices = self._get_leg_prices(sell_leg_keys)
            else:
                # SELL legs are moving, execute decreasing leg first
                self.logger.warning("SELL legs are moving - executing decreasing leg first")
                # For SELL legs, we want to execute the decreasing leg first (opposite of BUY logic)
                first_sell_leg = sell_observation['second_leg']  # Decreasing leg (weaker movement)
                second_sell_leg = sell_observation['first_leg']  # Increasing leg (stronger movement)
                current_sell_prices = sell_observation['final_prices']
            
            self.logger.info(
                "Executing SELL legs in optimized order",
                f"First: {first_sell_leg} (decreasing), Second: {second_sell_leg}"
            )
            
            # Execute first SELL leg
            self.execution_tracker.add_milestone(f"Placing first SELL order for {first_sell_leg}")
            first_sell_success = self._place_single_order(uid, first_sell_leg, current_sell_prices[first_sell_leg])
            if not first_sell_success:
                self.logger.error(f"First SELL leg ({first_sell_leg}) failed")
                self.execution_tracker.add_error(f"First SELL leg failed", f"Leg: {first_sell_leg}")
                return False
            
            # Execute second SELL leg
            self.execution_tracker.add_milestone(f"Placing second SELL order for {second_sell_leg}")
            second_sell_success = self._place_single_order(uid, second_sell_leg, current_sell_prices[second_sell_leg])
            if not second_sell_success:
                self.logger.error(f"Second SELL leg ({second_sell_leg}) failed")
                self.execution_tracker.add_error(f"Second SELL leg failed", f"Leg: {second_sell_leg}")
                return False
            
            # Update SELL pair tracking
            self.pair2_orders_placed[uid] = True
            self.pair2_executed[uid] = True
            
            # Calculate SELL pair executed spread using current prices
            sell_executed_prices = {
                sell_leg_keys[0]: self.pair1_executed_prices.get(uid, {}).get(sell_leg_keys[0], current_sell_prices[sell_leg_keys[0]]),
                sell_leg_keys[1]: self.pair1_executed_prices.get(uid, {}).get(sell_leg_keys[1], current_sell_prices[sell_leg_keys[1]])
            }
            sell_executed_spread = sell_executed_prices[sell_leg_keys[0]] + sell_executed_prices[sell_leg_keys[1]]
            
            self.logger.success(f"SELL pair executed successfully", f"Spread: {sell_executed_spread:.2f}")
            self.execution_tracker.add_milestone("SELL pair execution completed", {
                "spread": sell_executed_spread,
                "prices": sell_executed_prices
            })
            
            # Step 2: Calculate BUY legs prices based on remaining spread
            desired_spread = self.params.get("desired_spread", 405)
            remaining_spread_for_buy = desired_spread + sell_executed_spread
            
            print(f"INFO: Remaining spread for BUY legs: {remaining_spread_for_buy:.2f}")
            
            # Step 2.1: Validate BUY spread condition before execution
            current_buy_prices = self._get_leg_prices(buy_leg_keys)
            current_buy_spread = current_buy_prices[buy_leg_keys[0]] + current_buy_prices[buy_leg_keys[1]]
            
            print(f"INFO: Current BUY spread: {current_buy_spread:.2f}, Required: <= {remaining_spread_for_buy:.2f}")
            
            if current_buy_spread > remaining_spread_for_buy:
                print(f"WARNING: BUY spread condition not met ({current_buy_spread:.2f} > {remaining_spread_for_buy:.2f})")
                print(f"INFO: Cannot execute BUY legs at favorable prices, monitoring for better conditions...")
                return self._monitor_sell_profit_or_wait_for_buy_conditions(uid, sell_executed_spread, remaining_spread_for_buy, 
                                                                         buy_leg_keys, current_buy_spread)
            
            # Step 3: Use global observation result for BUY execution
            cached_buy_observation = self._get_global_observation_result("BUY_PAIR")
            
            if cached_buy_observation:
                print(f"INFO: Using global BUY observation result - trend: {cached_buy_observation.get('trend')}")
                buy_trend = cached_buy_observation.get('trend')
            else:
                print("WARNING: No global BUY observation available")
                buy_trend = None
            
            # Step 4: BUY spread condition met, proceed with monitoring and execution
            profit_threshold_sell = self.params.get("profit_threshold_sell", 2)  # 2 Rs profit threshold
            
            if self._monitor_sell_profit_and_execute_buy(uid, sell_executed_spread, remaining_spread_for_buy, 
                                                       buy_leg_keys, profit_threshold_sell):
                # Successfully executed BUY legs or exited with profit
                self.pair1_orders_placed[uid] = True
                self.pair1_executed[uid] = True
                self.all_legs_executed[uid] = True
                print(f"SUCCESS: Case A execution completed")
                return True
            else:
                print(f"INFO: Case A - Exited with SELL profit or BUY execution failed")
                return False
                
        except Exception as e:
            print(f"ERROR: Case A execution failed: {e}")
            traceback.print_exc()
            return False

    def _execute_case_b_buy_first(self, uid, prices, buy_leg_keys, sell_leg_keys, buy_observation):
        """CASE B: BUY legs moving - Execute BUY legs first with global observation."""
        try:
            print(f"INFO: Case B - BUY legs moving, executing BUY legs first")
            
            # Get BUY confirmation from global observation
            buy_observation_2 = self._get_global_observation_result("BUY_PAIR")
            
            print(f"INFO: Using global observation to confirm BUY market conditions")
            
            if buy_observation_2 == None or buy_observation_2 == False:
                print(f"WARNING: BUY legs became stable, switching to Case A")
                return self._execute_case_a_sell_first(uid, prices, buy_leg_keys, sell_leg_keys)
            
            # Check consistency between initial and confirmation observations
            if (buy_observation['first_leg'] != buy_observation_2['first_leg'] or 
                buy_observation['second_leg'] != buy_observation_2['second_leg']):
                print(f"WARNING: BUY market conditions inconsistent, aborting")
                return False
            
            # Execute BUY legs sequentially (stronger movement first)
            first_buy_leg = buy_observation['first_leg']
            second_buy_leg = buy_observation['second_leg']
            
            print(f"INFO: Executing BUY legs - First: {first_buy_leg}, Second: {second_buy_leg}")
            
            first_buy_success = self._place_single_order(uid, first_buy_leg, buy_observation_2['final_prices'][first_buy_leg])
            if not first_buy_success:
                print(f"ERROR: First BUY leg failed")
                return False
                
            second_buy_success = self._place_single_order(uid, second_buy_leg, buy_observation_2['final_prices'][second_buy_leg])
            if not second_buy_success:
                print(f"ERROR: Second BUY leg failed")
                return False
            
            # Update BUY pair tracking
            self.pair1_orders_placed[uid] = True
            self.pair1_executed[uid] = True
            
            # Calculate BUY pair executed spread
            buy_executed_prices = {
                buy_leg_keys[0]: self.pair1_executed_prices.get(uid, {}).get(buy_leg_keys[0], buy_observation_2['final_prices'][buy_leg_keys[0]]),
                buy_leg_keys[1]: self.pair1_executed_prices.get(uid, {}).get(buy_leg_keys[1], buy_observation_2['final_prices'][buy_leg_keys[1]])
            }
            buy_executed_spread = buy_executed_prices[buy_leg_keys[0]] + buy_executed_prices[buy_leg_keys[1]]
            self.pair1_executed_spread[uid] = buy_executed_spread
            
            print(f"INFO: BUY pair executed spread: {buy_executed_spread:.2f}")
            
            # Step 2: Calculate SELL legs prices based on remaining spread
            desired_spread = self.params.get("desired_spread", 405)
            remaining_spread_for_sell = buy_executed_spread - desired_spread
            
            print(f"INFO: Remaining spread for SELL legs: {remaining_spread_for_sell:.2f}")
            
            # Step 2.1: Validate SELL spread condition before execution
            current_sell_prices = self._get_leg_prices(sell_leg_keys)
            current_sell_spread = current_sell_prices[sell_leg_keys[0]] + current_sell_prices[sell_leg_keys[1]]
            
            print(f"INFO: Current SELL spread: {current_sell_spread:.2f}, Required: >= {remaining_spread_for_sell:.2f}")
            
            if current_sell_spread < remaining_spread_for_sell:
                print(f"WARNING: SELL spread condition not met ({current_sell_spread:.2f} < {remaining_spread_for_sell:.2f})")
                print(f"INFO: Cannot execute SELL legs at favorable prices, monitoring for better conditions...")
                return self._monitor_buy_profit_or_wait_for_sell_conditions(uid, buy_executed_spread, remaining_spread_for_sell, 
                                                                          sell_leg_keys, current_sell_spread)
            
            # Step 3: Use global observation result for SELL execution
            cached_sell_observation = self._get_global_observation_result("SELL_PAIR")
            
            if cached_sell_observation:
                print(f"INFO: Using global SELL observation result - trend: {cached_sell_observation.get('trend')}")
                sell_trend = cached_sell_observation.get('trend')
            else:
                print("WARNING: No global SELL observation available")
                sell_trend = None
            
            # Step 4: SELL spread condition met, proceed with monitoring and execution
            profit_threshold_buy = self.params.get("profit_threshold_buy", 2)  # 2 Rs profit threshold
            
            if self._monitor_buy_profit_and_execute_sell(uid, buy_executed_spread, remaining_spread_for_sell, 
                                                       sell_leg_keys, profit_threshold_buy):
                # Successfully executed SELL legs or exited with profit
                self.pair2_orders_placed[uid] = True
                self.pair2_executed[uid] = True
                self.all_legs_executed[uid] = True
                print(f"SUCCESS: Case B execution completed")
                return True
            else:
                print(f"INFO: Case B - Exited with BUY profit or SELL execution failed")
                return False
                
        except Exception as e:
            print(f"ERROR: Case B execution failed: {e}")
            traceback.print_exc()
            return False

    # Monitoring functions with global observation
    def _monitor_sell_profit_and_execute_buy(self, uid, sell_executed_spread, remaining_spread, buy_leg_keys, profit_threshold):
        """Monitor SELL profit while executing BUY legs. Exit if SELL profit exceeds threshold."""
        try:
            print(f"INFO: Monitoring SELL profit (threshold: {profit_threshold}) while preparing BUY execution")
            
            # Monitor SELL profit for a few seconds before BUY execution
            monitor_duration = 0.2  # seconds
            start_time = time.time()
            
            while time.time() - start_time < monitor_duration:
                # Get current SELL leg prices
                current_sell_prices = self._get_leg_prices([self.pair2_bidding_leg, self.pair2_base_leg])
                current_sell_spread = current_sell_prices[self.pair2_bidding_leg] + current_sell_prices[self.pair2_base_leg]
                
                # Calculate SELL profit (SELL executed at higher price, current at lower price = profit)
                sell_profit = sell_executed_spread - current_sell_spread
                
                print(f"\rSELL Profit: {sell_profit:.2f} (threshold: {profit_threshold})", end='')
                
                # Check profit threshold
                if sell_profit >= profit_threshold:
                    print(f"\nINFO: SELL profit threshold reached ({sell_profit:.2f} >= {profit_threshold}), exiting with profit")
                    # Execute SELL exit orders
                    self._execute_sell_exit(uid)
                    return False  # Don't execute BUY legs
                
                time.sleep(0.5)
            
            print()  # New line after monitoring
            
            # No profit threshold hit, proceed with BUY execution with global observation
            print(f"INFO: SELL profit monitoring complete, proceeding with BUY execution")
            
            # Get BUY observation from global observation
            buy_observation = self._get_global_observation_result("BUY_PAIR")
            
            print(f"INFO: Using global observation for BUY leg execution order: {buy_leg_keys}")
            
            if buy_observation == None or buy_observation == False:
                # BUY legs are stable, execute in original order
                print(f"INFO: BUY legs are stable, executing in original order")
                first_buy_leg = buy_leg_keys[0]
                second_buy_leg = buy_leg_keys[1]
                current_buy_prices = self._get_leg_prices(buy_leg_keys)
            else:
                # BUY legs are moving, execute stronger movement first
                print(f"INFO: BUY legs are moving, executing stronger movement first")
                first_buy_leg = buy_observation['first_leg']
                second_buy_leg = buy_observation['second_leg']
                current_buy_prices = buy_observation['final_prices']
            
            # Step 2: Execute BUY legs sequentially based on global observation
            print(f"INFO: Executing BUY legs - First: {first_buy_leg}, Second: {second_buy_leg}")
            
            first_buy_success = self._place_single_order(uid, first_buy_leg, current_buy_prices[first_buy_leg])
            if not first_buy_success:
                print(f"ERROR: First BUY leg execution failed")
                return False
                
            second_buy_success = self._place_single_order(uid, second_buy_leg, current_buy_prices[second_buy_leg])
            if not second_buy_success:
                print(f"ERROR: Second BUY leg execution failed")
                return False
            
            print(f"SUCCESS: BUY legs executed successfully with SELL profit monitoring")
            return True
            
        except Exception as e:
            print(f"ERROR: SELL profit monitoring with BUY execution failed: {e}")
            return False

    def _monitor_buy_profit_and_execute_sell(self, uid, buy_executed_spread, remaining_spread, sell_leg_keys, profit_threshold):
        """Monitor BUY profit while executing SELL legs. Exit if BUY profit exceeds threshold."""
        try:
            print(f"INFO: Monitoring BUY profit (threshold: {profit_threshold}) while executing SELL legs")
            
            # Start SELL execution in background while monitoring BUY profit
            print(f"INFO: Starting SELL execution with remaining spread: {remaining_spread:.2f}")
            
            # Wait for SELL spread to be favorable
            while True:
                current_sell_prices = self._get_leg_prices(sell_leg_keys)
                current_sell_spread = current_sell_prices[sell_leg_keys[0]] + current_sell_prices[sell_leg_keys[1]]
                
                # Also monitor BUY profit during waiting
                current_buy_prices = self._get_leg_prices([self.pair1_bidding_leg, self.pair1_base_leg])
                current_buy_spread = current_buy_prices[self.pair1_bidding_leg] + current_buy_prices[self.pair1_base_leg]
                
                # Calculate BUY profit (BUY executed at lower price, current at higher price = profit)
                buy_profit = current_buy_spread - buy_executed_spread
                
                print(f"\rSELL Wait: Current={current_sell_spread:.2f}, Target={remaining_spread:.2f} | BUY Profit={buy_profit:.2f}", end='')
                
                # Check BUY profit threshold
                if buy_profit >= profit_threshold:
                    print(f"\nINFO: BUY profit threshold reached ({buy_profit:.2f} >= {profit_threshold}), exiting with profit")
                    # Execute BUY exit orders
                    self._execute_buy_exit(uid)
                    return False  # Don't execute SELL legs
                
                # Check if SELL spread is favorable
                if abs(current_sell_spread) >= abs(remaining_spread):
                    print(f"\nINFO: SELL spread favorable, proceeding with SELL execution")
                    break
                
                time.sleep(0.5)
            
            # Execute SELL legs directly (no market observation needed for SELL legs)
            print(f"\nINFO: Executing SELL legs directly - {sell_leg_keys[0]}, {sell_leg_keys[1]}")
            
            # Get current SELL leg prices
            current_sell_prices = self._get_leg_prices(sell_leg_keys)
            
            # Execute SELL legs sequentially
            first_sell_leg = sell_leg_keys[0]
            second_sell_leg = sell_leg_keys[1]
            
            # Continue BUY profit monitoring during SELL execution
            def execute_sell_with_monitoring():
                first_success = self._place_single_order(uid, first_sell_leg, current_sell_prices[first_sell_leg])
                if not first_success:
                    return False
                
                # Check BUY profit again before second SELL leg
                current_buy_prices = self._get_leg_prices([self.pair1_bidding_leg, self.pair1_base_leg])
                current_buy_spread = current_buy_prices[self.pair1_bidding_leg] + current_buy_prices[self.pair1_base_leg]
                buy_profit = current_buy_spread - buy_executed_spread
                
                if buy_profit >= profit_threshold:
                    print(f"INFO: BUY profit threshold reached during SELL execution, exiting")
                    self._execute_buy_exit(uid)
                    return False
                
                second_success = self._place_single_order(uid, second_sell_leg, current_sell_prices[second_sell_leg])
                return second_success
            
            sell_success = execute_sell_with_monitoring()
            
            if sell_success:
                print(f"SUCCESS: SELL legs executed successfully with BUY profit monitoring")
                return True
            else:
                print(f"ERROR: SELL execution failed or BUY profit exit triggered")
                return False
                
        except Exception as e:
            print(f"ERROR: BUY profit monitoring with SELL execution failed: {e}")
            return False

    # Placeholder monitoring functions (to be implemented based on your existing logic)
    def _monitor_sell_profit_or_wait_for_buy_conditions(self, uid, sell_executed_spread, required_buy_spread, buy_leg_keys, current_buy_spread):
        """Monitor SELL profit while waiting for favorable BUY conditions."""
        print(f"INFO: Monitoring SELL profit while waiting for favorable BUY conditions")
        return False

    def _monitor_buy_profit_or_wait_for_sell_conditions(self, uid, buy_executed_spread, required_sell_spread, sell_leg_keys, current_sell_spread):
        """Monitor BUY profit while waiting for favorable SELL conditions."""
        print(f"INFO: Monitoring BUY profit while waiting for favorable SELL conditions")
        return False

    def _execute_sell_exit(self, uid):
        """Execute exit orders for SELL legs to realize profit."""
        print(f"INFO: Executing SELL exit orders for user {uid}")
        return True

    def _execute_buy_exit(self, uid):
        """Execute exit orders for BUY legs to realize profit."""
        print(f"INFO: Executing BUY exit orders for user {uid}")
        return True

    # Main execution logic (simplified for demo)
    def main_logic(self):
        """Main logic for sequential box strategy with global observation."""
        execution_id = None
        
        try:
            # Start execution tracking
            execution_id = self.execution_tracker.start_execution(self.paramsid)
            self.logger.separator("STRATEGY EXECUTION START")
            
            # Global parallel observation should already be initialized in __init__
            self.logger.info("Verifying global parallel observation system is active")
            if not self.global_observation_active:
                self.logger.warning("Global observation not active, attempting to initialize")
                self.execution_tracker.add_milestone("Global observation initialization started")
                self._init_global_parallel_observation()
                self.execution_tracker.add_milestone("Global observation system active")
            else:
                self.logger.success("Global parallel observation system already active")
                self.execution_tracker.add_milestone("Global observation system verified active")
            
            self.logger.info("Starting main execution loop")
            
            while True:
                try:
                    # Get current prices for all legs
                    all_leg_keys = list(self.legs.keys())
                    leg_prices = self._get_leg_prices(all_leg_keys)
                    
                    # Process all users
                    processed_users = 0
                    active_users = 0
                    
                    for uid in self.uids:
                        if not self.all_legs_executed.get(uid, False):
                            active_users += 1
                            self.logger.debug(f"Processing user {uid}")
                            self._execute_both_pairs(uid, leg_prices)
                            processed_users += 1
                    
                    # Log progress periodically
                    if processed_users > 0:
                        self.logger.info(
                            f"Processed {processed_users} users, {active_users} still active"
                        )
                    
                    # Check if all users completed
                    if active_users == 0:
                        self.logger.success("All users completed execution")
                        self.execution_tracker.add_milestone("All users completed")
                        break
                    
                    time.sleep(1)  # Main loop delay
                    
                except KeyboardInterrupt:
                    self.logger.warning("Strategy execution interrupted by user")
                    self.execution_tracker.add_milestone("User interrupt received")
                    break
                    
                except Exception as loop_error:
                    self.logger.error(
                        "Error in main execution loop",
                        f"Will retry in 5 seconds",
                        loop_error
                    )
                    self.execution_tracker.add_error("Main loop error", str(loop_error), loop_error)
                    time.sleep(5)
            
            # Successful completion
            self.execution_tracker.complete_execution("Strategy completed successfully")
            self.logger.separator("STRATEGY EXECUTION COMPLETED")
            
        except Exception as e:
            self.logger.error("Critical error in main logic", "Execution will be terminated", e)
            if self.execution_tracker.execution_data:
                self.execution_tracker.handle_crash(e)
            else:
                self.logger.error("Failed to initialize execution tracking", exception=e)
            
        finally:
            # Cleanup
            try:
                self.logger.info("Stopping global observation system")
                self._stop_global_parallel_observation()
                self.logger.success("Cleanup completed successfully")
            except Exception as cleanup_error:
                self.logger.error("Error during cleanup", exception=cleanup_error)

    def set_execution_mode(self, mode):
        """Set execution mode (LIVE or SIMULATION)"""
        self.execution_helper.set_execution_mode(mode)
        StrategyLoggingHelpers.info(f"Execution mode changed to: {mode}")
        
    def get_execution_mode(self):
        """Get current execution mode"""
        return self.execution_helper.execution_mode
    
    def get_simulation_summary(self):
        """Get summary of simulation orders (only relevant in SIMULATION mode)"""
        if self.execution_helper.execution_mode == StrategyExecutionHelpers.SIMULATION_MODE:
            return self.execution_helper.get_simulation_summary()
        else:
            return "Strategy is in LIVE mode - no simulation data available"
    
    def clear_simulation_data(self):
        """Clear simulation data (only relevant in SIMULATION mode)"""
        if self.execution_helper.execution_mode == StrategyExecutionHelpers.SIMULATION_MODE:
            return self.execution_helper.clear_simulation_data()
        else:
            StrategyLoggingHelpers.warning("Cannot clear simulation data - strategy is in LIVE mode")
            return 0

    def __del__(self):
        """Cleanup when strategy instance is destroyed."""
        try:
            if hasattr(self, 'global_observation_active') and self.global_observation_active:
                self._stop_global_parallel_observation()
            if hasattr(self, 'logger'):
                self.logger.info("Strategy instance cleanup completed")
        except Exception as e:
            if hasattr(self, 'logger'):
                self.logger.error("Cleanup failed", exception=e)
            else:
                print(f"ERROR: Cleanup failed: {e}")
