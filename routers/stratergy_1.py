from fastapi import APIRouter
from pydantic import BaseModel
import redis
import json
import os
import importlib.util, sys, pathlib, traceback
from typing import Optional
from fastapi.responses import JSONResponse
import threading
router = APIRouter()

r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

current_dir = os.path.dirname(__file__)
parent_dir = os.path.abspath(os.path.join(current_dir, ".."))
sys.path.append(parent_dir)
from nuvama import stratergies

class Stratergy1(BaseModel):
    symbol: str
    order_type: str
    quantity: int
    slices: int
    base_leg: str
    IOC_timeout: float
    call_strike: int
    put_strike: int
    desired_spread: float
    start_price: float
    user_ids: list
    expiry: int
    no_of_bid_ask_average: int
    action: str
    exit_start: float
    exit_desired_spread: float
    id: Optional[int]=None
    note: Optional[str]=None
    run_state: Optional[int]=None

@router.post("/stratergy/stratergy_1/add")
def store_data(item: Stratergy1):
    print("Req rec : ",item)
    
    new_id = r.incr("stratergy:id")
    item.id = new_id

    r.set(f"stratergies:stratergy_1_{new_id}", item.json())

    obj = stratergies.Stratergy1(new_id)
    t = threading.Thread(target=obj.main_logic, daemon=True)
    t.start()
    return {"message": "Data stored successfully", "id": new_id}

@router.put("/stratergy/stratergy_1/update")
def update_data(item:Stratergy1):
    print("Req rec update : ",item)
    r.set(f"stratergies:stratergy_1_{item.id}",item.json())
    return {"message": "Data updated successfully", "id": item.id}


@router.get("/stratergy/{sid}")
def get_all_stratergies(sid):
    redis_keys = r.keys(f"stratergies:{sid}_*")
    data = [json.loads(r.get(key)) for key in redis_keys]
    return JSONResponse(content=data, status_code=200)

@router.delete("/stratergy/{sid}/{paramsid}")
def delete_strategy(sid: str, paramsid: str):
    redis_keys = r.keys(f"stratergies:{sid}_{paramsid}*")
    for key in redis_keys:
        r.delete(key)
    return {"message": "Strategies deleted successfully"}