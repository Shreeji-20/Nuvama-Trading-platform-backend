from nuvama import stratergies_4leg
from nuvama import stratergies_direct_ioc_box
from nuvama import box_with_dynamic_strikes
# obj_4leg = stratergies_4leg.Stratergy4Leg("e93eaed7-d907-4703-9e5c-46acea6fb962")
# obj_4leg.main_logic()

# obj_dual = stratergies_dual_spread.StratergyDualSpread("fde5ebd6-3052-43cb-a8c8-3871b66c9705")
# obj_dual.main_logic()


params = {
    "desired_spread": 198,
    "exit_desired_spread": 201,
    "action": "BUY",
    "quantity_multiplier": 1,
    "slice_multiplier": 1,
    "user_ids": [
        "70249886"
    ],
    "run_state": 0,
    "order_type": "LIMIT",
    "IOC_timeout": 0.5,
    "exit_price_gap": 2,
    "no_of_bidask_average": 1,
    "notes": "Imported from MultiLeg: Price-adjust | Bidding Leg: NIFTY 25250 CE",
    "strategy_id": "075855a6-b104-4d60-b1cf-fc510641e98d",
    "redis_key": "4_leg:075855a6-b104-4d60-b1cf-fc510641e98d",
    "pricing_method": "depth",
    "depth_index": 1,
    "itm_steps": 2,
    "otm_steps": 2,
    "symbol" : "NIFTY",
}

obj_seq = box_with_dynamic_strikes.StratergyDirectIOCBoxDynamicStrikes(params)
# obj_seq._init_legs_and_orders()

obj_seq.main_logic()