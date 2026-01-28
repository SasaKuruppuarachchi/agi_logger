import socket
import time

def start_server(host='0.0.0.0', port=12345):
    # Create a socket object
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # Bind to the host and port
    server_socket.bind((host, port))
    print(f"Server listening on {host}:{port}")

    # Listen for connections
    server_socket.listen(1)

    while True:
        # Accept a client connection
        client_socket, addr = server_socket.accept()
        print(f"Connected by {addr}")

        try:
            # Send data to the client
            while True:
                message = "Hello from Server!"
                client_socket.sendall(message.encode())
                print(f"Sent: {message}")
                # Wait before sending the next message
                time.sleep(1)

        except BrokenPipeError:
            print(f"Client {addr} disconnected.")
        finally:
            client_socket.close()

if __name__ == "__main__":
    start_server()
