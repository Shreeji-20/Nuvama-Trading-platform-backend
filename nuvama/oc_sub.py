from basic_functions import common_functions

obj = common_functions()
obj.refresh_strikes_and_options(expiry=1,symbol='NIFTY')
obj.refresh_strikes_and_options(expiry=1,symbol='SENSEX')
obj.refresh_strikes_and_options(expiry=0,symbol='NIFTY')
obj.refresh_strikes_and_options(expiry=0,symbol='SENSEX')