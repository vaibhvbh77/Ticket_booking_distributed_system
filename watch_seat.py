import json, time, os
seat = input("Seat to watch (e.g. S5): ").strip().upper()
print("Watching", seat, "- Ctrl+C to stop\n")
while True:
    for f in ("node_node1.json","node_node2.json","node_node3.json"):
        try:
            d = json.load(open(f))
            s = d.get("seats", {}).get(seat, {"reserved": None, "by": ""})
            print(f"{f}: reserved={s['reserved']}, by={s['by'] or 'none'}")
        except Exception as e:
            print(f"{f}: ERROR {e}")
    print("-" * 40)
    time.sleep(1)
