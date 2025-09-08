# Sequential Box Strategy Parameters
# This file shows the expected parameter structure for the sequential box strategy

sequential_box_params = {
    # ===== LEG DEFINITIONS =====
    "leg1": {
        "symbol": "NIFTY", 
        "strike": 24800, 
        "type": "CE", 
        "expiry": 0, 
        "action": "BUY", 
        "quantity": 75
    },
    "leg2": {
        "symbol": "NIFTY", 
        "strike": 24800, 
        "type": "PE", 
        "expiry": 0, 
        "action": "SELL", 
        "quantity": 75
    },
    "leg3": {
        "symbol": "NIFTY", 
        "strike": 25200, 
        "type": "CE", 
        "expiry": 0, 
        "action": "SELL", 
        "quantity": 75
    },
    "leg4": {
        "symbol": "NIFTY", 
        "strike": 25200, 
        "type": "PE", 
        "expiry": 0, 
        "action": "BUY", 
        "quantity": 75
    },
    
    # ===== EXECUTION PAIRS =====
    "pair1_bidding_leg": "leg1",    # 24800 CALL BUY (executed first)
    "pair1_base_leg": "leg4",       # 25200 PUT BUY (executed with leg1)
    "pair2_bidding_leg": "leg2",    # 24800 PUT SELL (executed second)
    "pair2_base_leg": "leg3",       # 25200 CALL SELL (executed with leg2)
    
    # ===== STRATEGY PARAMETERS =====
    "desired_box_price": 397,       # Target box spread value
    "start_price": 400,             # Entry trigger for FULL BOX SPREAD (not just first pair)
    "exit_start": 399,              # Exit trigger for completed boxes
    "spread_tolerance": 5,          # Max points pair1 can move before exit
    "price_tolerance": 2,           # Price tolerance for pair2 entry
    
    # ===== EXECUTION PARAMETERS =====
    "action": "BUY",                # Global action (BUY or SELL)
    "order_type": "LIMIT",          # Order type (LIMIT or MARKET)
    "quantity_multiplier": 1,       # Multiply all quantities by this
    "default_quantity": 75,         # Default quantity if not specified per leg
    
    # ===== TIMING PARAMETERS =====
    "IOC_timeout": 0.5,             # IOC timeout in seconds
    "exit_price_gap": 2.0,          # Price gap for market exits
    
    # ===== PRICING PARAMETERS =====
    "pricing_method": "depth",      # "depth" or "average"
    "depth_index": 3,               # Depth index for pricing (when using depth)
    "no_of_bidask_average": 5,      # Number of levels to average (when using average)
    
    # ===== USER AND STATE =====
    "user_ids": ["70249886"],       # List of user IDs
    "run_state": 0,                 # 0=run, 1=pause, 2=exit
    "notes": "Sequential Box Strategy - ATM 25000",
    
    # ===== REDIS CONFIGURATION =====
    "strategy_id": "seq_box_001",
    "redis_key": "sequential_box:seq_box_001"
}

# ===== PARAMETER EXPLANATIONS =====

"""
STRATEGY FLOW:
1. PAIR 1 EXECUTION:
   - Monitor FULL 4-LEG BOX SPREAD (not just pair1 spread)
   - When full_box_spread < start_price (400), execute leg1 + leg4 (first pair)
   - Save executed prices and pair1 spread for tolerance monitoring

2. PAIR 2 CALCULATION:
   - Calculate remaining spread = executed_pair1_spread - desired_box_price (397)
   - This remaining spread must be achieved by leg2 + leg3
   - Calculate target price for leg2 based on current leg3 price

3. PAIR 2 EXECUTION:
   - When leg2 price is within tolerance of target price, execute leg2 + leg3
   - Box is now complete

4. MONITORING:
   - If pair1 spread moves > spread_tolerance (5 points), exit pair1 and stop
   - If box is complete, monitor for exit conditions

EXAMPLE CALCULATION:
- Full box spread = 398, triggers entry (< start_price 400)
- Execute pair1: leg1 (24800 CE BUY) + leg4 (25200 PE BUY)
- Pair1 executed spread = 8 (difference between leg1 and leg4 after considering actions)
- Remaining spread = 8 - 397 = -389 (need pair2 to contribute -389 to reach target)
- Calculate target prices for leg2 + leg3 to achieve remaining spread

SPREAD CALCULATION LOGIC:
- BUY legs: ADD to spread (we pay premium - positive cost)
- SELL legs: SUBTRACT from spread (we receive premium - negative cost)
- Box spread = +leg1 - leg2 - leg3 + leg4 (considering actions)
- Example: +205 - 31 - 28 + 197 = +343 (absolute value = 343)
- Final result: absolute value of net calculation

ENTRY CONDITION CHANGE:
- OLD: Entry based on pair1 spread only
- NEW: Entry based on full 4-leg box spread
- This ensures the entire box is mispriced before starting execution

PARAMETERS YOU CAN ADJUST:
- start_price: Entry trigger for FULL BOX SPREAD (not just first pair)
- desired_box_price: Target box value (usually strike_difference - time_value)
- spread_tolerance: How much pair1 can move before abandoning pair2
- price_tolerance: How close pair2 bidding leg must be to target
- exit_start: Exit trigger for completed boxes
"""
