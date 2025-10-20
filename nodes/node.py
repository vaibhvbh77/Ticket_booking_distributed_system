# nodes/node.py
# Minimal Raft-enabled distributed ticket booking node (leader + followers)
# - leader election (RequestVote)
# - leader heartbeat (AppendEntries with empty entries)
# - nextIndex / matchIndex basic retry for replication
# NOTE: simplified for demo: persistent state kept in JSON (Persist)

import time
import json
import threading
import random
import sys
from concurrent import futures
import grpc
import booking_pb2, booking_pb2_grpc

# -----------------------
# Persistence helper
# -----------------------
class Persist:
    def __init__(self, filename):
        self.fn = filename
        try:
            with open(self.fn, "r") as f:
                data = json.load(f)
            self.log = data.get("log", [])
            self.seats = data.get("seats", {})
            self.sessions = data.get("sessions", {})   # tokens
            self.users = data.get("users", {})
            self.currentTerm = data.get("currentTerm", 0)
            self.votedFor = data.get("votedFor", None)
        except Exception:
            # initialize default state
            self.log = []
            self.seats = {f"S{n}": {"reserved": False, "by": ""} for n in range(1, 11)}
            self.sessions = {}
            self.users = {}
            self.currentTerm = 0
            self.votedFor = None
            self._persist()

    def append_log(self, entry):
        # entry is dict {index, term, command, data}
        self.log.append(entry)
        self._persist()

    def apply_log(self, entry):
        cmd = entry.get("command")
        data = json.loads(entry.get("data"))
        if cmd == "reserve":
            seat = data["seat_id"]; client = data["client_id"]
            if seat in self.seats and not self.seats[seat]["reserved"]:
                self.seats[seat]["reserved"] = True
                self.seats[seat]["by"] = client
        elif cmd == "cancel":
            seat = data["seat_id"]; client = data["client_id"]
            if seat in self.seats and self.seats[seat]["reserved"] and self.seats[seat]["by"] == client:
                self.seats[seat]["reserved"] = False
                self.seats[seat]["by"] = ""
        self._persist()

    def _persist(self):
        with open(self.fn, "w") as f:
            json.dump({
                "log": self.log,
                "seats": self.seats,
                "sessions": self.sessions,
                "users": self.users,
                "currentTerm": self.currentTerm,
                "votedFor": self.votedFor
            }, f, indent=2)

    # user helpers (simple hashed passwords not currently used heavily)
    def add_user(self, username, pw_record):
        self.users[username] = pw_record
        self._persist()


