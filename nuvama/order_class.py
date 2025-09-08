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
    def __init__(self, user_obj_dict) -> None:
        self.user_obj_dict = user_obj_dict
        self.r = redis.Redis(host='localhost', port=6379, db=0)
        # Fixed ThreadPoolExecutor with 4 workers
        self.executor = ThreadPoolExecutor(max_workers=4)
        
    def place_order(self, order_details) -> dict:
        """
        Place a single order with improved error handling and logging.
        Returns the order details with order_id and placed_time added.
        """
        try:
            api_connect = self.user_obj_dict.get(order_details.get('user_id'))
            
            response = api_connect.PlaceTrade(
                Trading_Symbol=order_details.get("Trading_Symbol", ""),
                Exchange=order_details.get("Exchange", ExchangeEnum.NSE),
                Action=order_details.get("Action", ActionEnum.BUY),
                Duration=DurationEnum.DAY, 
                Order_Type=order_details.get("Order_Type", OrderTypeEnum.MARKET),
                Quantity=int(order_details.get("Slice_Quantity", 1)),
                Streaming_Symbol=order_details.get("Streaming_Symbol", "4963_NSE"),
                Limit_Price=str(abs(float(order_details.get("Limit_Price", "0")))),
                Disclosed_Quantity="0",
                TriggerPrice=order_details.get("TriggerPrice", "0"),
                ProductCode=order_details.get("ProductCode", ProductCodeENum.NRML),
                remark=order_details.get("remark", "")
            )
            
            response = orjson.loads(response)
            order_details['order_id'] = response['data']['oid']
            order_details['placed_time'] = response['srvTm']
            order_details['request'] = order_details
            order_details['response'] = response
            return order_details
            
        except Exception as e:
            print(f"ERROR: Failed to place order: {e}")
            print(traceback.format_exc())
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
                return (leg_index, True, result)
            else:
                return (leg_index, False, "Invalid order or zero quantity")
        except Exception as e:
            print(f"ERROR: Failed to place Base Leg {leg_index+1} order: {e}")
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
        
        # Use fixed ThreadPoolExecutor for parallel execution
        future_to_order = {
            self.executor.submit(self._place_base_leg_order, order, new_filled, i): i 
            for i, order in valid_orders
        }
        
        # Collect results as they complete
        for future in as_completed(future_to_order):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                leg_index = future_to_order[future]
                print(f"ERROR: Base leg {leg_index+1} order failed with exception: {e}")
                results.append((leg_index, False, str(e)))
        
        return results

    def _create_basket_order(self, base_leg_order, new_filled):
        """
        Create a basket order object from base leg order details.
        """
        try:
            from APIConnect.APIConnect import Order  # Import Order class
            
            return Order(
                Exchange=base_leg_order.get("Exchange", ExchangeEnum.NSE),
                TradingSymbol=base_leg_order.get("Trading_Symbol", ""),
                StreamingSymbol=base_leg_order.get("Streaming_Symbol", "4963_NSE"),
                Action=base_leg_order.get("Action", ActionEnum.BUY),
                ProductCode=base_leg_order.get("ProductCode", ProductCodeENum.NRML),
                OrderType=base_leg_order.get("Order_Type", OrderTypeEnum.MARKET),
                Duration=DurationEnum.DAY,
                Price=str(abs(float(base_leg_order.get("Limit_Price", "0")))),
                TriggerPrice=base_leg_order.get("TriggerPrice", "0"),
                Quantity=int(new_filled),
                DisclosedQuantity=base_leg_order.get("Disclosed_Quantity", "0"),
                GTDDate="NA",
                Remark=base_leg_order.get("remark", "Lord_Shreeji")
                # Remark="Lord_Shreeji"
            )
        except Exception as e:
            print(f"ERROR: Failed to create basket order: {e}")
            return None

    def _place_base_leg_orders_basket(self, base_leg_orders, new_filled, user_id):
        """
        Place multiple base leg orders using basket order API.
        Returns success status and results.
        """
        try:
            # Filter valid orders and create basket order list
            basket_orders = []
            for i, order in enumerate(base_leg_orders):
                if isinstance(order, dict) and new_filled > 0:
                    basket_order = self._create_basket_order(order, new_filled)
                    if basket_order:
                        basket_orders.append(basket_order)
            
            if not basket_orders:
                return False, "No valid orders to place"
            
            # Get API connection for user
            api_connect = self.user_obj_dict.get(user_id)
            if not api_connect:
                return False, f"No API connection found for user {user_id}"
            
            # Place basket order
            response = api_connect.PlaceBasketTrade(orderlist=basket_orders)
            print("Basket order resp : ",response)
            # breakpoint()
            return True, response
            
        except Exception as e:
            print(f"ERROR: Failed to place basket orders: {e}")
            return False, str(e)
       
    def cancel_order(self, order_details: dict):
        """
        Cancel an order with improved error handling and logging.
        """
        try:
            api_connect = self.user_obj_dict.get(order_details.get('user_id'))
            
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
            
            return response
            
        except Exception as e:
            print(f"ERROR: Failed to cancel order: {e}")
            return {"status": "error", "message": str(e)}
        
    def IOC_order(self, order_details: dict, *base_leg_orders, use_basket=True):
        """
        Simplified IOC order handler that supports multiple base leg orders.
        Args:
            order_details: Main bidding leg order details
            *base_leg_orders: Variable number of base leg order details
            use_basket: Whether to use basket orders for base legs (default: True)
        """
        # Fix Redis key format to match sequential box strategy
        order_id = order_details.get('order_id', '')
        redis_key = f"order:{order_details['user_id']}{order_details['remark']}{order_id}"
        qty = 0
        start = time.time()
       
        # Fix timeout handling - convert to float first, then int for seconds
        timeout = float(order_details.get('IOC', 0.5))
        user_id = order_details['user_id']
        
        # Track base leg order results
        base_leg_results = []
        
        # Main IOC monitoring loop
        while time.time() - start < timeout:
            try:
                if self.r.exists(redis_key):
                    order_data = orjson.loads(self.r.get(redis_key).decode())
                    current_filled_qty = int(order_data['response']['data']['fQty'])
                    
                    if current_filled_qty > qty:
                        # Calculate the new filled quantity
                        new_filled = current_filled_qty - qty
                        
                        # Place base leg orders
                        if base_leg_orders:
                            if use_basket:
                                # Use basket order approach
                                success, result = self._place_base_leg_orders_basket(
                                    base_leg_orders, new_filled, user_id)
                                if not success:
                                    print(f"ERROR: Basket order failed: {result}")
                                else:
                                    # Store basket result for tracking
                                    base_leg_results.append(('basket', success, result))
                            else:
                                # Use parallel individual orders
                                results = self._place_base_leg_orders_parallel(base_leg_orders, new_filled)
                                base_leg_results.extend(results)  # Store individual results
                                successful_orders = sum(1 for _, success, _ in results if success)
                        
                        qty = current_filled_qty
                        
                time.sleep(0.01)  # Small delay to prevent excessive polling
                
            except Exception as e:
                print(f"ERROR: IOC order monitoring failed: {e}")
        
        # Check if order completed successfully (compare with actual order quantity)
        target_qty = int(order_details.get('Slice_Quantity', order_details.get('Quantity', 0)))
        if qty >= target_qty:
            # Get fill prices using get_order_status
            remark = order_details.get('remark', 'Lord_Shreeji')
            bidding_leg_status = self.get_order_status(order_id, user_id, remark)
            
            # Collect base leg fill prices based on how they were placed
            base_leg_prices = []
            if base_leg_orders and base_leg_results:
                if use_basket:
                    # For basket orders, we can't easily get individual order IDs
                    # So we'll return the basket response info
                    for i, base_order in enumerate(base_leg_orders):
                        base_leg_prices.append({
                            'leg_index': i,
                            'filled_price': 0,  # Basket orders don't provide individual prices easily
                            'filled_qty': qty,  # Assume same fill qty as bidding leg
                            'basket_response': base_leg_results[-1][2] if base_leg_results else None
                        })
                else:
                    # For individual orders, get prices from each placed order
                    for leg_index, success, result in base_leg_results:
                        if success and isinstance(result, dict) and result.get('order_id'):
                            base_status = self.get_order_status(
                                result['order_id'], 
                                user_id, 
                                result.get('remark', remark)
                            )
                            base_leg_prices.append({
                                'leg_index': leg_index,
                                'filled_price': base_status.get('filled_price', 0),
                                'filled_qty': base_status.get('filled_qty', 0)
                            })
                        else:
                            base_leg_prices.append({
                                'leg_index': leg_index,
                                'filled_price': 0,
                                'filled_qty': 0,
                                'error': result if not success else None
                            })
            
            return True, {
                'bidding_leg': {
                    'filled_price': bidding_leg_status.get('filled_price', 0),
                    'filled_qty': bidding_leg_status.get('filled_qty', 0)
                },
                'base_legs': base_leg_prices
            }
        else:
            # Cancel incomplete order
            try:
                self.cancel_order(order_details)
                
                # Final check for any additional fills after cancellation
                if self.r.exists(redis_key):
                    order_data = orjson.loads(self.r.get(redis_key).decode())
                    final_filled_qty = int(order_data['response']['data']['fQty'])
                    
                    if final_filled_qty > qty:
                        # Place base leg orders for remaining quantity
                        remaining_filled = final_filled_qty - qty
                        if base_leg_orders:
                            if use_basket:
                                success, result = self._place_base_leg_orders_basket(
                                    base_leg_orders, remaining_filled, user_id)
                                if not success:
                                    print(f"ERROR: Final basket order failed: {result}")
                                else:
                                    base_leg_results.append(('basket_final', success, result))
                            else:
                                results = self._place_base_leg_orders_parallel(base_leg_orders, remaining_filled)
                                base_leg_results.extend(results)
                        qty = final_filled_qty
                        
                        # Check if we now have complete fill
                        if qty >= target_qty:
                            # Get fill prices for successful completion after cancellation
                            remark = order_details.get('remark', 'Lord_Shreeji')
                            bidding_leg_status = self.get_order_status(order_id, user_id, remark)
                            
                            base_leg_prices = []
                            if base_leg_orders and base_leg_results:
                                if use_basket:
                                    # For basket orders, return basket response info
                                    for i, base_order in enumerate(base_leg_orders):
                                        base_leg_prices.append({
                                            'leg_index': i,
                                            'filled_price': 0,  # Basket orders don't provide individual prices easily
                                            'filled_qty': qty,  # Assume same fill qty as bidding leg
                                            'basket_response': base_leg_results[-1][2] if base_leg_results else None
                                        })
                                else:
                                    # For individual orders, get prices from each placed order
                                    for leg_index, success, result in base_leg_results:
                                        if success and isinstance(result, dict) and result.get('order_id'):
                                            base_status = self.get_order_status(
                                                result['order_id'], 
                                                user_id, 
                                                result.get('remark', remark)
                                            )
                                            base_leg_prices.append({
                                                'leg_index': leg_index,
                                                'filled_price': base_status.get('filled_price', 0),
                                                'filled_qty': base_status.get('filled_qty', 0)
                                            })
                                        else:
                                            base_leg_prices.append({
                                                'leg_index': leg_index,
                                                'filled_price': 0,
                                                'filled_qty': 0,
                                                'error': result if not success else None
                                            })
                            
                            return True, {
                                'bidding_leg': {
                                    'filled_price': bidding_leg_status.get('filled_price', 0),
                                    'filled_qty': bidding_leg_status.get('filled_qty', 0)
                                },
                                'base_legs': base_leg_prices
                            }
                        else:
                            return False, {'error': 'Partial fill after cancellation'}
                    else:
                        return False, {'error': 'Order cancelled with no additional fills'}
            except Exception as e:
                print(f"ERROR: Order cancellation failed: {e}")
            
            return False, {'error': 'Order failed to complete'}
    
    def get_order_status(self, order_id: str, user_id: str, remark: str = "Lord_Shreeji") -> dict:
        """
        Get the current status of an order from Redis.
        Args:
            order_id: Order ID
            user_id: User ID
            remark: Order remark (default: "Lord_Shreeji")
        """
        try:
            # Use consistent Redis key format
            redis_key = f"order:{user_id}{remark}{order_id}"
            if self.r.exists(redis_key):
                order_data = orjson.loads(self.r.get(redis_key).decode())
                return {
                    "status": "found",
                    "filled_qty": int(order_data.get('response', {}).get('data', {}).get('fQty', 0)),
                    "total_qty": int(order_data.get('response', {}).get('data', {}).get('qty', 0)),
                    "filled_price": float(order_data.get('response', {}).get('data', {}).get('fPrc', 0)),
                    "order_data": order_data,
                    "order_status": order_data.get('response', {}).get('data', {}).get('sts', 'unknown')
                }
            else:
                return {"status": "not_found", "filled_qty": 0, "total_qty": 0}
        except Exception as e:
            print(f"ERROR: Failed to get order status: {e}")
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
                print(f"ERROR: Failed to place order in batch: {e}")
                results.append({"error": str(e), "original_order": order_details})
        return results
    
    def place_multiple_orders_parallel(self, order_list: list, max_workers: int = None) -> list:
        """
        Place multiple orders in parallel using the fixed ThreadPoolExecutor.
        Faster than sequential execution for multiple independent orders.
        """
        if not order_list:
            return []
        
        results = []
        
        # Use fixed ThreadPoolExecutor for parallel execution
        future_to_order = {
            self.executor.submit(self.place_order, order): i 
            for i, order in enumerate(order_list)
        }
        
        # Collect results as they complete
        for future in as_completed(future_to_order):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                order_index = future_to_order[future]
                print(f"ERROR: Order {order_index} failed with exception: {e}")
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
                print(f"ERROR: Failed to cancel order in batch: {e}")
                results.append({"error": str(e), "original_order": order_details})
        return results

    def place_basket_order(self, order_list: list, user_id: str) -> dict:
        """
        Place multiple orders using basket order API.
        Args:
            order_list: List of order details to be converted to basket orders
            user_id: User ID for API connection
        Returns:
            Response from basket order API
        """
        try:
            # Get API connection for user
            api_connect = self.user_obj_dict.get(user_id)
            if not api_connect:
                raise Exception(f"No API connection found for user {user_id}")
            
            # Convert order details to basket order format
            basket_orders = []
            for order_details in order_list:
                basket_order = self._create_basket_order(order_details, 
                                                       order_details.get('Slice_Quantity', 1))
                if basket_order:
                    basket_orders.append(basket_order)
            
            if not basket_orders:
                return {"status": "error", "message": "No valid orders to place"}
            
            # Place basket order
            response = api_connect.PlaceBasketTrade(orderlist=basket_orders)
            return {"status": "success", "response": response}
            
        except Exception as e:
            print(f"ERROR: Failed to place basket order: {e}")
            return {"status": "error", "message": str(e)}

    def modify_order(self, order_details: dict) -> dict:
        """
        Modify an existing order with improved error handling and logging.
        
        Args:
            order_details (dict): Dictionary containing order modification details with keys:
                - user_id: User ID for API connection
                - Trading_Symbol: Trading symbol
                - Exchange: Exchange enum (ExchangeEnum.NSE, etc.)
                - Action: Action enum (ActionEnum.BUY/SELL)
                - Duration: Duration enum (DurationEnum.DAY/IOC)
                - Order_Type: Order type enum (OrderTypeEnum.LIMIT/MARKET)
                - Quantity: New quantity
                - CurrentQuantity: Current quantity of the order
                - Streaming_Symbol: Streaming symbol
                - Limit_Price: New limit price
                - Order_ID: Order ID to modify
                - Disclosed_Quantity: Disclosed quantity (default: "0")
                - TriggerPrice: Trigger price (default: "0")
                - ProductCode: Product code enum (ProductCodeENum.NRML, etc.)
        
        Returns:
            dict: Response from API or error details
        """
        try:
            api_connect = self.user_obj_dict.get(order_details.get('user_id'))
            
            if not api_connect:
                return {"status": "error", "message": f"No API connection found for user {order_details.get('user_id')}"}
            
            response = api_connect.ModifyTrade(
                Trading_Symbol=order_details.get("Trading_Symbol", ""),
                Exchange=order_details.get("Exchange", ExchangeEnum.NSE),
                Action=order_details.get("Action", ActionEnum.BUY),
                Duration=order_details.get("Duration", DurationEnum.DAY),
                Order_Type=order_details.get("Order_Type", OrderTypeEnum.LIMIT),
                Quantity=int(order_details.get("Quantity", 1)),
                CurrentQuantity=int(order_details.get("CurrentQuantity", 1)),
                Streaming_Symbol=order_details.get("Streaming_Symbol", "4963_NSE"),
                Limit_Price=str(abs(float(order_details.get("Limit_Price", "0")))),
                Order_ID=order_details.get("Order_ID", ""),
                Disclosed_Quantity=order_details.get("Disclosed_Quantity", "0"),
                TriggerPrice=order_details.get("TriggerPrice", "0"),
                ProductCode=order_details.get("ProductCode", ProductCodeENum.NRML)
            )
            
            # Parse response if it's JSON
            if isinstance(response, str):
                try:
                    response = orjson.loads(response)
                except:
                    pass
            
            print(f"INFO: Order modification successful for Order ID: {order_details.get('Order_ID')}")
            return {"status": "success", "response": response}
            
        except Exception as e:
            print(f"ERROR: Failed to modify order {order_details.get('Order_ID', 'Unknown')}: {e}")
            return {"status": "error", "message": str(e)}

    def modify_multiple_orders(self, order_list: list) -> list:
        """
        Modify multiple orders sequentially.
        
        Args:
            order_list (list): List of order dictionaries to modify
        
        Returns:
            list: List of responses for each order modification
        """
        results = []
        for order_details in order_list:
            result = self.modify_order(order_details)
            results.append(result)
        return results

    def modify_multiple_orders_parallel(self, order_list: list, max_workers: int = None) -> list:
        """
        Modify multiple orders in parallel using ThreadPoolExecutor.
        
        Args:
            order_list (list): List of order dictionaries to modify
            max_workers (int): Maximum number of worker threads (default: None uses class default)
        
        Returns:
            list: List of responses for each order modification
        """
        results = []
        if not max_workers:
            max_workers = min(len(order_list), 4)  # Limit to 4 workers max
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_order = {executor.submit(self.modify_order, order): order for order in order_list}
            
            for future in as_completed(future_to_order):
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    print(f"ERROR: Order modification failed: {e}")
                    results.append({"status": "error", "message": str(e)})
        
        return results

    def __del__(self):
        """Cleanup method to shutdown ThreadPoolExecutor"""
        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=True)