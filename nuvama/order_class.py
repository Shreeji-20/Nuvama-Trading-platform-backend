import pandas as pd
from APIConnect.APIConnect import APIConnect 
import orjson
from constants.exchange import ExchangeEnum
from constants.order_type import OrderTypeEnum
from constants.product_code import ProductCodeENum
from constants.duration import DurationEnum
from constants.action import ActionEnum

import redis
import time
class Orders:
    def __init__(self) -> None:
        print("Initializing Orders class")
        # self.api_connect = APIConnect("iBe07GpBTEbbhg", "", "", False, "","../login_data/")
        self.user_obj_dict = {}
        self.r = redis.Redis(host='localhost', port=6379, db=0)
        
    def place_order(self, order_details) -> None:
        user = orjson.loads(self.r.get(f"user:{order_details.get('user_id')}").decode())
        # user = orjson.loads(self.r.get(f"user:70249886").decode())
        api_connect = APIConnect(user.get("apikey"), "", "", False, "",False)
        response = api_connect.PlaceTrade(Trading_Symbol = order_details.get("Trading_Symbol", ""),
                                  Exchange = order_details.get("Exchange", ExchangeEnum.NSE),
                                  Action = order_details.get("Action", ActionEnum.BUY),
                                  Duration = DurationEnum.DAY, 
                                  Order_Type = order_details.get("Order_Type", OrderTypeEnum.MARKET),
                                  Quantity = order_details.get("Slice_Quantity", 1),
                                  Streaming_Symbol = order_details.get("Streaming_Symbol", "4963_NSE"),
                                  Limit_Price = order_details.get("Limit_Price", "0"), 
                                  Disclosed_Quantity="0", 
                                  TriggerPrice=order_details.get("TriggerPrice", "0"),
                                  ProductCode = order_details.get("ProductCode", ProductCodeENum.NRML),
                                  remark=order_details.get("remark", ""))
        
        response = orjson.loads(response)
        order_details['order_id'] = response['data']['oid']
        order_details['placed_time'] = response['srvTm']
        return order_details
       
    
    def cancel_order(self, order_details: dict):
        user = orjson.loads(self.r.get(f"user:{order_details.get('user_id')}").decode())
        # user = orjson.loads(self.r.get(f"user:70249886").decode())
        api_connect = APIConnect(user.get("apikey"), "", "", False, "",False)
        response = api_connect.CancelTrade(Order_ID=order_details.get("order_id", ""),
                                                Exchange=order_details.get("Exchange", ExchangeEnum.NSE),
                                                Order_Type=order_details.get("Order_Type", OrderTypeEnum.MARKET),
                                                Product_Code=order_details.get("Product_Code", ProductCodeENum.NRML),
                                                Trading_Symbol= order_details.get("Trading_Symbol", "4963_NSE"),
                                                Action= order_details.get("Action", ActionEnum.BUY),
                                                Streaming_Symbol= order_details.get("Streaming_Symbol", "4963_NSE"),
                                                CurrentQuantity= 1)
        return response
    def IOC_order(self,order_details: dict,order_details_base_leg: dict):
        redis_key = f"order:{order_details['user_id']}" + f"{order_details['remark']}" + f"{order_details['order_id']}"
        qty = 0
        start = time.time()
        while time.time() - start < int(order_details['IOC']):
            try:
                if self.r.exists(redis_key):
                    order_data = orjson.loads(self.r.get(redis_key).decode())
                    if int(order_data['response']['data']['fQty']) > 0:
                        order_details_base_leg['Slice_Quantity'] = int(order_data['response']['data']['fQty']) - qty
                        if order_details_base_leg['Slice_Quantity']>0:
                            res = self.place_order(order_details_base_leg)
                            print("Base Leg order Placed !")
                        qty = int(order_data['response']['data']['fQty'])
                        # breakpoint()
                else:
                    pass
            except Exception as e:
                print("In IOC ORDER :",e )
        if qty == order_details['Slice_Quantity']:
            # print(f"Order {order_details['order_id']} completed successfully.")
            return True
        else:
            resp = self.cancel_order(order_details)
            order_data = orjson.loads(self.r.get(redis_key).decode())
            if int(order_data['response']['data']['fQty']) > 0:
                    order_details_base_leg['Slice_Quantity'] = int(order_data['response']['data']['fQty']) - qty
                    if order_details_base_leg['Slice_Quantity']>0:
                        res = self.place_order(order_details_base_leg)
                        print("Base Leg order Placed !")
                        qty = int(order_data['response']['data']['fQty'])
            print("\n Remaining Qty :",order_details['Quantity'] - qty,"\n")
            return False
           
                
            
       
    