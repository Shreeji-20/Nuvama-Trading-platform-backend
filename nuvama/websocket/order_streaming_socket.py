import orjson
import time
from APIConnect.APIConnect import APIConnect
import redis
from datetime import datetime
class OrderStreamingSocket:
    def __init__(self, user_id):
        self.user_id = user_id
        self.r = redis.Redis(host='localhost', port=6379, db=0)
        # user = orjson.loads(self.r.get(f"{user_id}").decode())
        # self.api_connect = APIConnect(str(user.get("apikey")), "", "", False, "",False)
        # self.api_connect.feedobj.feed_time_start()
        # self.order_streamer = self.api_connect.initOrdersStreaming(user_id, user_id)
        
    def order_streaming_callback(self, response):
        try:
            response = orjson.loads(response.encode())
            redis_key = f"order:{response['response']['data']['userID']}" + f"{response['response']['data']['rmk']}" + f"{response['response']['data']['oID']}"   
            self.r.set(redis_key, orjson.dumps(response).decode())
        except Exception as e:
            print(f"Error processing order streaming response: {str(e)}")
                
    def order_streaming_start(self):
        user = orjson.loads(self.r.get(f"user:{self.user_id}").decode())
        self.api_connect = APIConnect(str(user.get("apikey")), "", "", False, "",False)
        try:
            self.api_connect.feedobj.feed_time_start()
        except Exception as e:
            print(e)
        self.order_streamer = self.api_connect.initOrdersStreaming(self.user_id, self.user_id)
        # orders_streamer = self.api_connect.initOrdersStreaming("70204607", "70204607")
        self.order_streamer.subscribeOrdersFeed(self.order_streaming_callback)
        
        