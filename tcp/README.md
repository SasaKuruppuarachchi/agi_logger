# tcp_communication
TCP communication utilities for sending a single file from a server to a client over TCP.

## How it works
- `file_server.py` opens a TCP socket (default port `12344`), waits for a connection, sends simple metadata (`<filename>:<size>`) then streams the file in 1 KB chunks.
- `file_client.py` connects to the server, reads the metadata, acknowledges with `READY`, then downloads the file to the current directory.
- Both peers must be on the same network and reachable by IP. Only one client is served per connection loop iteration.

## Usage
- Start the server (defaults to port `12344`; prompts for file path if not provided):
	```bash
	python file_server.py -f path/to/file.txt           # use default port 12344
	python file_server.py -f path/to/file.txt -p 9000   # custom port
	python file_server.py                               # will prompt for file path
	```

- Start the client (defaults to port `12344`; prompts for server IP if not provided):
	```bash
	python file_client.py -s 192.168.1.10               # use default port 12344
	python file_client.py -s 192.168.1.10 -p 9000       # custom port
	python file_client.py -p 9000                       # will prompt for server IP
	python file_client.py                               # will prompt for server IP, uses default port 12344
	```

Notes:
- Ensure the chosen port is open and not blocked by firewalls.
- The received file is saved in the current working directory under the original filename.