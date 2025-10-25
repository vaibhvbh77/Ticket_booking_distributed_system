# frontend/api.py
from flask import Flask, request, jsonify, send_from_directory
import grpc
import booking_pb2, booking_pb2_grpc
import os
import sys

# allow imports from project root (so booking_pb2 can be found when running from repo root)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ---- app config ----
APP = Flask(__name__, static_folder="static", static_url_path="")

DEFAULT_LEADER = os.environ.get("LEADER_ADDR", "127.0.0.1:60051")
PEERS_SETTING = os.environ.get(
    "PEERS",
    "node1=127.0.0.1:60051,node2=127.0.0.1:60052,node3=127.0.0.1:60053",
)
# map like {"node1": "127.0.0.1:60051", ...}
PEERS_MAP = dict(p.split("=", 1) for p in PEERS_SETTING.split(","))

def create_client_stub(address=None):
    """Return a ClientAPI gRPC stub for the given address (or default leader)."""
    addr = address or DEFAULT_LEADER
    channel = grpc.insecure_channel(addr)
    return booking_pb2_grpc.ClientAPIStub(channel)

# ---- static file routes ----
@APP.route("/")
def serve_index():
    return send_from_directory(APP.static_folder, "index.html")

@APP.route("/login.html")
def serve_login():
    return send_from_directory(APP.static_folder, "login.html")

@APP.route("/<path:fn>")
def serve_static(fn):
    safe = os.path.normpath(fn)
    if safe.startswith(".."):
        return "Invalid path", 400
    return send_from_directory(APP.static_folder, safe)

# ---- API (proxy) routes ----
@APP.route("/login", methods=["POST"])
def rpc_login():
    payload = request.json or {}
    user = payload.get("username", "")
    pw = payload.get("password", "")
    try:
        stub = create_client_stub()
        resp = stub.Login(booking_pb2.LoginRequest(username=user, password=pw), timeout=3)
        return jsonify({"status": getattr(resp, "status", 0), "token": getattr(resp, "token", "")})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@APP.route("/get", methods=["POST"])
def rpc_get():
    payload = request.json or {}
    token = payload.get("token", "")
    try:
        stub = create_client_stub()
        resp = stub.GetSeats(booking_pb2.GetRequest(token=token), timeout=3)
        seats = [{"seat_id": s.seat_id, "reserved": s.reserved, "reserved_by": s.reserved_by} for s in resp.seats]
        return jsonify({"status": getattr(resp, "status", 0), "seats": seats})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# ---- helpers used by reserve/cancel logic ----
def _call_reserve_on(address, token, seat_id, client_id):
    try:
        stub = create_client_stub(address)
        response = stub.ReserveSeat(
            booking_pb2.ReserveRequest(token=token, seat_id=seat_id, client_id=client_id),
            timeout=5
        )
        return response, None
    except Exception as exc:
        return None, str(exc)

def _call_cancel_on(address, token, seat_id):
    try:
        stub = create_client_stub(address)
        response = stub.CancelSeat(booking_pb2.CancelRequest(token=token, seat_id=seat_id), timeout=5)
        return response, None
    except Exception as exc:
        return None, str(exc)

def _leader_hint_from_msg(msg):
    """
    Parse a NOT_LEADER hint if present. Servers sometimes return 'NOT_LEADER:<hint>'.
    The hint could be a node id (e.g. node2) or an address (127.0.0.1:60052).
    """
    if not msg or "NOT_LEADER:" not in msg:
        return None
    hint = msg.split("NOT_LEADER:", 1)[1].strip()
    # map node-id to address if available
    return PEERS_MAP.get(hint, hint)

