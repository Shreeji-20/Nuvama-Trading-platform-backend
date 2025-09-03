# Sequential Box Strategy - Improved Execution Logic
# Fixed: Only proceed to pair2 when pair1 has actual filled quantities

# PROBLEM FIXED:
"""
OLD LOGIC (INCORRECT):
1. Place pair1 orders
2. If order placement successful → set pair1_executed = True
3. Immediately evaluate pair2 conditions
❌ ISSUE: Orders placed ≠ Orders filled

NEW LOGIC (CORRECT):
1. Place pair1 orders
2. Wait for actual filled quantities
3. Only when both legs have fills → set pair1_executed = True
4. Then evaluate pair2 conditions
✅ CORRECT: Only proceed when we have actual positions
"""

# EXECUTION PHASES (CORRECTED):

execution_phases = {
    "Phase 1": {
        "action": "Place Pair1 Orders",
        "condition": "full_box_spread < start_price",
        "result": "Orders placed, but pair1_executed = False",
        "status": "waiting_for_pair1_fills"
    },
    
    "Phase 1.5": {
        "action": "Check Pair1 Filled Quantities",
        "condition": "Both legs have filled_qty > 0",
        "check": "_check_pair_filled_quantities(uid, pair1_bidding_leg, pair1_base_leg)",
        "success": "pair1_executed = True, proceed to pair2 evaluation",
        "failure": "Continue waiting, log partial fills"
    },
    
    "Phase 2": {
        "action": "Evaluate Pair2 Conditions",
        "condition": "pair1_executed = True AND pair2_executed = False",
        "logic": "Calculate remaining spread and place pair2 orders",
        "result": "Orders placed, but pair2_executed = False"
    },
    
    "Phase 2.5": {
        "action": "Check Pair2 Filled Quantities", 
        "condition": "Both legs have filled_qty > 0",
        "check": "_check_pair_filled_quantities(uid, pair2_bidding_leg, pair2_base_leg)",
        "success": "pair2_executed = True, all_legs_executed = True",
        "result": "Box strategy completed"
    },
    
    "Phase 3": {
        "action": "Monitor Exit Conditions",
        "condition": "all_legs_executed = True",
        "monitoring": "Box spread vs exit_start threshold",
        "action_on_exit": "Square off all positions"
    }
}

# QUANTITY TRACKING:
quantity_tracking = {
    "entry_qtys": {
        "description": "Tracks filled quantities for each leg per user",
        "format": "{uid: {leg_key: filled_quantity}}",
        "update_method": "_update_filled_quantities()",
        "source": "Redis order data (fQty field)"
    },
    
    "fill_verification": {
        "method": "_check_pair_filled_quantities()",
        "parameters": "uid, leg1_key, leg2_key",
        "returns": "both_filled (bool), leg1_qty (int), leg2_qty (int)",
        "logic": "both_filled = leg1_qty > 0 AND leg2_qty > 0"
    }
}

# STATE MANAGEMENT:
state_management = {
    "pair1_executed": {
        "set_to_true": "Only when both pair1 legs have filled quantities",
        "prevents": "Premature pair2 evaluation",
        "enables": "Accurate spread calculations based on actual positions"
    },
    
    "pair2_executed": {
        "set_to_true": "Only when both pair2 legs have filled quantities", 
        "enables": "all_legs_executed = True",
        "result": "Box strategy completion"
    },
    
    "all_legs_executed": {
        "condition": "pair1_executed = True AND pair2_executed = True",
        "meaning": "All 4 legs have actual filled positions",
        "triggers": "Exit monitoring phase"
    }
}

# LOGGING IMPROVEMENTS:
logging_improvements = [
    "✅ 'Pair1 orders placed - waiting for fills'",
    "✅ 'Pair1 FILLED - Bidding qty: X, Base qty: Y'", 
    "✅ 'Partial fill for pair1 - Bidding: X, Base: Y'",
    "✅ 'Pair2 orders placed - waiting for fills'",
    "✅ 'Pair2 FILLED - Full box COMPLETED'",
    "✅ DEBUG quantity checks with _check_pair_filled_quantities()"
]

# RISK MANAGEMENT BENEFITS:
risk_benefits = [
    "🛡️ No phantom positions: Only proceeds with actual fills",
    "📊 Accurate spread calculations based on real positions",
    "⏱️ Proper timing: Waits for execution before next phase",
    "🔍 Clear visibility: Logs partial fills and waiting states",
    "⚡ Efficient: Uses helper methods for clean quantity checking"
]

# EXAMPLE EXECUTION FLOW:
example_flow = {
    "Step 1": "Box spread 398 < start_price 403 → Place pair1 orders",
    "Step 2": "Order placement successful → 'waiting_for_pair1_fills'",
    "Step 3": "Check quantities: bidding_leg=75, base_leg=75 → Both filled!",
    "Step 4": "Set pair1_executed=True → Proceed to pair2 evaluation", 
    "Step 5": "Calculate bidding_leg price → Place pair2 orders",
    "Step 6": "Check quantities: bidding_leg=75, base_leg=75 → Both filled!",
    "Step 7": "Set all_legs_executed=True → Box strategy completed",
    "Step 8": "Monitor for exit conditions"
}

print("✅ Sequential Box Strategy - Execution Logic Fixed!")
print("🎯 Only proceeds to pair2 when pair1 has actual fills")
print("📊 Accurate quantity tracking and state management")
print("🛡️ Prevents phantom positions and timing issues")