# -----------------------
# Node service (Raft + Client API)
# -----------------------
class NodeServicer(booking_pb2_grpc.ClientAPIServicer, booking_pb2_grpc.RaftServicer):
    def __init__(self, node_id, peers):
        self.node_id = node_id
        self.peers = peers  # list of (pid, addr)
        self.persist = Persist(f"node_{node_id}.json")
        self.lock = threading.Lock()

        # replay persisted log entries so state is reconstructed on startup
        try:
            for e in list(self.persist.log):
                self.persist.apply_log(e)
        except Exception:
            pass

        # Raft runtime fields
        self.commitIndex = 0
        self.lastApplied = 0

        # State: follower | candidate | leader
        self.state = 'follower'
        self.votes_received = 0

        # election timer parameters (seconds) - randomized per node
        self.election_timeout_min = 0.8
        self.election_timeout_max = 1.4
        self._reset_election_timer()

        # heartbeat interval for leader
        self.heartbeat_interval = 0.25

        # replication indices (updated when node becomes leader)
        # initialize with conservative defaults
        last_index = len(self.persist.log)
        self.nextIndex = {pid: last_index + 1 for pid, _ in self.peers}
        self.matchIndex = {pid: 0 for pid, _ in self.peers}

        # thread stop flag
        self._stop_threads = False

        # start election loop thread
        threading.Thread(target=self._election_loop, daemon=True).start()

    # -----------------------
    # Election timer helpers
    # -----------------------
    def _reset_election_timer(self):
        self._election_deadline = time.time() + random.uniform(self.election_timeout_min, self.election_timeout_max)

    def _election_loop(self):
        while not getattr(self, "_stop_threads", False):
            time.sleep(0.05)
            if self.state != 'leader' and time.time() >= getattr(self, "_election_deadline", 0):
                # start election
                self._start_election()

    # -----------------------
    # Start election (candidate)
    # -----------------------
    def _start_election(self):
        with self.lock:
            self.state = 'candidate'
            self.persist.currentTerm += 1
            self.persist.votedFor = self.node_id
            self.persist._persist()
            term = self.persist.currentTerm
            self.votes_received = 1  # vote for self
            self._reset_election_timer()
            print(f"[RAFT] {self.node_id} starting election term={term}")

        lastLogIndex = len(self.persist.log)
        lastLogTerm = self.persist.log[-1]['term'] if self.persist.log else 0

        for pid, addr in self.peers:
            if pid == self.node_id:
                continue
            try:
                channel = grpc.insecure_channel(addr)
                stub = booking_pb2_grpc.RaftStub(channel)
                req = booking_pb2.RequestVoteArgs(
                    term=term,
                    candidateId=self.node_id,
                    lastLogIndex=lastLogIndex,
                    lastLogTerm=lastLogTerm
                )
                resp = stub.RequestVote(req, timeout=1)
                if getattr(resp, "voteGranted", False):
                    self.votes_received += 1
                if getattr(resp, "term", 0) > term:
                    # discovered higher term -> step down
                    with self.lock:
                        self.persist.currentTerm = resp.term
                        self.persist.votedFor = None
                        self.persist._persist()
                        self.state = 'follower'
                        self._reset_election_timer()
                    print(f"[RAFT] {self.node_id} stepping down, discovered higher term {resp.term}")
                    return
            except Exception:
                # ignore unreachable peers
                pass

        # check if we won
        if self.votes_received >= (len(self.peers) // 2) + 1:
            with self.lock:
                self.state = 'leader'
                lastIndex = len(self.persist.log)
                for pid, _ in self.peers:
                    self.nextIndex[pid] = lastIndex + 1
                    self.matchIndex[pid] = 0
            print(f"[RAFT] {self.node_id} became leader term={self.persist.currentTerm}")
            # start heartbeat thread
            threading.Thread(target=self._leader_heartbeat_loop, daemon=True).start()

    # -----------------------
    # Leader heartbeat loop
    # -----------------------
    def _leader_heartbeat_loop(self):
        while self.state == 'leader' and not getattr(self, "_stop_threads", False):
            for pid, addr in self.peers:
                if pid == self.node_id:
                    continue
                try:
                    channel = grpc.insecure_channel(addr)
                    stub = booking_pb2_grpc.RaftStub(channel)
                    prevIndex = len(self.persist.log)
                    prevTerm = self.persist.log[-1]['term'] if self.persist.log else 0
                    args = booking_pb2.AppendEntriesArgs(
                        term=self.persist.currentTerm,
                        leaderId=self.node_id,
                        prevLogIndex=prevIndex,
                        prevLogTerm=prevTerm,
                        entries=[],
                        leaderCommit=self.commitIndex
                    )
                    resp = stub.AppendEntries(args, timeout=1)
                    if getattr(resp, "term", 0) > self.persist.currentTerm:
                        with self.lock:
                            self.persist.currentTerm = resp.term
                            self.persist.votedFor = None
                            self.persist._persist()
                            self.state = 'follower'
                            self._reset_election_timer()
                        print(f"[RAFT] {self.node_id} stepping down due to higher term {resp.term}")
                        return
                except Exception:
                    # network/follower error - ignore for heartbeat
                    pass
            time.sleep(self.heartbeat_interval)

    # -----------------------
    # Authentication helpers (simple)
    # -----------------------
    def _create_token(self, username, ttl=3600):
        token = f"tok-{username}-{int(time.time())}"
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

    # -----------------------
    # Client API RPCs
    # -----------------------
    def Login(self, request, context):
        # simple username/password check omitted for brevity
        username = request.username or "guest"
        token = self._create_token(username)
        return booking_pb2.LoginResponse(status=0, token=token)

    def GetSeats(self, request, context):
        if not self._validate_token(request.token):
            return booking_pb2.GetResponse(status=1, seats=[])
        seats = []
        for sid, info in self.persist.seats.items():
            seats.append(booking_pb2.Seat(seat_id=sid, reserved=info["reserved"], reserved_by=info["by"]))
        return booking_pb2.GetResponse(status=0, seats=seats)

    def ReserveSeat(self, request, context):
        user = self._validate_token(request.token)
        if not user:
            return booking_pb2.ReserveReply(code=5, msg="UNAUTHENTICATED")

        with self.lock:
            # must be leader to handle client requests
            if self.state != 'leader':
                # return leader id so client can retry (we give node id; client may use mapping)
                leader_id = None
                for pid, addr in self.peers:
                    if pid == self.node_id:
                        leader_id = addr
                        break
                return booking_pb2.ReserveReply(code=1, msg=f"NOT_LEADER:{self.node_id}")

            seat_id = request.seat_id
            client = user
            s = self.persist.seats.get(seat_id)
            if s is None:
                return booking_pb2.ReserveReply(code=2, msg="NO_SUCH_SEAT")
            if s["reserved"]:
                return booking_pb2.ReserveReply(code=3, msg=f"ALREADY_RESERVED_BY:{s['by']}")

            # create new log entry
            entry = {
                "index": len(self.persist.log) + 1,
                "term": self.persist.currentTerm,
                "command": "reserve",
                "data": json.dumps({"seat_id": seat_id, "client_id": client})
            }
            # append to leader's log
            self.persist.append_log(entry)

            # replicate to followers using nextIndex/matchIndex simple retry
            successes = 1  # count leader itself
            for pid, addr in self.peers:
                if pid == self.node_id:
                    continue
                try:
                    # ensure nextIndex exists
                    ni = self.nextIndex.get(pid, len(self.persist.log))
                    # try to send entries starting from ni; if fail, decrement ni and retry
                    while ni > 0:
                        prevIndex = ni - 1
                        prevTerm = self.persist.log[prevIndex - 1]['term'] if prevIndex > 0 and len(self.persist.log) >= prevIndex else 0
                        entries_to_send = []
                        # build LogEntry protos from leader log from ni..end
                        for e in self.persist.log[ni - 1:]:
                            le = booking_pb2.LogEntry(index=e['index'], term=e['term'], command=e['command'], data=e['data'])
                            entries_to_send.append(le)

                        channel = grpc.insecure_channel(addr)
                        stub = booking_pb2_grpc.RaftStub(channel)
                        args = booking_pb2.AppendEntriesArgs(
                            term=self.persist.currentTerm,
                            leaderId=self.node_id,
                            prevLogIndex=prevIndex,
                            prevLogTerm=prevTerm,
                            entries=entries_to_send,
                            leaderCommit=self.commitIndex
                        )
                        resp = stub.AppendEntries(args, timeout=2)
                        # if follower reports higher term, step down
                        if getattr(resp, "term", 0) > self.persist.currentTerm:
                            with self.lock:
                                self.persist.currentTerm = resp.term
                                self.persist.votedFor = None
                                self.persist._persist()
                                self.state = 'follower'
                                self._reset_election_timer()
                            break
                        if getattr(resp, "success", False):
                            # success - update indices
                            self.matchIndex[pid] = getattr(resp, "matchIndex", len(self.persist.log))
                            self.nextIndex[pid] = self.matchIndex[pid] + 1
                            successes += 1
                            break
                        else:
                            # follower rejected, back off nextIndex and retry
                            mi = getattr(resp, "matchIndex", None)
                            if mi is not None and isinstance(mi, int) and mi >= 0:
                                ni = mi + 1
                            else:
                                ni = max(1, ni - 1)
                            self.nextIndex[pid] = ni
                    # end while
                except Exception as e:
                    print("[RAFT] replicate error", pid, e)

            # commit if majority replicated
            if successes >= (len(self.peers) // 2) + 1:
                self.commitIndex = entry["index"]
                # apply locally
                self.persist.apply_log(entry)
                # update matchIndex for leader itself
                self.matchIndex[self.node_id] = entry["index"]
                return booking_pb2.ReserveReply(code=0, msg="RESERVED")
            else:
                # replication failed
                return booking_pb2.ReserveReply(code=4, msg="REPLICATION_FAILED")

    def CancelSeat(self, request, context):
        user = self._validate_token(request.token)
        if not user:
            return booking_pb2.Status(code=5, msg="UNAUTHENTICATED")

        with self.lock:
            if self.state != 'leader':
                return booking_pb2.Status(code=1, msg=f"NOT_LEADER:{self.node_id}")

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
                    ni = self.nextIndex.get(pid, len(self.persist.log))
                    while ni > 0:
                        prevIndex = ni - 1
                        prevTerm = self.persist.log[prevIndex - 1]['term'] if prevIndex > 0 and len(self.persist.log) >= prevIndex else 0
                        entries_to_send = []
                        for e in self.persist.log[ni - 1:]:
                            le = booking_pb2.LogEntry(index=e['index'], term=e['term'], command=e['command'], data=e['data'])
                            entries_to_send.append(le)

                        channel = grpc.insecure_channel(addr)
                        stub = booking_pb2_grpc.RaftStub(channel)
                        args = booking_pb2.AppendEntriesArgs(
                            term=self.persist.currentTerm,
                            leaderId=self.node_id,
                            prevLogIndex=prevIndex,
                            prevLogTerm=prevTerm,
                            entries=entries_to_send,
                            leaderCommit=self.commitIndex
                        )
                        resp = stub.AppendEntries(args, timeout=2)
                        if getattr(resp, "term", 0) > self.persist.currentTerm:
                            with self.lock:
                                self.persist.currentTerm = resp.term
                                self.persist.votedFor = None
                                self.persist._persist()
                                self.state = 'follower'
                                self._reset_election_timer()
                            break
                        if getattr(resp, "success", False):
                            self.matchIndex[pid] = getattr(resp, "matchIndex", len(self.persist.log))
                            self.nextIndex[pid] = self.matchIndex[pid] + 1
                            successes += 1
                            break
                        else:
                            mi = getattr(resp, "matchIndex", None)
                            if mi is not None and isinstance(mi, int) and mi >= 0:
                                ni = mi + 1
                            else:
                                ni = max(1, ni - 1)
                            self.nextIndex[pid] = ni
                except Exception as e:
                    print("[RAFT] replicate error during cancel", pid, e)

            if successes >= (len(self.peers) // 2) + 1:
                self.commitIndex = entry["index"]
                self.persist.apply_log(entry)
                self.matchIndex[self.node_id] = entry["index"]
                return booking_pb2.Status(code=0, msg="CANCEL_OK")
            else:
                return booking_pb2.Status(code=6, msg="CANCEL_REPLICATION_FAILED")

    # -----------------------
    # Raft RPCs (follower handlers)
    # -----------------------
    def RequestVote(self, request, context):
        with self.lock:
            # reject if candidate term < current
            if request.term < self.persist.currentTerm:
                return booking_pb2.RequestVoteReply(term=self.persist.currentTerm, voteGranted=False)

            # if candidate term > currentTerm, update and clear vote
            if request.term > self.persist.currentTerm:
                self.persist.currentTerm = request.term
                self.persist.votedFor = None
                self.persist._persist()
                self.state = 'follower'

            # if not voted or voted for candidate, check up-to-date
            if self.persist.votedFor is None or self.persist.votedFor == request.candidateId:
                lastLogIndex = len(self.persist.log)
                lastLogTerm = self.persist.log[-1]['term'] if self.persist.log else 0
                # up-to-date verification (term then index)
                if (request.lastLogTerm > lastLogTerm) or (request.lastLogTerm == lastLogTerm and request.lastLogIndex >= lastLogIndex):
                    self.persist.votedFor = request.candidateId
                    self.persist._persist()
                    self._reset_election_timer()
                    return booking_pb2.RequestVoteReply(term=self.persist.currentTerm, voteGranted=True)

            return booking_pb2.RequestVoteReply(term=self.persist.currentTerm, voteGranted=False)

    def AppendEntries(self, request, context):
        with self.lock:
            # reject if leader term < currentTerm
            if request.term < self.persist.currentTerm:
                return booking_pb2.AppendEntriesReply(term=self.persist.currentTerm, success=False, matchIndex=len(self.persist.log))

            # if leader's term is newer, update our term and step down
            if request.term > self.persist.currentTerm:
                self.persist.currentTerm = request.term
                self.persist.votedFor = None
                self.persist._persist()
                self.state = 'follower'

            # reset election timer since we heard from leader
            self._reset_election_timer()

            local_len = len(self.persist.log)
            # if prevLogIndex > local length, we are missing earlier entries -> reply false
            if request.prevLogIndex > local_len:
                return booking_pb2.AppendEntriesReply(term=self.persist.currentTerm, success=False, matchIndex=local_len)

            # if prevLogIndex > 0 check term
            if request.prevLogIndex > 0:
                local_prev_term = self.persist.log[request.prevLogIndex - 1]['term']
                if local_prev_term != request.prevLogTerm:
                    # conflict - truncate log at prevLogIndex-1
                    self.persist.log = self.persist.log[:request.prevLogIndex - 1]
                    self.persist._persist()
                    return booking_pb2.AppendEntriesReply(term=self.persist.currentTerm, success=False, matchIndex=len(self.persist.log))

            # append entries that we don't already have
            for e in request.entries:
                if len(self.persist.log) >= e.index:
                    # already have; skip
                    continue
                entry = {"index": e.index, "term": e.term, "command": e.command, "data": e.data}
                self.persist.append_log(entry)

            # apply to state machine up to leaderCommit
            while self.lastApplied < request.leaderCommit and self.lastApplied < len(self.persist.log):
                self.persist.apply_log(self.persist.log[self.lastApplied])
                self.lastApplied += 1

            return booking_pb2.AppendEntriesReply(term=self.persist.currentTerm, success=True, matchIndex=len(self.persist.log))


# -----------------------
# Server bootstrap
# -----------------------
def serve(node_id, hostport, peers):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    servicer = NodeServicer(node_id, peers)
    booking_pb2_grpc.add_ClientAPIServicer_to_server(servicer, server)
    booking_pb2_grpc.add_RaftServicer_to_server(servicer, server)
    server.add_insecure_port(hostport)
    server.start()
    print(f"Node {node_id} started at {hostport} state={servicer.state}")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Shutting down node", node_id)
        servicer._stop_threads = True
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
        for p in peers_arg.split(","):
            pid, addr = p.split("=")
            peers.append((pid, addr))
    # ensure self present
    if not any(p[0] == node_id for p in peers):
        peers.append((node_id, hostport))
    serve(node_id, hostport, peers)