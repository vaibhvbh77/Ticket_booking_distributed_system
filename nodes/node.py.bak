
import time, json, threading, uuid, sys
from concurrent import futures
import grpc
import booking_pb2, booking_pb2_grpc


class Persist:
    def __init__(self, filename):
        self.fn = filename
        try:
            with open(self.fn, "r") as f:
                data = json.load(f)
            self.log = data.get("log", [])
            self.seats = data.get("seats", {})
            self.sessions = data.get("sessions", {})   # store tokens
            self.currentTerm = data.get("currentTerm", 0)
            self.votedFor = data.get("votedFor", None)
        except Exception:
            self.log = []
            self.seats = {f"S{n}": {"reserved": False, "by": ""} for n in range(1, 11)}
            self.sessions = {}
            self.currentTerm = 0
            self.votedFor = None
            self._persist()

    def append_log(self, entry):
        self.log.append(entry)
        self._persist()

    def apply_log(self, entry):
        cmd = entry.get("command")
        data = json.loads(entry.get("data"))
        if cmd == "reserve":
            seat = data["seat_id"]
            client = data["client_id"]
            if not self.seats[seat]["reserved"]:
                self.seats[seat]["reserved"] = True
                self.seats[seat]["by"] = client
        elif cmd == "cancel":
            seat = data["seat_id"]
            client = data["client_id"]
            if self.seats[seat]["reserved"] and self.seats[seat]["by"] == client:
                self.seats[seat]["reserved"] = False
                self.seats[seat]["by"] = ""
        self._persist()

    def _persist(self):
        with open(self.fn, "w") as f:
            json.dump({
                "log": self.log,
                "seats": self.seats,
                "sessions": self.sessions,
                "currentTerm": self.currentTerm,
                "votedFor": self.votedFor
            }, f, indent=2)


