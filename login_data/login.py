from APIConnect.APIConnect import APIConnect
import redis
import orjson
import traceback
from login_data import simple_login
class Login:
    def __init__(self) -> None:
        self.r = redis.Redis(host='localhost', port=6379, db=0)
        self.user_data = {}
        # pass
        
    def get_user_data(self,user_id:str):
       
        self.user_data = orjson.loads(self.r.get(f"user:{user_id}").decode())
        print("User data: ", self.user_data)
        
    def login(self,user_id):
        try:
            print(f"🔍 Fetching user data for: {user_id}")
            self.user_data = orjson.loads(self.r.get(f"user:{user_id}").decode())
            print(f"✅ User data retrieved for: {user_id}")
            
            print(f"🌐 Initializing web login for: {user_id}")
            web_login = simple_login.SimpleNuvamaLogin(
                api_key=self.user_data['apikey'],
                headless=True,
                totp_secret=self.user_data['totp_secret']
            )
            
            print(f"🔐 Starting Nuvama web login for: {user_id}")
            reqid = web_login.login(
                username=self.user_data['userid'],
                password=self.user_data['password']
            )
            
            if not reqid:
                raise Exception("Web login failed - no request ID returned")
            
            print(f"✅ Web login successful, reqid: {reqid}")
            
            print(f"🔧 Initializing API connection for: {user_id}")
            obj = APIConnect(
                        self.user_data['apikey'],
                        self.user_data['apisecret'],
                        str(reqid) if reqid else "",
                        True,
                        "",
                        True
                    )
            self.r.set(f"reqid:{user_id}", f"{reqid}")
            print(f"💾 Storing reqid in Redis for: {user_id}")
            self.r.set(f"{user_id}_reqid", f"{reqid}")
            self.r.expire(f"{user_id}_reqid", 43200)  # Set expiration to 12 hours 
            
            print(f"🎉 Login successful for user_id: {user_id}")
            return True
        except Exception as e:
            error_msg = f"Error during login for {user_id}: {str(e)}"
            print(f"❌ {error_msg}")
            return error_msg


# try:
#     obj = Login()
#     obj.get_user_data("70204607")
#     obj.login("70204607","613567c9d9527597")
# except Exception as e:
#     print(traceback.format_exc())


