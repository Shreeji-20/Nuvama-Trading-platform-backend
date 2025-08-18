
stratergies = {
    "Stratergy1": {
        "params-1":{
            # "broker"
            "symbol": "SENSEX",
            "order_type": "limit",
            "quantity": 20 * 1,
            "slices":1 * 20, # Lot wise slicing
            "base_leg": "CE",
            "IOC": True,
            "IOC_timeout": 5 * 1000,
            "call_strike": 80600,
            "put_strike": 80600,
            "desired_spread": 693,
            "start_price":693,
            "user_id": "70204607",
            "expiry":1,
            "no_of_bidask_average": 5,
            "action":"buy",
            "exit_start":1000,
            "exit_desired_spread":1000
            },
        "params-2":{
            # "broker"
            "symbol": "NIFTY",
            "order_type": "limit",
            "quantity": 75 * 1,
            "slices":1 * 75, # Lot wise slicing
            "base_leg": "CE",
            "IOC": True,
            "IOC_timeout": 5 * 1000,
            "call_strike": 24650,
            "put_strike": 24650,
            "desired_spread": 281.8,
            "start_price":282.9,
            "user_id": "70204607",
            "expiry":1,
            "no_of_bidask_average": 5,
            "action":"buy",
            "exit_start":1000,
            "exit_desired_spread":1000
            },
        }
    }