@APP.route("/reserve", methods=["POST"])
def rpc_reserve():
    payload = request.json or {}
    token = payload.get("token", "")
    seat = payload.get("seat_id", "")
    client_id = payload.get("client_id", "web-user")

    tried = set()
    # 1) try the configured leader first
    leader_addr = os.environ.get("LEADER_ADDR", DEFAULT_LEADER)
    resp, err = _call_reserve_on(leader_addr, token, seat, client_id)
    tried.add(leader_addr)
    if resp is not None:
        code = getattr(resp, "code", None)
        msg = getattr(resp, "msg", "") or ""
        # if follower told us NOT_LEADER: try the hint
        if code == 1 and isinstance(msg, str) and msg.startswith("NOT_LEADER:"):
            hint_addr = _leader_hint_from_msg(msg)
            if hint_addr and hint_addr not in tried:
                r2, e2 = _call_reserve_on(hint_addr, token, seat, client_id)
                tried.add(hint_addr)
                if r2 is not None:
                    return jsonify({"code": getattr(r2, "code", None), "msg": getattr(r2, "msg", None)})
        else:
            return jsonify({"code": code, "msg": msg})

    # 2) try all known peers one-by-one
    for pid, addr in PEERS_MAP.items():
        if addr in tried:
            continue
        r, e = _call_reserve_on(addr, token, seat, client_id)
        tried.add(addr)
        if r is not None:
            code = getattr(r, "code", None)
            msg = getattr(r, "msg", "") or ""
            if code == 1 and isinstance(msg, str) and msg.startswith("NOT_LEADER:"):
                hint_addr = _leader_hint_from_msg(msg)
                if hint_addr and hint_addr not in tried:
                    r2, e2 = _call_reserve_on(hint_addr, token, seat, client_id)
                    tried.add(hint_addr)
                    if r2 is not None:
                        return jsonify({"code": getattr(r2, "code", None), "msg": getattr(r2, "msg", None)})
            else:
                return jsonify({"code": code, "msg": msg})

    return jsonify({"error": err or "no node reachable"}), 500

@APP.route("/cancel", methods=["POST"])
def rpc_cancel():
    payload = request.json or {}
    token = payload.get("token", "")
    seat = payload.get("seat_id", "")

    tried = set()
    leader_addr = os.environ.get("LEADER_ADDR", DEFAULT_LEADER)

    resp, err = _call_cancel_on(leader_addr, token, seat)
    tried.add(leader_addr)
    if resp is not None:
        code = getattr(resp, "code", None)
        msg = getattr(resp, "msg", "") or ""
        if code == 1 and isinstance(msg, str) and msg.startswith("NOT_LEADER:"):
            hint_addr = _leader_hint_from_msg(msg)
            if hint_addr and hint_addr not in tried:
                r2, e2 = _call_cancel_on(hint_addr, token, seat)
                tried.add(hint_addr)
                if r2 is not None:
                    return jsonify({"code": getattr(r2, "code", None), "msg": getattr(r2, "msg", None)})
        else:
            return jsonify({"code": code, "msg": msg})

    for pid, addr in PEERS_MAP.items():
        if addr in tried:
            continue
        r, e = _call_cancel_on(addr, token, seat)
        tried.add(addr)
        if r is not None:
            code = getattr(r, "code", None)
            msg = getattr(r, "msg", "") or ""
            if code == 1 and isinstance(msg, str) and msg.startswith("NOT_LEADER:"):
                hint_addr = _leader_hint_from_msg(msg)
                if hint_addr and hint_addr not in tried:
                    r2, e2 = _call_cancel_on(hint_addr, token, seat)
                    tried.add(hint_addr)
                    if r2 is not None:
                        return jsonify({"code": getattr(r2, "code", None), "msg": getattr(r2, "msg", None)})
            else:
                return jsonify({"code": code, "msg": msg})

    return jsonify({"error": err or "no node reachable"}), 500

@APP.route("/ask", methods=["POST"])
def proxy_ask():
    import requests
    q = (request.json or {}).get("q", "")
    try:
        r = requests.post("http://127.0.0.1:8000/ask", json={"q": q}, timeout=5)
        return jsonify(r.json())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

if __name__ == "__main__":
    print("Frontend shim listening on http://127.0.0.1:8080")
    # If booking_pb2 cannot be imported, run with PYTHONPATH=. from repo root
    APP.run(host="127.0.0.1", port=8080, debug=True)