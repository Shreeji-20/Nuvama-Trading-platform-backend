from APIConnect.APIConnect import APIConnect
import redis
import orjson
import traceback

class Login:
    def __init__(self) -> None:
        self.r = redis.Redis(host='localhost', port=6379, db=0)
        self.user_data = {}
        # pass
        
    def get_user_data(self,user_id:str):
       
        self.user_data = orjson.loads(self.r.get(f"user:{user_id}").decode())
        print("User data: ", self.user_data)
        
    def login(self,user_id,reqid=None):
        try:
            self.user_data = orjson.loads(self.r.get(f"user:{user_id}").decode())
            obj = APIConnect(
                        self.user_data['apikey'],
                        self.user_data['apisecret'],
                        str(reqid) if reqid else "",
                        True,
                        "",
                        True
                    )
            
            self.user_data['reqid'] = reqid
            self.r.set(f"{user_id}_reqid", f"{reqid}")
            self.r.expire(f"{user_id}_reqid", 43200)  # Set expiration to 12 hours 
            # obj.feedobj.timer.stop()
            print("Login successful for user_id: ", user_id)
            return True
        except Exception as e:
            print("Error during login: ", e)
            # print(traceback.format_exc())
            return e


# try:
#     obj = Login()
#     obj.get_user_data("70204607")
#     obj.login("70204607","613567c9d9527597")
# except Exception as e:
#     print(traceback.format_exc())


