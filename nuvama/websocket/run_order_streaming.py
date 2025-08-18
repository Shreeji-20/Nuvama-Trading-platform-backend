import sys
import os
from order_streaming_socket import OrderStreamingSocket

def main():
    if len(sys.argv) < 2:
        print("Usage: run_order_streaming.py <user_id>")
        return 2
    user_id = sys.argv[1]
    sock = OrderStreamingSocket(user_id)
    # This call blocks and manages its own reconnect loop
    sock.order_streaming_start()

if __name__ == '__main__':
    sys.exit(main())
