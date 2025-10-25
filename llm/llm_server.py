# llm_server.py
# A simple Flask-based LLM helper that answers seat-related queries by checking the booking data file.

from flask import Flask, request, jsonify
import json
import re
from pathlib import Path

app = Flask(__name__)

DATA_FILE = Path("node_node1.json")

@app.route("/ask", methods=["POST"])
def handle_query():
    payload = request.get_json() or {}
    query = (payload.get("q") or "").lower().strip()
    response = "I'm not sure about that."

    # detect seat id patterns like s1, s2, ..., s10
    seat_match = re.search(r"s\d{1,2}", query)
    seat_id = seat_match.group(0).upper() if seat_match else None

    # attempt to read the node's seat data
    try:
        if DATA_FILE.exists():
            data = json.loads(DATA_FILE.read_text())
            seat_data = data.get("seats", {})
            if seat_id and seat_id in seat_data:
                info = seat_data[seat_id]
                if info.get("reserved"):
                    response = f"Seat {seat_id} is booked by {info.get('by', 'unknown')}."
                else:
                    response = f"Seat {seat_id} is currently available."
            else:
                response = "Please specify a valid seat, e.g., S1 or S5."
        else:
            response = "Booking data not found on this node."
    except Exception as exc:
        response = f"Could not read booking data ({exc})."

    # provide context-aware guidance
    if "available" in query:
        response += " Tip: Use the 'get' command to view all available seats."
    elif "cancel" in query:
        response = "To cancel a seat, you can use the 'cancel' option in the client."

    return jsonify({"answer": response})

if __name__ == "__main__":
    print("LLM service running on port 8000")
    app.run(host="0.0.0.0", port=8000)