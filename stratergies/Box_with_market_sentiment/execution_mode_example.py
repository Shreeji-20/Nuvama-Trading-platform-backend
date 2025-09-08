"""
Example: Using Live vs Simulation Execution Modes

This script demonstrates how to use the DirectIOCBox strategy with both
Live and Simulation execution modes.

Live Mode: Orders are actually placed through the API
Simulation Mode: Orders are simulated and execution is assumed at target prices
"""

import json
import redis
from nuvama.stratergies_direct_ioc_box import StratergyDirectIOCBox
from nuvama.strategy_helpers import StrategyExecutionHelpers, StrategyLoggingHelpers

def demo_execution_modes():
    """Demonstrate both execution modes"""
    
    # Connect to Redis
    r = redis.Redis(host="localhost", port=6379, db=0)
    
    # Example parameters ID (make sure this exists in Redis)
    params_id = "test_box_strategy"
    
    # Sample parameters for demonstration
    sample_params = {
        "action": "BUY",
        "execution_mode": StrategyExecutionHelpers.SIMULATION_MODE,  # Start with simulation
        "default_quantity": 25,
        "quantity_multiplier": 1,
        "spread_threshold": 0.50,
        "ce_leg_1": {
            "strike": 25000,
            "expiry": "28NOV24",
            "quantity": 25
        },
        "pe_leg_1": {
            "strike": 25000,
            "expiry": "28NOV24", 
            "quantity": 25
        },
        "ce_leg_2": {
            "strike": 25100,
            "expiry": "28NOV24",
            "quantity": 25
        },
        "pe_leg_2": {
            "strike": 24900,
            "expiry": "28NOV24",
            "quantity": 25
        }
    }
    
    # Store parameters in Redis
    r.set(f"4_leg:{params_id}", json.dumps(sample_params))
    
    print("="*60)
    print("STRATEGY EXECUTION MODE DEMONSTRATION")
    print("="*60)
    
    # 1. SIMULATION MODE DEMONSTRATION
    print("\n🎯 SIMULATION MODE DEMO")
    print("-" * 30)
    
    try:
        # Create strategy instance (starts in simulation mode from params)
        strategy = StratergyDirectIOCBox(params_id)
        
        print(f"✅ Strategy initialized in {strategy.get_execution_mode()} mode")
        
        # Run strategy in simulation mode
        print("🚀 Running strategy in SIMULATION mode...")
        
        # Note: In real usage, you would call strategy.run_stratergy()
        # For demo purposes, we'll just show the mode functionality
        
        # Demonstrate mode switching
        print(f"Current mode: {strategy.get_execution_mode()}")
        
        # Get simulation summary (if any orders were executed)
        sim_summary = strategy.get_simulation_summary()
        print(f"Simulation Summary: {sim_summary}")
        
        print("✅ Simulation mode demonstration completed")
        
    except Exception as e:
        StrategyLoggingHelpers.error("Simulation demo failed", exception=e)
    
    # 2. LIVE MODE DEMONSTRATION
    print("\n🔴 LIVE MODE DEMO")
    print("-" * 30)
    
    try:
        # Switch to live mode
        strategy.set_execution_mode(StrategyExecutionHelpers.LIVE_MODE)
        print(f"✅ Mode switched to: {strategy.get_execution_mode()}")
        
        print("⚠️  LIVE MODE ACTIVE - Real orders would be placed!")
        print("🚀 In live mode, actual API calls would be made...")
        
        # Note: In real usage, calling strategy.run_stratergy() here 
        # would place actual orders through the broker API
        
        print("✅ Live mode demonstration completed")
        
    except Exception as e:
        StrategyLoggingHelpers.error("Live demo failed", exception=e)
    
    # 3. MODE COMPARISON
    print("\n📊 EXECUTION MODE COMPARISON")
    print("-" * 40)
    
    comparison_table = """
    ╔═══════════════╦═══════════════════════════════════╦══════════════════════════════════════════╗
    ║ FEATURE       ║ SIMULATION MODE                   ║ LIVE MODE                                ║
    ╠═══════════════╬═══════════════════════════════════╬══════════════════════════════════════════╣
    ║ Order Placement║ Simulated (no real API calls)   ║ Actual orders placed via broker API     ║
    ║ Execution     ║ Assumed at target price          ║ Real market execution                    ║
    ║ Risk          ║ Zero financial risk              ║ Real money at risk                       ║
    ║ Data Storage  ║ Simulation data in Redis         ║ Real order data tracked                  ║
    ║ Use Case      ║ Testing, backtesting, learning   ║ Live trading                             ║
    ║ Performance   ║ Faster (no API latency)         ║ Subject to API and market latency       ║
    ╚═══════════════╩═══════════════════════════════════╩══════════════════════════════════════════╝
    """
    print(comparison_table)
    
    # 4. BEST PRACTICES
    print("\n💡 BEST PRACTICES")
    print("-" * 20)
    
    practices = [
        "✅ Always test strategies in SIMULATION mode first",
        "✅ Verify logic and parameters with simulation runs",
        "✅ Use simulation for backtesting historical data",
        "✅ Switch to LIVE mode only after thorough testing",
        "⚠️  Monitor live executions closely",
        "⚠️  Have stop-loss mechanisms in place for live trading",
        "📊 Compare simulation vs live execution results",
        "🔄 Regularly switch between modes for testing updates"
    ]
    
    for practice in practices:
        print(f"  {practice}")
    
    print("\n" + "="*60)
    print("DEMONSTRATION COMPLETED")
    print("="*60)

