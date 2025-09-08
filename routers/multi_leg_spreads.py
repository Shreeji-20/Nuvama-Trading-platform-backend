from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi import APIRouter
import redis
import json
import uuid
from datetime import datetime
from typing import List, Optional
from routers.models_multileg_spreads import (
    MultiLegStrategy,
    StrategyCreateRequest,
    StrategyUpdateRequest,
    StrategyResponse,
    StrategiesListResponse,
    ErrorResponse
)


router = APIRouter()

# Redis connection
try:
    redis_client = redis.Redis(
        host='localhost',
        port=6379,
        db=0,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5
    )
    # Test connection
    redis_client.ping()
    print("✅ Redis connection established")
except Exception as e:
    print(f"❌ Redis connection failed: {e}")
    redis_client = None


def generate_strategy_id() -> str:
    """Generate unique strategy ID"""
    return str(uuid.uuid4())


def get_redis_key(strategy_id: str, strategy_name: str) -> str:
    """Generate Redis key in format: multileg_spreads:{id}_{name}"""
    # Clean strategy name for Redis key (remove special characters)
    clean_name = "".join(c if c.isalnum() or c in '-_' else '_' for c in strategy_name)
    return f"multileg_spreads:{strategy_id}_{clean_name}"


def get_all_strategy_keys() -> List[str]:
    """Get all strategy keys from Redis"""
    if not redis_client:
        return []
    
    try:
        return redis_client.keys("multileg_spreads:*")
    except Exception as e:
        print(f"Error getting strategy keys: {e}")
        return []


@router.get("/")
async def root():
    """Health check endpoint"""
    redis_status = "connected" if redis_client else "disconnected"
    return {
        "message": "Nuvama Multi-Leg Spreads API",
        "redis_status": redis_status,
        "timestamp": datetime.now().isoformat()
    }


@router.post("/multileg-spreads", response_model=StrategyResponse)
async def create_strategy(strategy: StrategyCreateRequest):
    """Create a new multi-leg strategy"""
    if not redis_client:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis connection not available"
        )
    
    try:
        # Generate unique ID
        strategy_id = generate_strategy_id()
        
        # Create strategy object with timestamps
        now = datetime.now()
        strategy_data = MultiLegStrategy(
            id=strategy_id,
            name=strategy.name,
            legs=strategy.legs,
            totalSpread=strategy.totalSpread,
            created_at=now,
            updated_at=now
        )
        
        # Generate Redis key
        redis_key = get_redis_key(strategy_id, strategy.name)
        
        # Store in Redis
        redis_client.set(redis_key, strategy_data.json())
        
        print(f"✅ Strategy created: {redis_key}")
        
        return StrategyResponse(**strategy_data.dict())
        
    except Exception as e:
        print(f"❌ Error creating strategy: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create strategy: {str(e)}"
        )


@router.get("/multileg-spreads", response_model=StrategiesListResponse)
async def get_all_strategies():
    """Get all multi-leg strategies"""
    if not redis_client:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis connection not available"
        )
    
    try:
        # Get all strategy keys
        strategy_keys = get_all_strategy_keys()
        strategies = []
        
        for key in strategy_keys:
            try:
                # Get strategy data from Redis
                strategy_json = redis_client.get(key)
                if strategy_json:
                    strategy_data = json.loads(strategy_json)
                    strategies.append(StrategyResponse(**strategy_data))
            except Exception as e:
                print(f"❌ Error loading strategy from key {key}: {e}")
                continue
        
        # Sort by created_at (newest first)
        strategies.sort(key=lambda x: x.created_at, reverse=True)
        
        print(f"✅ Retrieved {len(strategies)} strategies")
        
        return StrategiesListResponse(
            strategies=strategies,
            count=len(strategies)
        )
        
    except Exception as e:
        print(f"❌ Error getting strategies: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve strategies: {str(e)}"
        )


