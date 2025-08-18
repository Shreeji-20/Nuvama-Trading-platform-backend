from order_streaming_socket import OrderStreamingSocket
import os
import time
import traceback
order_streaming_socket = OrderStreamingSocket("70249886")
while True:
    try:
        print("Starting")
        order_streaming_socket.order_streaming_start()
        time.sleep(1)
        while True:
            if order_streaming_socket.api_connect.feedobj.t_read:
                if order_streaming_socket.api_connect.feedobj.t_read.is_alive():
                    print("Connected...",end='  \r')
                    time.sleep(0.5)
                else:   
                    print(order_streaming_socket.api_connect.feedobj.t_read.is_alive(),order_streaming_socket.api_connect.feedobj.timer.isTimerActive())
                    raise Exception("Reading Thread Stopped or heartbeat timer stopped restarting")
            else:
                raise Exception("Exception Created.")
    except Exception as e:
        # print(traceback.format_exc())
        print("Error occured : ",e," Restarting...")
        order_streaming_socket.api_connect.feedobj.shutdown()
        # print(f"[Error] {e} — restarting order streaming...")
        time.sleep(1)  # Short delay before reconnecting
    except KeyboardInterrupt:
        os._exit(1)