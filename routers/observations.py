"""
FastAPI router for Box Strategy Observation endpoints using Instance IDs
"""

from fastapi import APIRouter, HTTPException
from typing import Optional
import redis
import json
from datetime import datetime

router = APIRouter(prefix="/observations", tags=["observations"])

# Redis connection
r = redis.Redis(host="localhost", port=6379, db=0)

@router.get("/instances")
async def get_strategy_instances():
    """Get all available strategy instances."""
    try:
        # Get instances from active instances list
        active_instances_key = "strategy_instances:active"
        instance_ids = list(r.smembers(active_instances_key))
        instance_ids = [id.decode('utf-8') if isinstance(id, bytes) else id for id in instance_ids]
        
        # Also get instances with observations
        with_observations_key = "strategy_instances:with_observations"
        obs_instance_ids = list(r.smembers(with_observations_key))
        obs_instance_ids = [id.decode('utf-8') if isinstance(id, bytes) else id for id in obs_instance_ids]
        
        # Combine and deduplicate
        all_instance_ids = list(set(instance_ids + obs_instance_ids))
        all_instance_ids.sort()
        
        # Get metadata for each instance
        instances_data = []
        for instance_id in all_instance_ids:
            # Use the correct metadata key from the strategy
            metadata_key = f"strategy_instance:{instance_id}"
            metadata = r.get(metadata_key)
            if metadata:
                try:
                    metadata_json = json.loads(metadata.decode('utf-8'))
                    instances_data.append({
                        "instance_id": instance_id,
                        **metadata_json
                    })
                except json.JSONDecodeError:
                    instances_data.append({"instance_id": instance_id, "status": "unknown"})
            else:
                instances_data.append({"instance_id": instance_id, "status": "no_metadata"})
        
        return {"instances": instances_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch strategy instances: {str(e)}")

@router.get("/instances/{instance_id}/execution-ids")
async def get_execution_ids_by_instance(instance_id: str):
    """Get all available execution IDs for observations for a specific instance."""
    try:
        execution_list_key = f"box_observations:{instance_id}:execution_ids"
        execution_ids = list(r.smembers(execution_list_key))
        execution_ids = [id.decode('utf-8') if isinstance(id, bytes) else id for id in execution_ids]
        execution_ids.sort(reverse=True)  # Most recent first
        return {"execution_ids": execution_ids, "instance_id": instance_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch execution IDs for instance {instance_id}: {str(e)}")

@router.get("/instances/{instance_id}/observation/{execution_id}")
async def get_observation_by_instance(instance_id: str, execution_id: str):
    """Get observation data for a specific execution ID and instance."""
    try:
        redis_key = f"box_observation:{instance_id}:{execution_id}"
        observation_data = r.get(redis_key)
        
        if not observation_data:
            raise HTTPException(status_code=404, detail=f"Observation not found for instance {instance_id}, execution ID: {execution_id}")
        
        observation_json = json.loads(observation_data.decode('utf-8'))
        return observation_json
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse observation data")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch observation: {str(e)}")

@router.get("/instances/{instance_id}/latest/{symbol}")
async def get_latest_observation_by_instance(instance_id: str, symbol: str = "NIFTY"):
    """Get the latest observation for a specific symbol and instance."""
    try:
        latest_key = f"box_observation:{instance_id}:latest:{symbol.upper()}"
        observation_data = r.get(latest_key)
        
        if not observation_data:
            raise HTTPException(status_code=404, detail=f"No recent observation found for instance {instance_id}, symbol: {symbol}")
        
        observation_json = json.loads(observation_data.decode('utf-8'))
        return observation_json
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse observation data")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch latest observation: {str(e)}")