class NodeServicer(booking_pb2_grpc.ClientAPIServicer, booking_pb2_grpc.RaftServicer):
    def __init__(self, node_id, peers):
        self.node_id = node_id
        self.peers = peers
        self.persist = Persist(f"node_{node_id}.json")
        self.lock = threading.Lock()

        # rebuild from logs
        for e in list(self.persist.log):
            self.persist.apply_log(e)

        self.commitIndex = 0
        self.lastApplied = 0
        self.leader_id = self.peers[0][0] if self.peers else self.node_id

    # --- Helpers ---
    def _create_token(self, username, ttl=3600):
        token = f"tok-{uuid.uuid4().hex[:8]}"
        expiry = time.time() + ttl
        self.persist.sessions[token] = {"username": username, "expiry": expiry}
        self.persist._persist()
        return token

    def _validate_token(self, token):
        if not token or token not in self.persist.sessions:
            return None
        sess = self.persist.sessions[token]
        if sess["expiry"] < time.time():
            del self.persist.sessions[token]
            self.persist._persist()
            return None
        return sess["username"]

    # --- Client API ---
    def Login(self, request, context):
        user = request.username or "guest"
        token = self._create_token(user)
        return booking_pb2.LoginResponse(status=0, token=token)

    def GetSeats(self, request, context):
        if not self._validate_token(request.token):
            return booking_pb2.GetResponse(status=1, seats=[])
        seats = [
            booking_pb2.Seat(seat_id=k, reserved=v["reserved"], reserved_by=v["by"])
            for k, v in self.persist.seats.items()
        ]
        return booking_pb2.GetResponse(status=0, seats=seats)

    def ReserveSeat(self, request, context):
        user = self._validate_token(request.token)
        if not user:
            return booking_pb2.ReserveReply(code=5, msg="UNAUTHENTICATED")

        with self.lock:
            if self.node_id != self.leader_id:
                return booking_pb2.ReserveReply(code=1, msg=f"NOT_LEADER:{self.leader_id}")

            seat_id = request.seat_id
            s = self.persist.seats.get(seat_id)
            if s is None:
                return booking_pb2.ReserveReply(code=2, msg="NO_SUCH_SEAT")
            if s["reserved"]:
                return booking_pb2.ReserveReply(code=3, msg=f"ALREADY_RESERVED_BY:{s['by']}")

            entry = {
                "index": len(self.persist.log) + 1,
                "term": self.persist.currentTerm,
                "command": "reserve",
                "data": json.dumps({"seat_id": seat_id, "client_id": user})
            }
            self.persist.append_log(entry)

            successes = 1
            for pid, addr in self.peers:
                if pid == self.node_id:
                    continue
                try:
                    channel = grpc.insecure_channel(addr)
                    stub = booking_pb2_grpc.RaftStub(channel)
                    le = booking_pb2.LogEntry(
                        index=entry["index"], term=entry["term"],
                        command=entry["command"], data=entry["data"]
                    )
                    args = booking_pb2.AppendEntriesArgs(
                        term=self.persist.currentTerm,
                        leaderId=self.node_id,
                        prevLogIndex=entry["index"] - 1,
                        prevLogTerm=0,
                        entries=[le],
                        leaderCommit=entry["index"]
                    )
                    resp = stub.AppendEntries(args, timeout=2)
                    if resp.success:
                        successes += 1
                except Exception as e:
                    print("replicate error", pid, e)

            if successes >= (len(self.peers) // 2) + 1:
                self.commitIndex = entry["index"]
                self.persist.apply_log(entry)
                return booking_pb2.ReserveReply(code=0, msg="RESERVED")
            else:
                return booking_pb2.ReserveReply(code=4, msg="REPLICATION_FAILED")

    def CancelSeat(self, request, context):
        user = self._validate_token(request.token)
        if not user:
            return booking_pb2.Status(code=5, msg="UNAUTHENTICATED")

        with self.lock:
            if self.node_id != self.leader_id:
                return booking_pb2.Status(code=1, msg=f"NOT_LEADER:{self.leader_id}")

            seat_id = request.seat_id
            s = self.persist.seats.get(seat_id)
            if s is None:
                return booking_pb2.Status(code=2, msg="NO_SUCH_SEAT")

            if not s["reserved"]:
                return booking_pb2.Status(code=3, msg="SEAT_NOT_RESERVED")

            if s["by"] != user:
                return booking_pb2.Status(code=4, msg=f"NOT_OWNER:{s['by']}")

            entry = {
                "index": len(self.persist.log) + 1,
                "term": self.persist.currentTerm,
                "command": "cancel",
                "data": json.dumps({"seat_id": seat_id, "client_id": user})
            }
            self.persist.append_log(entry)

            successes = 1
            for pid, addr in self.peers:
                if pid == self.node_id:
                    continue
                try:
                    channel = grpc.insecure_channel(addr)
                    stub = booking_pb2_grpc.RaftStub(channel)
                    le = booking_pb2.LogEntry(
                        index=entry["index"], term=entry["term"],
                        command=entry["command"], data=entry["data"]
                    )
                    args = booking_pb2.AppendEntriesArgs(
                        term=self.persist.currentTerm,
                        leaderId=self.node_id,
                        prevLogIndex=entry["index"] - 1,
                        prevLogTerm=0,
                        entries=[le],
                        leaderCommit=entry["index"]
                    )
                    resp = stub.AppendEntries(args, timeout=2)
                    if resp.success:
                        successes += 1
                except Exception as e:
                    print("replicate error during cancel", pid, e)

            if successes >= (len(self.peers) // 2) + 1:
                self.commitIndex = entry["index"]
                self.persist.apply_log(entry)
                return booking_pb2.Status(code=0, msg="CANCEL_OK")
            else:
                return booking_pb2.Status(code=6, msg="CANCEL_REPLICATION_FAILED")

    # --- Raft RPCs ---
    def RequestVote(self, request, context):
        with self.lock:
            if request.term >= self.persist.currentTerm:
                self.persist.currentTerm = request.term
                self.persist.votedFor = request.candidateId
                self.persist._persist()
                return booking_pb2.RequestVoteReply(term=self.persist.currentTerm, voteGranted=True)
            else:
                return booking_pb2.RequestVoteReply(term=self.persist.currentTerm, voteGranted=False)

    def AppendEntries(self, request, context):
        with self.lock:
            for e in request.entries:
                entry = {"index": e.index, "term": e.term, "command": e.command, "data": e.data}
                if len(self.persist.log) < entry["index"]:
                    self.persist.append_log(entry)
            while self.lastApplied < request.leaderCommit and self.lastApplied < len(self.persist.log):
                self.persist.apply_log(self.persist.log[self.lastApplied])
                self.lastApplied += 1
            return booking_pb2.AppendEntriesReply(term=self.persist.currentTerm, success=True, matchIndex=len(self.persist.log))


def serve(node_id, hostport, peers):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    servicer = NodeServicer(node_id, peers)
    booking_pb2_grpc.add_ClientAPIServicer_to_server(servicer, server)
    booking_pb2_grpc.add_RaftServicer_to_server(servicer, server)
    server.add_insecure_port(hostport)
    server.start()
    print(f"Node {node_id} started at {hostport} leader={servicer.leader_id}")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        server.stop(0)


if __name__ == "__main__":
    node_id = sys.argv[1]
    hostport = sys.argv[2]
    peers_arg = sys.argv[3] if len(sys.argv) > 3 else ""
    peers = []
    if peers_arg:
        for p in peers_arg.split(","):
            pid, addr = p.split("=")
            peers.append((pid, addr))
    if not any(p[0] == node_id for p in peers):
        peers.append((node_id, hostport))
    serve(node_id, hostport, peers)