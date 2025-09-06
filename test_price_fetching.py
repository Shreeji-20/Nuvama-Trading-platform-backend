"""
Quick test to verify that _get_leg_prices is fetching fresh data correctly
"""
import time
import sys
import os

# Add the nuvama directory to the Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

def test_price_fetching():
    try:
        from nuvama.stratergies_direct_ioc_box import StratergyDirectIOCBox
        
        # You'll need to provide a valid params ID that exists in Redis
        # Replace 'your_params_id' with an actual params ID from your Redis
        params_id = input("Enter a valid params ID from Redis: ").strip()
        
        if not params_id:
            print("No params ID provided, exiting test")
            return
            
        print(f"Testing price fetching with params ID: {params_id}")
        
        # Initialize strategy
        strategy = StratergyDirectIOCBox(params_id)
        
        # Get all leg keys
        leg_keys = list(strategy.legs.keys())
        print(f"Available legs: {leg_keys}")
        
        if len(leg_keys) >= 2:
            # Test price fetching multiple times to see if prices change
            print("\nTesting fresh price fetching (5 samples with 1-second intervals):")
            for i in range(5):
                prices = strategy._get_leg_prices(leg_keys[:2])
                print(f"Sample {i+1}: {prices}")
                time.sleep(1)
            
            print("\nTesting 3-second observation:")
            result = strategy._observe_market_for_case_decision(leg_keys[0], leg_keys[1], 3)
            print(f"Observation result: {result}")
        else:
            print("Not enough legs available for testing")
            
    except Exception as e:
        print(f"Test failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_price_fetching()
