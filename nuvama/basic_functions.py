import pandas as pd
import json
import redis
import orjson
from datetime import datetime
class common_functions:
    def __init__(self):
        self.r = redis.Redis(host='localhost', port=6379, db=0)
        if self.r.exists("instruments_csv"):
            self.df = pd.DataFrame(orjson.loads(self.r.get("instruments_csv").decode())) 
        else:
            raise Exception("Scrip Master Details not found")
        self.strikes_nifty = []
        self.strikes_sensex = []
        self.options_mapper = {}

    def get_ltp_indices(self,symbol):
        ltp = json.loads(self.r.get(f"reduced_quotes:{symbol.upper()}").decode())
        print(f"ltp {symbol.upper()} : ", ltp)
        return float(ltp['response']['data']['ltp'])
        
    def get_strikes(self,symbol, depth):
        
        # Check if strikes are already cached
        self.strikes_nifty = json.loads(self.r.get("strikes:NIFTY")) if self.r.get("strikes:NIFTY") else []
        self.strikes_sensex = json.loads(self.r.get("strikes:SENSEX")) if self.r.get("strikes:SENSEX") else []
        ltp = self.get_ltp_indices(symbol)
        # Get the strikes for a given symbol and depth
        if symbol.upper() == 'NIFTY':
            strikes = []
            for i in range(0, depth + 1):
                strikes.append(ltp + (i * 50))
                strikes.append(ltp - (i * 50))
            strikes = [str(float((round(strike/50)*50))) for strike in strikes ]
            if self.strikes_nifty:
                print("Using cached strikes data for NIFTY.")
                self.strikes_nifty = list(set(self.strikes_nifty + strikes))
                self.r.set("strikes:NIFTY",json.dumps(self.strikes_nifty))
                return self.strikes_nifty
            else:
                self.r.set("strikes:NIFTY",json.dumps(strikes))
                self.strikes_nifty = strikes
                return strikes
        elif symbol.upper() == 'SENSEX':
            strikes = []
            for i in range(0, depth + 1):
                strikes.append(ltp + (i * 100))
                strikes.append(ltp - (i * 100))
            strikes = [str(float((round(strike/100)*100))) for strike in strikes ]
            self.r.set("strikes:SENSEX",json.dumps(strikes))
            if self.strikes_sensex:
                print("Using cached strikes data for SENSEX.")
                self.strikes_sensex = list(set(self.strikes_sensex + strikes))
                self.r.set("strikes:SENSEX",json.dumps(self.strikes_sensex))
                return self.strikes_sensex
            else:
                self.r.set("strikes:SENSEX",json.dumps(strikes))
                self.strikes_sensex = strikes
                return strikes
               
        
    def get_filtered_df(self, symbol, strikes=[],expiry=0,exchange="NFO"):
        
        if exchange.upper() in ["NFO","BFO"]:
            filtered_df = self.df[(self.df['symbolname'] == symbol.upper()) & 
                                  (self.df['strikeprice'].isin((strikes))) &
                                  (self.df['exchange'] == exchange.upper())]

            expiries = filtered_df['expiry'].unique()
        
            expiries = [datetime.strptime(d, "%d/%b/%y") for d in expiries]
            expiries = sorted(expiries)
            expiries = [d.strftime("%d/%b/%y") for d in expiries]
        
            filtered_df = filtered_df[filtered_df['expiry'] == expiries[expiry].upper()]
            return filtered_df
        elif exchange.upper() == "NSE":
            filtered_df = self.df[(self.df['symbolname'].isin(symbol)) & 
                                  (self.df['exchange'] == exchange.upper())]
            return filtered_df
        else:
            raise Exception("Exchange not supported for options fetching")
    
    def refresh_strikes_and_options(self,expiry=0,symbol='NIFTY',exchange="NFO"):
        if exchange.upper() in ["NFO","BFO"]:
            strikes = self.get_strikes(symbol.upper(), 10)
            self.options_df = self.get_filtered_df(symbol.upper(), strikes, expiry, exchange)
        elif exchange.upper() == "NSE":
            self.options_df = self.get_filtered_df(symbol, [], expiry, exchange)
            # print("NSE data : ",self.options_df)
            # breakpoint()
        else:
            raise Exception("Exchange not supported for options fetching")
        option_mapper = {
            row['exchangetoken']: {
                'symbolname': row['symbolname'],
                'expiry': expiry,
                'strikeprice': row['strikeprice'],
                'optiontype': row['optiontype'],
                'tradingsymbol': row['tradingsymbol'],
            } for _, row in self.options_df.iterrows()
        }
        self.options_mapper = self.options_mapper | option_mapper
        self.r.publish("strikes_updates", json.dumps(option_mapper))
        self.r.set("option_mapper", json.dumps(self.options_mapper),ex=43200)
        return option_mapper
       

if __name__ == "__main__":
    common_obj = common_functions()
    ltp = common_obj.get_ltp_indices('Nifty')
    print(type(ltp))

    strikes = common_obj.get_strikes('Nifty', 20, ltp)
    print(strikes)
    filtered = common_obj.get_filtered_df('Nifty', common_obj.get_strikes('NIFTY', 20, ltp),0)
    print(filtered['exchangetoken'].tolist())
