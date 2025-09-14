"""
Direct IOC Box Strategy - Main Logic
Execute legs in pairs with direct IOC orders using bid/ask pricing
"""

from ast import Pass
import datetime
import os
from pickle import TRUE
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


class StratergyDirectIOCBoxDynamicStrikes:
    def __init__(self,params) -> None:
        try:
            print("Initializing Direct IOC Box Strategy with Dynamic Strikes... 123")
            self.params=params
            self.r = redis.Redis(host="localhost", port=6379, db=0)
            self.execution_tracker = StrategyExecutionTracker(self.r, "DirectIOCBoxDynamicStrikes")
            self.logger = StrategyLoggingHelpers
            
            self.lot_sizes = json.loads(self.r.get("lotsizes"))
            self._init_user_connections()

            self.templates_lock = threading.Lock()
            self.executor = ThreadPoolExecutor(max_workers=5)

            # Initialize execution mode (default is SIMULATION)
            execution_mode = self.params.get('execution_mode', StrategyExecutionHelpers.SIMULATION_MODE)
            self.execution_helper = StrategyExecutionHelpers(self.r, execution_mode)
            
            # Log execution mode
            StrategyLoggingHelpers.info(
                f"Strategy initialized in {execution_mode} mode",
                f"Params ID: {datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            
            self.entry_legs = {}
            self.open_order = False
            
            self.global_action = self.params.get('action', 'BUY').upper()
            # Load option mapper
            self._load_option_mapper()

            # Initialize tracking dictionaries
            self._init_tracking_data()

            # Initialize helper classes
            self._init_helpers()

            # Initialize legs and order templates
            self._init_legs_and_orders()
            # breakpoint()
            # Start global parallel observation
            self._init_global_parallel_observation()
            live_atm_thread = threading.Thread(target=self._live_atm_update_thread,daemon=True)
            live_atm_thread.start()
            self.live_atm_update_thread = live_atm_thread
        except Exception as e:
            StrategyLoggingHelpers.error("Failed to initialize strategy", exception=e)
            print(traceback.format_exc())
            # breakpoint()
            raise

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
        self.live_atm_update_thread = None
        self.stop_live_atm_thread = False
        self.current_atm = None
        self.current_atm_strike = None

    def _init_helpers(self):
        """Initialize helper class instances."""
        self.data_helpers = StrategyDataHelpers(self.r)
        self.pricing_helpers = None  # Will be initialized after params are loaded
       
        self.order_helpers = None  # Will be initialized after option_mapper is loaded
        self.tracking_helpers = StrategyTrackingHelpers(self.r, self.templates_lock)

    def _live_atm_update_thread(self):
        """Background thread to update parameters from Redis."""
        
        while not self.stop_live_atm_thread:
            try:
                ltp_base_index = json.loads(self.r.get(f"reduced_quotes:{self.params.get('symbol',"NIFTY")}"))
                ltp_base_index = float(ltp_base_index['response']['data'].get('ltp',0))
                atm_base_index = int(round(ltp_base_index / 50) * 50)
                if self.current_atm_strike != atm_base_index and (abs(ltp_base_index - self.current_atm) >= 50) and self.open_order==False:
                    self.logger.info(f"ATM updated to {self.current_atm_strike} based on LTP {self.current_atm} ,previous ATM {self.current_atm_strike} , previous LTP {self.current_atm}")
                    self.current_atm_strike = atm_base_index
                    self.current_atm = ltp_base_index
                    self._init_legs_and_orders()
                   
                    
                    
            except Exception as e:
                self.logger.error("Params update thread failed", exception=e)
                print(traceback.format_exc())

    def _init_global_parallel_observation(self):
        """
        Initialize global parallel observation for both pairs at startup.
        
        Note: This system runs continuously with 2-second observations for real-time
        execution decisions during order placement. For the initial CASE A/B decision,
        we use a separate dedicated 10-second observation via _observe_market_for_case_decision().
        """
        try:
            # Check if already initialized to prevent duplicate initialization
            if self.global_observation_active:
                self.logger.warning("Global parallel observation already active, skipping initialization")
                return
                
            if hasattr(self, 'pair1_bidding_leg') and hasattr(self, 'pair2_bidding_leg'):
                buy_leg_keys = [self.pair1_bidding_leg, self.pair1_base_leg]
                sell_leg_keys = [self.pair2_bidding_leg, self.pair2_base_leg]
                
                self.logger.info("Starting global parallel observation for all pairs (2-second cycles)")
                
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

    def _observe_market_for_pair(self, leg1_key, leg2_key, observation_duration=2,isExit=False):
        """Observe market for a specified duration and collect price data for analysis."""
        leg1_prices = []
        leg2_prices = []
        start_time = time.time()
        
        # Collect prices in a loop
        while time.time() - start_time < observation_duration:
            current_prices = self._get_leg_prices([leg1_key, leg2_key],isExit)
            leg1_price = current_prices.get(leg1_key, 0)
            leg2_price = current_prices.get(leg2_key, 0)
            
            # Only add valid prices
            if leg1_price > 0 and leg2_price > 0:
                leg1_prices.append(leg1_price)
                leg2_prices.append(leg2_price)
            
            time.sleep(0.2)  # Check every 200ms
        
        # Ensure we have enough data points
        if len(leg1_prices) < 3:
            self.logger.warning(f"Insufficient data for pair observation ({len(leg1_prices)} samples)")
            return False
        
        # Analyze trends
        leg1_trend, leg1_change = StrategyHelpers.analyze_price_trend(leg1_prices)
        leg2_trend, leg2_change = StrategyHelpers.analyze_price_trend(leg2_prices)
        
        # Determine leg action type (BUY or SELL) from the first leg
        leg1_action = self.entry_legs[leg1_key]['info'].get('action', self.global_action).upper()
        leg_action_type = "BUY" if leg1_action == "BUY" else "SELL"
        
        # Use new execution strategy logic
        execution_strategy = self._determine_execution_strategy(
            leg1_key, leg2_key, leg1_trend, leg2_trend, leg1_change, leg2_change, leg_action_type,False
        )
        
        execution_strategy_exit = self._determine_execution_strategy(
            leg1_key, leg2_key, leg1_trend, leg2_trend, leg1_change, leg2_change, leg_action_type,True
        )
        
        # Return execution details for proceeding with orders
        return {
            'first_leg': execution_strategy['first_leg'] if execution_strategy['action'] == 'EXECUTE' else None,
            'second_leg': execution_strategy['second_leg'] if execution_strategy['action'] == 'EXECUTE' else None,
            'trend': 'strategic_execution',
            'execution_strategy': execution_strategy,
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
            },
            'leg_action_type': leg_action_type,
            'exit_execution_strategy': execution_strategy_exit,
            'first_leg_exit': execution_strategy_exit['first_leg'] if execution_strategy_exit['action'] == 'EXECUTE' else None,
            'second_leg_exit': execution_strategy_exit['second_leg'] if execution_strategy_exit['action'] == 'EXECUTE' else None
        }

    def _determine_execution_strategy(self, leg1_key, leg2_key, leg1_trend, leg2_trend, leg1_change, leg2_change, leg_action_type="BUY",isExit=False):
        """
        Determine execution strategy based on market trends and order type.
        
        Args:
            leg1_key: First leg identifier
            leg2_key: Second leg identifier  
            leg1_trend: Trend for first leg ("STABLE", "INCREASING", "DECREASING")
            leg2_trend: Trend for second leg ("STABLE", "INCREASING", "DECREASING")
            leg1_change: Price change for first leg
            leg2_change: Price change for second leg
            leg_action_type: "BUY" or "SELL" to determine execution rules
            
        Returns:
            dict: Execution strategy with action and order details
        """
        try:
            
            if not isExit:
                if leg_action_type.upper() == "SELL":
                    return self._determine_sell_execution_strategy(leg1_key, leg2_key, leg1_trend, leg2_trend, leg1_change, leg2_change,isExit)
                else:  # BUY
                    return self._determine_buy_execution_strategy(leg1_key, leg2_key, leg1_trend, leg2_trend, leg1_change, leg2_change,isExit)
            else:
                # For exit, use same logic as entry but with different thresholds if needed
                if leg_action_type.upper() == "SELL":
                    return self._determine_buy_execution_strategy(leg1_key, leg2_key, leg1_trend, leg2_trend, leg1_change, leg2_change,isExit)
                else:  # BUY
                    return self._determine_sell_execution_strategy(leg1_key, leg2_key, leg1_trend, leg2_trend, leg1_change, leg2_change,isExit)
                
        except Exception as e:
            self.logger.error("Failed to determine execution strategy", exception=e)
            return {"action": "SKIP", "reason": "strategy_determination_failed"}

    def _determine_sell_execution_strategy(self, leg1_key, leg2_key, leg1_trend, leg2_trend, leg1_change, leg2_change,isExit):
        """
        SELL execution rules:
        1. Both legs stable -> execute legs
        2. One decreasing, other stable -> do not execute  
        3. One increasing, other decreasing -> execute decreasing first
        4. One increasing, other stable -> execute stable first
        """
        # Rule 1: Both legs stable -> execute
        if leg1_trend == "STABLE" and leg2_trend == "STABLE":
            # self.logger.success("SELL Strategy: Both legs stable - executing both legs")
            return {
                "action": "EXECUTE",
                "strategy": "both_stable",
                "first_leg": leg1_key,
                "second_leg": leg2_key,
                "reason": "Both legs are stable - safe to execute"
            }
        
        # Rule 2: One decreasing, other stable -> do not execute
        if ((leg1_trend == "DECREASING" and leg2_trend == "STABLE") or 
            (leg1_trend == "STABLE" and leg2_trend == "DECREASING")):
            # self.logger.warning("SELL Strategy: One decreasing, other stable - skipping execution")
            return {
                "action": "SKIP",
                "strategy": "decreasing_with_stable", 
                "reason": "One leg decreasing with other stable - unfavorable for SELL"
            }
        
        # Rule 3: One increasing, other decreasing -> execute decreasing first
        if ((leg1_trend == "INCREASING" and leg2_trend == "DECREASING") or
            (leg1_trend == "DECREASING" and leg2_trend == "INCREASING")):
            decreasing_leg = leg1_key if leg1_trend == "DECREASING" else leg2_key
            increasing_leg = leg2_key if leg1_trend == "DECREASING" else leg1_key
            # self.logger.success(f"SELL Strategy: Mixed trends - executing decreasing leg ({decreasing_leg}) first")
            return {
                "action": "EXECUTE",
                "strategy": "decreasing_first",
                "first_leg": decreasing_leg,
                "second_leg": increasing_leg,
                "reason": f"Execute decreasing leg first to capture better SELL price"
            }
        
        # Rule 4: One increasing, other stable -> execute stable first
        if ((leg1_trend == "INCREASING" and leg2_trend == "STABLE") or
            (leg1_trend == "STABLE" and leg2_trend == "INCREASING")):
            stable_leg = leg1_key if leg1_trend == "STABLE" else leg2_key
            increasing_leg = leg2_key if leg1_trend == "STABLE" else leg1_key
            # self.logger.success(f"SELL Strategy: Increasing with stable - executing stable leg ({stable_leg}) first")
            return {
                "action": "EXECUTE", 
                "strategy": "stable_first",
                "first_leg": stable_leg,
                "second_leg": increasing_leg,
                "reason": f"Execute stable leg first, then capture increasing trend"
            }
        
        # Additional cases: Both increasing or both decreasing
        if leg1_trend == "INCREASING" and leg2_trend == "INCREASING":
            # Both increasing - execute based on change magnitude (stronger increase first)
            if abs(leg1_change) >= abs(leg2_change):
                first_leg, second_leg = leg1_key, leg2_key
            else:
                first_leg, second_leg = leg2_key, leg1_key
            # self.logger.info("SELL Strategy: Both increasing - executing stronger increase first")
            return {
                "action": "EXECUTE",
                "strategy": "both_increasing",
                "first_leg": first_leg,
                "second_leg": second_leg,
                "reason": "Both increasing - execute stronger movement first"
            }
        
        if leg1_trend == "DECREASING" and leg2_trend == "DECREASING":
            # Both decreasing - execute based on change magnitude (stronger decrease first)
            return {
                "action": "SKIP",
                "strategy": "decreasing_with_decreasing", 
                "reason": "Both Decreasing - unfavorable for SELL"
            }
        
        # Fallback
        # self.logger.warning("SELL Strategy: Unhandled case - using default execution")
        return {
            "action": "EXECUTE",
            "strategy": "default",
            "first_leg": leg1_key,
            "second_leg": leg2_key,
            "reason": "Unhandled trend combination - default execution"
        }

    def _determine_buy_execution_strategy(self, leg1_key, leg2_key, leg1_trend, leg2_trend, leg1_change, leg2_change,isExit):
        """
        BUY execution rules:
        1. Both stable -> execute
        2. One increasing, other stable -> do not execute
        3. One stable, other decreasing -> execute stable first  
        4. One increasing, other decreasing -> execute increasing first
        """
        # Rule 1: Both legs stable -> execute
        if leg1_trend == "STABLE" and leg2_trend == "STABLE":
            # self.logger.success("BUY Strategy: Both legs stable - executing both legs")
            return {
                "action": "EXECUTE",
                "strategy": "both_stable",
                "first_leg": leg1_key,
                "second_leg": leg2_key,
                "reason": "Both legs are stable - safe to execute"
            }
        
        # Rule 2: One increasing, other stable -> do not execute
        if ((leg1_trend == "INCREASING" and leg2_trend == "STABLE") or
            (leg1_trend == "STABLE" and leg2_trend == "INCREASING")):
            # self.logger.warning("BUY Strategy: One increasing, other stable - skipping execution")
            return {
                "action": "SKIP",
                "strategy": "increasing_with_stable",
                "reason": "One leg increasing with other stable - unfavorable for BUY"
            }
        
        # Rule 3: One stable, other decreasing -> execute stable first
        if ((leg1_trend == "STABLE" and leg2_trend == "DECREASING") or
            (leg1_trend == "DECREASING" and leg2_trend == "STABLE")):
            stable_leg = leg1_key if leg1_trend == "STABLE" else leg2_key
            decreasing_leg = leg2_key if leg1_trend == "STABLE" else leg1_key
            # self.logger.success(f"BUY Strategy: Stable with decreasing - executing stable leg ({stable_leg}) first")
            return {
                "action": "EXECUTE",
                "strategy": "stable_first",
                "first_leg": stable_leg,
                "second_leg": decreasing_leg,
                "reason": f"Execute stable leg first, then capture decreasing price"
            }
        
        # Rule 4: One increasing, other decreasing -> execute increasing first
        if ((leg1_trend == "INCREASING" and leg2_trend == "DECREASING") or
            (leg1_trend == "DECREASING" and leg2_trend == "INCREASING")):
            increasing_leg = leg1_key if leg1_trend == "INCREASING" else leg2_key
            decreasing_leg = leg2_key if leg1_trend == "INCREASING" else leg1_key
            # self.logger.success(f"BUY Strategy: Mixed trends - executing increasing leg ({increasing_leg}) first")
            return {
                "action": "EXECUTE",
                "strategy": "increasing_first",
                "first_leg": increasing_leg,
                "second_leg": decreasing_leg,
                "reason": f"Execute increasing leg first to secure better BUY position"
            }
        
        # Additional cases: Both increasing or both decreasing
        if leg1_trend == "INCREASING" and leg2_trend == "INCREASING":
            # Both increasing - execute based on change magnitude (stronger increase first)
            # self.logger.warning("BUY Strategy: Both increasing - skipping execution")
            return {
                "action": "SKIP",
                "strategy": "increasing_with_increasing", 
                "reason": "Both Incresing - unfavorable for SELL"
            }
        
        if leg1_trend == "DECREASING" and leg2_trend == "DECREASING":
            # Both decreasing - execute based on change magnitude (stronger decrease first) 
            if abs(leg1_change) >= abs(leg2_change):
                first_leg, second_leg = leg1_key, leg2_key
            else:
                first_leg, second_leg = leg2_key, leg1_key
            # self.execution_tracker.add_observation("BUY Strategy: Both decreasing - executing stronger decrease first")
            return {
                "action": "EXECUTE",
                "strategy": "both_decreasing",
                "first_leg": first_leg,
                "second_leg": second_leg,
                "reason": "Both decreasing - execute stronger movement first for better BUY price"
            }
        
            # Fallback
        self.logger.warning("BUY Strategy: Unhandled case - using default execution")
        return {
            "action": "EXECUTE",
            "strategy": "default",
            "first_leg": leg1_key,
            "second_leg": leg2_key,
            "reason": "Unhandled trend combination - default execution"
        }

    def _observe_market_for_case_decision(self, leg1_key, leg2_key, observation_duration=10,isExit=False):
        """
        Dedicated 10-second market observation specifically for CASE A/B decision.
        More comprehensive analysis with detailed logging for critical decision making.
        """
        try:
            self.logger.info(
                f"Starting {observation_duration}-second dedicated observation for CASE decision",
                f"Legs: {leg1_key}, {leg2_key}"
            )
            
            leg1_prices = []
            leg2_prices = []
            timestamps = []
            start_time = time.time()
            
            # Collect prices every 200ms for the specified duration
            sample_count = 0
            valid_samples = 0
            atm_base_index = self.current_atm_strike
            while time.time() - start_time < observation_duration:
                if self.current_atm_strike != atm_base_index:
                    self.logger.info(f"ATM changed during observation from {atm_base_index} to {self.current_atm_strike}, aborting CASE decision observation")
                    return 1
                current_time = time.time()
                current_prices = self._get_leg_prices([leg1_key, leg2_key],isExit)
                leg1_price = current_prices.get(leg1_key, 0)
                leg2_price = current_prices.get(leg2_key, 0)
                
                if leg1_price > 0 and leg2_price > 0:  # Valid prices
                    if leg1_prices and leg1_price != leg1_prices[-1]: 
                        leg1_prices.append(leg1_price)
                    elif not leg1_prices:
                        leg1_prices.append(leg1_price)
                    else:
                        continue  # Skip duplicate price
                    if leg2_prices and leg2_price != leg2_prices[-1]:
                        leg2_prices.append(leg2_price)
                        timestamps.append(current_time)
                        valid_samples += 1
                    elif not leg2_prices:
                        leg2_prices.append(leg2_price)
                        timestamps.append(current_time)
                        valid_samples += 1
                    else:
                        continue  # Skip duplicate price
                    
                    
                    # Log first few samples to verify we're getting fresh data
                    if valid_samples <= 3:
                        self.logger.debug(
                            f"Sample {valid_samples}: {leg1_key}={leg1_price:.2f}, {leg2_key}={leg2_price:.2f}",
                            f"Fresh data fetched at {current_time:.3f}"
                        )
                else:
                    self.logger.warning(f"Invalid prices received: {leg1_key}={leg1_price}, {leg2_key}={leg2_price}")
                
                sample_count += 1
                
                # Progress logging every 2 seconds
                elapsed = current_time - start_time
                if valid_samples > 0 and int(elapsed) % 2 == 0 and elapsed > 0:
                    progress = (elapsed / observation_duration) * 100
                    self.logger.debug(
                        f"Case observation progress: {progress:.0f}%",
                        f"Valid samples: {valid_samples}/{sample_count}, {leg1_key}: {leg1_price:.2f}, {leg2_key}: {leg2_price:.2f}"
                    )
                
                time.sleep(0.2)  # 200ms intervals
            
            # Ensure we have enough valid data points
            if len(leg1_prices) < 10:
                self.logger.warning(
                    f"Insufficient valid data for case decision ({len(leg1_prices)} samples out of {sample_count} attempts)",
                    "Defaulting to CASE A (STABLE)"
                )
                return False
            
            # Enhanced trend analysis for critical decision
            leg1_trend, leg1_change = StrategyHelpers.analyze_price_trend(leg1_prices)
            leg2_trend, leg2_change = StrategyHelpers.analyze_price_trend(leg2_prices)
            
            # Calculate additional statistics for better decision making
            leg1_volatility = self._calculate_price_volatility(leg1_prices)
            leg2_volatility = self._calculate_price_volatility(leg2_prices)
            leg1_direction = "UP" if leg1_prices[-1] > leg1_prices[0] else "DOWN" if leg1_prices[-1] < leg1_prices[0] else "FLAT"
            leg2_direction = "UP" if leg2_prices[-1] > leg2_prices[0] else "DOWN" if leg2_prices[-1] < leg2_prices[0] else "FLAT"
            
            # Log detailed analysis
            self.logger.info(
                f"10-second observation completed - {sample_count} samples",
                f"{leg1_key}: {leg1_trend} ({leg1_direction}, change: {leg1_change:.2f}, volatility: {leg1_volatility:.2f})\n" +
                f"{leg2_key}: {leg2_trend} ({leg2_direction}, change: {leg2_change:.2f}, volatility: {leg2_volatility:.2f})"
            )
            
            # Enhanced decision logic: Both legs must be STABLE for CASE A
            if leg1_trend == "STABLE" and leg2_trend == "STABLE":
                self.logger.success(
                    "DECISION: Both BUY legs are STABLE over 10 seconds → CASE A",
                    f"Leg volatilities: {leg1_key}={leg1_volatility:.2f}, {leg2_key}={leg2_volatility:.2f}"
                )
                return False  # CASE A
            
            # At least one leg is moving - CASE B
            moving_legs = []
            if leg1_trend != "STABLE":
                moving_legs.append(f"{leg1_key}({leg1_trend})")
            if leg2_trend != "STABLE":
                moving_legs.append(f"{leg2_key}({leg2_trend})")
            
            self.logger.warning(
                f"DECISION: BUY legs are MOVING over 10 seconds → CASE B",
                f"Moving legs: {', '.join(moving_legs)}"
            )
            
            # Determine execution order for moving legs (stronger movement first)
            if not isExit:
                if abs(leg1_change) > abs(leg2_change):
                    first_leg, second_leg = leg1_key, leg2_key
                    primary_change, secondary_change = leg1_change, leg2_change
                elif abs(leg2_change) > abs(leg1_change):
                    first_leg, second_leg = leg2_key, leg1_key
                    primary_change, secondary_change = leg2_change, leg1_change
                else:
                    # Equal movement, use original order
                    first_leg, second_leg = leg1_key, leg2_key
                    primary_change, secondary_change = leg1_change, leg2_change
            else:
                if abs(leg1_change) < abs(leg2_change):
                    first_leg, second_leg = leg1_key, leg2_key
                    primary_change, secondary_change = leg1_change, leg2_change
                elif abs(leg2_change) < abs(leg1_change):
                    first_leg, second_leg = leg2_key, leg1_key
                    primary_change, secondary_change = leg2_change, leg1_change
                else:
                    # Equal movement, use original order
                    first_leg, second_leg = leg1_key, leg2_key
                    primary_change, secondary_change = leg1_change, leg2_change
            
            return {
                'first_leg': first_leg,
                'second_leg': second_leg,
                'trend': 'moving',
                'observation_duration': observation_duration,
                'sample_count': sample_count,
                'price_data': {
                    leg1_key: leg1_prices,
                    leg2_key: leg2_prices,
                    'timestamps': timestamps
                },
                'trends': {
                    leg1_key: {
                        'trend': leg1_trend, 
                        'change': leg1_change, 
                        'volatility': leg1_volatility,
                        'direction': leg1_direction
                    },
                    leg2_key: {
                        'trend': leg2_trend, 
                        'change': leg2_change, 
                        'volatility': leg2_volatility,
                        'direction': leg2_direction
                    }
                },
                'final_prices': {
                    leg1_key: leg1_prices[-1],
                    leg2_key: leg2_prices[-1]
                },
                'execution_order': {
                    'primary_leg': first_leg,
                    'secondary_leg': second_leg,
                    'primary_change': primary_change,
                    'secondary_change': secondary_change
                }
            }
            
        except Exception as e:
            self.logger.error(f"Failed to observe market for case decision", exception=e)
            self.logger.warning("Defaulting to CASE A due to observation error")
            return False  # Default to CASE A on error

    def _get_leg_prices(self, leg_keys, is_exit=False):
        """Get current prices for specified legs based on their actions."""
        return self.pricing_helpers.get_leg_prices(
            self.entry_legs, leg_keys, self.global_action, self.data_helpers, is_exit
        )

    def _calculate_price_volatility(self, prices):
        """Calculate price volatility (standard deviation) for a price series."""
        return self.pricing_helpers.calculate_price_volatility(prices)

    def _get_lot_size(self):
        """Get the lot size for the trading symbol."""
        if hasattr(self, 'entry_legs') and self.entry_legs:
            first_leg = next(iter(self.entry_legs.values()))
            symbol = first_leg['info'].get('symbol', 'NIFTY').upper()
            return self.lot_sizes.get(symbol, 75)
        return 75

    def _determine_leg_pairs(self, all_leg_keys):
        """Automatically determine pairs based on BUY/SELL actions."""
        buy_legs = []
        sell_legs = []
        
        for leg_key in all_leg_keys:
            leg_action = self.entry_legs[leg_key]['info'].get('action', self.global_action).upper()
            if leg_action == "BUY":
                buy_legs.append(leg_key)
            else:
                sell_legs.append(leg_key)
        
        self.logger.info("Auto-detected BUY legs", str(buy_legs))
        self.logger.info("Auto-detected SELL legs", str(sell_legs))
        
        # Assign pairs based on available legs
        pair1_bidding, pair1_base = self._assign_pair(buy_legs, sell_legs, all_leg_keys)
        pair2_bidding, pair2_base = self._assign_remaining_pair(
            sell_legs, buy_legs, all_leg_keys, pair1_bidding, pair1_base)
        
        return pair1_bidding, pair1_base, pair2_bidding, pair2_base

    def _assign_pair(self, primary_legs, secondary_legs, all_leg_keys):
        """Assign first pair from primary legs."""
        if len(primary_legs) >= 2:
            return primary_legs[0], primary_legs[1]
        
    def _assign_remaining_pair(self, sell_legs, buy_legs, all_leg_keys, pair1_bidding, pair1_base):
        """Assign second pair from remaining legs."""
        if len(sell_legs) >= 2:
            return sell_legs[0], sell_legs[1]

    def _init_legs_and_orders(self):
        """Initialize 4-leg sequential box strategy with optimized leg pairing."""
        # Load legs data
        ltp_base_index = json.loads(self.r.get(f"reduced_quotes:{self.params.get('symbol',"NIFTY")}")) or 0
        ltp_base_index = float(ltp_base_index['response']['data']['ltp'] or 0)
        atm_base_index = int(round(ltp_base_index / 50) * 50)
        self.current_atm = ltp_base_index
        self.current_atm_strike = atm_base_index
        
      
        for i in range(0,4):
            self.entry_legs[f'leg{i+1}'] = {
                "strike":atm_base_index - (self.params.get('itm_steps'))*50 if i%3==0 else atm_base_index + (self.params.get('otm_steps'))*50,
                "type":"CE" if i%2==0 else "PE",
                "symbol":self.params.get('symbol',"NIFTY"),
                "expiry":self.params.get('expiry',""),
                "quantity":self.params.get('quantity',75),
                "action":"BUY" if i<2 else "SELL"
                    }
       
        # Load base legs
        base_leg_keys = ["leg1", "leg2", "leg3", "leg4"] # Fixed 4 legs for box strategy
        all_leg_keys = base_leg_keys
        
     
        for leg_key in base_leg_keys:
            leg_info = self.entry_legs.get(leg_key)
            if not leg_info:
                print(f"WARNING: Missing leg info for {leg_key}")
                continue
            self.entry_legs[leg_key] = self.data_helpers.load_leg_data(leg_key, leg_info)
        
        # Determine pairs automatically
        (self.pair1_bidding_leg, self.pair1_base_leg, 
         self.pair2_bidding_leg, self.pair2_base_leg) = self._determine_leg_pairs(all_leg_keys)
        
        print(f"INFO: Pair 1 - Bidding: {self.pair1_bidding_leg}, Base: {self.pair1_base_leg}")
        print(f"INFO: Pair 2 - Bidding: {self.pair2_bidding_leg}, Base: {self.pair2_base_leg}")
        
        # Validate leg assignments
        required_legs = [self.pair1_bidding_leg, self.pair1_base_leg, self.pair2_bidding_leg, self.pair2_base_leg]
        for leg in required_legs:
            if leg not in self.entry_legs:
                raise RuntimeError(f"Missing required leg: {leg}")
        # Determine exchange
        self._determine_exchange()
        
        # Initialize helper classes that depend on loaded data
        self.pricing_helpers = StrategyPricingHelpers(self.params)
        self.order_helpers = StrategyOrderHelpers(self.params, self.option_mapper, self.exchange)
        
        # Setup user data
        self._setup_user_data(all_leg_keys)
        
        # Create order templates
        self._create_order_templates(all_leg_keys=['leg1','leg2','leg3','leg4'])
        

    def _determine_exchange(self):
        """Determine exchange from first leg symbol."""
        first_leg = list(self.entry_legs.values())[0]['data']
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

    def _create_order_templates(self, all_leg_keys=['leg1','leg2','leg3','leg4']):
        """Create order templates for all legs and users."""
        self.order_templates = {}
        self.exit_order_templates = {}

        for uid in self.uids:
            self.order_templates[uid] = {}
            self.exit_order_templates[uid] = {}
            
            for leg_key in all_leg_keys:
                leg_data = self.entry_legs[leg_key]['data']
                leg_action = self.entry_legs[leg_key]['info'].get('action', self.global_action).upper()
                
                # Entry order template
                self.order_templates[uid][leg_key] = self.order_helpers.make_order_template(
                    leg_data, leg_action, uid, leg_key,lotsizes=self.lot_sizes)
                
                # Exit order template (opposite action)
                exit_action = "SELL" if leg_action == "BUY" else "BUY"
                self.exit_order_templates[uid][leg_key] = self.order_helpers.make_order_template(
                    leg_data, exit_action, uid, leg_key,lotsizes=self.lot_sizes)

    def _check_desired_quantity_reached(self, uid,isExit=False):
        """Check if the desired quantities have been reached for all legs."""
        return StrategyQuantityHelpers.check_desired_quantity_reached(
            self.entry_legs, self.params, self.entry_qtys if not isExit else self.exit_qtys, uid
        )

    def _get_remaining_quantity_for_leg(self, uid, leg_key, isExit=False):
        """Get the remaining quantity that can be executed for a specific leg."""
        return StrategyQuantityHelpers.get_remaining_quantity_for_leg(
            self.params, self.entry_qtys if not isExit else self.exit_qtys, uid, leg_key, isExit
        )

    def _place_modify_order_until_complete(self, uid, leg_key, max_attempts=30, modify_interval=0.3,isExit=False):
        """
        Place order using MODIFY strategy until completion.
        
        Args:
            uid: User ID
            leg_key: Leg identifier  
            max_attempts: Maximum modification attempts
            modify_interval: Time between modifications (seconds)
            
        Returns:
            dict: Execution result with filled_qty, filled_price, success status
        """
        try:
            
            self.logger.info(f"Starting MODIFY execution for {leg_key}", f"User: {uid}, Max attempts: {max_attempts}")
            
            # Get remaining quantity and prepare initial order
            remaining_qty, desired_qty, current_qty = self._get_remaining_quantity_for_leg(uid, leg_key,isExit)
            if remaining_qty <= 0:
                self.logger.success(f"Desired quantity already reached for {leg_key}")
                return {"success": True, "filled_qty": current_qty, "filled_price": 0, "reason": "already_filled"}
            
            # Get current price and prepare order
            if not isExit:
                current_prices = self._get_leg_prices([leg_key],isExit)
                order = self.order_templates[uid][leg_key].copy()
                order["Quantity"] = remaining_qty
                if order['Quantity'] < order['Slice_Quantity']:
                    order['Slice_Quantity'] = order['Quantity']
            else:
                current_prices = self._get_leg_prices([leg_key],isExit)
                order = self.exit_order_templates[uid][leg_key].copy()
                order["Quantity"] = int(self.entry_qtys[uid][leg_key]) - int(self.exit_qtys[uid][leg_key])
                if order["Quantity"] < order['Slice_Quantity']:
                    order['Slice_Quantity'] = order['Quantity']
                if order["Quantity"] <= 0:
                    self.logger.success(f"Exit quantity already reached for {leg_key}")
                    return {"success": True, "filled_qty": self.exit_qtys[uid][leg_key], "filled_price": 0, "reason": "already_exited"}
            
            # Add price adjustment based on action
            tick = 0.05 if order.get("Action") == ActionEnum.BUY else -0.05
            order["Limit_Price"] = StrategyHelpers.format_limit_price(float(current_prices[leg_key]) + tick)
            
            # Place initial order
            self.logger.info(f"Placing initial order for {leg_key}", f"Qty: {remaining_qty}, Price: {order['Limit_Price']}")
            success, result = self.execution_helper.execute_order(self.order, order, uid, leg_key)
            
            if not success:
                self.logger.error(f"Initial order placement failed for {leg_key}")
                return {"success": False, "filled_qty": 0, "filled_price": 0, "reason": "initial_placement_failed"}
            
            # if self.execution_helper.execution_mode == "SIMULATION":
            order_id = result.get('order_id') if isinstance(result, dict) else None
            if not order_id:
                self.logger.error(f"No order ID received for {leg_key}")
                return {"success": False, "filled_qty": 0, "filled_price": 0, "reason": "no_order_id"}
            
            # Monitor and modify until completion
            total_filled_qty = 0
            attempt = 0
            previous_price = float(order["Limit_Price"])
            while attempt < max_attempts and total_filled_qty < remaining_qty:
                time.sleep(modify_interval)
                # Check order status
                if self.execution_helper.execution_mode == "SIMULATION":
                    status = result
                    current_filled = status.get('quantity', 0)
                    filled_price = order.get('Limit_Price', 0)
                    order_status = status.get('status', 'unknown')
                else:
                    status = self.order.get_order_status(order_id, uid, order.get('remark', 'Lord_Shreeji'))
                    current_filled = status.get('filled_qty', 0)
                    filled_price = status.get('filled_price', 0)
                    order_status = status.get('order_status', 'unknown')
                
                self.logger.debug(f"MODIFY attempt {attempt} for {leg_key}", 
                                f"Filled: {current_filled}/{remaining_qty}, Price: {filled_price}, Status: {order_status}")
                
                if current_filled >= remaining_qty:
                    # Order completed
                    self.logger.success(f"MODIFY execution completed for {leg_key}", 
                                      f"Filled: {current_filled}, Price: {filled_price}")
                    if isExit:
                        self.exit_qtys[uid][leg_key] += int(total_filled_qty)
                    else:
                        self.entry_qtys[uid][leg_key] += int(total_filled_qty)
                    return {"success": True, "filled_qty": current_filled, "filled_price": filled_price, "order_id": order_id}
                
                # Get fresh price for modification
                fresh_prices = self._get_leg_prices([leg_key],isExit)
                new_limit_price = StrategyHelpers.format_limit_price(float(fresh_prices[leg_key]) + tick)
                if float(new_limit_price) == previous_price:
                    self.logger.debug(f"No price change for {leg_key}, skipping modification")
                    continue  # Skip modification if price hasn't changed
                # Modify order with new price
                modify_details = {
                    'user_id': uid,
                    'Order_ID': order_id,
                    'Trading_Symbol': order.get('Trading_Symbol'),
                    'Exchange': order.get('Exchange'),
                    'Action': order.get('Action'),
                    'Order_Type': order.get('Order_Type', OrderTypeEnum.LIMIT),
                    'Quantity': order.get('Slice_Quantity',remaining_qty),
                    'CurrentQuantity': order.get('Slice_Quantity') - current_filled,
                    'Limit_Price': new_limit_price,
                    'Streaming_Symbol': order.get('Streaming_Symbol'),
                    'ProductCode': order.get('ProductCode')
                }
                
                modify_result = self.order.modify_order(modify_details)
                
                if modify_result.get('status') == 'success':
                    self.logger.debug(f"Order modified for {leg_key}", f"New price: {new_limit_price}")
                else:
                    self.logger.warning(f"Order modification failed for {leg_key}", str(modify_result))
                
                total_filled_qty = current_filled
                attempt += 1
            # Final status check
            if self.execution_helper.execution_mode == "LIVE":
                final_status = self.order.get_order_status(order_id, uid, order.get('remark', 'Lord_Shreeji'))
                final_filled = final_status.get('filled_qty', 0)
                final_price = final_status.get('filled_price', 0)
            else:
                final_status = result
                final_filled = final_status.get('quantity', 0)
                final_price = order.get('Limit_Price', 0)
            
            if final_filled >= order['Slice_Quantity']:
                self.logger.success(f"MODIFY execution completed for {leg_key} after {attempt} attempts")
                if not isExit:
                    self.entry_qtys[uid][leg_key] += final_filled
                else:
                    self.exit_qtys[uid][leg_key] += final_filled
                return {"success": True, "filled_qty": final_filled, "filled_price": final_price, "order_id": order_id}
            else:
                self.logger.warning(f"MODIFY execution incomplete for {leg_key}", 
                                  f"Filled: {final_filled}/{remaining_qty} after {attempt} attempts")
                return {"success": False, "filled_qty": final_filled, "filled_price": final_price, 
                       "reason": "max_attempts_reached", "order_id": order_id}
                
        except Exception as e:
            self.logger.error(f"Exception during MODIFY execution for {leg_key}", exception=traceback.format_exc())
            self.logger.error(f"MODIFY execution failed for {leg_key}", exception=e)
            return {"success": False, "filled_qty": 0, "filled_price": 0, "reason": str(e)}

    def _place_ioc_order_with_retry(self, uid, leg_key, max_retries=1, isExit=False):
        """
        Place IOC order with retry logic and spread condition checking.
        
        Args:
            uid: User ID
            leg_key: Leg identifier
            max_retries: Maximum retry attempts
            spread_check_func: Function to check if spread condition is still met (returns True/False)
            
        Returns:
            dict: Execution result with filled_qty, filled_price, success status
        """
        try:
            self.logger.info(f"Starting IOC execution with retry for {leg_key}", f"User: {uid}, Max retries: {max_retries}")
            
            retry_count = 0
            total_filled_qty = 0
            
            while retry_count < max_retries:
                
                # Get remaining quantity
                remaining_qty, desired_qty, current_qty = self._get_remaining_quantity_for_leg(uid, leg_key,isExit)
                if remaining_qty <= 0:
                    self.logger.success(f"Desired quantity already reached for {leg_key}")
                    return {"success": True, "filled_qty": current_qty, "filled_price": 0, "reason": "already_filled"}
                
                # Prepare IOC order
                current_prices = self._get_leg_prices([leg_key], isExit)
                if not isExit:
                    order = self.order_templates[uid][leg_key].copy() 
                    order["Quantity"] = remaining_qty

                    if remaining_qty <= order['Slice_Quantity']:
                        order["Slice_Quantity"] = int(remaining_qty)  # Exit full executed qty
                    order["Order_Type"] = OrderTypeEnum.LIMIT
                    order["Duration"] = DurationEnum.IOC
                    order["IOC"] = 0.5  # 500ms timeout
                else:
                    order = self.exit_order_templates[uid][leg_key].copy() 
                    order["Quantity"] = self.entry_qtys[uid][leg_key] - self.exit_qtys[uid][leg_key]  # Exit full executed qty
                    if int(order["Quantity"]) <= order['Slice_Quantity']:
                        order["Slice_Quantity"] = int(order["Quantity"])  # Exit full executed qty
                    if order['Quantity'] <= 0:
                        # self.logger.info(f"No remaining quantity to execute for {leg_key}")
                        return {"success": True, "filled_qty": current_qty, "filled_price": 0, "reason": "no_remaining_qty"}
                    order["Order_Type"] = OrderTypeEnum.LIMIT
                    order["Duration"] = DurationEnum.IOC
                    order["IOC"] = 0.5
                
                
                # Add price adjustment
                tick = 0.05 if order.get("Action") == ActionEnum.BUY else -0.05
                order["Limit_Price"] = StrategyHelpers.format_limit_price(float(current_prices[leg_key]) + tick)
                
                retry_count += 1
                self.logger.info(f"IOC attempt {retry_count}/{max_retries} for {leg_key}", 
                               f"Qty: {remaining_qty}, Price: {order['Limit_Price']}")
                
                # Place IOC order
                success, result = self.execution_helper.execute_order(self.order, order, uid, leg_key)
                
                if success:
                    # IOC order placed successfully - check fill
                    order_id = result.get('order_id') if isinstance(result, dict) else None
                    if order_id:
                        # Wait a moment for fill data
                        time.sleep(0.2)
                        if self.execution_helper.execution_mode == "SIMULATION":
                            status = result
                            filled_qty = status.get('quantity', 0)
                            filled_price = order['Limit_Price']
                        else:
                            status = self.order.get_order_status(order_id, uid, order.get('remark', 'Lord_Shreeji'))
                            filled_qty = status.get('filled_qty', 0)
                            filled_price = status.get('filled_price', 0)
                        
                        total_filled_qty += filled_qty
                        
                        if filled_qty > 0:
                            self.logger.success(f"IOC order filled for {leg_key}", 
                                              f"Filled: {filled_qty}, Price: {filled_price}")
                            
                            # Check if we have enough quantity
                            if total_filled_qty >= order['Slice_Quantity']:
                                if isExit:
                                    self.exit_qtys[uid][leg_key] += int(total_filled_qty)
                                else:
                                    self.entry_qtys[uid][leg_key] += int(total_filled_qty)
                                
                                return {"success": True, "filled_qty": int(total_filled_qty), "filled_price": filled_price, 
                                       "attempts": retry_count}
                        else:
                            self.logger.warning(f"IOC order placed but not filled for {leg_key}")
                    else:
                        self.logger.warning(f"IOC order placed but no order ID for {leg_key}")
                else:
                    self.logger.warning(f"IOC order placement failed for {leg_key}", str(result))
                
                # Small delay before retry
                if retry_count < max_retries:
                    time.sleep(0.1)
            
            # All retries exhausted
            if total_filled_qty > 0:
                self.logger.warning(f"IOC execution partially completed for {leg_key}", 
                                  f"Filled: {total_filled_qty}/{desired_qty} after {retry_count} attempts")
                return {"success": False, "filled_qty": int(total_filled_qty), "filled_price": 0, 
                       "reason": "partial_fill", "attempts": retry_count}
            else:
                self.logger.error(f"IOC execution failed for {leg_key}", f"No fills after {retry_count} attempts")
                return {"success": False, "filled_qty": 0, "filled_price": 0, 
                       "reason": "no_fills", "attempts": retry_count}
                       
        except Exception as e:
            self.logger.error(f"IOC execution with retry failed for {leg_key}", exception=e)
            return {"success": False, "filled_qty": 0, "filled_price": 0, "reason": str(e)}

    def _execute_both_pairs(self, uid, prices, isExit=False):
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
            self._execute_dual_strategy_pairs(uid, prices,isExit)
            
        except KeyboardInterrupt:
            self.logger.warning("Execution interrupted by user")
            self.execution_tracker.add_milestone("User interrupt during pair execution")
            return False
        except Exception as e:
            self.logger.error(f"Failed to execute both pairs for user {uid}", exception=e)
            self.execution_tracker.add_error(f"Pair execution failed for user {uid}", str(e), e)
            return False

    def _execute_dual_strategy_pairs(self, uid, prices,isExit=False):
        """Execute pairs using dual strategy approach with dedicated 10-second BUY pair observation."""
        try:
            buy_pair_observation = 1
            while buy_pair_observation == 1:
                self.logger.separator(f"DUAL STRATEGY EXECUTION - USER {uid}")
                
                # Step 1: Identify BUY and SELL leg pairs
                buy_leg_keys = [self.pair1_bidding_leg, self.pair1_base_leg]
                sell_leg_keys = [self.pair2_bidding_leg, self.pair2_base_leg]
                
                self.logger.info(
                    "Starting 10-second dedicated observation for CASE A/B decision",
                    f"BUY legs: {buy_leg_keys}"
                )
                
                # Step 2: Perform dedicated 10-second observation for BUY pair decision
                self.execution_tracker.add_milestone("Starting 10-second BUY pair observation for case decision", {
                    "user": uid,
                    "buy_legs": buy_leg_keys,
                    "sell_legs": sell_leg_keys,
                    "observation_duration": 10
                })
                
                # Dedicated 10-second observation specifically for CASE A/B decision
                buy_pair_observation = self._observe_market_for_case_decision(buy_leg_keys[0], buy_leg_keys[1], 60,isExit)
                # breakpoint()
                self.execution_tracker.add_observation("BUY_PAIR_CASE_DECISION", {
                    "user": uid,
                    "buy_legs": buy_leg_keys,
                    "sell_legs": sell_leg_keys,
                    "observation_result": buy_pair_observation,
                    "observation_duration": 10
                })
                if buy_pair_observation == 1:
                   continue
                # Step 3: Make CASE A/B decision based on 10-second observation
                if buy_pair_observation == False or buy_pair_observation is None:
                    # Both BUY legs are STABLE over 10 seconds - CASE A: Execute SELL legs first
                    self.logger.success("CASE A: BUY legs are STABLE over 10 seconds - executing SELL legs first")
                    case_type = "CASE_A"
                    self.execution_tracker.add_milestone(f"User {uid} executing CASE A", {
                        "strategy": "SELL_FIRST",
                        "reason": "BUY_legs_stable_10_seconds",
                        "observation_details": buy_pair_observation
                    })
                    self._execute_case_a_sell_first(uid, prices, buy_leg_keys, sell_leg_keys,isExit)
                else:
                    # BUY legs are moving over 10 seconds - CASE B: Execute BUY legs first with profit monitoring
                    self.logger.warning("CASE B: BUY legs are MOVING over 10 seconds - executing BUY legs first")
                    case_type = "CASE_B"
                    self.execution_tracker.add_milestone(f"User {uid} executing CASE B", {
                        "strategy": "BUY_FIRST",
                        "reason": "BUY_legs_moving_10_seconds",
                        "observation_details": buy_pair_observation
                    })
                    self._execute_case_b_buy_first(uid, prices, buy_leg_keys, sell_leg_keys, buy_pair_observation,isExit)
            return True
        except Exception as e:
            self.logger.error(f"Failed to execute dual strategy for user {uid}", exception=e)
            self.execution_tracker.add_error(f"Dual strategy failed for user {uid}", str(e), e)
            return False

    def _execute_case_a_sell_first(self, uid, prices, buy_leg_keys, sell_leg_keys,isExit=False):
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
            first_sell_leg, second_sell_leg, execution_reason = self.get_legs_sequence_from_observation(sell_observation, [sell_leg_keys[0],sell_leg_keys[1]],isExit)
            self.logger.info("Using global observation for SELL leg execution order", str(sell_leg_keys))

            self.logger.info(f"SELL legs execution order determined: {first_sell_leg}, {second_sell_leg}")

            self.logger.info(
                "Executing SELL legs with strategic analysis",
                f"First: {first_sell_leg}, Second: {second_sell_leg} | {execution_reason}"
            )
            
            # Execute first SELL leg with MODIFY strategy
            self.execution_tracker.add_milestone(f"Placing first SELL order (MODIFY) for {first_sell_leg}")
            first_sell_result = self._place_modify_order_until_complete(uid, first_sell_leg,30,0.3,isExit)
            self.open_order = True
            if not first_sell_result['success']:
                self.logger.error(f"First SELL leg ({first_sell_leg}) failed")
                self.execution_tracker.add_error(f"First SELL leg failed", f"Leg: {first_sell_leg}")
                return False

            # Execute second SELL leg with MODIFY strategy
            self.execution_tracker.add_milestone(f"Placing second SELL order (MODIFY) for {second_sell_leg}")
            second_sell_result = self._place_modify_order_until_complete(uid, second_sell_leg,30,0.3,isExit)
            if not second_sell_result['success']:
                self.logger.error(f"Second SELL leg ({second_sell_leg}) failed")
                self.execution_tracker.add_error(f"Second SELL leg failed", f"Leg: {second_sell_leg}")
                return False
            
            current_sell_prices = self._get_leg_prices(sell_leg_keys,isExit)
            
            # Calculate SELL pair executed spread using current prices
            sell_executed_prices = {
                sell_leg_keys[0]: float(first_sell_result.get('filled_price', current_sell_prices[sell_leg_keys[0]])),
                sell_leg_keys[1]: float(second_sell_result.get('filled_price', current_sell_prices[sell_leg_keys[1]]))
            }
            sell_executed_spread = sell_executed_prices[sell_leg_keys[0]] + sell_executed_prices[sell_leg_keys[1]]
            
            self.logger.success(f"SELL pair executed successfully", f"Spread: {sell_executed_spread:.2f}")
            self.execution_tracker.add_milestone("SELL pair execution completed", {
                "spread": sell_executed_spread,
                "prices": sell_executed_prices
            })
            
            
            # Step 2: Calculate BUY legs prices based on remaining spread
            if isExit:
                desired_spread = self.params.get("exit_desired_spread", 405)
            else:
                desired_spread = self.params.get("desired_spread", 405)
                
            remaining_spread_for_buy = desired_spread + sell_executed_spread
            
            print(f"INFO: Remaining spread for BUY legs: {remaining_spread_for_buy:.2f}")
            
            # Step 2.1: Validate BUY spread condition before execution
            current_buy_prices = self._get_leg_prices(buy_leg_keys,isExit)
            current_buy_spread = current_buy_prices[buy_leg_keys[0]] + current_buy_prices[buy_leg_keys[1]]
            profit_threshold_buy = self.params.get("profit_threshold_buy", 3)
            print(f"INFO: Current BUY spread: {current_buy_spread:.2f}, Required: <= {remaining_spread_for_buy:.2f}")
            # breakpoint()
            self._monitor_sell_profit_and_execute_buy(uid, sell_executed_spread, remaining_spread_for_buy, 
                                                                         buy_leg_keys, profit_threshold_buy,isExit)
            # breakpoint()
            return True
        except Exception as e:
            print(f"ERROR: Case A execution failed: {e}")
            traceback.print_exc()
            return False

    def _execute_case_b_buy_first(self, uid, prices, buy_leg_keys, sell_leg_keys, buy_observation,isExit=False):
        """CASE B: BUY legs moving - Execute BUY legs first with global observation."""
        try:
            print(f"INFO: Case B - BUY legs moving, executing BUY legs first")
            
            # Get BUY confirmation from global observation
            buy_observation_2 = self._get_global_observation_result("BUY_PAIR")
            
            print(f"INFO: Using global observation to confirm BUY market conditions")
            
            if buy_observation_2 == None or buy_observation_2 == False:
                print(f"WARNING: BUY legs became stable, switching to Case A")
                return self._execute_case_a_sell_first(uid, prices, buy_leg_keys, sell_leg_keys)
            
            # Handle new strategic execution format
            first_buy_leg, second_buy_leg, execution_reason = self.get_legs_sequence_from_observation(buy_observation_2, [buy_leg_keys[0],buy_leg_keys[1]],isExit)
            
            print(f"INFO: Executing BUY legs with MODIFY strategy - First: {first_buy_leg}, Second: {second_buy_leg}")
            
            # Execute first BUY leg with MODIFY strategy
            first_buy_result = self._place_modify_order_until_complete(uid, first_buy_leg,30,0.3,isExit)
            self.open_order = True
            if not first_buy_result['success']:
                print(f"ERROR: First BUY leg failed")
                return False
                
            # Execute second BUY leg with MODIFY strategy  
            second_buy_result = self._place_modify_order_until_complete(uid, second_buy_leg, 30,0.3,isExit)
            if not second_buy_result['success']:
                print(f"ERROR: Second BUY leg failed")
                return False
            
            # Calculate BUY pair executed spread
            buy_executed_prices = {
                buy_leg_keys[0]: float(first_buy_result.get('filled_price', prices[buy_leg_keys[0]])),
                buy_leg_keys[1]: float(second_buy_result.get('filled_price', prices[buy_leg_keys[1]]))
            }
            buy_executed_spread = buy_executed_prices[buy_leg_keys[0]] + buy_executed_prices[buy_leg_keys[1]]
            self.pair1_executed_spread[uid] = buy_executed_spread
           
            print(f"INFO: BUY pair executed spread: {buy_executed_spread:.2f}")
            
            # Step 2: Calculate SELL legs prices based on remaining spread
            if isExit:
                desired_spread = self.params.get("exit_desired_spread", 405)
            else:
                desired_spread = self.params.get("desired_spread", 405)
            remaining_spread_for_sell = buy_executed_spread - desired_spread
            
            print(f"INFO: Remaining spread for SELL legs: {remaining_spread_for_sell:.2f}")
            
            # Step 2.1: Validate SELL spread condition before execution
            current_sell_prices = self._get_leg_prices(sell_leg_keys,isExit)
            current_sell_spread = current_sell_prices[sell_leg_keys[0]] + current_sell_prices[sell_leg_keys[1]]
            
            print(f"INFO: Current SELL spread: {current_sell_spread:.2f}, Required: >= {remaining_spread_for_sell:.2f}")
            
            profit_threshold_buy = self.params.get("profit_threshold_buy", 2)
            # breakpoint()
            self._monitor_buy_profit_and_execute_sell(uid, buy_executed_spread, remaining_spread_for_sell, 
                                                           sell_leg_keys, profit_threshold_buy,isExit)
            # breakpoint()
            return True
        except Exception as e:
            print(f"ERROR: Case B execution failed: {e}")
            traceback.print_exc()
            return False
        
    def get_legs_sequence_from_observation(self, observation, leg_keys,isExit=False):
        """Determine leg execution sequence from observation data."""
        if observation == None or observation == False:
            return leg_keys[0], leg_keys[1], "No strategic observation available"
        
        if not isExit:
            if isinstance(observation, dict) and 'execution_strategy' in observation:
                strategy = observation['execution_strategy']
                if strategy['action'] == 'SKIP':
                    return None, None, f"Execution skipped: {strategy['reason']}"
                return strategy['first_leg'], strategy['second_leg'], strategy['reason']
        else:
            if isinstance(observation, dict) and 'exit_execution_strategy' in observation:
                strategy = observation['exit_execution_strategy']
                if strategy['action'] == 'SKIP':
                    return None, None, f"Exit execution skipped: {strategy['reason']}"
                
                return strategy['first_leg_exit'], strategy['second_leg_exit'], strategy['reason']
        
        # Fallback to old logic
        first_leg = observation.get('first_leg', leg_keys[0])
        second_leg = observation.get('second_leg', leg_keys[1])
        return first_leg, second_leg, "Fallback execution"

    # Monitoring functions with global observation
    def _monitor_sell_profit_and_execute_buy(self, uid, sell_executed_spread, remaining_spread, buy_leg_keys, profit_threshold,isExit=False):
        """Monitor SELL profit while executing BUY legs. Exit if SELL profit exceeds threshold."""
        try:
            print(f"INFO: Monitoring SELL profit (threshold: {profit_threshold}) while preparing BUY execution")
        
            while True:
                current_prices = self._get_leg_prices(list(self.entry_legs.keys()),isExit)
                current_sell_spread = current_prices[self.pair2_bidding_leg] + current_prices[self.pair2_base_leg]
                current_buy_spread = current_prices[self.pair1_bidding_leg] + current_prices[self.pair1_base_leg]
                sell_profit = sell_executed_spread - current_sell_spread
                
                print(f"SELL Profit: {sell_profit:.2f} current_buy_spread : {current_buy_spread} rbs:{remaining_spread} (threshold: {profit_threshold})", end='\r')
                
                # Check profit threshold
                if sell_profit >= profit_threshold:
                    print(f"\nINFO: SELL profit threshold reached ({sell_profit:.2f} >= {profit_threshold}), exiting with profit")
                    # Execute SELL exit orders
                    self._execute_sell_exit(uid,not isExit)
                    self.execution_tracker.add_milestone(f"User {uid} exited with SELL profit", {
                        "user": uid,
                        "profit": sell_profit,
                        "spread": current_sell_spread
                    })
                    return True  # Exit with profit
                    
                 # Check if SELL spread is favorable
                if abs(current_buy_spread) <= abs(remaining_spread):
                    print(f"\nINFO: BUY spread favorable, proceeding with SELL execution\n")
    
                    buy_observation = self._get_global_observation_result("BUY_PAIR")
                    print(f"INFO: Using global observation for BUY leg execution order: {buy_leg_keys}")
                    first_buy_leg, second_buy_leg, execution_reason = self.get_legs_sequence_from_observation(buy_observation, buy_leg_keys,isExit)
                    
                    # Step 2: Execute BUY legs sequentially based on global observation
                    print(f"INFO: Executing BUY legs - First: {first_buy_leg}, Second: {second_buy_leg}")
                    
                    first_buy_success = self._place_ioc_order_with_retry(uid, first_buy_leg,1,isExit)
                    if not first_buy_success['success']:
                        print(f"ERROR: First BUY leg execution failed")
                        continue
                    self.entry_qtys[uid][first_buy_leg] = first_buy_success.get('filled_qty', 0)
                    second_buy_success = self._place_ioc_order_with_retry(uid, second_buy_leg, 20,isExit) # Assuming this would execute
                    if not second_buy_success['success']:
                        print(f"ERROR: Second BUY leg execution failed")
                        continue
                    self.entry_qtys[uid][second_buy_leg] = second_buy_success.get('filled_qty', 0)
                    print(f"SUCCESS: BUY legs executed successfully with SELL profit monitoring")
                    self.all_legs_executed[uid] = True
                    self.execution_tracker.add_milestone(f"User {uid} completed execution with BUY after SELL profit monitoring", { 
                        "user": uid,
                        "sell_profit": sell_profit,
                        "spread": current_buy_spread
                    })
                    return True
            
        except Exception as e:
            print(traceback.format_exc())
            print(f"ERROR: SELL profit monitoring with BUY execution failed: {e}")
            return False

    def _monitor_buy_profit_and_execute_sell(self, uid, buy_executed_spread, remaining_spread, sell_leg_keys, profit_threshold,isExit=False):
        """Monitor BUY profit while executing SELL legs. Exit if BUY profit exceeds threshold."""
        try:
            print(f"INFO: Monitoring BUY profit (threshold: {profit_threshold}) while executing SELL legs")
            
            print(f"INFO: Starting SELL execution with remaining spread: {remaining_spread:.2f}")
            
            # Wait for SELL spread to be favorable
            while True:
                current_prices = self._get_leg_prices(list(self.entry_legs.keys()),isExit)
                current_sell_spread = current_prices[sell_leg_keys[0]] + current_prices[sell_leg_keys[1]]
                current_buy_spread = current_prices[self.pair1_bidding_leg] + current_prices[self.pair1_base_leg]
                
                # Calculate BUY profit (BUY executed at lower price, current at higher price = profit)
                buy_profit = current_buy_spread - buy_executed_spread
                
                print(f"SELL Wait: Current={current_sell_spread:.2f}, Target={remaining_spread:.2f} | BUY Profit={buy_profit:.2f}", end='\r')
                
                # Check BUY profit threshold
                if buy_profit >= profit_threshold:
                    print(f"\nINFO: BUY profit threshold reached ({buy_profit:.2f} >= {profit_threshold}), exiting with profit")
                    self._execute_buy_exit(uid,not isExit)
                    self.execution_tracker.add_milestone(f"User {uid} exited with BUY profit", {
                        "user": uid,
                        "profit": buy_profit,
                        "spread": current_buy_spread
                    })
                    return True  # Exit with profit
                
                # Check if SELL spread is favorable
                if abs(current_sell_spread) >= abs(remaining_spread):
                    print(f"\nINFO: SELL spread favorable, proceeding with SELL execution")

                    sell_observation = self._get_global_observation_result("SELL_PAIR")

                    print(f"INFO: Using global observation for SELL leg execution order: {sell_leg_keys}")

                    first_sell_leg, second_sell_leg, execution_reason = self.get_legs_sequence_from_observation(sell_observation, sell_leg_keys,isExit)
                    
                    # Execute SELL legs sequentially based on global observation -> pending select legs based on observation
                    
                    first_success = self._place_ioc_order_with_retry(uid, first_sell_leg, 1,isExit)
                    if not first_success['success']:
                        continue
                    
                    second_success = self._place_ioc_order_with_retry(uid, second_sell_leg, 20,isExit) # Assuming this would execute
                    if second_success['success']:
                        print(f"SUCCESS: SELL legs executed successfully with BUY profit monitoring")
                        self.all_legs_executed[uid] = True
                        self.execution_tracker.add_milestone(f"User {uid} completed execution with SELL after BUY profit monitoring", {
                            "user": uid,
                            "buy_profit": buy_profit,
                            "spread": current_sell_spread
                        })
                        return True
                    else:
                        continue
                
        except Exception as e:
            print(f"ERROR: BUY profit monitoring with SELL execution failed: {e}")
            return False

    def _execute_sell_exit(self, uid,isExit=False):
        """Execute exit orders for SELL legs to realize profit."""
        self.logger.info(f"Triggering complete exit execution for user {uid}")
        
        # Get current prices for exit execution
        
        sell_observation = self._get_global_observation_result("SELL_PAIR")
        print(f"INFO: Using global observation for SELL leg execution order: {sell_observation}")
        first_sell_leg, second_sell_leg, execution_reason = self.get_legs_sequence_from_observation(sell_observation, [self.pair2_bidding_leg, self.pair2_base_leg],isExit)
        
        first_sell_success = self._place_ioc_order_with_retry(uid, first_sell_leg, 1, isExit)
        if not first_sell_success.get('success', False):
            print(f"ERROR: First SELL leg execution failed")
            return False
        second_sell_success = self._place_ioc_order_with_retry(uid, second_sell_leg, 20, isExit) # Assuming this would execute
        if not second_sell_success.get('success', False):
            print(f"ERROR: Second SELL leg execution failed")
            return False
        # Execute complete exit strategy
        return True

    def _execute_buy_exit(self, uid,isExit):
        """Execute exit orders for BUY legs to realize profit."""  
        self.logger.info(f"Triggering complete exit execution for user {uid}")
        
        buy_observation = self._get_global_observation_result("BUY_PAIR")
        print(f"INFO: Using global observation for BUY leg execution order: {buy_observation}")
        first_buy_leg, second_buy_leg, execution_reason = self.get_legs_sequence_from_observation(buy_observation, [self.pair1_bidding_leg, self.pair1_base_leg],isExit)
        first_buy_success = self._place_ioc_order_with_retry(uid, first_buy_leg, 1, isExit)
        if not first_buy_success:
            print(f"ERROR: First BUY leg execution failed")
            return False
        self.exit_qtys[uid][first_buy_leg] = first_buy_success.get('filled_qty', 0)
        second_buy_success = self._place_ioc_order_with_retry(uid, second_buy_leg, 20, isExit) # Assuming this would execute
        if not second_buy_success:
            print(f"ERROR: Second BUY leg execution failed")
            return False
        self.exit_qtys[uid][second_buy_leg] = second_buy_success.get('filled_qty', 0)
        
        # Execute complete exit strategy
        return True
    # Main execution logic (simplified for demo)
    def main_logic(self):
        """Main logic for sequential box strategy with global observation."""
        execution_id = None
        
        try:
            # Start execution tracking
            execution_id = self.execution_tracker.start_execution(f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_BoxStrategy")
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
                    all_leg_keys = list(self.entry_legs.keys())
                    leg_prices = self._get_leg_prices(all_leg_keys)
                    
                    # Process all users
                    processed_users = 0
                    active_users = 0
                    
                    for uid in self.uids:
                        if not self.all_legs_executed.get(uid, False):
                            active_users += 1
                            self.logger.debug(f"Processing user {uid}")
                            self._execute_both_pairs(uid, leg_prices,isExit=False)
                            breakpoint()
                            self._execute_both_pairs(uid, leg_prices,isExit=True)
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
                    
                    print("\nStartin Again\n")
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
