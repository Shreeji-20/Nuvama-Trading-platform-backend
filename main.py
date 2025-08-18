from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import redis
import json
from fastapi.responses import JSONResponse
import pandas as pd
import time
import traceback
from routers import users , spreads, stratergy_1

# Connect to Redis
r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

app = FastAPI()
app.include_router(users.router)
app.include_router(spreads.router)
app.include_router(stratergy_1.router)


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

