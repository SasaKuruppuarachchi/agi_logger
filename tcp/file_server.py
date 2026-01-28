import socket
import os
import sys
import argparse

def send_file(file_path, host='0.0.0.0', port=12344):
    # Create a socket object
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((host, port))
    server_socket.listen(1)

    print(f"Server is listening on {host}:{port}")

    while True:
        # Wait for a client to connect
        client_socket, addr = server_socket.accept()
        print(f"Connected by {addr}")

        try:
            # Check if file exists
            if not os.path.exists(file_path):
                print(f"File {file_path} not found.")
                client_socket.sendall(b"ERROR: File not found.")
                continue

            # Send file metadata (size and name)
            file_size = os.path.getsize(file_path)
            file_name = os.path.basename(file_path)
            metadata = f"{file_name}:{file_size}"
            client_socket.sendall(metadata.encode())
            print(f"Sent metadata: {metadata}")

            # Wait for acknowledgment
            ack = client_socket.recv(1024).decode()
            if ack != "READY":
                print("Client not ready. Disconnecting.")
                continue

            # Send file data in chunks
            with open(file_path, 'rb') as f:
                while (chunk := f.read(1024)):
                    client_socket.sendall(chunk)

            print(f"File {file_name} sent successfully.")
        finally:
            client_socket.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send a file over TCP")
    parser.add_argument(
        "-f", "--file",
        type=str,
        help="Path to the file to send"
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=12344,
        help="Port to listen on (default: 12344)"
    )
    
    args = parser.parse_args()
    
    # Get file path from arguments or prompt user
    file_path = args.file
    if not file_path:
        file_path = input("Please enter the file path: ")
    
    # Validate file path is not empty
    if not file_path:
        print("Error: File path cannot be empty.")
        sys.exit(1)
    
    send_file(file_path, port=args.port)
