from nuvama import stratergies_4leg
from nuvama import stratergies_direct_ioc_box

# obj_4leg = stratergies_4leg.Stratergy4Leg("e93eaed7-d907-4703-9e5c-46acea6fb962")
# obj_4leg.main_logic()

# obj_dual = stratergies_dual_spread.StratergyDualSpread("fde5ebd6-3052-43cb-a8c8-3871b66c9705")
# obj_dual.main_logic()

obj_seq = stratergies_direct_ioc_box.StratergyDirectIOCBox("075855a6-b104-4d60-b1cf-fc510641e98d")
# obj_seq._init_legs_and_orders()

obj_seq.main_logic()