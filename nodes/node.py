# nodes/node.py
import time
import json
import threading
import random
import sys
import os
import hashlib
from concurrent import futures
import grpc
import booking_pb2, booking_pb2_grpc

# -----------------------
# Simple persistent storage
# -----------------------
class Storage:
    def __init__(self, path):
        self.path = path
        try:
            with open(self.path, "r") as fh:
                payload = json.load(fh)
            self.log = payload.get("log", [])
            self.seats = payload.get("seats", {})
            self.sessions = payload.get("sessions", {})
            self.users = payload.get("users", {})
            self.term = payload.get("currentTerm", 0)
            self.voted_for = payload.get("votedFor", None)
        except Exception:
            # default initial state
            self.log = []
            self.seats = {f"S{n}": {"reserved": False, "by": ""} for n in range(1, 11)}
            self.sessions = {}
            self.users = {}
            self.term = 0
            self.voted_for = None
            self._flush()

    def append(self, entry):
        self.log.append(entry)
        self._flush()

    def apply(self, entry):
        cmd = entry.get("command")
        data = json.loads(entry.get("data"))
        if cmd == "reserve":
            sid = data["seat_id"]; who = data["client_id"]
            if sid in self.seats and not self.seats[sid]["reserved"]:
                self.seats[sid]["reserved"] = True
                self.seats[sid]["by"] = who
        elif cmd == "cancel":
            sid = data["seat_id"]; who = data["client_id"]
            if sid in self.seats and self.seats[sid]["reserved"] and self.seats[sid]["by"] == who:
                self.seats[sid]["reserved"] = False
                self.seats[sid]["by"] = ""
        self._flush()

    def _flush(self):
        with open(self.path, "w") as fh:
            json.dump({
                "log": self.log,
                "seats": self.seats,
                "sessions": self.sessions,
                "users": self.users,
                "currentTerm": self.term,
                "votedFor": self.voted_for
            }, fh, indent=2)

    # password helpers (salted sha256)
    def create_user(self, username, password_plain):
        salt = os.urandom(8).hex()
        h = hashlib.sha256((salt + password_plain).encode("utf-8")).hexdigest()
        self.users = getattr(self, "users", {})
        self.users[username] = {"salt": salt, "pw_hash": h}
        self._flush()

    def check_user(self, username, password_plain):
        u = getattr(self, "users", {}).get(username)
        if not u:
            return False
        if "salt" in u and "pw_hash" in u:
            expected = u["pw_hash"]
            return hashlib.sha256((u["salt"] + password_plain).encode("utf-8")).hexdigest() == expected
        if "password_hash" in u:
            return hashlib.sha256(password_plain.encode("utf-8")).hexdigest() == u["password_hash"]
        return False


