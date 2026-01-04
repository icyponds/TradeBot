#!/usr/bin/env python3
import subprocess
import time
import signal
import sys
import os

def main():
    duration = 1800 # 30 minutes
    print(f"Starting TradeBot for {duration} seconds...")
    
    # Start the bot
    # Use output buffering=0 for real-time logs if needed, but here we just let it write to log file
    process = subprocess.Popen(
        [sys.executable, "-m", "src.main"],
        cwd=os.getcwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    print(f"Bot started with PID {process.pid}")
    
    try:
        # Wait loop
        start_time = time.time()
        while time.time() - start_time < duration:
            if process.poll() is not None:
                print(f"Bot exited early with code {process.returncode}")
                # Print last few lines of stderr for debugging
                _, stderr = process.communicate()
                print("STDERR execution output:")
                print(stderr[-500:] if stderr else "No stderr")
                sys.exit(process.returncode)
            time.sleep(1)
            
        print("Time limit reached. Initiating graceful shutdown...")
        process.terminate()
        
        try:
            process.wait(timeout=30)
            print("Bot stopped successfully.")
        except subprocess.TimeoutExpired:
            print("Bot did not stop in time, forcing kill...")
            process.kill()
            
    except KeyboardInterrupt:
        print("Interrupted by user. Stopping bot...")
        process.terminate()
        process.wait()
        
if __name__ == "__main__":
    main()
