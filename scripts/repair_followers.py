# scripts/repair_followers.py
# Usage: PYTHONPATH=. python3 scripts/repair_followers.py leader_json follower_addr1 follower_addr2
# Example:
# PYTHONPATH=. python3 scripts/repair_followers.py node_node1.json 127.0.0.1:60052 127.0.0.1:60053

import sys, json, time
import grpc
import booking_pb2, booking_pb2_grpc

def get_follower_matchindex(addr):
    ch = grpc.insecure_channel(addr)
    stub = booking_pb2_grpc.RaftStub(ch)
    # send empty AppendEntries to probe matchIndex
    args = booking_pb2.AppendEntriesArgs(term=0, leaderId="repair", prevLogIndex=0, prevLogTerm=0, entries=[], leaderCommit=0)
    try:
        resp = stub.AppendEntries(args, timeout=3)
        return getattr(resp, "matchIndex", 0)
    except Exception as e:
        print("Probe error for", addr, "->", e)
        return None

def push_entry(addr, le_index, le_term, le_command, le_data):
    ch = grpc.insecure_channel(addr)
    stub = booking_pb2_grpc.RaftStub(ch)
    le = booking_pb2.LogEntry(index=le_index, term=le_term, command=le_command, data=le_data)
    args = booking_pb2.AppendEntriesArgs(term=0, leaderId="repair", prevLogIndex=le_index-1, prevLogTerm=0, entries=[le], leaderCommit=le_index)
    try:
        resp = stub.AppendEntries(args, timeout=5)
        return resp
    except Exception as e:
        return e

def main():
    if len(sys.argv) < 3:
        print("usage: repair_followers.py leader_json follower_addr [follower_addr2 ...]")
        sys.exit(1)

    leader_file = sys.argv[1]
    followers = sys.argv[2:]
    with open(leader_file, "r") as f:
        leader = json.load(f)
    log = leader.get("log", [])
    if not log:
        print("Leader log empty — nothing to push.")
        return
    leader_len = len(log)
    print("Leader log length:", leader_len)

    for addr in followers:
        print("\n=== Repairing follower:", addr)
        match = get_follower_matchindex(addr)
        if match is None:
            print("Cannot reach follower", addr, " — skip")
            continue
        print("Follower matchIndex (local log length):", match)
        # send missing entries from match+1 .. leader_len
        for i in range(match+1, leader_len+1):
            e = next((x for x in log if x.get("index") == i), None)
            if not e:
                print("Leader missing entry index", i, " — abort for this follower")
                break
            print("-> pushing entry", i, "command=", e.get("command"))
            resp = push_entry(addr, e.get("index"), e.get("term", 0), e.get("command"), e.get("data"))
            print("   reply:", resp)
            # small delay so follower has time to write
            time.sleep(0.2)
        print("Finished repairing", addr)

if __name__ == "__main__":
    main()
