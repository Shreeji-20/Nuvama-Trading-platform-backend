from nuvama import stratergies_4leg
from nuvama import stratergies_direct_ioc_box

# obj_4leg = stratergies_4leg.Stratergy4Leg("e93eaed7-d907-4703-9e5c-46acea6fb962")
# obj_4leg.main_logic()

# obj_dual = stratergies_dual_spread.StratergyDualSpread("fde5ebd6-3052-43cb-a8c8-3871b66c9705")
# obj_dual.main_logic()

obj_seq = stratergies_direct_ioc_box.StratergyDirectIOCBox("f6038a59-c4b8-4f0d-a6cf-968a4475bac9")
obj_seq.main_logic()