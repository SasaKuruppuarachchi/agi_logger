import socket
import time

def start_client(server_ip, server_port=12345):
    # Create a socket object
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        # Connect to the server
        client_socket.connect((server_ip, server_port))
        print(f"Connected to server at {server_ip}:{server_port}")

        # Receive data from the server
        while True:
            data = client_socket.recv(1024)  # Buffer size
            if not data:
                break
            print(f"Received: {data.decode()}")

    except ConnectionRefusedError:
        print("Connection refused. Make sure the server is running.")
    finally:
        client_socket.close()

if __name__ == "__main__":
    SERVER_IP = "127.0.0.1"  # Replace with the IP address of Computer 1
    start_client(SERVER_IP)
