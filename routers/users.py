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
import subprocess


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
    totp_secret:Optional[str]=None
    lastLogin:Optional[str]=None
    password:Optional[str]=None


@router.post("/user")
def add_user(user:User):
    try:
        print("Req Received : ",user)
        r.set(f"user:{user.userid}",user.json())
        # Note: No longer setting reqid as it's now password/totp_secret based
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
        # Step 1: Start login process
        print(f"🚀 Starting login process for user: {user.userid}")
        
        # Step 2: Attempt web login
        print(f"🔐 Attempting web login for user: {user.userid}")
        islogin = login_obj.login(user_id=user.userid)
        
        if islogin != True:
            print(f"❌ Web login failed for user {user.userid}: {islogin}")
            raise Exception(f"Web login failed: {islogin}")
        
        print(f"✅ Web login successful for user: {user.userid}")
        
        # Step 3: Launch order streaming
        print(f"🔄 Launching order streaming for user: {user.userid}")
        try:
            subprocess.run(["start", "cmd", "/k", "python3 C:\\Users\\shree\\OneDrive\\Desktop\\TrueData\\nuvama\\websocket\\order_streaming.py", user.userid], shell=True)
            print(f"✅ Order streaming launched for user: {user.userid}")
            
            if user.userid == "70249886":
                subprocess.run(["start", "cmd", "/k", "python3 C:\\Users\\shree\\OneDrive\\Desktop\\TrueData\\nuvama\\websocket\\central_socket_data.py"], shell=True)
                print(f"✅ Central socket data launched for user: {user.userid}")
        except Exception as e:
            print(f"⚠️ Failed to launch order streaming for user {user.userid}: {e}")
            # Don't fail the entire login for this
        
        print(f"🎉 Login process completed successfully for user: {user.userid}")
        return JSONResponse(
            content={
                "message": "Login successful!", 
                "id": user.userid,
                "status": "success",
                "steps": [
                    {"step": "web_login", "status": "completed", "message": "Web login successful"},
                    {"step": "order_streaming", "status": "completed", "message": "Order streaming launched"},
                    {"step": "login_complete", "status": "completed", "message": "Login process completed"}
                ]
            },
            status_code=200
        )
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Login failed for user {user.userid}: {error_msg}")
        return JSONResponse(
            status_code=400,
            content={
                "error": "Login Failed", 
                "message": error_msg,
                "status": "error",
                "user_id": user.userid
            }
        )