# -----------------------
# Node implementation (Raft-ish)
# -----------------------
class BookingNode(booking_pb2_grpc.ClientAPIServicer, booking_pb2_grpc.RaftServicer):
    def __init__(self, node_name, peers):
        self.node_name = node_name
        self.peers = peers  # list of tuples (id, addr)
        self.store = Storage(f"node_{node_name}.json")
        self.lock = threading.Lock()

        # restore state machine
        try:
            for e in list(self.store.log):
                self.store.apply(e)
        except Exception:
            pass

        # raft runtime
        self.commit_index = 0
        self.last_applied = 0

        self.role = "follower"   # follower | candidate | leader
        self.votes = 0

        # timers and intervals
        self.election_min = 0.7
        self.election_max = 1.4
        self._reset_election_deadline()
        self.heartbeat_interval = 0.25

        # replication bookkeeping
        last_idx = len(self.store.log)
        self.next_index = {pid: last_idx + 1 for pid, _ in self.peers}
        self.match_index = {pid: 0 for pid, _ in self.peers}

        self._stop = False
        threading.Thread(target=self._election_runner, daemon=True).start()

    # election helpers
    def _reset_election_deadline(self):
        self._deadline = time.time() + random.uniform(self.election_min, self.election_max)

    def _election_runner(self):
        while not getattr(self, "_stop", False):
            time.sleep(0.05)
            if self.role != "leader" and time.time() >= getattr(self, "_deadline", 0):
                self._begin_election()

    def _begin_election(self):
        with self.lock:
            self.role = "candidate"
            self.store.term += 1
            self.store.voted_for = self.node_name
            self.store._flush()
            term = self.store.term
            self.votes = 1
            self._reset_election_deadline()
            print(f"[RAFT] {self.node_name} initiating election term={term}")

        last_idx = len(self.store.log)
        last_term = self.store.log[-1]["term"] if self.store.log else 0

        for pid, addr in self.peers:
            if pid == self.node_name:
                continue
            try:
                ch = grpc.insecure_channel(addr)
                stub = booking_pb2_grpc.RaftStub(ch)
                req = booking_pb2.RequestVoteArgs(
                    term=term,
                    candidateId=self.node_name,
                    lastLogIndex=last_idx,
                    lastLogTerm=last_term
                )
                resp = stub.RequestVote(req, timeout=1)
                if getattr(resp, "voteGranted", False):
                    self.votes += 1
                if getattr(resp, "term", 0) > term:
                    with self.lock:
                        self.store.term = resp.term
                        self.store.voted_for = None
                        self.store._flush()
                        self.role = "follower"
                        self._reset_election_deadline()
                    print(f"[RAFT] {self.node_name} stepping down: discovered term {resp.term}")
                    return
            except Exception:
                # ignore unreachable peers
                pass

        if self.votes >= (len(self.peers) // 2) + 1:
            with self.lock:
                self.role = "leader"
                lastIndex = len(self.store.log)
                for pid, _ in self.peers:
                    self.next_index[pid] = lastIndex + 1
                    self.match_index[pid] = 0
            print(f"[RAFT] {self.node_name} elected leader term={self.store.term}")
            threading.Thread(target=self._leader_heartbeat, daemon=True).start()

    def _leader_heartbeat(self):
        while self.role == "leader" and not getattr(self, "_stop", False):
            for pid, addr in self.peers:
                if pid == self.node_name:
                    continue
                try:
                    ch = grpc.insecure_channel(addr)
                    stub = booking_pb2_grpc.RaftStub(ch)
                    prev_idx = len(self.store.log)
                    prev_term = self.store.log[-1]["term"] if self.store.log else 0
                    args = booking_pb2.AppendEntriesArgs(
                        term=self.store.term,
                        leaderId=self.node_name,
                        prevLogIndex=prev_idx,
                        prevLogTerm=prev_term,
                        entries=[],
                        leaderCommit=self.commit_index
                    )
                    resp = stub.AppendEntries(args, timeout=1)
                    if getattr(resp, "term", 0) > self.store.term:
                        with self.lock:
                            self.store.term = resp.term
                            self.store.voted_for = None
                            self.store._flush()
                            self.role = "follower"
                            self._reset_election_deadline()
                        print(f"[RAFT] {self.node_name} stepping down (higher term {resp.term})")
                        return
                except Exception:
                    pass
            time.sleep(self.heartbeat_interval)

    # ---- auth helpers ----
    def _issue_token(self, user, ttl=3600):
        token = f"tok-{user}-{int(time.time())}"
        expiry = time.time() + ttl
        self.store.sessions[token] = {"username": user, "expiry": expiry}
        self.store._flush()
        return token

    def _validate_token(self, token):
        if not token or token not in self.store.sessions:
            return None
        sess = self.store.sessions[token]
        if sess["expiry"] < time.time():
            del self.store.sessions[token]
            self.store._flush()
            return None
        return sess["username"]

    # -----------------------
    # Client RPCs (gRPC)
    # -----------------------
    def Login(self, request, context):
        user = (request.username or "guest").strip()
        pwd = getattr(request, "password", "") or ""
        if not self.store.check_user(user, pwd):
            print(f"[AUTH] login failed for {user}")
            return booking_pb2.LoginResponse(status=1, token="")
        token = self._issue_token(user)
        print(f"[AUTH] {user} logged in, token={token}")
        return booking_pb2.LoginResponse(status=0, token=token)

    def GetSeats(self, request, context):
        if not self._validate_token(request.token):
            return booking_pb2.GetResponse(status=1, seats=[])
        items = []
        for sid, info in self.store.seats.items():
            items.append(booking_pb2.Seat(seat_id=sid, reserved=info["reserved"], reserved_by=info["by"]))
        return booking_pb2.GetResponse(status=0, seats=items)

    def ReserveSeat(self, request, context):
        user = self._validate_token(request.token)
        if not user:
            return booking_pb2.ReserveReply(code=5, msg="UNAUTHENTICATED")

        with self.lock:
            if self.role != "leader":
                # reply with NOT_LEADER:<node_id> so client can retry
                return booking_pb2.ReserveReply(code=1, msg=f"NOT_LEADER:{self.node_name}")

            sid = request.seat_id
            s = self.store.seats.get(sid)
            if s is None:
                return booking_pb2.ReserveReply(code=2, msg="NO_SUCH_SEAT")
            if s["reserved"]:
                return booking_pb2.ReserveReply(code=3, msg=f"ALREADY_RESERVED_BY:{s['by']}")

            entry = {
                "index": len(self.store.log) + 1,
                "term": self.store.term,
                "command": "reserve",
                "data": json.dumps({"seat_id": sid, "client_id": user})
            }
            self.store.append(entry)

            # replicate to followers (simple nextIndex/backoff)
            success_count = 1
            for pid, addr in self.peers:
                if pid == self.node_name:
                    continue
                try:
                    ni = self.next_index.get(pid, len(self.store.log))
                    while ni > 0:
                        prev_idx = ni - 1
                        prev_term = self.store.log[prev_idx - 1]["term"] if prev_idx > 0 and len(self.store.log) >= prev_idx else 0
                        batch = []
                        for e in self.store.log[ni - 1:]:
                            le = booking_pb2.LogEntry(index=e["index"], term=e["term"], command=e["command"], data=e["data"])
                            batch.append(le)
                        ch = grpc.insecure_channel(addr)
                        stub = booking_pb2_grpc.RaftStub(ch)
                        args = booking_pb2.AppendEntriesArgs(
                            term=self.store.term,
                            leaderId=self.node_name,
                            prevLogIndex=prev_idx,
                            prevLogTerm=prev_term,
                            entries=batch,
                            leaderCommit=self.commit_index
                        )
                        resp = stub.AppendEntries(args, timeout=2)
                        if getattr(resp, "term", 0) > self.store.term:
                            with self.lock:
                                self.store.term = resp.term
                                self.store.voted_for = None
                                self.store._flush()
                                self.role = "follower"
                                self._reset_election_deadline()
                            break
                        if getattr(resp, "success", False):
                            self.match_index[pid] = getattr(resp, "matchIndex", len(self.store.log))
                            self.next_index[pid] = self.match_index[pid] + 1
                            success_count += 1
                            break
                        else:
                            mi = getattr(resp, "matchIndex", None)
                            if isinstance(mi, int) and mi >= 0:
                                ni = mi + 1
                            else:
                                ni = max(1, ni - 1)
                            self.next_index[pid] = ni
                except Exception as exc:
                    print("[RAFT] replicate error", pid, exc)

            if success_count >= (len(self.peers) // 2) + 1:
                self.commit_index = entry["index"]
                self.store.apply(entry)
                self.match_index[self.node_name] = entry["index"]
                return booking_pb2.ReserveReply(code=0, msg="RESERVED")
            else:
                return booking_pb2.ReserveReply(code=4, msg="REPLICATION_FAILED")

    def CancelSeat(self, request, context):
        user = self._validate_token(request.token)
        if not user:
            return booking_pb2.Status(code=5, msg="UNAUTHENTICATED")

        with self.lock:
            if self.role != "leader":
                return booking_pb2.Status(code=1, msg=f"NOT_LEADER:{self.node_name}")

            sid = request.seat_id
            s = self.store.seats.get(sid)
            if s is None:
                return booking_pb2.Status(code=2, msg="NO_SUCH_SEAT")
            if not s["reserved"]:
                return booking_pb2.Status(code=3, msg="SEAT_NOT_RESERVED")
            if s["by"] != user:
                return booking_pb2.Status(code=4, msg=f"NOT_OWNER:{s['by']}")

            entry = {
                "index": len(self.store.log) + 1,
                "term": self.store.term,
                "command": "cancel",
                "data": json.dumps({"seat_id": sid, "client_id": user})
            }
            self.store.append(entry)

            success_count = 1
            for pid, addr in self.peers:
                if pid == self.node_name:
                    continue
                try:
                    ni = self.next_index.get(pid, len(self.store.log))
                    while ni > 0:
                        prev_idx = ni - 1
                        prev_term = self.store.log[prev_idx - 1]["term"] if prev_idx > 0 and len(self.store.log) >= prev_idx else 0
                        batch = []
                        for e in self.store.log[ni - 1:]:
                            le = booking_pb2.LogEntry(index=e["index"], term=e["term"], command=e["command"], data=e["data"])
                            batch.append(le)
                        ch = grpc.insecure_channel(addr)
                        stub = booking_pb2_grpc.RaftStub(ch)
                        args = booking_pb2.AppendEntriesArgs(
                            term=self.store.term,
                            leaderId=self.node_name,
                            prevLogIndex=prev_idx,
                            prevLogTerm=prev_term,
                            entries=batch,
                            leaderCommit=self.commit_index
                        )
                        resp = stub.AppendEntries(args, timeout=2)
                        if getattr(resp, "term", 0) > self.store.term:
                            with self.lock:
                                self.store.term = resp.term
                                self.store.voted_for = None
                                self.store._flush()
                                self.role = "follower"
                                self._reset_election_deadline()
                            break
                        if getattr(resp, "success", False):
                            self.match_index[pid] = getattr(resp, "matchIndex", len(self.store.log))
                            self.next_index[pid] = self.match_index[pid] + 1
                            success_count += 1
                            break
                        else:
                            mi = getattr(resp, "matchIndex", None)
                            if isinstance(mi, int) and mi >= 0:
                                ni = mi + 1
                            else:
                                ni = max(1, ni - 1)
                            self.next_index[pid] = ni
                except Exception as exc:
                    print("[RAFT] replicate error during cancel", pid, exc)

            if success_count >= (len(self.peers) // 2) + 1:
                self.commit_index = entry["index"]
                self.store.apply(entry)
                self.match_index[self.node_name] = entry["index"]
                return booking_pb2.Status(code=0, msg="CANCEL_OK")
            else:
                return booking_pb2.Status(code=6, msg="CANCEL_REPLICATION_FAILED")

    # -----------------------
    # Raft RPC handlers (followers)
    # -----------------------
    def RequestVote(self, request, context):
        with self.lock:
            if request.term < self.store.term:
                return booking_pb2.RequestVoteReply(term=self.store.term, voteGranted=False)

            if request.term > self.store.term:
                self.store.term = request.term
                self.store.voted_for = None
                self.store._flush()
                self.role = "follower"

            if self.store.voted_for is None or self.store.voted_for == request.candidateId:
                last_idx = len(self.store.log)
                last_term = self.store.log[-1]["term"] if self.store.log else 0
                if (request.lastLogTerm > last_term) or (request.lastLogTerm == last_term and request.lastLogIndex >= last_idx):
                    self.store.voted_for = request.candidateId
                    self.store._flush()
                    self._reset_election_deadline()
                    return booking_pb2.RequestVoteReply(term=self.store.term, voteGranted=True)
            return booking_pb2.RequestVoteReply(term=self.store.term, voteGranted=False)

    def AppendEntries(self, request, context):
        with self.lock:
            if request.term < self.store.term:
                return booking_pb2.AppendEntriesReply(term=self.store.term, success=False, matchIndex=len(self.store.log))

            if request.term > self.store.term:
                self.store.term = request.term
                self.store.voted_for = None
                self.store._flush()
                self.role = "follower"

            # heard from leader -> reset election timeout
            self._reset_election_deadline()

            local_len = len(self.store.log)
            if request.prevLogIndex > local_len:
                return booking_pb2.AppendEntriesReply(term=self.store.term, success=False, matchIndex=local_len)

            if request.prevLogIndex > 0:
                local_prev_term = self.store.log[request.prevLogIndex - 1]["term"]
                if local_prev_term != request.prevLogTerm:
                    self.store.log = self.store.log[:request.prevLogIndex - 1]
                    self.store._flush()
                    return booking_pb2.AppendEntriesReply(term=self.store.term, success=False, matchIndex=len(self.store.log))

            for e in request.entries:
                if len(self.store.log) >= e.index:
                    continue
                entry = {"index": e.index, "term": e.term, "command": e.command, "data": e.data}
                self.store.append(entry)

            while self.last_applied < request.leaderCommit and self.last_applied < len(self.store.log):
                self.store.apply(self.store.log[self.last_applied])
                self.last_applied += 1

            return booking_pb2.AppendEntriesReply(term=self.store.term, success=True, matchIndex=len(self.store.log))


# -----------------------
# Server bootstrap
# -----------------------
def serve(name, addr, peers):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    svc = BookingNode(name, peers)
    booking_pb2_grpc.add_ClientAPIServicer_to_server(svc, server)
    booking_pb2_grpc.add_RaftServicer_to_server(svc, server)
    server.add_insecure_port(addr)
    server.start()
    print(f"Node {name} listening on {addr} (role={svc.role})")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Shutting down", name)
        svc._stop = True
        server.stop(0)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python nodes/node.py <node_id> <host:port> [peerlist]")
        sys.exit(1)
    node_id = sys.argv[1]
    hostport = sys.argv[2]
    peers_arg = sys.argv[3] if len(sys.argv) > 3 else ""
    peers = []
    if peers_arg:
        for token in peers_arg.split(","):
            pid, a = token.split("=", 1)
            peers.append((pid, a))
    if not any(p[0] == node_id for p in peers):
        peers.append((node_id, hostport))
    serve(node_id, hostport, peers)