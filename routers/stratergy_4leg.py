from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from fastapi import APIRouter
from typing import List, Optional, Dict, Any
import redis
import json
import uuid
from datetime import datetime
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
router = APIRouter()

# Redis connection
try:
    redis_client = redis.Redis(
        host='localhost',
        port=6379,
        db=0,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
        retry_on_timeout=True
    )
    # Test connection
    redis_client.ping()
    logger.info("Successfully connected to Redis")
except redis.ConnectionError as e:
    logger.error(f"Failed to connect to Redis: {e}")
    redis_client = None

# Pydantic models for request validation
class LegModel(BaseModel):
    symbol: str = Field(..., description="Symbol name (e.g., NIFTY)")
    strike: float = Field(..., description="Strike price")
    type: str = Field(..., description="Option type (CE/PE)")
    expiry: int = Field(..., description="Expiry cycle (0=current week, 1=next week, 2=following week, etc.)")
    action: str = Field(default="BUY", description="Individual leg action (BUY/SELL)")
    quantity: int = Field(..., description="Individual leg quantity")

class BiddingLegModel(LegModel):
    """Bidding leg model that inherits from LegModel and includes quantity"""
    pass

class AdvancedOptionsStrategy(BaseModel):
    # Dynamic legs - can have leg1, leg2, leg3, leg4, etc. with individual quantities
    bidding_leg: BiddingLegModel
    base_legs: List[str] = Field(..., description="List of base leg keys")
    bidding_leg_key: str = Field(default="bidding_leg", description="Key for bidding leg")
    desired_spread: float = Field(..., description="Desired spread value")
    exit_desired_spread: float = Field(..., description="Exit desired spread value")
    start_price: float = Field(..., description="Start price")
    exit_start: float = Field(..., description="Exit start price")
    action: str = Field(..., description="Global BUY or SELL (used as fallback)")
    slice_multiplier: int = Field(..., description="Multiplier for calculating total slices")
    user_ids: List[str] = Field(..., description="List of user IDs")
    run_state: int = Field(default=0, description="Run state (0=Running, 1=Paused, 2=Stopped, 3=Not Started)")
    order_type: str = Field(default="LIMIT", description="Order type")
    IOC_timeout: float = Field(default=30.0, description="IOC timeout in seconds")
    exit_price_gap: float = Field(default=2.0, description="Exit price gap")
    no_of_bidask_average: int = Field(default=1, description="Number of bid/ask average")
    notes: Optional[str] = Field(default="", description="Strategy notes or comments")
    
    # Allow extra fields for dynamic legs (leg1, leg2, leg3, etc.) each with their own action and quantity
    class Config:
        extra = "allow"

class StrategyResponse(BaseModel):
    strategy_id: str
    message: str
    timestamp: str
    redis_key: str



# Get all 4-leg strategies
@router.get("/advanced-options")
async def get_all_strategies():
    """Get all advanced options strategies from Redis (supports dynamic legs)"""
    if not redis_client:
        raise HTTPException(status_code=500, detail="Redis connection not available")
    
    try:
        # Get all keys matching the pattern
        keys = redis_client.keys("4_leg:*")
        strategies = []
        
        for key in keys:
            strategy_data = redis_client.get(key)
            if strategy_data:
                strategy = json.loads(strategy_data)
                # Add strategy_id from the key for identification purposes in response
                strategy_id = key.replace("4_leg:", "")
                
                # Count legs dynamically
                legs_count = len([k for k in strategy.keys() if k.startswith('leg') and isinstance(strategy[k], dict)])
                
                strategy_with_id = {
                    "strategy_id": strategy_id,
                    "redis_key": key,
                    "legs_count": legs_count,  # Add legs count for info
                    **strategy  # Original data in exact format
                }
                strategies.append(strategy_with_id)
        
        return {
            "strategies": strategies,
            "count": len(strategies),
            "timestamp": datetime.now().isoformat()
        }
    
    except Exception as e:
        logger.error(f"Error retrieving strategies: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving strategies: {str(e)}")

