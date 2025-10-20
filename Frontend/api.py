# frontend/api.py
from flask import Flask, request, jsonify, send_from_directory
import booking_pb2, booking_pb2_grpc
import grpc
import os

# static folder is frontend/static relative to this file
app = Flask(__name__, static_folder="static", static_url_path="")

LEADER_ADDR = os.environ.get("LEADER_ADDR", "127.0.0.1:60051")

def make_stub(addr=None):
    addr = addr or LEADER_ADDR
    ch = grpc.insecure_channel(addr)
    return booking_pb2_grpc.ClientAPIStub(ch)

# Serve root index
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

# Serve login page explicitly
@app.route("/login.html")
def login_page():
    return send_from_directory(app.static_folder, "login.html")

# Generic static file route so /index.html, /styles.css, etc. work
@app.route("/<path:filename>")
def static_files(filename):
    # sanitize path
    safe_path = os.path.normpath(filename)
    # prevent escaping the static folder
    if safe_path.startswith(".."):
        return "Invalid path", 400
    return send_from_directory(app.static_folder, safe_path)

# ---- API endpoints (proxy to gRPC nodes) ----
@app.route("/login", methods=["POST"])
def login():
    data = request.json or {}
    username = data.get("username","")
    password = data.get("password","")
    try:
        stub = make_stub()
        resp = stub.Login(booking_pb2.LoginRequest(username=username, password=password), timeout=3)
        return jsonify({"status": getattr(resp,"status",0), "token": getattr(resp,"token","")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/get", methods=["POST"])
def get_seats():
    data = request.json or {}
    token = data.get("token","")
    try:
        stub = make_stub()
        resp = stub.GetSeats(booking_pb2.GetRequest(token=token), timeout=3)
        seats = [{"seat_id": s.seat_id, "reserved": s.reserved, "reserved_by": s.reserved_by} for s in resp.seats]
        return jsonify({"status": getattr(resp,"status",0), "seats": seats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/reserve", methods=["POST"])
def reserve():
    data = request.json or {}
    token = data.get("token","")
    seat = data.get("seat_id","")
    client_id = data.get("client_id","web-user")
    try:
        stub = make_stub()
        resp = stub.ReserveSeat(booking_pb2.ReserveRequest(token=token, seat_id=seat, client_id=client_id), timeout=5)
        return jsonify({"code": getattr(resp,"code",None), "msg": getattr(resp,"msg",None)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/cancel", methods=["POST"])
def cancel():
    data = request.json or {}
    token = data.get("token","")
    seat = data.get("seat_id","")
    try:
        stub = make_stub()
        resp = stub.CancelSeat(booking_pb2.CancelRequest(token=token, seat_id=seat), timeout=5)
        return jsonify({"code": getattr(resp,'code',None), "msg": getattr(resp,'msg',None)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/ask", methods=["POST"])
def ask():
    # proxy to your LLM server if you have it at 127.0.0.1:8000
    import requests
    q = (request.json or {}).get("q","")
    try:
        resp = requests.post("http://127.0.0.1:8000/ask", json={"q": q}, timeout=5)
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    print("Starting frontend shim on http://127.0.0.1:8080")
    app.run(host="127.0.0.1", port=8080, debug=True)