@router.get("/multileg-spreads/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(strategy_id: str):
    """Get a specific strategy by ID"""
    if not redis_client:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis connection not available"
        )
    
    try:
        # Find the strategy key by ID (since we need the name for the full key)
        strategy_keys = get_all_strategy_keys()
        target_key = None
        
        for key in strategy_keys:
            if key.startswith(f"multileg_spreads:{strategy_id}_"):
                target_key = key
                break
        
        if not target_key:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Strategy with ID {strategy_id} not found"
            )
        
        # Get strategy data from Redis
        strategy_json = redis_client.get(target_key)
        if not strategy_json:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Strategy with ID {strategy_id} not found"
            )
        
        strategy_data = json.loads(strategy_json)
        print(f"✅ Retrieved strategy: {target_key}")
        
        return StrategyResponse(**strategy_data)
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error getting strategy {strategy_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve strategy: {str(e)}"
        )


@router.put("/multileg-spreads/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(strategy_id: str, strategy_update: StrategyUpdateRequest):
    """Update an existing strategy"""
    if not redis_client:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis connection not available"
        )
    
    try:
        # Find the existing strategy key
        strategy_keys = get_all_strategy_keys()
        old_key = None
        
        for key in strategy_keys:
            if key.startswith(f"multileg_spreads:{strategy_id}_"):
                old_key = key
                break
        
        if not old_key:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Strategy with ID {strategy_id} not found"
            )
        
        # Get existing strategy data
        existing_json = redis_client.get(old_key)
        if not existing_json:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Strategy with ID {strategy_id} not found"
            )
        
        existing_data = json.loads(existing_json)
        existing_strategy = MultiLegStrategy(**existing_data)
        
        # Update fields if provided
        updated_data = existing_strategy.dict()
        if strategy_update.name is not None:
            updated_data['name'] = strategy_update.name
        if strategy_update.legs is not None:
            updated_data['legs'] = [leg.dict() for leg in strategy_update.legs]
        if strategy_update.totalSpread is not None:
            updated_data['totalSpread'] = strategy_update.totalSpread
        
        # Update timestamp
        updated_data['updated_at'] = datetime.now()
        
        # Create updated strategy
        updated_strategy = MultiLegStrategy(**updated_data)
        
        # Generate new Redis key (in case name changed)
        new_key = get_redis_key(strategy_id, updated_strategy.name)
        
        # If key changed, delete old key and set new one
        if old_key != new_key:
            redis_client.delete(old_key)
            print(f"🗑️ Deleted old key: {old_key}")
        
        # Set updated strategy
        redis_client.set(new_key, updated_strategy.json())
        print(f"✅ Strategy updated: {new_key}")
        
        return StrategyResponse(**updated_strategy.dict())
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error updating strategy {strategy_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update strategy: {str(e)}"
        )


@router.delete("/multileg-spreads/{strategy_id}")
async def delete_strategy(strategy_id: str):
    """Delete a strategy"""
    if not redis_client:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis connection not available"
        )
    
    try:
        # Find the strategy key by ID
        strategy_keys = get_all_strategy_keys()
        target_key = None
        
        for key in strategy_keys:
            if key.startswith(f"multileg_spreads:{strategy_id}_"):
                target_key = key
                break
        
        if not target_key:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Strategy with ID {strategy_id} not found"
            )
        
        # Delete from Redis
        result = redis_client.delete(target_key)
        
        if result == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Strategy with ID {strategy_id} not found"
            )
        
        print(f"✅ Strategy deleted: {target_key}")
        
        return {"message": f"Strategy {strategy_id} deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error deleting strategy {strategy_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete strategy: {str(e)}"
        )


# Health check for Redis
@router.get("/health/redis")
async def redis_health():
    """Check Redis connection health"""
    if not redis_client:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "redis": "disconnected"}
        )
    
    try:
        redis_client.ping()
        strategy_count = len(get_all_strategy_keys())
        return {
            "status": "healthy",
            "redis": "connected",
            "strategies_count": strategy_count
        }
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unhealthy",
                "redis": "error",
                "error": str(e)
            }
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
