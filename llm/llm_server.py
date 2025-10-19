from flask import Flask, request, jsonify
import json, re

app = Flask(__name__)

@app.route("/ask", methods=["POST"])
def ask():
    data = request.get_json() or {}
    q = data.get("q", "").lower()
    answer = "Hmm, not sure."

    # try to detect seat id like s1, s2...s10
    match = re.search(r's\d{1,2}', q)
    seat = match.group(0).upper() if match else None

    try:
        d = json.load(open("node_node1.json"))
        if seat and seat in d.get("seats", {}):
            info = d["seats"][seat]
            if info["reserved"]:
                answer = f"Seat {seat} is already reserved by {info['by']}."
            else:
                answer = f"Seat {seat} is currently free."
        else:
            answer = "Please mention a valid seat like S1 or S5."
    except Exception:
        answer = "LLM couldn't access booking data."

    # extra helpful hints
    if "available" in q:
        answer += " Use the 'get' command to view all seats."
    elif "cancel" in q:
        answer = "To cancel a booking, use the 'cancel' command (coming soon)."

    return jsonify({"answer": answer})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
