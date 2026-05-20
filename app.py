from flask import Flask, Response
import os
app = Flask(__name__)

@app.route("/")
def index():
    return Response(b"<h1>ProFloat is working</h1>", mimetype="text/html")

@app.route("/health")
def health():
    return "ok"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
