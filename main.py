from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import redis
import json
from fastapi.responses import JSONResponse
import pandas as pd
import time
import traceback
from routers import users , spreads, stratergy_1,multi_leg_spreads ,stratergy_4leg, observations

# Connect to Redis
r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

app = FastAPI()
app.include_router(users.router)
app.include_router(spreads.router)
app.include_router(stratergy_1.router)
app.include_router(multi_leg_spreads.router)
app.include_router(stratergy_4leg.router)
app.include_router(observations.router)


# Allow CORS from any origin (you can restrict this later)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or ["http://localhost:3000"] for specific domains
    allow_credentials=True,
    allow_methods=["*"],  # GET, POST, etc.
    allow_headers=["*"],
)
# Pydantic model for JSON request

@app.get("/")
def landing():
    return {"message": "Welcome to the Trading API"}

@app.get("/index")
def get_index_data():
    try:
        redis_keys = r.keys("reduced_quotes:*")
        if redis_keys:
            data = [json.loads(r.get(key)) for key in redis_keys]
            return JSONResponse(content=data, status_code=200)
        else:
            raise Exception("Index data not found")
    except Exception as e:
        print(traceback.format_exc())
        return JSONResponse(
            status_code=400,
            content={"error": "Bad Request", "message": str(e)}
        )
        
@app.get("/lotsizes")
def get_lotsizes_data():
    try:
        lotsizes = json.loads(r.get("lotsizes"))
        return JSONResponse(content=lotsizes, status_code=200)
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": "Bad Request", "message": str(e)}
        )

@app.get("/optiondata")
def get_options_data():
    try:
        option_mapper = json.loads(r.get("option_mapper"))
        df = pd.DataFrame(option_mapper).T
        redis_keys = r.keys("depth:*")
        data = [json.loads(r.get(key)) for key in redis_keys]
        
        get_symbol = lambda x: x["response"]["data"].get("symbol", "")
        update_data = dict.get  # shortcut to avoid attribute lookup each time

        for item in data:
            symbol = get_symbol(item)
            values = update_data(option_mapper, symbol)
            if values:
                item["response"]["data"].update(values)
        t2 = time.time()
        return JSONResponse(
            content=data,
            status_code=200
        )
        
    except Exception as e:
        print(traceback.format_exc())
        return JSONResponse(
        status_code=400,
        content={"error": "Bad Request", "message": str(e)}
    )

@app.get("/orders")
def get_orders_data():
    """
    Fetch all order keys from Redis with format 'order:*' and return JSON data
    """
    try:
        # Get all keys matching the pattern "order:*"
        order_keys = r.keys("order:*")
        
        if not order_keys:
            return JSONResponse(
                content={
                    "message": "No orders found",
                    "count": 0,
                    "orders": []
                },
                status_code=200
            )
        
        # Fetch data for each order key
        orders_data = []
        for key in order_keys:
            try:
                order_data = json.loads(r.get(key))
                # Add the Redis key as metadata
                order_data["redis_key"] = key
                orders_data.append(order_data)
            except (json.JSONDecodeError, TypeError) as e:
                # Handle cases where the data might not be valid JSON
                print(f"Error parsing data for key {key}: {e}")
                orders_data.append({
                    "redis_key": key,
                    "error": f"Invalid JSON data: {str(e)}",
                    "raw_data": r.get(key)
                })
        
        # Sort orders by timestamp if available (most recent first)
        try:
            orders_data.sort(
                key=lambda x: x.get("timestamp", x.get("created_at", 0)), 
                reverse=True
            )
        except:
            pass  # If sorting fails, return unsorted data
        
        return JSONResponse(
            content={
                "message": "Orders retrieved successfully",
                "count": len(orders_data),
                "orders": orders_data
            },
            status_code=200
        )
        
    except Exception as e:
        print(f"Error in /orders endpoint: {traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal Server Error",
                "message": str(e),
                "details": "Failed to fetch orders from Redis"
            }
        )

