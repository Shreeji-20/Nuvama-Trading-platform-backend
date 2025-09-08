# Sequential Box Strategy - Pricing Logic Analysis
# Spread Tolerance Check vs Exit Order Pricing

# QUESTION ANSWERED:
"""
Q: While checking spread tolerance in phase 2 and executing pair1 exit, 
   which price is used for placing exit orders?

A: FIXED - Now uses current market prices for exit orders!
"""

# PRICING BREAKDOWN:

pricing_analysis = {
    "spread_tolerance_check": {
        "method": "_check_pair1_spread_tolerance(uid, current_prices)",
        "price_source": "current_prices from _get_leg_prices(all_leg_keys)",
        "data_source": "Real-time Redis depth data",
        "purpose": "Compare current spread vs executed spread",
        "pricing_side": "Market prices (BUY uses ask, SELL uses bid)",
        "example": "If pair1 spread moves from 8 to 15 (>5 tolerance) → trigger exit"
    },
    
    "pair1_exit_orders_OLD": {
        "method": "_execute_pair1_exit(uid) - OLD VERSION",
        "price_source": "exit_order_templates with Limit_Price='0'",
        "issue": "❌ Using static '0' price from template creation",
        "problem": "Exit orders placed with invalid pricing",
        "risk": "Orders may not execute or execute at wrong prices"
    },
    
    "pair1_exit_orders_FIXED": {
        "method": "_execute_pair1_exit(uid) - NEW VERSION",
        "price_source": "_get_leg_prices(exit_leg_keys, is_exit=True)",
        "data_source": "Real-time Redis depth data",
        "pricing_side": "Exit pricing (opposite of entry direction)",
        "price_update": "exit_order['Limit_Price'] = _format_limit_price(current_exit_price)",
        "logging": "Logs actual exit prices used",
        "example": "bidding_leg exit: 32.50, base_leg exit: 198.75"
    }
}

# EXIT PRICING LOGIC:
exit_pricing_logic = {
    "exit_price_determination": {
        "method": "_get_leg_prices(leg_keys, is_exit=True)",
        "logic": "Uses opposite side of entry pricing",
        "BUY_leg_exit": "Uses bidValues (sell to exit BUY position)",
        "SELL_leg_exit": "Uses askValues (buy to cover SELL position)",
        "pricing_method": "Same as entry (depth/average based on params)"
    },
    
    "price_formatting": {
        "method": "_format_limit_price(price)",
        "formula": "round(max(0.05, abs(price)) * 20) / 20",
        "ensures": "Positive price with proper tick size",
        "minimum": "0.05 (prevents zero/negative prices)"
    }
}

# EXECUTION FLOW COMPARISON:

execution_comparison = {
    "OLD_FLOW": {
        "1_tolerance_check": "Use current market prices ✅",
        "2_trigger_exit": "Spread moved beyond tolerance ✅", 
        "3_exit_orders": "Use template with Limit_Price='0' ❌",
        "4_order_execution": "Invalid pricing, poor fills ❌"
    },
    
    "NEW_FLOW": {
        "1_tolerance_check": "Use current market prices ✅",
        "2_trigger_exit": "Spread moved beyond tolerance ✅",
        "3_get_exit_prices": "Fetch current market prices ✅",
        "4_update_orders": "Set Limit_Price to current prices ✅",
        "5_order_execution": "Proper pricing, better fills ✅"
    }
}

# REAL-TIME EXAMPLE:

example_scenario = {
    "market_situation": {
        "pair1_executed_spread": 8.0,
        "current_market_spread": 15.2,  
        "spread_movement": 7.2,
        "spread_tolerance": 5.0,
        "trigger": "7.2 > 5.0 → Execute pair1 exit"
    },
    
    "current_market_prices": {
        "pair1_bidding_leg": {
            "entry_side": "askValues (was BUY entry)",
            "exit_side": "bidValues (BUY exit via SELL)",
            "current_exit_price": 32.50
        },
        "pair1_base_leg": {
            "entry_side": "bidValues (was SELL entry)", 
            "exit_side": "askValues (SELL exit via BUY)",
            "current_exit_price": 198.75
        }
    },
    
    "exit_order_execution": {
        "bidding_leg_exit": {
            "action": "SELL (exit BUY position)",
            "quantity": 75,
            "limit_price": "32.50 (current market bid)",
            "template_vs_actual": "Template: '0' → Actual: '32.50'"
        },
        "base_leg_exit": {
            "action": "BUY (exit SELL position)",
            "quantity": 75, 
            "limit_price": "198.75 (current market ask)",
            "template_vs_actual": "Template: '0' → Actual: '198.75'"
        }
    }
}

# KEY IMPROVEMENTS:

improvements = [
    "✅ Exit orders now use real-time market prices",
    "✅ Proper exit pricing (opposite side of entry)",
    "✅ Consistent pricing methodology with spread checks",
    "✅ Better order execution probability",
    "✅ Accurate exit price logging",
    "✅ Risk reduction from invalid pricing",
    "✅ Applied to both pair1_exit and full_exit methods"
]

# RISK MITIGATION:

risk_mitigation = {
    "price_accuracy": "Exit prices match current market conditions",
    "execution_probability": "Proper limit prices increase fill likelihood", 
    "consistency": "Same pricing logic for tolerance check and exit",
    "transparency": "Logged exit prices for audit trail",
    "fail_safe": "Minimum price of 0.05 prevents invalid orders"
}

print("✅ Exit Order Pricing FIXED!")
print("📊 Now uses current market prices for exit orders")
print("🎯 Consistent pricing between tolerance check and exit execution")
print("🛡️ Better risk management with proper exit pricing")
