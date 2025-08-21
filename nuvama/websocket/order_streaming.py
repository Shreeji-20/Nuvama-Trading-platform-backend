from order_streaming_socket import OrderStreamingSocket
import os
import time
import traceback
import sys
user_id = sys.argv[1]
order_streaming_socket = OrderStreamingSocket(str(user_id))
while True:
    try:
        print("Starting")
        order_streaming_socket.order_streaming_start()
        time.sleep(1)
        while True:
            # Check both reading thread and timer status
            feedobj = order_streaming_socket.api_connect.feedobj
            if feedobj.t_read:
                if feedobj.t_read.is_alive():
                    # Also check if timer is active - both should be working
                    if feedobj.timer and feedobj.timer.isTimerActive():
                        print("Connected...",end='  \r')
                        time.sleep(0.5)
                    else:
                        print("Timer not active, restarting...")
                        raise Exception("Heartbeat timer stopped - restarting")
                else:   
                    print(f"Read thread: {feedobj.t_read.is_alive()}, Timer: {feedobj.timer.isTimerActive() if feedobj.timer else False}")
                    raise Exception("Reading Thread Stopped - restarting")
            else:
                raise Exception("No reading thread - restarting")
    except Exception as e:
        # print(traceback.format_exc())
        print("Error occured : ",e," Restarting...")
        try:
            order_streaming_socket.api_connect.feedobj.shutdown()
        except:
            pass
        # print(f"[Error] {e} — restarting order streaming...")
        time.sleep(2)  # Slightly longer delay before reconnecting
    except KeyboardInterrupt:
        os._exit(1)