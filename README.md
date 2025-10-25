🎟️ Distributed Ticket Booking System
A minimal distributed seat booking system using gRPC, Raft-based leader election, and an integrated LLM assistant.
⸻
🧩 0. Prerequisites (macOS / Linux) • Python 3.9+ (3.10 or 3.11 recommended) • git • The following ports should be free: 60051, 60052, 60053, 8080, 8000 (or change ports in commands if needed)
⸻
💻 Windows Users
All commands below assume macOS/Linux. For Windows: • Replace export with $env:VAR="value" • Replace / with \ in paths • Replace rm with Remove-Item
🚀 1. Clone & Setup
Clone (if not already)

git clone https://github.com/vaibhvbh77/Ticket_booking_distributed_system.git cd Ticket_booking_distributed_system
Create & activate virtual environment

python3 -m venv venv source venv/bin/activate # (macOS/Linux)
Install dependencies

pip install -r requirements.txt
👤 2. Add a User (One-time setup per node)
Run the following once to add a default user (user1 / mysecret) to all node files: python3 - <<'PY' import json, hashlib, os def add_user_to(file, username, password): try: d = json.load(open(file)) except Exception: d = {} users = d.get("users", {}) salt = os.urandom(8).hex() pw_hash = hashlib.sha256((salt + password).encode()).hexdigest() users[username] = {"salt": salt, "pw_hash": pw_hash} d["users"] = users with open(file, "w") as f: json.dump(d, f, indent=2) print("added", username, "to", file)
add_user_to("node_node1.json", "user1", "mysecret") add_user_to("node_node2.json", "user1", "mysecret") PY
🧠 3. Run Nodes (in separate terminals)
Open two terminals, from project root:
Terminal 1:PYTHONPATH=. python3 nodes/node.py node1 127.0.0.1:60051 "node1=127.0.0.1:60051,node2=127.0.0.1:60052,node3=127.0.0.1:60053"
Terminal 2:PYTHONPATH=. python3 nodes/node.py node2 127.0.0.1:60052 "node1=127.0.0.1:60051,node2=127.0.0.1:60052,node3=127.0.0.1:60053"
🤖 4. Start the LLM Helper python3 llm_server.py This starts a small assistant API at http://127.0.0.1:8000 Used for seat-related natural language queries.
💬 6. Start the CLI Client PYTHONPATH=. python3 client/client.py 127.0.0.1:60051
🎯 7. Typical Actions (CLI Demo)
At the > prompt:
1. login Username: user1 Password: mysecret Result: Logged in, token: tok-user1-...
2. get Result: S1 S2 S3 S4 (availability list)
3. reserve S2 Result: code=0 msg=RESERVED
4. cancel S2 Result: code=0 msg=CANCEL_OK
🧠 8. Ask the LLM (Natural Language Queries)
From CLI or frontend “Ask LLM” box: ask> Is seat S2 available? ask> Who reserved seat S3? ask> How to cancel my booking?
🧩 9. Project Structure Ticket_booking_distributed_system/ ├── nodes/ # Raft-enabled booking servers ├── client/ # CLI client for testing ├── llm_server.py # Lightweight question-answer helper ├── booking.proto # gRPC definitions ├── booking_pb2.py # Generated protobufs ├── booking_pb2_grpc.py └── requirements.txt
⸻
🔗 10. Demo Video
🎥 Demo link: [Add your YouTube/Drive link here]
🌐 Tags
#grpc #distributed-systems #raft #python #flask #llm