# Get specific strategy by ID
@router.get("/advanced-options/{strategy_id}")
async def get_strategy(strategy_id: str):
    """Get a specific advanced options strategy by ID (supports dynamic legs)"""
    if not redis_client:
        raise HTTPException(status_code=500, detail="Redis connection not available")
    
    try:
        redis_key = f"4_leg:{strategy_id}"
        strategy_data = redis_client.get(redis_key)
        
        if not strategy_data:
            raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")
        
        strategy = json.loads(strategy_data)
        
        # Count legs dynamically
        legs_count = len([k for k in strategy.keys() if k.startswith('leg') and isinstance(strategy[k], dict)])
        legs_list = [k for k in strategy.keys() if k.startswith('leg') and isinstance(strategy[k], dict)]
        
        # Add strategy_id and redis_key for identification, but keep original data structure
        strategy_with_meta = {
            "strategy_id": strategy_id,
            "redis_key": redis_key,
            "legs_count": legs_count,
            "legs_list": legs_list,
            **strategy  # Original data in exact format
        }
        
        return strategy_with_meta
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving strategy {strategy_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving strategy: {str(e)}")

# Create new 4-leg strategy
@router.post("/advanced-options", response_model=StrategyResponse)
async def create_strategy(strategy: AdvancedOptionsStrategy):
    """Create a new advanced options strategy with dynamic legs"""
    if not redis_client:
        raise HTTPException(status_code=500, detail="Redis connection not available")
    
    try:
        # Generate unique strategy ID
        strategy_id = str(uuid.uuid4())
        redis_key = f"4_leg:{strategy_id}"
        
        # Convert to dict to handle dynamic legs
        strategy_dict = strategy.dict()
        
        # Extract leg data (dynamic legs like leg1, leg2, leg3, etc.)
        legs_data = {}
        non_leg_fields = {
            'bidding_leg', 'base_legs', 'bidding_leg_key', 'desired_spread', 
            'exit_desired_spread', 'start_price', 'exit_start', 'action', 
            'slice_multiplier', 'user_ids', 'run_state', 'order_type', 
            'IOC_timeout', 'exit_price_gap', 'no_of_bidask_average', 'notes'
        }
        
        for key, value in strategy_dict.items():
            if key not in non_leg_fields and key.startswith('leg'):
                legs_data[key] = value
        
        # Validate that we have at least one leg
        if not legs_data:
            raise HTTPException(status_code=400, detail="At least one leg is required")
        
        # Validate bidding_leg data
        if not strategy_dict.get("bidding_leg"):
            raise HTTPException(status_code=400, detail="Missing or invalid bidding_leg data")
        
        # Validate base_legs references
        valid_leg_keys = list(legs_data.keys())
        for base_leg in strategy_dict["base_legs"]:
            if base_leg not in valid_leg_keys:
                raise HTTPException(status_code=400, detail=f"Invalid base_leg reference: {base_leg}. Available legs: {valid_leg_keys}")
        
        # Prepare data for storage (exact format as frontend sends)
        strategy_data = strategy_dict
        
        # Store in Redis in exact format requested (no additional fields)
        redis_client.setex(
            redis_key,
            3600 * 24 * 7,  # 7 days expiration
            json.dumps(strategy_data, default=str)
        )
        
        logger.info(f"Strategy {strategy_id} created and stored in Redis with key {redis_key}")
        logger.info(f"Strategy has {len(legs_data)} legs: {list(legs_data.keys())}")
        
        return StrategyResponse(
            strategy_id=strategy_id,
            message="Strategy created successfully",
            timestamp=datetime.now().isoformat(),
            redis_key=redis_key
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating strategy: {e}")
        raise HTTPException(status_code=500, detail=f"Error creating strategy: {str(e)}")

# Update existing strategy
@router.put("/advanced-options/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(strategy_id: str, strategy: AdvancedOptionsStrategy):
    """Update an existing advanced options strategy"""
    if not redis_client:
        raise HTTPException(status_code=500, detail="Redis connection not available")
    
    try:
        redis_key = f"4_leg:{strategy_id}"
        
        # Check if strategy exists
        existing_data = redis_client.get(redis_key)
        if not existing_data:
            raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")
        
        # Convert to dict to handle dynamic legs
        strategy_dict = strategy.dict()
        
        # Extract leg data (dynamic legs like leg1, leg2, leg3, etc.)
        legs_data = {}
        non_leg_fields = {
            'bidding_leg', 'base_legs', 'bidding_leg_key', 'desired_spread', 
            'exit_desired_spread', 'start_price', 'exit_start', 'action', 
            'slice_multiplier', 'user_ids', 'run_state', 'order_type', 
            'IOC_timeout', 'exit_price_gap', 'no_of_bidask_average', 'notes'
        }
        
        for key, value in strategy_dict.items():
            if key not in non_leg_fields and key.startswith('leg'):
                legs_data[key] = value
        
        # Validate that we have at least one leg
        if not legs_data:
            raise HTTPException(status_code=400, detail="At least one leg is required")
        
        # Validate base_legs references
        valid_leg_keys = list(legs_data.keys())
        for base_leg in strategy_dict["base_legs"]:
            if base_leg not in valid_leg_keys:
                raise HTTPException(status_code=400, detail=f"Invalid base_leg reference: {base_leg}. Available legs: {valid_leg_keys}")
        
        # Prepare updated data (exact format - no additional metadata)
        strategy_data = strategy_dict
        
        # Store updated data in Redis
        redis_client.setex(
            redis_key,
            3600 * 24 * 7,  # 7 days expiration
            json.dumps(strategy_data, default=str)
        )
        
        logger.info(f"Strategy {strategy_id} updated in Redis with {len(legs_data)} legs")
        
        return StrategyResponse(
            strategy_id=strategy_id,
            message="Strategy updated successfully",
            timestamp=datetime.now().isoformat(),
            redis_key=redis_key
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating strategy {strategy_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error updating strategy: {str(e)}")

# Delete strategy
@router.delete("/advanced-options/{strategy_id}")
async def delete_strategy(strategy_id: str):
    """Delete a 4-leg options strategy"""
    if not redis_client:
        raise HTTPException(status_code=500, detail="Redis connection not available")
    
    try:
        redis_key = f"4_leg:{strategy_id}"
        
        # Check if strategy exists
        if not redis_client.exists(redis_key):
            raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")
        
        # Delete from Redis
        deleted_count = redis_client.delete(redis_key)
        
        if deleted_count > 0:
            logger.info(f"Strategy {strategy_id} deleted from Redis")
            return {
                "message": "Strategy deleted successfully",
                "strategy_id": strategy_id,
                "timestamp": datetime.now().isoformat()
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to delete strategy")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting strategy {strategy_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error deleting strategy: {str(e)}")

# Update strategy run state
@router.patch("/advanced-options/{strategy_id}/run-state")
async def update_run_state(strategy_id: str, run_state: int):
    """Update the run state of a strategy"""
    if not redis_client:
        raise HTTPException(status_code=500, detail="Redis connection not available")
    
    if run_state not in [0, 1, 2, 3]:
        raise HTTPException(status_code=400, detail="Invalid run_state. Must be 0, 1, 2, or 3")
    
    try:
        redis_key = f"4_leg:{strategy_id}"
        
        # Get existing strategy
        strategy_data = redis_client.get(redis_key)
        if not strategy_data:
            raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")
        
        # Update run state (keep exact format, just update the run_state field)
        strategy = json.loads(strategy_data)
        strategy["run_state"] = run_state
        
        # Save back to Redis (exact format - no additional timestamp)
        redis_client.setex(
            redis_key,
            3600 * 24 * 7,
            json.dumps(strategy, default=str)
        )
        
        run_state_labels = {0: "Running", 1: "Paused", 2: "Stopped", 3: "Not Started"}
        
        return {
            "message": f"Strategy run state updated to {run_state_labels[run_state]}",
            "strategy_id": strategy_id,
            "run_state": run_state,
            "timestamp": datetime.now().isoformat()
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating run state for strategy {strategy_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error updating run state: {str(e)}")

# Validate strategy data (useful for debugging)
@router.post("/advanced-options/validate")
async def validate_strategy_data(data: dict):
    """Validate strategy data format for debugging"""
    try:
        # Extract leg data
        legs_data = {}
        non_leg_fields = {
            'bidding_leg', 'base_legs', 'bidding_leg_key', 'desired_spread', 
            'exit_desired_spread', 'start_price', 'exit_start', 'action', 
            'slice_multiplier', 'user_ids', 'run_state', 'order_type', 
            'IOC_timeout', 'exit_price_gap', 'no_of_bidask_average', 'notes'
        }
        
        for key, value in data.items():
            if key not in non_leg_fields and key.startswith('leg'):
                legs_data[key] = value
        
        # Validation results
        validation_results = {
            "valid": True,
            "errors": [],
            "legs_found": list(legs_data.keys()),
            "legs_count": len(legs_data),
            "base_legs": data.get("base_legs", []),
            "has_bidding_leg": "bidding_leg" in data and data["bidding_leg"] is not None,
            "notes_included": "notes" in data
        }
        
        # Check for at least one leg
        if not legs_data:
            validation_results["valid"] = False
            validation_results["errors"].append("At least one leg is required")
        
        # Check bidding leg
        if not data.get("bidding_leg"):
            validation_results["valid"] = False
            validation_results["errors"].append("Missing or invalid bidding_leg data")
        
        # Check base_legs references
        valid_leg_keys = list(legs_data.keys())
        for base_leg in data.get("base_legs", []):
            if base_leg not in valid_leg_keys:
                validation_results["valid"] = False
                validation_results["errors"].append(f"Invalid base_leg reference: {base_leg}. Available legs: {valid_leg_keys}")
        
        # Check required fields
        required_fields = ["desired_spread", "exit_desired_spread", "start_price", "exit_start", "action", "slice_multiplier", "user_ids"]
        for field in required_fields:
            if field not in data:
                validation_results["valid"] = False
                validation_results["errors"].append(f"Missing required field: {field}")
        
        # Validate that each leg has a quantity field
        for leg_key, leg_data in legs_data.items():
            if 'quantity' not in leg_data:
                validation_results["valid"] = False
                validation_results["errors"].append(f"Missing quantity field in {leg_key}")
        
        return validation_results
    
    except Exception as e:
        return {
            "valid": False,
            "errors": [f"Validation error: {str(e)}"],
            "legs_found": [],
            "legs_count": 0,
            "base_legs": [],
            "has_bidding_leg": False,
            "notes_included": False
        }