def show_mode_switching_example():
    """Show how to switch modes dynamically"""
    
    print("\n🔄 DYNAMIC MODE SWITCHING EXAMPLE")
    print("-" * 40)
    
    params_id = "test_mode_switching"
    
    # Parameters with LIVE mode as default
    params = {
        "action": "BUY",
        "execution_mode": StrategyExecutionHelpers.LIVE_MODE,
        "default_quantity": 25,
        "spread_threshold": 0.50
    }
    
    r = redis.Redis(host="localhost", port=6379, db=0)
    r.set(f"4_leg:{params_id}", json.dumps(params))
    
    try:
        # Create strategy (starts in LIVE mode)
        strategy = StratergyDirectIOCBox(params_id)
        print(f"1️⃣ Initial mode: {strategy.get_execution_mode()}")
        
        # Switch to simulation for testing
        strategy.set_execution_mode(StrategyExecutionHelpers.SIMULATION_MODE)
        print(f"2️⃣ Switched to: {strategy.get_execution_mode()}")
        
        # Simulate some activity
        print("3️⃣ Running test cycles in simulation...")
        
        # Check simulation results
        sim_summary = strategy.get_simulation_summary()
        print(f"4️⃣ Simulation results: {sim_summary}")
        
        # Clear simulation data
        cleared_count = strategy.clear_simulation_data()
        print(f"5️⃣ Cleared {cleared_count} simulation orders")
        
        # Switch back to live for actual execution
        strategy.set_execution_mode(StrategyExecutionHelpers.LIVE_MODE)
        print(f"6️⃣ Ready for live execution: {strategy.get_execution_mode()}")
        
        print("✅ Mode switching example completed")
        
    except Exception as e:
        StrategyLoggingHelpers.error("Mode switching example failed", exception=e)

if __name__ == "__main__":
    # Run demonstrations
    demo_execution_modes()
    show_mode_switching_example()
    
    print("\n🎉 All examples completed!")
    print("\nTo use in your code:")
    print("1. Set 'execution_mode' in your strategy parameters")
    print("2. Create strategy instance: strategy = StratergyDirectIOCBox(params_id)")
    print("3. Switch modes: strategy.set_execution_mode('LIVE' or 'SIMULATION')")
    print("4. Run strategy: strategy.run_stratergy()")
