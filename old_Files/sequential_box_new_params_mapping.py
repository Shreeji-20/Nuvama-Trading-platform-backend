# Sequential Box Strategy - Updated Parameter Mapping
# This shows how your new parameter structure works with the sequential execution

# YOUR INPUT PARAMETERS:
your_params = {
    "bidding_leg": {
        "symbol": "NIFTY",
        "strike": 24700,
        "type": "PE", 
        "expiry": 0,
        "action": "SELL",
        "quantity": 75
    },
    "base_legs": ["leg1", "leg2", "leg4"],
    "bidding_leg_key": "bidding_leg",
    "leg1": {
        "symbol": "NIFTY",
        "strike": 24300,
        "type": "CE",
        "expiry": 0,
        "action": "SELL", 
        "quantity": 75
    },
    "leg2": {
        "symbol": "NIFTY",
        "strike": 24300,
        "type": "PE",
        "expiry": 0,
        "action": "BUY",
        "quantity": 75
    },
    "leg4": {
        "symbol": "NIFTY",
        "strike": 24700,
        "type": "CE",
        "expiry": 0,
        "action": "BUY",
        "quantity": 75
    },
    "desired_spread": 405,
    "start_price": 403,
    "exit_start": 1,
    "action": "SELL",
    "quantity_multiplier": 2,
    "user_ids": ["70204607"],
    "run_state": 0
}

# AUTO-DETECTED PAIRS BASED ON BUY/SELL ACTIONS:
"""
BUY legs detected: ['leg2', 'leg4']
- leg2: 24300 PE BUY
- leg4: 24700 CE BUY

SELL legs detected: ['bidding_leg', 'leg1'] 
- bidding_leg: 24700 PE SELL
- leg1: 24300 CE SELL

AUTO-ASSIGNED PAIRS (BY ACTION TYPE):
Pair 1 (BUY legs): leg2 (24300 PE BUY) + leg4 (24700 CE BUY)
Pair 2 (SELL legs): bidding_leg (24700 PE SELL) + leg1 (24300 CE SELL)
"""

# SEQUENTIAL EXECUTION FLOW:
execution_flow = {
    "Phase 1": {
        "condition": "full_box_spread < start_price (403)",
        "action": "Execute Pair 1: Both BUY legs together",
        "legs": ["leg2: 24300 PE BUY", "leg4: 24700 CE BUY"],
        "monitoring": "Track pair1 spread for tolerance"
    },
    
    "Phase 2": {
        "condition": "Calculate remaining spread and target price for Pair 2",
        "calculation": "remaining_spread = executed_pair1_spread - desired_spread (405)",
        "target": "Calculate target price for bidding_leg based on leg1 current price",
        "action": "Execute Pair 2: Both SELL legs together",
        "legs": ["bidding_leg: 24700 PE SELL", "leg1: 24300 CE SELL"]
    },
    
    "Phase 3": {
        "condition": "Monitor completed box for exit", 
        "exit_trigger": "box_spread reaches exit_start (1) or run_state = 2",
        "action": "Square off all positions"
    }
}

# SPREAD CALCULATION:
spread_calc = {
    "formula": "BUY legs ADD (+), SELL legs SUBTRACT (-)",
    "calculation": "+leg2 + leg4 - bidding_leg - leg1",
    "example": "+150 + 205 - 31 - 28 = +296",
    "result": "abs(296) = 296"
}

# KEY FEATURES:
features = {
    "auto_pairing": "Automatically pairs BUY legs with SELL legs",
    "dynamic_legs": "Works with any number of legs (not just 4)",
    "bidding_priority": "Uses bidding_leg as primary execution leg",
    "spread_based_entry": "Enters when full box spread < start_price",
    "sequential_execution": "Executes pairs sequentially with monitoring",
    "risk_management": "Exits pair1 if spread moves beyond tolerance"
}

# PARAMETER MAPPINGS:
parameter_mapping = {
    "start_price": "Entry trigger for full box spread (was 400, now 403)",
    "desired_spread": "Target spread value (was desired_box_price 397, now 405)", 
    "exit_start": "Exit trigger for completed boxes (was 399, now 1)",
    "spread_tolerance": "Default 5 (not in your params, uses default)",
    "price_tolerance": "Default 2 (not in your params, uses default)",
    "quantity_multiplier": "Multiplies all leg quantities by 2",
    "action": "Global action SELL (strategy will adapt)",
    "user_ids": "Single user 70204607"
}

print("Sequential Box Strategy Updated!")
print("✅ Auto-detects BUY/SELL leg pairs")
print("✅ Uses full box spread for entry")
print("✅ Sequential execution with monitoring")
print("✅ Dynamic leg handling")
print("✅ Updated parameter mapping")
