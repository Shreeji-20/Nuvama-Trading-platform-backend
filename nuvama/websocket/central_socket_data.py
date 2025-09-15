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

# Import tick data manager from the same directory
try:
    from tick_data_manager import TickDataManager
except ImportError:
    # Fallback: try importing from current directory
    sys.path.append(current_dir)
    from tick_data_manager import TickDataManager

from basic_functions import common_functions
class CentralSocketData:
    def __init__(self, enable_tick_logging=True, tick_data_base_dir="tick_data"):
        self.r = redis.Redis(host='localhost', port=6379, db=0)
        self.api_connect = APIConnect("iBe07GpBTEbbhg", "", "", True, "",False)
        
        # Initialize tick data manager for high-performance tick logging
        self.enable_tick_logging = enable_tick_logging
        if self.enable_tick_logging:
            try:
                self.tick_manager = TickDataManager(
                    base_directory=tick_data_base_dir,
                    enable_compression=True,  # Compress files to save space
                    buffer_size=500,  # Buffer 500 ticks before writing
                    flush_interval=3.0  # Flush every 3 seconds
                )
                print(f"[SUCCESS] Tick data logging enabled - Directory: {tick_data_base_dir}")
            except Exception as e:
                print(f"[ERROR] Failed to initialize tick data manager: {e}")
                self.enable_tick_logging = False
                self.tick_manager = None
        else:
            self.tick_manager = None
            print("[WARNING] Tick data logging disabled")
        
        if self.r.exists("option_mapper"):
            self.options_data = orjson.loads(self.r.get("option_mapper").decode())
        else:
            self.options_data = {}
        self.quotes_streamer = self.api_connect.initReducedQuotesStreaming()
        self.depth_streamer = self.api_connect.initDepthStreaming()
        
        
        self.reduced_quotes = ['-29','-101']
        self.quotes_streamer_start()
        time.sleep(2)
        self.common_obj = common_functions()
        
        # if not self.options_data:
        self.common_obj.refresh_strikes_and_options(expiry=2,symbol='NIFTY',exchange="NFO")
        self.common_obj.refresh_strikes_and_options(expiry=3,symbol='NIFTY',exchange="NFO")
        self.common_obj.refresh_strikes_and_options(expiry=1,symbol='NIFTY',exchange="NFO")
        self.common_obj.refresh_strikes_and_options(expiry=1,symbol='SENSEX',exchange="BFO")
        self.common_obj.refresh_strikes_and_options(expiry=2,symbol='SENSEX',exchange="BFO")
        self.common_obj.refresh_strikes_and_options(expiry=3,symbol='SENSEX',exchange="BFO")
        self.common_obj.refresh_strikes_and_options(expiry=0,symbol='NIFTY',exchange="NFO")
        self.common_obj.refresh_strikes_and_options(expiry=0,symbol='SENSEX',exchange="BFO")
        equity_symbols = self.r.lrange('excel_column_a_data', 0, -1)
        equity_symbols = [symbol.decode('utf-8') for symbol in equity_symbols]
        print("Equity Symbols for options fetching: ", equity_symbols)
        self.common_obj.refresh_strikes_and_options(expiry=0,symbol=equity_symbols,exchange="NSE")
            
                
        time.sleep(2)
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
            print("ReducedQuotesFeedCallback response received", response)
            response = orjson.loads(response.encode())
            if str(response['response']['data']['sym']) == "-29":
                symbol = "NIFTY"
            elif str(response['response']['data']['sym']) == "-101":
                symbol = "SENSEX"
            else:
                symbol = "OTHER"
            response['response']['data']['symbol'] = symbol
            
            # Save tick data to files for simulation (high-performance, non-blocking)
            if self.enable_tick_logging and self.tick_manager:
                try:
                    self.tick_manager.save_quotes_tick(symbol, response)
                except Exception as tick_error:
                    # Don't let tick logging errors affect main processing
                    pass
            # breakpoint()
            # Continue with original Redis storage
            self.r.set(f"reduced_quotes:{symbol}", orjson.dumps(response).decode())
        except Exception as e:
            print(f"Error processing response (callbackfun): {str(e)}")
            
    def quotes_streamer_start(self,symbol=[]):
        self.quotes_streamer.subscribeReducedQuotesFeed(self.reduced_quotes,  self.ReducedQuotesFeedCallback)
    def quotes_streamer_stop(self):
        self.quotes_streamer.unsubscribeReducedQuotesFeed()
    def shutdown(self):
        self.quotes_streamer.shutdown()
        # Stop tick data manager if running
        if self.enable_tick_logging and self.tick_manager:
            try:
                print("[INFO] Stopping tick data manager...")
                self.tick_manager.stop()
            except Exception as e:
                print(f"[ERROR] Error stopping tick data manager: {e}")
    
    def get_tick_data_stats(self):
        """Get tick data logging statistics"""
        if self.enable_tick_logging and self.tick_manager:
            return self.tick_manager.get_stats()
        else:
            return {"tick_logging": "disabled"}
    
    def print_tick_data_stats(self):
        """Print tick data logging statistics"""
        if self.enable_tick_logging and self.tick_manager:
            self.tick_manager.print_stats()
        else:
            print("[INFO] Tick data logging is disabled")
    
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
                redis_key = f"depth:{symbolname or streaming_symbol}"

            # Save tick data to files for simulation (high-performance, non-blocking)
            if self.enable_tick_logging and self.tick_manager:
                try:
                    self.tick_manager.save_depth_tick(redis_key, response)
                except Exception as tick_error:
                    # Don't let tick logging errors affect main processing
                    pass

            # Continue with original Redis storage
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
        # Stop tick data manager if running
        if self.enable_tick_logging and self.tick_manager:
            try:
                print("[INFO] Stopping tick data manager...")
                self.tick_manager.stop()
            except Exception as e:
                print(f"[ERROR] Error stopping tick data manager: {e}")
        
        
    
