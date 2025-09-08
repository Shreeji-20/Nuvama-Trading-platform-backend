"""
Tick Data Utilities - Tools for analyzing and replaying saved tick data
"""

import os
import time
import orjson
from datetime import datetime, date
from tick_data_manager import TickDataReader
import argparse
import statistics


class TickDataAnalyzer:
    """Analyze saved tick data files"""
    
    def __init__(self, base_directory="tick_data"):
        self.reader = TickDataReader(base_directory)
        self.base_directory = base_directory
    
    def analyze_date(self, date_str):
        """Analyze tick data for a specific date"""
        print(f"\n[INFO] ANALYZING TICK DATA FOR {date_str}")
        print("=" * 50)
        
        files = self.reader.list_available_files(date_str)
        date_path = os.path.join(self.base_directory, date_str)
        
        total_ticks = 0
        total_size = 0
        symbols_analyzed = set()
        
        # Analyze quotes files
        print("\n[INFO] QUOTES DATA:")
        print("-" * 20)
        for filename in files["quotes"]:
            filepath = os.path.join(date_path, "quotes", filename)
            file_size = os.path.getsize(filepath)
            total_size += file_size
            
            tick_count = 0
            timestamps = []
            
            for tick in self.reader.read_tick_file(filepath):
                tick_count += 1
                timestamps.append(tick['timestamp'])
                symbols_analyzed.add(tick.get('symbol', 'UNKNOWN'))
            
            total_ticks += tick_count
            
            if timestamps:
                start_time = datetime.fromtimestamp(min(timestamps)).strftime("%H:%M:%S")
                end_time = datetime.fromtimestamp(max(timestamps)).strftime("%H:%M:%S")
                duration = max(timestamps) - min(timestamps)
                avg_frequency = tick_count / duration if duration > 0 else 0
                
                print(f"  [INFO] {filename}")
                print(f"     [INFO] Ticks: {tick_count:,}")
                print(f"     [INFO] Size: {file_size/1024/1024:.2f} MB")
                print(f"     [INFO] Time: {start_time} - {end_time}")
                print(f"     [INFO] Freq: {avg_frequency:.1f} ticks/sec")
                print()
        
        # Analyze depth files
        print("\n[INFO] DEPTH DATA:")
        print("-" * 20)
        for filename in files["depth"]:
            filepath = os.path.join(date_path, "depth", filename)
            file_size = os.path.getsize(filepath)
            total_size += file_size
            
            tick_count = 0
            timestamps = []
            
            for tick in self.reader.read_tick_file(filepath):
                tick_count += 1
                timestamps.append(tick['timestamp'])
                
                # Extract symbol from redis_key
                redis_key = tick.get('redis_key', '')
                if redis_key.startswith('depth:'):
                    symbol_part = redis_key[6:].split('_')[0]
                    symbols_analyzed.add(symbol_part)
            
            total_ticks += tick_count
            
            if timestamps:
                start_time = datetime.fromtimestamp(min(timestamps)).strftime("%H:%M:%S")
                end_time = datetime.fromtimestamp(max(timestamps)).strftime("%H:%M:%S")
                duration = max(timestamps) - min(timestamps)
                avg_frequency = tick_count / duration if duration > 0 else 0
                
                print(f"  [INFO] {filename}")
                print(f"     [INFO] Ticks: {tick_count:,}")
                print(f"     [INFO] Size: {file_size/1024/1024:.2f} MB")
                print(f"     [INFO] Time: {start_time} - {end_time}")
                print(f"     [INFO] Freq: {avg_frequency:.1f} ticks/sec")
                print()
        
        # Summary
        print("\n[INFO] SUMMARY:")
        print("-" * 15)
        print(f"[INFO] Total ticks: {total_ticks:,}")
        print(f"[INFO] Total size: {total_size/1024/1024:.2f} MB")
        print(f"[INFO] Total files: {len(files['quotes']) + len(files['depth'])}")
        print(f"[INFO] Symbols: {', '.join(sorted(symbols_analyzed))}")
        print(f"[INFO] Compression ratio: ~{(total_ticks * 200) / total_size:.1f}x" if total_size > 0 else "")
    
    def compare_dates(self, date1, date2):
        """Compare tick data between two dates"""
        print(f"\n[INFO] COMPARING {date1} vs {date2}")
        print("=" * 40)
        
        for date_str in [date1, date2]:
            files = self.reader.list_available_files(date_str)
            date_path = os.path.join(self.base_directory, date_str)
            
            total_ticks = 0
            total_size = 0
            
            for file_type in ["quotes", "depth"]:
                for filename in files[file_type]:
                    filepath = os.path.join(date_path, file_type, filename)
                    if os.path.exists(filepath):
                        total_size += os.path.getsize(filepath)
                        for tick in self.reader.read_tick_file(filepath):
                            total_ticks += 1
            
            print(f"\n[INFO] {date_str}:")
            print(f"   [INFO] Ticks: {total_ticks:,}")
            print(f"   [INFO] Size: {total_size/1024/1024:.2f} MB")
            print(f"   [INFO] Files: {len(files['quotes']) + len(files['depth'])}")
    
    def find_active_hours(self, date_str, symbol=None):
        """Find the most active trading hours"""
        print(f"\n[INFO] FINDING ACTIVE HOURS FOR {date_str}")
        if symbol:
            print(f"[INFO] Symbol: {symbol}")
        print("=" * 40)
        
        files = self.reader.list_available_files(date_str)
        date_path = os.path.join(self.base_directory, date_str)
        
        hourly_counts = {}
        
        # Process all files
        for file_type in ["quotes", "depth"]:
            for filename in files[file_type]:
                if symbol and symbol not in filename:
                    continue
                    
                filepath = os.path.join(date_path, file_type, filename)
                
                for tick in self.reader.read_tick_file(filepath):
                    hour = datetime.fromtimestamp(tick['timestamp']).hour
                    hourly_counts[hour] = hourly_counts.get(hour, 0) + 1
        
        # Sort by hour and display
        for hour in sorted(hourly_counts.keys()):
            count = hourly_counts[hour]
            bar_length = min(50, count // max(hourly_counts.values()) * 50) if hourly_counts.values() else 0
            bar = "#" * int(bar_length)
            print(f"{hour:02d}:00 |{bar:<50}| {count:,} ticks")


class TickDataSimulator:
    """Simulate market data using saved tick files"""
    
    def __init__(self, base_directory="tick_data"):
        self.reader = TickDataReader(base_directory)
    
    def simulate_market_session(self, date_str, symbol=None, speed_multiplier=1.0, start_hour=9, end_hour=16):
        """Simulate a market session with saved tick data"""
        print(f"\n[INFO] SIMULATING MARKET SESSION FOR {date_str}")
        if symbol:
            print(f"[INFO] Symbol: {symbol}")
        print(f"[INFO] Speed: {speed_multiplier}x")
        print(f"[INFO] Hours: {start_hour:02d}:00 - {end_hour:02d}:00")
        print("=" * 50)
        
        # Convert hours to timestamps for filtering
        date_obj = datetime.strptime(date_str, "%Y%m%d")
        start_timestamp = date_obj.replace(hour=start_hour).timestamp()
        end_timestamp = date_obj.replace(hour=end_hour).timestamp()
        
        tick_count = 0
        start_time = time.time()
        
        try:
            for tick in self.reader.simulate_tick_replay(date_str, symbol, speed_multiplier):
                # Filter by time range
                if tick['timestamp'] < start_timestamp or tick['timestamp'] > end_timestamp:
                    continue
                
                tick_count += 1
                
                # Process the tick (you can customize this)
                self._process_simulated_tick(tick)
                
                # Print progress every 1000 ticks
                if tick_count % 1000 == 0:
                    elapsed = time.time() - start_time
                    rate = tick_count / elapsed if elapsed > 0 else 0
                    tick_time = datetime.fromtimestamp(tick['timestamp']).strftime("%H:%M:%S")
                    print(f"[INFO] {tick_count:,} ticks | {rate:.0f} ticks/sec | Market time: {tick_time}")
        
        except KeyboardInterrupt:
            print(f"\n[INFO] Simulation stopped by user")
        
        elapsed = time.time() - start_time
        print(f"\n[SUCCESS] Simulation completed")
        print(f"[INFO] Processed {tick_count:,} ticks in {elapsed:.1f} seconds")
        # print(f"[INFO] Average rate: {tick_count/elapsed:.0f} ticks/sec")
    
    def _process_simulated_tick(self, tick):
        """Process a simulated tick (customize this method)"""
        # This is where you would implement your trading logic
        # For now, we'll just update some basic statistics
        
        if tick['type'] == 'quotes':
            # Process quotes data
            symbol = tick.get('symbol')
            data = tick.get('data', {}).get('response', {}).get('data', {})
            ltp = data.get('ltp')
            if ltp:
                # You could update your strategy with new price
                pass
        
        elif tick['type'] == 'depth':
            # Process depth data
            redis_key = tick.get('redis_key')
            data = tick.get('data', {}).get('response', {}).get('data', {})
            bid = data.get('bid')
            ask = data.get('ask')
            if bid and ask:
                # You could update your order book with new bid/ask
                pass


def main():
    """Main CLI interface"""
    parser = argparse.ArgumentParser(description="Tick Data Analysis and Simulation Tools")
    parser.add_argument("command", choices=["list", "analyze", "compare", "hours", "simulate"], 
                       help="Command to execute")
    parser.add_argument("--date", help="Date in YYYYMMDD format")
    parser.add_argument("--date2", help="Second date for comparison (YYYYMMDD format)")
    parser.add_argument("--symbol", help="Symbol to filter (e.g., NIFTY, SENSEX)")
    parser.add_argument("--speed", type=float, default=1.0, help="Simulation speed multiplier")
    parser.add_argument("--start-hour", type=int, default=9, help="Start hour for simulation")
    parser.add_argument("--end-hour", type=int, default=16, help="End hour for simulation")
    parser.add_argument("--base-dir", default="tick_data", help="Base directory for tick data")
    
    args = parser.parse_args()
    
    if args.command == "list":
        reader = TickDataReader(args.base_dir)
        dates = reader.list_available_dates()
        print("\n[INFO] AVAILABLE DATES:")
        print("-" * 20)
        for date_str in dates:
            files = reader.list_available_files(date_str)
            total_files = len(files['quotes']) + len(files['depth'])
            print(f"  {date_str} ({total_files} files)")
    
    elif args.command == "analyze":
        if not args.date:
            print("[ERROR] --date required for analyze command")
            return
        analyzer = TickDataAnalyzer(args.base_dir)
        analyzer.analyze_date(args.date)
    
    elif args.command == "compare":
        if not args.date or not args.date2:
            print("[ERROR] --date and --date2 required for compare command")
            return
        analyzer = TickDataAnalyzer(args.base_dir)
        analyzer.compare_dates(args.date, args.date2)
    
    elif args.command == "hours":
        if not args.date:
            print("[ERROR] --date required for hours command")
            return
        analyzer = TickDataAnalyzer(args.base_dir)
        analyzer.find_active_hours(args.date, args.symbol)
    
    elif args.command == "simulate":
        if not args.date:
            print("[ERROR] --date required for simulate command")
            return
        simulator = TickDataSimulator(args.base_dir)
        simulator.simulate_market_session(
            args.date, args.symbol, args.speed, 
            args.start_hour, args.end_hour
        )


if __name__ == "__main__":
    main()
