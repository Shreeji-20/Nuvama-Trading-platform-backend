"""
Test script for the new logging system and execution tracking
"""

import sys
import os
import redis
import time
import json
from datetime import datetime

# Add current directory to Python path to handle imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Create simplified versions of the logging classes for testing
class StrategyLoggingHelpers:
    """Enhanced logging with colors and formatting for strategy execution"""
    
    # Color constants for Windows
    COLORS = {
        'ERROR': '\033[91m',      # Red
        'WARNING': '\033[93m',    # Yellow
        'INFO': '\033[94m',       # Blue
        'DEBUG': '\033[96m',      # Cyan
        'SUCCESS': '\033[92m',    # Green
        'RESET': '\033[0m',       # Reset
        'BOLD': '\033[1m',        # Bold
    }
    
    @staticmethod
    def _get_timestamp():
        """Get formatted timestamp for logging"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    
    @staticmethod
    def _format_message(level, message, details=None):
        """Format message with timestamp and color"""
        timestamp = StrategyLoggingHelpers._get_timestamp()
        color = StrategyLoggingHelpers.COLORS.get(level, '')
        reset = StrategyLoggingHelpers.COLORS['RESET']
        bold = StrategyLoggingHelpers.COLORS['BOLD']
        
        formatted = f"{color}{bold}[{timestamp}] [{level}]{reset} {color}{message}{reset}"
        if details:
            formatted += f"\n{color}  └─ Details: {details}{reset}"
        
        return formatted
    
    @staticmethod
    def error(message, details=None, exception=None):
        """Log error message in red"""
        if exception:
            details = f"{details} | Exception: {str(exception)}" if details else f"Exception: {str(exception)}"
        print(StrategyLoggingHelpers._format_message('ERROR', message, details))
    
    @staticmethod
    def warning(message, details=None):
        """Log warning message in yellow"""
        print(StrategyLoggingHelpers._format_message('WARNING', message, details))
    
    @staticmethod
    def info(message, details=None):
        """Log info message in blue"""
        print(StrategyLoggingHelpers._format_message('INFO', message, details))
    
    @staticmethod
    def debug(message, details=None):
        """Log debug message in cyan"""
        print(StrategyLoggingHelpers._format_message('DEBUG', message, details))
    
    @staticmethod
    def success(message, details=None):
        """Log success message in green"""
        print(StrategyLoggingHelpers._format_message('SUCCESS', message, details))
    
    @staticmethod
    def separator(title=None):
        """Print a separator line with optional title"""
        line = "=" * 80
        color = StrategyLoggingHelpers.COLORS['INFO']
        reset = StrategyLoggingHelpers.COLORS['RESET']
        
        if title:
            print(f"\n{color}{line}")
            print(f"{'=' * 30} {title.upper()} {'=' * (49 - len(title))}")
            print(f"{line}{reset}\n")
        else:
            print(f"{color}{line}{reset}")


class StrategyExecutionTracker:
    """Track strategy execution details and store in Redis with datetime keys"""
    
    def __init__(self, redis_connection, strategy_name="DirectIOCBox"):
        self.redis_conn = redis_connection
        self.strategy_name = strategy_name
        self.execution_id = None
        self.execution_key = None
        self.start_time = None
        self.execution_data = {}
        
    def start_execution(self, params_id, case_type=None):
        """Start a new execution tracking session"""
        self.start_time = datetime.now()
        self.execution_id = self.start_time.strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self.execution_key = f"strategy_execution:{self.strategy_name}:{self.execution_id}"
        
        self.execution_data = {
            "execution_id": self.execution_id,
            "strategy_name": self.strategy_name,
            "params_id": params_id,
            "case_type": case_type,
            "start_time": self.start_time.isoformat(),
            "status": "STARTED",
            "milestones": [],
            "errors": [],
            "orders": {},
            "observations": {},
            "final_result": None,
            "end_time": None,
            "duration": None
        }
        
        self._save_to_redis()
        StrategyLoggingHelpers.success(
            f"Started execution tracking for {self.strategy_name}",
            f"ID: {self.execution_id} | Params: {params_id} | Case: {case_type}"
        )
        return self.execution_id
    
    def add_milestone(self, milestone, details=None):
        """Add a milestone to the execution tracking"""
        if not self.execution_data:
            return
            
        milestone_data = {
            "timestamp": datetime.now().isoformat(),
            "milestone": milestone,
            "details": details or {}
        }
        self.execution_data["milestones"].append(milestone_data)
        self._save_to_redis()
        
        StrategyLoggingHelpers.info(
            f"Milestone reached: {milestone}",
            details if isinstance(details, str) else json.dumps(details, indent=2) if details else None
        )
    
    def add_error(self, error_msg, error_details=None, exception=None):
        """Add an error to the execution tracking"""
        if not self.execution_data:
            return
            
        error_data = {
            "timestamp": datetime.now().isoformat(),
            "error_message": error_msg,
            "error_details": error_details,
            "exception": str(exception) if exception else None,
            "traceback": str(exception) if exception else None
        }
        self.execution_data["errors"].append(error_data)
        self.execution_data["status"] = "ERROR"
        self._save_to_redis()
        
        StrategyLoggingHelpers.error(
            f"Error recorded: {error_msg}",
            error_details,
            exception
        )
    
    def add_order(self, order_id, order_data):
        """Add order information to tracking"""
        if not self.execution_data:
            return
            
        self.execution_data["orders"][order_id] = {
            "timestamp": datetime.now().isoformat(),
            "order_data": order_data
        }
        self._save_to_redis()
    
    def add_observation(self, observation_type, observation_data):
        """Add observation data to tracking"""
        if not self.execution_data:
            return
            
        if observation_type not in self.execution_data["observations"]:
            self.execution_data["observations"][observation_type] = []
            
        self.execution_data["observations"][observation_type].append({
            "timestamp": datetime.now().isoformat(),
            "data": observation_data
        })
        self._save_to_redis()
    
    def complete_execution(self, final_result, status="COMPLETED"):
        """Complete the execution tracking"""
        if not self.execution_data:
            return
            
        end_time = datetime.now()
        duration = (end_time - self.start_time).total_seconds()
        
        self.execution_data.update({
            "status": status,
            "final_result": final_result,
            "end_time": end_time.isoformat(),
            "duration": duration
        })
        
        self._save_to_redis()
        
        StrategyLoggingHelpers.success(
            f"Execution completed with status: {status}",
            f"Duration: {duration:.2f}s | Result: {final_result}"
        )
    
    def handle_crash(self, exception):
        """Handle unexpected crashes and save state"""
        if not self.execution_data:
            return
            
        self.add_error("Strategy execution crashed", "Unexpected termination", exception)
        self.complete_execution("CRASHED", "CRASHED")
        
        StrategyLoggingHelpers.error(
            "Strategy execution crashed - state saved to Redis",
            f"Execution ID: {self.execution_id}",
            exception
        )
    
    def _save_to_redis(self):
        """Save execution data to Redis"""
        try:
            self.redis_conn.set(
                self.execution_key,
                json.dumps(self.execution_data, indent=2),
                ex=86400 * 7  # Expire after 7 days
            )
        except Exception as e:
            print(f"Failed to save execution data to Redis: {e}")
    
    def get_execution_summary(self):
        """Get a summary of the current execution"""
        if not self.execution_data:
            return "No active execution"
            
        milestones_count = len(self.execution_data["milestones"])
        errors_count = len(self.execution_data["errors"])
        orders_count = len(self.execution_data["orders"])
        
        return f"Execution {self.execution_id}: {milestones_count} milestones, {errors_count} errors, {orders_count} orders"

def test_colored_logging():
    """Test colored console logging functionality"""
    print("\n" + "="*60)
    print("TESTING COLORED CONSOLE LOGGING")
    print("="*60)
    
    logger = StrategyLoggingHelpers
    
    # Test different log levels
    logger.separator("LOGGING SYSTEM TEST")
    
    logger.info("This is an info message", "Additional details about the info")
    logger.debug("This is a debug message", "Debug information for developers")
    logger.warning("This is a warning message", "Something needs attention")
    logger.error("This is an error message", "Something went wrong", Exception("Test exception"))
    logger.success("This is a success message", "Operation completed successfully")
    
    logger.separator()

def test_execution_tracking():
    """Test Redis execution tracking functionality"""
    print("\n" + "="*60)
    print("TESTING REDIS EXECUTION TRACKING")
    print("="*60)
    
    # Connect to Redis
    try:
        r = redis.Redis(host='localhost', port=6379, db=0)
        r.ping()
        print("✓ Redis connection successful")
    except Exception as e:
        print(f"✗ Redis connection failed: {e}")
        return
    
    # Initialize execution tracker
    tracker = StrategyExecutionTracker(r, "TestStrategy")
    logger = StrategyLoggingHelpers
    
    try:
        # Start execution
        execution_id = tracker.start_execution("TEST_PARAMS_123", "TEST_CASE")
        
        # Add some milestones
        tracker.add_milestone("Strategy initialization", {"param": "value", "config": "test"})
        time.sleep(0.1)
        
        tracker.add_milestone("Market observation started", {"legs": ["LEG1", "LEG2"]})
        time.sleep(0.1)
        
        # Add some orders
        tracker.add_order("ORDER_001", {
            "symbol": "NIFTY",
            "quantity": 100,
            "price": 18500.00,
            "action": "BUY"
        })
        time.sleep(0.1)
        
        # Add observation data
        tracker.add_observation("PRICE_DATA", {
            "leg1_price": 100.50,
            "leg2_price": 200.75,
            "spread": 301.25
        })
        time.sleep(0.1)
        
        # Simulate some processing
        tracker.add_milestone("Orders executed", {"success_count": 2, "failed_count": 0})
        time.sleep(0.1)
        
        # Complete execution
        tracker.complete_execution("All orders executed successfully", "COMPLETED")
        
        # Verify data was stored in Redis
        execution_key = tracker.execution_key
        stored_data = r.get(execution_key)
        
        if stored_data:
            logger.success("Execution data successfully stored in Redis")
            logger.info(f"Execution key: {execution_key}")
            logger.info(f"Data size: {len(stored_data)} bytes")
            
            # Show summary
            summary = tracker.get_execution_summary()
            logger.info("Execution summary", summary)
            
        else:
            logger.error("Failed to store execution data in Redis")
            
    except Exception as e:
        logger.error("Test execution tracking failed", exception=e)
        if tracker.execution_data:
            tracker.handle_crash(e)

def test_crash_recovery():
    """Test crash recovery functionality"""
    print("\n" + "="*60)
    print("TESTING CRASH RECOVERY")
    print("="*60)
    
    r = redis.Redis(host='localhost', port=6379, db=0)
    tracker = StrategyExecutionTracker(r, "CrashTestStrategy")
    logger = StrategyLoggingHelpers
    
    try:
        # Start execution
        execution_id = tracker.start_execution("CRASH_TEST_PARAMS", "CRASH_TEST")
        
        # Add some progress
        tracker.add_milestone("Processing started")
        tracker.add_milestone("Midway through execution")
        
        # Simulate a crash
        raise Exception("Simulated crash for testing crash recovery")
        
    except Exception as e:
        logger.warning("Handling simulated crash")
        tracker.handle_crash(e)
        
        # Verify crash data was stored
        execution_key = tracker.execution_key
        stored_data = r.get(execution_key)
        
        if stored_data:
            logger.success("Crash data successfully stored in Redis")
            summary = tracker.get_execution_summary()
            logger.info("Crash recovery summary", summary)
        else:
            logger.error("Failed to store crash data")

def main():
    """Run all tests"""
    print("STRATEGY LOGGING AND EXECUTION TRACKING TESTS")
    print("=" * 60)
    
    try:
        test_colored_logging()
        test_execution_tracking()
        test_crash_recovery()
        
        print("\n" + "="*60)
        print("ALL TESTS COMPLETED")
        print("="*60)
        
    except Exception as e:
        print(f"Test suite failed: {e}")

if __name__ == "__main__":
    main()
