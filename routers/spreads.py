from fastapi import APIRouter
from pydantic import BaseModel
import redis
import json
from typing import Optional
from fastapi.responses import JSONResponse

router = APIRouter()

r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)


class Spread(BaseModel):
    index: str
    leg1Strike: str
    leg1Expiry: str
    leg1OptionType: str
    leg1OrderType: str

    leg2Strike: str
    leg2Expiry: str
    leg2OptionType: str
    leg2OrderType: str
    id:Optional[int]=None
    spread: None
    
    
@router.post("/spreads")
def add_spread(spread:Spread):
    try:
        new_id = r.incr("spread_counter")
        spread.id = int(new_id)
        r.set(f"spreads:{new_id}",spread.json())
        return {"message":"Spread added successfully","id":new_id}
    except Exception as e:
        return JSONResponse(content={"message": str(e)}, status_code=500)
    
@router.get("/spreads")
def get_spreads():
    try:
        spreads = []
        for key in r.scan_iter("spreads:*"):
            spreads.append(json.loads(r.get(key)))
        return spreads
    except Exception as e:
        return JSONResponse(content={"message": str(e)}, status_code=500)

@router.delete("/spreads/{spread_id}")
def delete_spread(spread_id: int):
    try:
        r.delete(f"spreads:{spread_id}")
        return {"message": "Spread deleted successfully"}
    except Exception as e:
        return JSONResponse(content={"message": str(e)}, status_code=500)