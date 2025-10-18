# nodes/node.py
# Simple distributed ticket booking node (leader + followers)
import time
import json
import threading
import sys
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
            self.currentTerm = data.get("currentTerm", 0)
            self.votedFor = data.get("votedFor", None)
        except Exception:
            self.log = []
            self.seats = {f"S{n}": {"reserved": False, "by": ""} for n in range(1, 11)}
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
            seat = data["seat_id"]; client = data["client_id"]
            if not self.seats[seat]["reserved"]:
                self.seats[seat]["reserved"] = True
                self.seats[seat]["by"] = client
        elif cmd == "cancel":
            seat = data["seat_id"]; client = data["client_id"]
            if self.seats[seat]["reserved"] and self.seats[seat]["by"] == client:
                self.seats[seat]["reserved"] = False
                self.seats[seat]["by"] = ""
        self._persist()

    def _persist(self):
        with open(self.fn, "w") as f:
            json.dump({
                "log": self.log,
                "seats": self.seats,
                "currentTerm": self.currentTerm,
                "votedFor": self.votedFor
            }, f, indent=2)

class NodeServicer(booking_pb2_grpc.ClientAPIServicer, booking_pb2_grpc.RaftServicer):
    def __init__(self, node_id, peers):
        self.node_id = node_id
        self.peers = peers
        self.persist = Persist(f"node_{node_id}.json")
        self.lock = threading.Lock()

        # Replay persisted log entries so state is reconstructed on startup
        try:
            for e in list(self.persist.log):
                # each e is a dict: {"index": ..., "term": ..., "command": ..., "data": "..."}
                # apply_log is idempotent for our simple commands
                self.persist.apply_log(e)
        except Exception:
            pass

        self.commitIndex = 0
        self.lastApplied = 0
        self.leader_id = self.peers[0][0] if self.peers else self.node_id

    # ---- ClientAPI ----
    def Login(self, request, context):
        return booking_pb2.LoginResponse(status=0, token=f"token-{request.username}")

    def GetSeats(self, request, context):
        seats = []
        for sid, info in self.persist.seats.items():
            seats.append(booking_pb2.Seat(seat_id=sid, reserved=info["reserved"], reserved_by=info["by"]))
        return booking_pb2.GetResponse(status=0, seats=seats)

    def ReserveSeat(self, request, context):
        with self.lock:
            if self.node_id != self.leader_id:
                return booking_pb2.ReserveReply(code=1, msg=f"NOT_LEADER:{self.leader_id}")

            seat_id = request.seat_id
            client = request.client_id
            s = self.persist.seats.get(seat_id)
            if s is None:
                return booking_pb2.ReserveReply(code=2, msg="NO_SUCH_SEAT")
            if s["reserved"]:
                return booking_pb2.ReserveReply(code=3, msg=f"ALREADY_RESERVED_BY:{s['by']}")

            entry = {
                "index": len(self.persist.log) + 1,
                "term": self.persist.currentTerm,
                "command": "reserve",
                "data": json.dumps({"seat_id": seat_id, "client_id": client})
            }
            # append to leader's log
            self.persist.append_log(entry)

            # replicate to followers (wait for replies, count successes)
            successes = 1
            for pid, addr in self.peers:
                if pid == self.node_id:
                    continue
                try:
                    channel = grpc.insecure_channel(addr)
                    stub = booking_pb2_grpc.RaftStub(channel)
                    le = booking_pb2.LogEntry(index=entry["index"], term=entry["term"], command=entry["command"], data=entry["data"])
                    args = booking_pb2.AppendEntriesArgs(
                        term=self.persist.currentTerm,
                        leaderId=self.node_id,
                        prevLogIndex=entry["index"] - 1,
                        prevLogTerm=0,
                        entries=[le],
                        leaderCommit=entry["index"]  # ask followers to apply this entry (simplified)
                    )
                    resp = stub.AppendEntries(args, timeout=2)
                    if resp.success:
                        successes += 1
                except Exception as e:
                    print("replicate error", pid, e)

            # if majority -> commit and apply locally
            if successes >= (len(self.peers) // 2) + 1:
                self.commitIndex = entry["index"]
                self.persist.apply_log(entry)
                return booking_pb2.ReserveReply(code=0, msg="RESERVED")
            else:
                return booking_pb2.ReserveReply(code=4, msg="REPLICATION_FAILED")

    def CancelSeat(self, request, context):
        # For demo: simple immediate success (could follow ReserveSeat flow)
        return booking_pb2.Status(code=0, msg="CANCEL_OK")

    # ---- Raft RPCs ----
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
                # append if not already present
                if len(self.persist.log) >= entry["index"]:
                    pass
                else:
                    self.persist.append_log(entry)
            # apply up to leaderCommit
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
    # ensure self present
    if not any(p[0] == node_id for p in peers):
        peers.append((node_id, hostport))
    serve(node_id, hostport, peers)
