#!/bin/bash

echo "🚀 Starting application setup..."
# 1. Activate Python virtual environment
echo "📦 Activating virtual environment..."
echo "$(pwd)"

source ~/../workspace/venv/bin/activate

# 2. Start Redis server with config
echo "🧠 Starting Redis server with config..."
redis-server ~/../workspace/redis.conf 

# Give Redis a few seconds to start (optional but useful)
sleep 2

# 3. Navigate to backend project
echo "📁 Changing to backend directory..."
cd ~/../workspace/Nuvama-Trading-platform-backend/
echo "$(pwd)"
# 4. Start Uvicorn server in background with nohup
echo "🌐 Starting Uvicorn server..."
nohup uvicorn main:app --reload --host 0.0.0.0 --port 8000 > uvicorn.log 2>&1 < /dev/null &

# 5. Disown the background job
disown %1

python direct_login_req.py
# 6. Move back to previous directory
cd ..

echo "Uvicorn Server Started Successfully"

cd Nuvama-Trading-platform

nohup npm run dev -- --host --port 5173 > react.log 2>&1 < /dev/null &

disown %1

echo "React started Successfully"

echo "✅ All processes started."
