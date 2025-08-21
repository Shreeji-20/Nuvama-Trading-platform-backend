from nuvama import stratergies
import sys
stratergy_id = sys.argv[1] if len(sys.argv) > 1 else "1"
obj = stratergies.Stratergy1(int(stratergy_id))
obj.main_logic()