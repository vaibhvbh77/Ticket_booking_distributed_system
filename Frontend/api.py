# frontend/api.py
from flask import Flask, request, jsonify, send_from_directory
import booking_pb2, booking_pb2_grpc
import grpc
import os

app = Flask(__name__, static_folder="static")

LEADER_ADDR = os.environ.get("LEADER_ADDR", "127.0.0.1:60051")

def make_stub(addr=None):
    addr = addr or LEADER_ADDR
    ch = grpc.insecure_channel(addr)
    return booking_pb2_grpc.ClientAPIStub(ch)

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/login", methods=["POST"])
def login():
    data = request.json or {}
    username = data.get("username","")
    password = data.get("password","")
    try:
        stub = make_stub()
        resp = stub.Login(booking_pb2.LoginRequest(username=username, password=password), timeout=3)
        return jsonify({"status": resp.status, "token": getattr(resp,"token","")})
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
        return jsonify({"status": resp.status, "seats": seats})
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
        return jsonify({"code": resp.code, "msg": resp.msg})
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
        # Status message fields may be code/msg
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
    app.run(port=8080, debug=True)