@router.get("/instances/{instance_id}/live-metrics")
async def get_live_metrics_by_instance(instance_id: str):
    """Get live trading metrics for a specific instance."""
    try:
        # Get live metrics from Redis for specific instance
        metrics_key = f"live_metrics:{instance_id}:latest"
        metrics_data = r.get(metrics_key)
        
        if metrics_data:
            try:
                metrics_json = json.loads(metrics_data.decode('utf-8'))
                return metrics_json
            except json.JSONDecodeError:
                pass
        
        # Try alternative keys for different metric types
        entry_qtys_key = f"entry_quantities:{instance_id}:latest"
        exit_qtys_key = f"exit_quantities:{instance_id}:latest"
        spread_key = f"executed_spread:{instance_id}:latest"
        pnl_key = f"pnl:{instance_id}:latest"
        
        entry_qtys_data = r.get(entry_qtys_key)
        exit_qtys_data = r.get(exit_qtys_key)
        spread_data = r.get(spread_key)
        pnl_data = r.get(pnl_key)
        
        # Parse individual metrics
        entry_quantities = 0
        exit_quantities = 0
        executed_spread = 0
        pnl = 0
        
        try:
            if entry_qtys_data:
                entry_quantities = json.loads(entry_qtys_data.decode('utf-8'))
        except (json.JSONDecodeError, TypeError):
            pass
            
        try:
            if exit_qtys_data:
                exit_quantities = json.loads(exit_qtys_data.decode('utf-8'))
        except (json.JSONDecodeError, TypeError):
            pass
            
        try:
            if spread_data:
                executed_spread = json.loads(spread_data.decode('utf-8'))
        except (json.JSONDecodeError, TypeError):
            pass
            
        try:
            if pnl_data:
                pnl = json.loads(pnl_data.decode('utf-8'))
        except (json.JSONDecodeError, TypeError):
            pass
        
        return {
            "instance_id": instance_id,
            "entry_quantities": entry_quantities,
            "exit_quantities": exit_quantities,
            "executed_spread": executed_spread,
            "pnl": pnl,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch live metrics for instance {instance_id}: {str(e)}")

@router.get("/instances/{instance_id}/global-observation")
async def get_global_observation_by_instance(instance_id: str):
    """Get current global observation data for a specific instance."""
    try:
        # Fetch from exactly 3 Redis keys
        latest_key = f"global_observation:{instance_id}:latest"
        buy_pair_key = f"global_observation:{instance_id}:BUY_PAIR"
        sell_pair_key = f"global_observation:{instance_id}:SELL_PAIR"
        
        global_data = {}
        
        # Fetch from latest key first
        latest_data = r.get(latest_key)
        if latest_data:
            try:
                global_data = json.loads(latest_data.decode('utf-8'))
            except json.JSONDecodeError:
                pass
        
        # Fetch and merge buy_pair data
        buy_pair_data = r.get(buy_pair_key)
        if buy_pair_data:
            try:
                buy_pair_json = json.loads(buy_pair_data.decode('utf-8'))
                if 'buy_pair' not in global_data:
                    global_data['buy_pair'] = {}
                global_data['buy_pair'].update(buy_pair_json)
            except json.JSONDecodeError:
                pass
        
        # Fetch and merge sell_pair data
        sell_pair_data = r.get(sell_pair_key)
        if sell_pair_data:
            try:
                sell_pair_json = json.loads(sell_pair_data.decode('utf-8'))
                if 'sell_pair' not in global_data:
                    global_data['sell_pair'] = {}
                global_data['sell_pair'].update(sell_pair_json)
            except json.JSONDecodeError:
                pass
        
        # If no data found, return empty structure with proper fields
        if not global_data:
            global_data = {
                "instance_id": instance_id,
                "buy_pair": {
                    "trend": "N/A", 
                    "execution_strategy": {"strategy": "N/A", "action": "N/A", "reason": "No market data"},
                    "exit_execution_strategy": {"strategy": "N/A", "action": "N/A", "reason": "No market data"},
                    "first_leg": "N/A",
                    "second_leg": "N/A", 
                    "first_leg_exit": "N/A",
                    "second_leg_exit": "N/A",
                    "trends": ["N/A"],
                    "final_prices": {"entry": 0, "exit": 0},
                    "timestamp": datetime.now().isoformat()
                },
                "sell_pair": {
                    "trend": "N/A",
                    "execution_strategy": {"strategy": "N/A", "action": "N/A", "reason": "No market data"},
                    "exit_execution_strategy": {"strategy": "N/A", "action": "N/A", "reason": "No market data"},
                    "first_leg": "N/A",
                    "second_leg": "N/A",
                    "first_leg_exit": "N/A", 
                    "second_leg_exit": "N/A",
                    "trends": ["N/A"],
                    "final_prices": {"entry": 0, "exit": 0},
                    "timestamp": datetime.now().isoformat()
                },
                "case_decision": "N/A",
                "current_case_decision": "N/A", 
                "symbol": "N/A",
                "atm_strike": "N/A",
                "timestamp": datetime.now().isoformat()
            }
        else:
            # Ensure required fields exist
            global_data['instance_id'] = instance_id
            if 'current_case_decision' not in global_data:
                global_data['current_case_decision'] = global_data.get('case_decision', 'N/A')
            if 'status' not in global_data:
                global_data['status'] = 'Active'
        
        return global_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch global observation for instance {instance_id}: {str(e)}")

@router.get("/instances/{instance_id}/global-leg-details")
async def get_global_leg_details_by_instance(instance_id: str):
    """Get global leg details for a specific instance."""
    try:
        # Use the correct Redis key pattern from the strategy
        global_leg_details_key = f"global_leg_details:{instance_id}"
        leg_details_data = r.get(global_leg_details_key)
        
        if leg_details_data:
            try:
                leg_details = json.loads(leg_details_data.decode('utf-8'))
                return leg_details
            except json.JSONDecodeError:
                pass
        
        # If no global leg details, return default structure
        default_leg_details = {
            "instance_id": instance_id,
            "leg1": {"strike": 0, "option_type": "PE", "expiry": "", "action": "BUY", "current_price": 0, "quantity": 0, "symbol": ""},
            "leg2": {"strike": 0, "option_type": "CE", "expiry": "", "action": "BUY", "current_price": 0, "quantity": 0, "symbol": ""},
            "leg3": {"strike": 0, "option_type": "PE", "expiry": "", "action": "SELL", "current_price": 0, "quantity": 0, "symbol": ""},
            "leg4": {"strike": 0, "option_type": "CE", "expiry": "", "action": "SELL", "current_price": 0, "quantity": 0, "symbol": ""}
        }
        
        return default_leg_details
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch global leg details for instance {instance_id}: {str(e)}")

@router.get("/instances/{instance_id}/case-decision-observation")
async def get_case_decision_observation_by_instance(instance_id: str):
    """Get the latest case decision observation for a specific instance."""
    try:
        # Try to get the latest case decision observation
        latest_case_decision_key = f"case_decision_observation:{instance_id}:latest"
        observation_data = r.get(latest_case_decision_key)
        
        if observation_data:
            try:
                observation_json = json.loads(observation_data.decode('utf-8'))
                return observation_json
            except json.JSONDecodeError:
                pass
        
        # If no case decision observation found, return default structure
        default_observation = {
            "instance_id": instance_id,
            "case_decision": "N/A",
            "reason": "no_observation_data",
            "status": "unknown",
            "timestamp": datetime.now().isoformat(),
            "observation_duration": 0,
            "sample_count": 0,
            "valid_samples": 0,
            "symbol": "N/A",
            "atm_strike": "N/A",
            "action": "N/A",
            "isExit": False,
            "legs": [],
            "trends": {},
            "execution_order": {
                "primary_leg": "N/A",
                "secondary_leg": "N/A"
            }
        }
        
        return default_observation
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch case decision observation for instance {instance_id}: {str(e)}")

@router.get("/instances/{instance_id}/case-decision-observations")
async def get_case_decision_observations_by_instance(instance_id: str):
    """Get all case decision observation IDs for a specific instance."""
    try:
        case_decision_ids_key = f"case_decision_observations:{instance_id}:ids"
        observation_ids = list(r.smembers(case_decision_ids_key))
        observation_ids = [id.decode('utf-8') if isinstance(id, bytes) else id for id in observation_ids]
        observation_ids.sort(reverse=True)  # Most recent first
        return {"observation_ids": observation_ids, "instance_id": instance_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch case decision observation IDs for instance {instance_id}: {str(e)}")

@router.get("/instances/{instance_id}/case-decision-observation/{observation_id}")
async def get_specific_case_decision_observation(instance_id: str, observation_id: str):
    """Get a specific case decision observation by observation ID and instance."""
    try:
        case_decision_key = f"case_decision_observation:{instance_id}:{observation_id}"
        observation_data = r.get(case_decision_key)
        
        if not observation_data:
            raise HTTPException(status_code=404, detail=f"Case decision observation not found for instance {instance_id}, observation ID: {observation_id}")
        
        observation_json = json.loads(observation_data.decode('utf-8'))
        return observation_json
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse case decision observation data")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch case decision observation: {str(e)}")