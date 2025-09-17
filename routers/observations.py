"""
FastAPI router for Box Strategy Observation endpoints
"""

from fastapi import APIRouter, HTTPException
from typing import List, Optional
import redis
import json
from datetime import datetime

router = APIRouter(prefix="/observations", tags=["observations"])

# Redis connection
r = redis.Redis(host="localhost", port=6379, db=0)

@router.get("/execution-ids")
async def get_execution_ids():
    """Get all available execution IDs for observations."""
    try:
        execution_list_key = "box_observations:execution_ids"
        execution_ids = list(r.smembers(execution_list_key))
        execution_ids = [id.decode('utf-8') if isinstance(id, bytes) else id for id in execution_ids]
        execution_ids.sort(reverse=True)  # Most recent first
        return {"execution_ids": execution_ids}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch execution IDs: {str(e)}")

@router.get("/observation/{execution_id}")
async def get_observation(execution_id: str):
    """Get observation data for a specific execution ID."""
    try:
        redis_key = f"box_observation:{execution_id}"
        observation_data = r.get(redis_key)
        
        if not observation_data:
            raise HTTPException(status_code=404, detail=f"Observation not found for execution ID: {execution_id}")
        
        observation_json = json.loads(observation_data.decode('utf-8'))
        return observation_json
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse observation data")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch observation: {str(e)}")

@router.get("/latest/{symbol}")
async def get_latest_observation(symbol: str = "NIFTY"):
    """Get the latest observation for a specific symbol."""
    try:
        latest_key = f"box_observation:latest:{symbol.upper()}"
        observation_data = r.get(latest_key)
        
        if not observation_data:
            raise HTTPException(status_code=404, detail=f"No recent observation found for symbol: {symbol}")
        
        observation_json = json.loads(observation_data.decode('utf-8'))
        return observation_json
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse observation data")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch latest observation: {str(e)}")

@router.get("/all")
async def get_all_observations(limit: Optional[int] = 50):
    """Get all observations with optional limit."""
    try:
        execution_list_key = "box_observations:execution_ids"
        execution_ids = list(r.smembers(execution_list_key))
        execution_ids = [id.decode('utf-8') if isinstance(id, bytes) else id for id in execution_ids]
        execution_ids.sort(reverse=True)  # Most recent first
        
        # Apply limit
        if limit and limit > 0:
            execution_ids = execution_ids[:limit]
        
        observations = []
        for execution_id in execution_ids:
            try:
                redis_key = f"box_observation:{execution_id}"
                observation_data = r.get(redis_key)
                if observation_data:
                    observation_json = json.loads(observation_data.decode('utf-8'))
                    observations.append(observation_json)
            except Exception as e:
                print(f"Error processing observation {execution_id}: {e}")
                continue
        
        return {"observations": observations, "total": len(observations)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch observations: {str(e)}")

@router.delete("/observation/{execution_id}")
async def delete_observation(execution_id: str):
    """Delete a specific observation."""
    try:
        redis_key = f"box_observation:{execution_id}"
        result = r.delete(redis_key)
        
        if result == 0:
            raise HTTPException(status_code=404, detail=f"Observation not found for execution ID: {execution_id}")
        
        # Remove from execution list
        execution_list_key = "box_observations:execution_ids"
        r.srem(execution_list_key, execution_id)
        
        return {"message": f"Observation {execution_id} deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete observation: {str(e)}")

@router.get("/stats")
async def get_observation_stats():
    """Get statistics about observations."""
    try:
        execution_list_key = "box_observations:execution_ids"
        execution_ids = list(r.smembers(execution_list_key))
        
        total_observations = len(execution_ids)
        
        # Get case distribution
        case_a_count = 0
        case_b_count = 0
        error_count = 0
        
        for execution_id in execution_ids:
            try:
                redis_key = f"box_observation:{execution_id.decode('utf-8') if isinstance(execution_id, bytes) else execution_id}"
                observation_data = r.get(redis_key)
                if observation_data:
                    observation_json = json.loads(observation_data.decode('utf-8'))
                    case_decision = observation_json.get('case_decision', '')
                    if case_decision == 'CASE_A':
                        case_a_count += 1
                    elif case_decision == 'CASE_B':
                        case_b_count += 1
                    elif observation_json.get('status') == 'error':
                        error_count += 1
            except Exception:
                continue
        
        return {
            "total_observations": total_observations,
            "case_distribution": {
                "CASE_A": case_a_count,
                "CASE_B": case_b_count,
                "errors": error_count
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch stats: {str(e)}")