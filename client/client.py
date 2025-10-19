import sys
import grpc
import requests
import booking_pb2, booking_pb2_grpc

def make_stub(addr):
    channel = grpc.insecure_channel(addr)
    return booking_pb2_grpc.ClientAPIStub(channel)

def interactive(addr):
    stub = make_stub(addr)
    token = ""
    client_id = "cli-user"
    print("Commands: login <name>, get, reserve <S#>, ask <question>, quit")
    while True:
        try:
            cmd = input("> ").strip().split()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not cmd:
            continue
        if cmd[0] == "quit":
            break

        if cmd[0] == "login":
            name = cmd[1] if len(cmd) > 1 else input("Username: ")
            pw = input("Password: ")
            try:
                r = stub.Login(booking_pb2.LoginRequest(username=name, password=pw))
                if getattr(r, "status", 1) != 0 or not getattr(r, "token", ""):
                    print("Login failed")
                else:
                    token = r.token
                    print("Logged in, token:", token)
            except Exception as e:
                print("Login RPC error:", e)
                continue

        elif cmd[0] == "get":
            r = stub.GetSeats(booking_pb2.GetRequest(token=token))
            for s in r.seats:
                status = "reserved" if s.reserved else "free"
                print(s.seat_id, status, "by:"+s.reserved_by)

        elif cmd[0] == "ask":
            q = " ".join(cmd[1:]) if len(cmd) > 1 else input("Question: ")
            try:
                resp = requests.post("http://127.0.0.1:8000/ask", json={"q": q}, timeout=5)
                resp.raise_for_status()
                print("LLM:", resp.json().get("answer"))
            except Exception as e:
                print("LLM call failed:", e)

        elif cmd[0] == "cancel":
            # usage: cancel S1
            if len(cmd) < 2:
                print("usage: cancel S1")
                continue
            seat = cmd[1].upper()
            try:
                r = stub.CancelSeat(booking_pb2.CancelRequest(token=token, seat_id=seat))
                # If follower returns NOT_LEADER redirect pattern (Status with msg starting NOT_LEADER:)
                msg = getattr(r, "msg", "")
                code = getattr(r, "code", None)
                if code == 1 and isinstance(msg, str) and msg.startswith("NOT_LEADER:"):
                    leader = msg.split(":", 1)[1]
                    print("Redirected to leader", leader)
                    stub = make_stub(leader)
                    r2 = stub.CancelSeat(booking_pb2.CancelRequest(token=token, seat_id=seat))
                    print(f"Result: code={getattr(r2,'code',None)} msg={getattr(r2,'msg',None)}")
                else:
                    print(f"Result: code={code} msg={msg}")
            except Exception as e:
                print("Cancel failed:", e)        

                

        elif cmd[0] == "reserve":
            if len(cmd) < 2:
                print("usage: reserve S1")
                continue
            seat = cmd[1]
            try:
                r = stub.ReserveSeat(booking_pb2.ReserveRequest(token=token, seat_id=seat, client_id=client_id))
            except Exception as e:
                print("RPC error:", e)
                continue

            # handle not-leader reply (server returns NOT_LEADER:<leader_id_or_addr>)
            if r.code == 1 and r.msg.startswith("NOT_LEADER:"):
                leader = r.msg.split(":", 1)[1]
                print("Server says not leader. Leader id/addr:", leader)
                # if leader looks like host:port, retry automatically
                if ":" in leader:
                    print("Retrying on leader address:", leader)
                    try:
                        stub = make_stub(leader)
                        r2 = stub.ReserveSeat(booking_pb2.ReserveRequest(token=token, seat_id=seat, client_id=client_id))
                        print("Result:", r2.code, r2.msg)
                    except Exception as e:
                        print("Retry RPC error:", e)
                else:
                    print("Leader returned an id (not an address). Re-run client connecting directly to leader address.")
            else:
                print("Result:", r.code, r.msg)

        else:
            print("unknown command")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("usage: python client/client.py <node_addr>")
        sys.exit(1)
    interactive(sys.argv[1])
