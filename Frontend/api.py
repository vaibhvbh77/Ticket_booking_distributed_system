# frontend/api.py
# Flask front-end shim for the distributed booking demo.
# - retries reserve/cancel to find current leader using PEERS_MAP and NOT_LEADER hints
# - serves static frontend from frontend/static

from flask import Flask, request, jsonify, send_from_directory
import booking_pb2, booking_pb2_grpc
import grpc
import os
import sys

# allow importing booking_pb2 from project root (parent of frontend/)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Create Flask app (must be before route decorators)
app = Flask(__name__, static_folder="static", static_url_path="")

# Configuration: leader address and peers map (can be overridden via env)
LEADER_ADDR = os.environ.get("LEADER_ADDR", "127.0.0.1:60051")
PEERS_ENV = os.environ.get("PEERS", "node1=127.0.0.1:60051,node2=127.0.0.1:60052,node3=127.0.0.1:60053")
PEERS_MAP = dict(p.split("=", 1) for p in PEERS_ENV.split(","))

def make_stub(addr=None):
    addr = addr or LEADER_ADDR
    ch = grpc.insecure_channel(addr)
    return booking_pb2_grpc.ClientAPIStub(ch)

# --- static routes ---
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

@app.route("/login.html")
def login_page():
    return send_from_directory(app.static_folder, "login.html")

@app.route("/<path:filename>")
def static_files(filename):
    safe_path = os.path.normpath(filename)
    if safe_path.startswith(".."):
        return "Invalid path", 400
    return send_from_directory(app.static_folder, safe_path)

# --- API endpoints ---
@app.route("/login", methods=["POST"])
def login():
    data = request.json or {}
    username = data.get("username", "")
    password = data.get("password", "")
    try:
        stub = make_stub()
        resp = stub.Login(booking_pb2.LoginRequest(username=username, password=password), timeout=3)
        return jsonify({"status": getattr(resp, "status", 0), "token": getattr(resp, "token", "")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/get", methods=["POST"])
def get_seats():
    data = request.json or {}
    token = data.get("token", "")
    try:
        stub = make_stub()
        resp = stub.GetSeats(booking_pb2.GetRequest(token=token), timeout=3)
        seats = [{"seat_id": s.seat_id, "reserved": s.reserved, "reserved_by": s.reserved_by} for s in resp.seats]
        return jsonify({"status": getattr(resp, "status", 0), "seats": seats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- reserve/cancel helpers ---
def _try_reserve_on(addr, token, seat, client_id):
    try:
        stub = make_stub(addr)
        resp = stub.ReserveSeat(
            booking_pb2.ReserveRequest(token=token, seat_id=seat, client_id=client_id),
            timeout=5
        )
        return resp, None
    except Exception as e:
        return None, str(e)

def _try_cancel_on(addr, token, seat):
    try:
        stub = make_stub(addr)
        resp = stub.CancelSeat(booking_pb2.CancelRequest(token=token, seat_id=seat), timeout=5)
        return resp, None
    except Exception as e:
        return None, str(e)

@app.route("/reserve", methods=["POST"])
def reserve():
    data = request.json or {}
    token = data.get("token", "")
    seat = data.get("seat_id", "")
    client_id = data.get("client_id", "web-user")

    tried_addrs = set()
    leader_addr = LEADER_ADDR

    # 1) try configured leader first
    resp, err = _try_reserve_on(leader_addr, token, seat, client_id)
    tried_addrs.add(leader_addr)
    if resp is not None:
        code = getattr(resp, "code", None)
        msg = getattr(resp, "msg", "") or ""
        if code == 1 and isinstance(msg, str) and msg.startswith("NOT_LEADER:"):
            # try hint
            hint = msg.split(":", 1)[1].strip()
            hint_addr = PEERS_MAP.get(hint, hint)
            if hint_addr not in tried_addrs:
                resp2, err2 = _try_reserve_on(hint_addr, token, seat, client_id)
                tried_addrs.add(hint_addr)
                if resp2 is not None:
                    return jsonify({"code": getattr(resp2, "code", None), "msg": getattr(resp2, "msg", None)})
        else:
            return jsonify({"code": code, "msg": msg})

    # 2) try all peers
    for pid, addr in PEERS_MAP.items():
        if addr in tried_addrs:
            continue
        resp3, err3 = _try_reserve_on(addr, token, seat, client_id)
        tried_addrs.add(addr)
        if resp3 is not None:
            code3 = getattr(resp3, "code", None)
            msg3 = getattr(resp3, "msg", "") or ""
            if code3 == 1 and isinstance(msg3, str) and msg3.startswith("NOT_LEADER:"):
                hint = msg3.split(":", 1)[1].strip()
                hint_addr = PEERS_MAP.get(hint, hint)
                if hint_addr not in tried_addrs:
                    resp4, err4 = _try_reserve_on(hint_addr, token, seat, client_id)
                    tried_addrs.add(hint_addr)
                    if resp4 is not None:
                        return jsonify({"code": getattr(resp4, "code", None), "msg": getattr(resp4, "msg", None)})
            else:
                return jsonify({"code": code3, "msg": msg3})

    last_err = err or "no node reachable"
    return jsonify({"error": last_err}), 500

@app.route("/cancel", methods=["POST"])
def cancel():
    data = request.json or {}
    token = data.get("token", "")
    seat = data.get("seat_id", "")

    tried_addrs = set()
    leader_addr = LEADER_ADDR

    resp, err = _try_cancel_on(leader_addr, token, seat)
    tried_addrs.add(leader_addr)
    if resp is not None:
        code = getattr(resp, "code", None)
        msg = getattr(resp, "msg", "") or ""
        if code == 1 and isinstance(msg, str) and msg.startswith("NOT_LEADER:"):
            hint = msg.split(":", 1)[1].strip()
            hint_addr = PEERS_MAP.get(hint, hint)
            if hint_addr not in tried_addrs:
                resp2, err2 = _try_cancel_on(hint_addr, token, seat)
                tried_addrs.add(hint_addr)
                if resp2 is not None:
                    return jsonify({"code": getattr(resp2, "code", None), "msg": getattr(resp2, "msg", None)})
        else:
            return jsonify({"code": code, "msg": msg})

    for pid, addr in PEERS_MAP.items():
        if addr in tried_addrs:
            continue
        resp3, err3 = _try_cancel_on(addr, token, seat)
        tried_addrs.add(addr)
        if resp3 is not None:
            code3 = getattr(resp3, "code", None)
            msg3 = getattr(resp3, "msg", "") or ""
            if code3 == 1 and isinstance(msg3, str) and msg3.startswith("NOT_LEADER:"):
                hint = msg3.split(":", 1)[1].strip()
                hint_addr = PEERS_MAP.get(hint, hint)
                if hint_addr not in tried_addrs:
                    resp4, err4 = _try_cancel_on(hint_addr, token, seat)
                    tried_addrs.add(hint_addr)
                    if resp4 is not None:
                        return jsonify({"code": getattr(resp4, "code", None), "msg": getattr(resp4, "msg", None)})
            else:
                return jsonify({"code": code3, "msg": msg3})

    last_err = err or "no node reachable"
    return jsonify({"error": last_err}), 500

@app.route("/ask", methods=["POST"])
def ask():
    import requests
    q = (request.json or {}).get("q", "")
    try:
        resp = requests.post("http://127.0.0.1:8000/ask", json={"q": q}, timeout=5)
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    print("Starting frontend shim on http://127.0.0.1:8080")
    # Suggest using PYTHONPATH=. if booking_pb2 import fails
    app.run(host="127.0.0.1", port=8080, debug=True)