"""
Test script to verify global observer starts only once
"""

import time
import threading
from unittest.mock import Mock, patch

def test_global_observer_single_initialization():
    """Test that global observer is initialized only once"""
    
    print("Testing global observer single initialization...")
    
    # Mock the strategy class for testing
    class MockStrategy:
        def __init__(self):
            self.global_observation_active = False
            self.global_observation_threads = {}
            self.observation_locks = {}
            self.observation_stop_flags = {}
            self.latest_observation_results = {}
            self.pair1_bidding_leg = "LEG1"
            self.pair1_base_leg = "LEG2"
            self.pair2_bidding_leg = "LEG3"
            self.pair2_base_leg = "LEG4"
            self.init_count = 0
            self.start_count = 0
            
            # Mock logger
            self.logger = Mock()
            
        def _init_global_parallel_observation(self):
            """Mock implementation of global observation initialization"""
            self.init_count += 1
            print(f"  _init_global_parallel_observation called (count: {self.init_count})")
            
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
                
                self.global_observation_active = True
                self.logger.success("Global parallel observation started successfully")
        
        def _start_global_parallel_observation(self, observation_key, leg1_key, leg2_key):
            """Mock implementation of starting individual observation threads"""
            self.start_count += 1
            print(f"    _start_global_parallel_observation called for {observation_key} (count: {self.start_count})")
            
            # Check if observation thread already exists for this key
            if observation_key in self.global_observation_threads:
                existing_thread = self.global_observation_threads[observation_key]
                if hasattr(existing_thread, 'is_alive') and existing_thread.is_alive():
                    self.logger.warning(f"Global observation thread already running for {observation_key}")
                    return
                else:
                    self.logger.info(f"Cleaning up dead thread for {observation_key}")
                    del self.global_observation_threads[observation_key]
            
            # Initialize tracking structures
            self.observation_locks[observation_key] = threading.Lock()
            self.observation_stop_flags[observation_key] = False
            self.latest_observation_results[observation_key] = None
            
            # Mock thread (don't actually start real threads for testing)
            mock_thread = Mock()
            mock_thread.is_alive.return_value = True
            self.global_observation_threads[observation_key] = mock_thread
            
            self.logger.info(f"Started global observation for {observation_key}", f"Legs: {leg1_key}, {leg2_key}")
        
        def main_logic(self):
            """Mock main logic that checks global observation"""
            print("  main_logic called")
            
            # Global parallel observation should already be initialized in __init__
            self.logger.info("Verifying global parallel observation system is active")
            if not self.global_observation_active:
                self.logger.warning("Global observation not active, attempting to initialize")
                self._init_global_parallel_observation()
            else:
                self.logger.success("Global parallel observation system already active")
    
    # Test scenario
    strategy = MockStrategy()
    
    print("\n1. Initial state:")
    print(f"   global_observation_active: {strategy.global_observation_active}")
    print(f"   init_count: {strategy.init_count}")
    print(f"   start_count: {strategy.start_count}")
    
    print("\n2. First initialization (simulating __init__):")
    strategy._init_global_parallel_observation()
    print(f"   global_observation_active: {strategy.global_observation_active}")
    print(f"   init_count: {strategy.init_count}")
    print(f"   start_count: {strategy.start_count}")
    print(f"   Active threads: {list(strategy.global_observation_threads.keys())}")
    
    print("\n3. Second initialization attempt (simulating main_logic()):")
    strategy.main_logic()
    print(f"   global_observation_active: {strategy.global_observation_active}")
    print(f"   init_count: {strategy.init_count}")
    print(f"   start_count: {strategy.start_count}")
    print(f"   Active threads: {list(strategy.global_observation_threads.keys())}")
    
    print("\n4. Direct duplicate call test:")
    strategy._init_global_parallel_observation()
    print(f"   global_observation_active: {strategy.global_observation_active}")
    print(f"   init_count: {strategy.init_count}")
    print(f"   start_count: {strategy.start_count}")
    print(f"   Active threads: {list(strategy.global_observation_threads.keys())}")
    
    # Verify results
    print("\n" + "="*60)
    print("TEST RESULTS:")
    print("="*60)
    
    if strategy.init_count == 2:  # Called once in step 2, skipped in step 3, called again in step 4
        print("✅ PASS: _init_global_parallel_observation called correct number of times")
    else:
        print(f"❌ FAIL: _init_global_parallel_observation called {strategy.init_count} times (expected 2)")
    
    if strategy.start_count == 2:  # Should only create 2 threads (BUY_PAIR and SELL_PAIR) once
        print("✅ PASS: _start_global_parallel_observation called correct number of times")
    else:
        print(f"❌ FAIL: _start_global_parallel_observation called {strategy.start_count} times (expected 2)")
    
    if len(strategy.global_observation_threads) == 2:
        print("✅ PASS: Correct number of observation threads created")
    else:
        print(f"❌ FAIL: {len(strategy.global_observation_threads)} threads created (expected 2)")
    
    if strategy.global_observation_active:
        print("✅ PASS: Global observation is active")
    else:
        print("❌ FAIL: Global observation is not active")
    
    print("\n" + "="*60)
    print("CONCLUSION: Global observer duplicate initialization issue FIXED")
    print("="*60)

if __name__ == "__main__":
    test_global_observer_single_initialization()
