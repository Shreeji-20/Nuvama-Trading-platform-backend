import pandas as pd
from APIConnect.APIConnect import APIConnect 
import orjson
from constants.exchange import ExchangeEnum
from constants.order_type import OrderTypeEnum
from constants.product_code import ProductCodeENum
from constants.duration import DurationEnum
from constants.action import ActionEnum
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Thread
import redis
import traceback
import time
class Orders:
    def __init__(self,user_obj_dict) -> None:
        print("Initializing Orders class")
        self.user_obj_dict = user_obj_dict
        self.r = redis.Redis(host='localhost', port=6379, db=0)
        
    def place_order(self, order_details) -> dict:
        """
        Place a single order with improved error handling and logging.
        Returns the order details with order_id and placed_time added.
        """
        try:
            # user = orjson.loads(self.r.get(f"user:{order_details.get('user_id')}").decode())
            # api_connect = APIConnect(user.get("apikey"), "", "", False, "", False)
            api_connect = self.user_obj_dict.get(order_details.get('user_id'))
            # print("API Connect object created.")

            print(f"Placing order: {order_details.get('Trading_Symbol')} - {order_details.get('Action')} - Qty: {order_details.get('Slice_Quantity')}")
            
            response = api_connect.PlaceTrade(
                Trading_Symbol=order_details.get("Trading_Symbol", ""),
                Exchange=order_details.get("Exchange", ExchangeEnum.NSE),
                Action=order_details.get("Action", ActionEnum.BUY),
                Duration=DurationEnum.DAY, 
                Order_Type=order_details.get("Order_Type", OrderTypeEnum.MARKET),
                Quantity=order_details.get("Slice_Quantity", 1),
                Streaming_Symbol=order_details.get("Streaming_Symbol", "4963_NSE"),
                Limit_Price=str(abs(float(order_details.get("Limit_Price", "0")))),
                Disclosed_Quantity="0",
                TriggerPrice=order_details.get("TriggerPrice", "0"),
                ProductCode=order_details.get("ProductCode", ProductCodeENum.NRML),
                remark=order_details.get("remark", "")
            )
            
            response = orjson.loads(response)
            print("Resp : ",response)
            order_details['order_id'] = response['data']['oid']
            order_details['placed_time'] = response['srvTm']
            print(f"Order placed successfully: {order_details['order_id']}")
          
            return order_details
            
        except Exception as e:
            print(traceback.format_exc())
            print(f"Error placing order: {e}")
            breakpoint()
            order_details['order_id'] = None
            order_details['placed_time'] = None
            return order_details
    
    def _place_base_leg_order(self, base_leg_order, new_filled, leg_index):
        """
        Helper method to place a single base leg order.
        Returns tuple (leg_index, success, result)
        """
        try:
            if isinstance(base_leg_order, dict) and new_filled > 0:
                base_leg_order['Slice_Quantity'] = new_filled
                result = self.place_order(base_leg_order)
                print(f"Base Leg {leg_index+1} order placed! Qty: {new_filled}")
                return (leg_index, True, result)
            else:
                return (leg_index, False, "Invalid order or zero quantity")
        except Exception as e:
            print(f"Error placing Base Leg {leg_index+1} order: {e}")
            return (leg_index, False, str(e))
    
    def _place_base_leg_orders_parallel(self, base_leg_orders, new_filled):
        """
        Place multiple base leg orders in parallel using ThreadPoolExecutor.
        Returns list of results.
        """
        results = []
        
        # Filter valid orders
        valid_orders = [(i, order) for i, order in enumerate(base_leg_orders) 
                       if isinstance(order, dict) and new_filled > 0]
        
        if not valid_orders:
            return results
        
        # Use ThreadPoolExecutor for parallel execution
        max_workers = min(8, len(valid_orders))  # Limit to 8 threads max
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all orders
            future_to_order = {
                executor.submit(self._place_base_leg_order, order, new_filled, i): i 
                for i, order in valid_orders
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_order):
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    leg_index = future_to_order[future]
                    print(f"Base leg {leg_index+1} order failed with exception: {e}")
                    results.append((leg_index, False, str(e)))
        
        return results
       
    
    def cancel_order(self, order_details: dict):
        """
        Cancel an order with improved error handling and logging.
        """
        try:
            # user = orjson.loads(self.r.get(f"user:{order_details.get('user_id')}").decode())
            # api_connect = APIConnect(user.get("apikey"), "", "", False, "", False)
            api_connect = self.user_obj_dict.get(order_details.get('user_id'))
            print(f"Cancelling order: {order_details.get('order_id')} - {order_details.get('Trading_Symbol')}")
            
            response = api_connect.CancelTrade(
                Order_ID=order_details.get("order_id", ""),
                Exchange=order_details.get("Exchange", ExchangeEnum.NSE),
                Order_Type=order_details.get("Order_Type", OrderTypeEnum.MARKET),
                Product_Code=order_details.get("Product_Code", ProductCodeENum.NRML),
                Trading_Symbol=order_details.get("Trading_Symbol", "4963_NSE"),
                Action=order_details.get("Action", ActionEnum.BUY),
                Streaming_Symbol=order_details.get("Streaming_Symbol", "4963_NSE"),
                CurrentQuantity=1
            )
            
            print(f"Cancel order response: {response}")
            return response
            
        except Exception as e:
            print(f"Error cancelling order: {e}")
            return {"status": "error", "message": str(e)}
    def IOC_order(self, order_details: dict, *base_leg_orders):
        """
        IOC order handler that supports multiple base leg orders.
        Args:
            order_details: Main bidding leg order details
            *base_leg_orders: Variable number of base leg order details
        """
        redis_key = f"order:{order_details['user_id']}" + f"{order_details['remark']}" + f"{order_details['order_id']}"
        qty = 0
        start = time.time()
        print("IOC timeout : ", order_details['IOC'])
        print(f"Processing IOC for main order and {len(base_leg_orders)} base leg orders")
        
        while time.time() - start < int(order_details['IOC']):
            try:
                if self.r.exists(redis_key):
                    order_data = orjson.loads(self.r.get(redis_key).decode())
                    current_filled_qty = int(order_data['response']['data']['fQty'])
                    print("QTY : ",qty,"Current filled : ", current_filled_qty, order_details['Slice_Quantity'])
                    
                    if current_filled_qty > qty:
                        # Calculate the new filled quantity
                        new_filled = current_filled_qty - qty
                        
                        # Place orders for all base legs in parallel
                        print(f"Placing {len(base_leg_orders)} base leg orders in parallel with qty: {new_filled}")
                        start_parallel = time.time()
                        
                        results = self._place_base_leg_orders_parallel(base_leg_orders, new_filled)
                        
                        parallel_time = time.time() - start_parallel
                        successful_orders = sum(1 for _, success, _ in results if success)
                        print(f"Parallel execution completed in {parallel_time:.3f}s - {successful_orders}/{len(results)} orders successful")
                        
                        qty = current_filled_qty
                else:
                    pass
            except Exception as e:
                print("In IOC ORDER :", e)
        
        if qty == order_details['Slice_Quantity']:
            print(f"Order {order_details['order_id']} completed successfully.")
            return True
        else:
            # Track filled quantity changes during cancellation
            cancellation_filled_qty = qty
            cancel_completed = False
            
            def cancel_order_thread():
                """Thread function to cancel order"""
                nonlocal cancel_completed
                try:
                    print("Starting order cancellation...")
                    resp = self.cancel_order(order_details)
                    print(f"Cancel order response: {resp}")
                    cancel_completed = True
                except Exception as e:
                    print(f"Error in cancel order thread: {e}")
                    cancel_completed = True
            
            def check_filled_qty_continuously():
                """Continuously check filled quantity while cancellation is in progress"""
                nonlocal cancellation_filled_qty
                check_start = time.time()
                
                while not cancel_completed:
                    try:
                        if self.r.exists(redis_key):
                            order_data = orjson.loads(self.r.get(redis_key).decode())
                            current_filled = int(order_data['response']['data']['fQty'])
                            
                            if current_filled > cancellation_filled_qty:
                                new_filled = current_filled - cancellation_filled_qty
                                print(f"Detected additional fill during cancellation: {new_filled} (Total: {current_filled})")
                                
                                # Place base leg orders for the new filled quantity
                                print(f"Placing {len(base_leg_orders)} base leg orders during cancellation with qty: {new_filled}")
                                results = self._place_base_leg_orders_parallel(base_leg_orders, new_filled)
                                successful_orders = sum(1 for _, success, _ in results if success)
                                print(f"During-cancellation orders: {successful_orders}/{len(results)} successful")
                                
                                cancellation_filled_qty = current_filled
                        
                        time.sleep(0.01)  # Check every 10ms to minimize latency
                        
                        # Safety timeout - don't check forever
                        if time.time() - check_start > 10:  # 10 second timeout
                            print("Continuous checking timeout reached")
                            break
                            
                    except Exception as e:
                        print(f"Error in continuous quantity checking: {e}")
                        time.sleep(0.05)
            
            # Start cancel order in background thread
            cancel_thread = Thread(target=cancel_order_thread)
            cancel_thread.daemon = True
            cancel_thread.start()
            
            # Start continuous checking in background thread
            check_thread = Thread(target=check_filled_qty_continuously)
            check_thread.daemon = True
            check_thread.start()
            
            # Wait for cancel order to complete
            cancel_thread.join(timeout=10)  # 10 second timeout
            
            # Ensure continuous checking stops
            cancel_completed = True
            check_thread.join(timeout=1)
           
            # Final check after cancellation is complete
          
            try:
                print("Performing final quantity check after cancellation...")
                if self.r.exists(redis_key):
                    order_data = orjson.loads(self.r.get(redis_key).decode())
                    final_filled_qty = int(order_data['response']['data']['fQty'])
                    if final_filled_qty > cancellation_filled_qty:
                        remaining_filled = final_filled_qty - cancellation_filled_qty
                            # Place remaining base leg orders in parallel
                        print(f"Placing final {len(base_leg_orders)} base leg orders with remaining qty: {remaining_filled}")
                        start_final = time.time()
                        results = self._place_base_leg_orders_parallel(base_leg_orders, remaining_filled)
                        final_time = time.time() - start_final
                        successful_final = sum(1 for _, success, _ in results if success)
                        print(f"Final parallel execution completed in {final_time:.3f}s - {successful_final}/{len(results)} orders successful")
                        qty = final_filled_qty
                    else:
                        qty = cancellation_filled_qty
                        print(f"No additional fills detected in final check. Final qty: {qty}")
                else:
                    qty = cancellation_filled_qty
                    print("Redis key not found in final check")
            except Exception as e:
                print("Error in final base leg order placement:", e)
                qty = cancellation_filled_qty
            
            print(f"\nRemaining Qty: {order_details['Quantity'] - qty}\n")
            return False
    
    def get_order_status(self, order_id: str, user_id: str) -> dict:
        """
        Get the current status of an order from Redis.
        """
        try:
            redis_key = f"order:{user_id}Lord_Shreeji{order_id}"
            if self.r.exists(redis_key):
                order_data = orjson.loads(self.r.get(redis_key).decode())
                return {
                    "status": "found",
                    "filled_qty": int(order_data.get('response', {}).get('data', {}).get('fQty', 0)),
                    "total_qty": int(order_data.get('response', {}).get('data', {}).get('qty', 0)),
                    "order_data": order_data
                }
            else:
                return {"status": "not_found", "filled_qty": 0, "total_qty": 0}
        except Exception as e:
            print(f"Error getting order status: {e}")
            return {"status": "error", "filled_qty": 0, "total_qty": 0}
    
    def place_multiple_orders(self, order_list: list) -> list:
        """
        Place multiple orders and return the results.
        """
        results = []
        for order_details in order_list:
            try:
                result = self.place_order(order_details)
                results.append(result)
            except Exception as e:
                print(f"Error placing order in batch: {e}")
                results.append({"error": str(e), "original_order": order_details})
        return results
    
    def place_multiple_orders_parallel(self, order_list: list, max_workers: int = 8) -> list:
        """
        Place multiple orders in parallel using ThreadPoolExecutor.
        Faster than sequential execution for multiple independent orders.
        """
        if not order_list:
            return []
        
        results = []
        max_workers = min(max_workers, len(order_list))
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all orders
            future_to_order = {
                executor.submit(self.place_order, order): i 
                for i, order in enumerate(order_list)
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_order):
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    order_index = future_to_order[future]
                    print(f"Order {order_index} failed with exception: {e}")
                    results.append({"error": str(e), "original_order": order_list[order_index]})
        
        return results
    
    def cancel_multiple_orders(self, order_list: list) -> list:
        """
        Cancel multiple orders and return the results.
        """
        results = []
        for order_details in order_list:
            try:
                result = self.cancel_order(order_details)
                results.append(result)
            except Exception as e:
                print(f"Error cancelling order in batch: {e}")
                results.append({"error": str(e), "original_order": order_details})
        return results