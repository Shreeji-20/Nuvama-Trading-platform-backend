import redis
import orjson
import time
from APIConnect.APIConnect import APIConnect

import json
import traceback
import pandas as pd
import os
import sys
# Path to the directory containing the module
current_dir = os.path.dirname(__file__)

# Go up one level to reach "child"
parent_dir = os.path.abspath(os.path.join(current_dir, ".."))

# Add to sys.path
sys.path.append(parent_dir)

from basic_functions import common_functions
class CentralSocketData:
    def __init__(self):
        self.r = redis.Redis(host='localhost', port=6379, db=0)
        self.api_connect = APIConnect("iBe07GpBTEbbhg", "", "", True, "",False)
        if self.r.exists("option_mapper"):
            self.options_data = orjson.loads(self.r.get("option_mapper").decode())
        else:
            self.options_data = {}
        self.quotes_streamer = self.api_connect.initReducedQuotesStreaming()
        self.depth_streamer = self.api_connect.initDepthStreaming()
        
        
        self.reduced_quotes = ['-29','-101']
        self.quotes_streamer_start()
        time.sleep(10)
        self.common_obj = common_functions()
        
        if not self.options_data:
            self.common_obj.refresh_strikes_and_options(expiry=1,symbol='NIFTY')
            self.common_obj.refresh_strikes_and_options(expiry=1,symbol='SENSEX')
            self.common_obj.refresh_strikes_and_options(expiry=0,symbol='NIFTY')
            self.common_obj.refresh_strikes_and_options(expiry=0,symbol='SENSEX')
        
        self.strikes_updates_channel = self.r.pubsub()
        self.strikes_updates_channel.subscribe('strikes_updates')

    def listen_for_strikes_updates(self):
        try:
            for message in self.strikes_updates_channel.listen():
                if message['type'] == 'message':
                    print("New Subscription received")
                    new_subscription_data = json.loads(message['data'])
                    self.options_data = self.options_data | new_subscription_data # strike prices list
                    # self.depth_streamer_stop()
                    # time.sleep(3)  # Optional: wait before restarting
                    self.depth_streamer_start(list(new_subscription_data.keys()))
        except Exception as e:
            raise e
               
    def ReducedQuotesFeedCallback(self,response):
        try:
            response = orjson.loads(response.encode())
            if str(response['response']['data']['sym']) == "-29":
                symbol = "NIFTY"
            elif str(response['response']['data']['sym']) == "-101":
                symbol = "SENSEX"
            else:
                symbol = "OTHER"
            print(response)
            self.r.set(f"reduced_quotes:{symbol}", orjson.dumps(response).decode())
        except Exception as e:
            print(f"Error processing response (callbackfun): {str(e)}")
            
    def quotes_streamer_start(self,symbol=[]):
        self.quotes_streamer.subscribeReducedQuotesFeed(self.reduced_quotes,  self.ReducedQuotesFeedCallback)
    def quotes_streamer_stop(self):
        self.quotes_streamer.unsubscribeReducedQuotesFeed()
    def shutdown(self):
        self.quotes_streamer.shutdown()
    
    def DepthStreamerCallback(self, response):
        try:
            response = orjson.loads(response.encode())
            # print(type(response))
            # streaming symbol contained in the payload
            streaming_symbol = response['response']['data'].get('symbol')
            details = self.options_data.get(streaming_symbol, {})
            # merge details into the response data safely
            try:
                if details:
                    # ensure we're updating the dict, not the symbol string
                    response['response']['data'].update(details)
            except Exception as e:
                print(f"Failed to merge option details into response data: {e}")

            # construct redis key defensively using available fields
            symbolname = details.get('symbolname') or response['response']['data'].get('symbolname') or streaming_symbol
            strike = details.get('strikeprice') or response['response']['data'].get('strikeprice')
            opt_type = details.get('optiontype') or response['response']['data'].get('optiontype')
            expiry = details.get('expiry') or response['response']['data'].get('expiry')

            if symbolname and strike and opt_type:
                redis_key = f"depth:{symbolname}_{strike}_{opt_type}-{expiry}"
            else:
                # fallback to a generic key containing the streaming symbol
                redis_key = f"depth:{streaming_symbol}"

            self.r.set(redis_key, orjson.dumps(response).decode())
        except Exception as e:
            print(f"Error processing response (DepthStreamerCallback): {str(e)}")
    
    def depth_streamer_start(self,symbols=[]):
        if symbols:
            self.depth_streamer.subscribeDepthFeed(symbols, self.DepthStreamerCallback)
        else:
            pass
    def depth_streamer_stop(self):
        self.depth_streamer.unsubscribeDepthFeed()
    def depth_shutdown(self):
        self.depth_streamer.shutdown()
        
        
    
import threading
if __name__ == "__main__":
    socket = CentralSocketData() 
    t1 = threading.Thread(target=socket.listen_for_strikes_updates,daemon=True).start()
    
    while True:
        try:
            print("Starting")
            socket = CentralSocketData() 
            if socket.r.exists("option_mapper"):
                socket.options_data = orjson.loads(socket.r.get("option_mapper"))
            try:
                socket.api_connect.feedobj.feed_time_start()
            except Exception as e:
                pass
            socket.quotes_streamer_start()
            socket.depth_streamer_start(list(socket.options_data.keys()))
          
            while True:
                try:
                    if socket.api_connect.feedobj.t_read:
                        if socket.api_connect.feedobj.t_read.is_alive():
                            pass
                        else:
                            raise Exception("Tread not alive")
                    else:
                        raise Exception("Exception tread")
                    if socket.api_connect.feedobj.timer:
                        if socket.api_connect.feedobj.timer.isTimerActive():
                            pass
                        else:
                            raise Exception("Exception timer")
                    else:
                        raise Exception("Time obj not found")
                    time.sleep(0.5)
                except KeyboardInterrupt:
                    os._exit(1)
                except Exception as e:
                   raise e
        except Exception as e:
            print(traceback.format_exc())
            print("Error Occured: ",e, " Restarting...")
            socket.shutdown()
            socket.depth_shutdown()
            time.sleep(1)
        except KeyboardInterrupt:
            os._exit(1)
            