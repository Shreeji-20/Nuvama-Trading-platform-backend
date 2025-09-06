import json

# Mock redis for testing
class MockRedis:
    def set(self, key, value, ex=None):
        print(f"Mock Redis: Setting {key} with expiry {ex}")
        return True
    
    def get(self, key):
        return None

# Test the JSON-safe conversion without actual Redis dependency
try:
    from nuvama.strategy_helpers import StrategyExecutionTracker
    
    # Create a mock redis connection
    mock_redis = MockRedis()
    tracker = StrategyExecutionTracker(mock_redis)
    
    # Create a circular reference scenario
    test_data = {}
    test_data['self'] = test_data  # Circular reference
    test_data['normal'] = 'value'
    test_data['nested'] = {'key': 'value'}
    
    # Test the _make_json_safe method
    safe_data = tracker._make_json_safe(test_data)
    print('Safe data conversion successful!')
    print('Result:', json.dumps(safe_data, indent=2))
    
    # Test the complete add_order flow
    tracker.start_execution("test_params_123")
    tracker.add_order("test_order", {
        "quantity": 100,
        "price": 25.50,
        "action": "BUY",
        "circular_ref": test_data  # This should be handled safely
    })
    print('\nOrder tracking test successful!')
    
except Exception as e:
    print(f"Error during testing: {e}")
    import traceback
    traceback.print_exc()
