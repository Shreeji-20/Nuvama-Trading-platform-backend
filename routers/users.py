# backend/routers/users.py
from fastapi import APIRouter
from pydantic import BaseModel
import redis
import json
from typing import Optional
from fastapi.responses import JSONResponse
from login_data import login
import os
import sys
from nuvama.websocket import order_streaming_socket
import threading

# Path to the directory containing the module
current_dir = os.path.dirname(__file__)
# Go up one level to reach "child"
parent_dir = os.path.abspath(os.path.join(current_dir, ".."))
# Add to sys.path
sys.path.append(parent_dir)

from login_data import login


r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
login_obj = login.Login()

router = APIRouter()


class User(BaseModel):
    userid: str
    apikey: str
    apisecret:str
    reqid:Optional[str]=None
    lastLogin:Optional[str]=None
    
    
@router.post("/user")
def add_user(user:User):
    try:
        print("Req Received : ",user)
        r.set(f"user:{user.userid}",user.json())
        r.set(f"reqid:{user.userid}",user.reqid)
        r.expire(f"reqid:{user.userid}",43200)
        return {"message": "Data stored successfully", "id": user.userid}
    except Exception as e:
        print(e)
        return {"error":e}

@router.get("/users")
def get_all_users():
    try:
        redis_keys = r.keys("user:*")
        data = [json.loads(r.get(key)) for key in redis_keys]
        return JSONResponse(content=data,status_code=200)
    except Exception as e:
        return {"error":e}

@router.post("/deleteuser")
def delete_user(user:User):
    try:
        if r.exists(f"user:{user.userid}"):
            r.delete(f"user:{user.userid}")
        else:
            raise Exception("User Doesnt Exist !")
        
    except Exception as e:
        return JSONResponse(
        status_code=400,
        content={"error": "Bad Request", "message": e}
    )
@router.post("/userlogin")
def userlogin(user: User):
    try:
        islogin = login_obj.login(user_id=user.userid,reqid=user.reqid)
        
        if islogin!=True:
            raise Exception(islogin)
        else:
            # Launch a new cmd window that runs the order streaming runner for this user
            try:
                script_path = os.path.abspath(os.path.join(parent_dir, "nuvama", "websocket", "run_order_streaming.py"))
                activate = os.path.abspath(os.path.join(parent_dir, "venv", "Scripts", "activate.bat"))
                # Use start to open new cmd window, activate venv, then run using python3 and keep window open with /k
                # The call command is required to run a batch file and return to the current cmd instance
                cmd = f'cmd.exe /c start cmd /k "call \"{activate}\" && python3 \"{script_path}\" {user.userid}"'
                os.system(cmd)
            except Exception as e:
                print("Failed to launch order streaming cmd:", e)
            return JSONResponse(
        content={"message": "Login successful!", "id": user.userid},
        status_code=200
    )
    except Exception as e:
        return JSONResponse(
        status_code=400,
        content={"error": "Bad Request", "message": str(e)}
    )