import threading
if __name__ == "__main__":
    # Statistics printing thread
    def print_stats_periodically(socket_instance):
        """Print tick data statistics every 60 seconds"""
        while True:
            try:
                time.sleep(60)  # Print stats every minute
                socket_instance.print_tick_data_stats()
            except Exception as e:
                print(f"[ERROR] Error printing stats: {e}")
                break
    
    socket = CentralSocketData() 
    # breakpoint()
    t1 = threading.Thread(target=socket.listen_for_strikes_updates, daemon=True)
    t1.start()
    
    # Start stats printing thread
    t2 = threading.Thread(target=print_stats_periodically, args=(socket,), daemon=True)
    t2.start()
    
    while True:
        try:
            print("Starting Central Socket Data with Tick Logging...")
            socket = CentralSocketData(
                enable_tick_logging=True,  # Enable tick data logging
                tick_data_base_dir="tick_data"  # Base directory for tick files
            ) 
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
                    feedobj = socket.api_connect.feedobj
                    # Check reading thread
                    if feedobj.t_read:
                        if not feedobj.t_read.is_alive():
                            raise Exception("Reading thread not alive")
                    else:
                        raise Exception("No reading thread")
                    
                    # Check timer
                    if feedobj.timer:
                        if not feedobj.timer.isTimerActive():
                            raise Exception("Timer not active")
                    else:
                        raise Exception("Timer object not found")
                    
                    time.sleep(0.5)
                except KeyboardInterrupt:
                    print("\n[INFO] Keyboard interrupt - shutting down gracefully...")
                    socket.print_tick_data_stats()  # Print final stats
                    socket.shutdown()
                    socket.depth_shutdown()
                    os._exit(1)
                except Exception as e:
                   raise e
        except Exception as e:
            print(traceback.format_exc())
            print("Error Occured: ",e, " Restarting...")
            try:
                socket.shutdown()
                socket.depth_shutdown()
            except:
                pass
            time.sleep(2)  # Longer delay before restart
        except KeyboardInterrupt:
            print("\n[INFO] Keyboard interrupt - shutting down gracefully...")
            try:
                socket.print_tick_data_stats()  # Print final stats
                os._exit(1)
                socket.shutdown()
                socket.depth_shutdown()
            except:
                pass
            os._exit(1)
            