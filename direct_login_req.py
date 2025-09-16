import requests
import redis
import json

r = redis.Redis(host='localhost', port=6379, db=0)

data = json.loads(r.get('user:70249886'))

response = requests.post(
    'http://100.98.41.25:8000/userlogin',json=data)


