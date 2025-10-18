from flask import Flask, request, jsonify
app = Flask(__name__)

@app.route("/ask", methods=["POST"])
def ask():
    payload = request.get_json(silent=True) or {}
    q = payload.get("q", "")
    # very small demo logic — you can replace with real model calls later
    ql = q.lower()
    if "why" in ql and ("fail" in ql or "failed" in ql):
        return jsonify({"answer":"Likely the seat was already reserved by another client."})
    if "status" in ql or "booking" in ql:
        return jsonify({"answer":"Send your booking id and I'll check — demo server returns canned answers."})
    return jsonify({"answer":"Demo LLM: I can answer simple booking questions like 'why did my booking fail?'."})

if __name__ == "__main__":
    # dev server on port 8000
    app.run(host="0.0.0.0", port=8000)
