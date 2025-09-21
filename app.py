# app.py
from flask import Flask, jsonify, request, session, redirect
from flask_cors import CORS
import psycopg2
from psycopg2 import pool
import os

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")

# Important: in production restrict allowed origins instead of "*"
CORS(app, supports_credentials=True, resources={r"/*": {"origins": "*"}})

port = int(os.environ.get("PORT", 3000))

# --- Neon DB URL (keep in env for production) ---
neon_db_url = os.environ.get(
    "NEON_DB_URL",
    "postgresql://neondb_owner:npg_jgROvpDtrm03@ep-hidden-truth-aev5l7a7-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
)

# Set up the PostgreSQL connection pool
try:
    postgreSQL_pool = psycopg2.pool.SimpleConnectionPool(
        minconn=1,
        maxconn=20,
        dsn=neon_db_url
    )
    print("Successfully created PostgreSQL connection pool.")
except (Exception, psycopg2.Error) as error:
    print("Failed to create PostgreSQL connection pool:", error)
    postgreSQL_pool = None

# run_query returns (rows, columns) or (None, None) on error
def run_query(query, params=None):
    if postgreSQL_pool is None:
        print("No DB pool available")
        return None, None
    conn = None
    cur = None
    try:
        conn = postgreSQL_pool.getconn()
        cur = conn.cursor()
        if params:
            cur.execute(query, params)
        else:
            cur.execute(query)
        rows = cur.fetchall()
        cols = [desc[0] for desc in cur.description] if cur.description else []
        cur.close()
        return rows, cols
    except (Exception, psycopg2.Error) as error:
        print("Error while connecting to PostgreSQL or executing query:", error)
        if cur:
            try:
                cur.close()
            except:
                pass
        return None, None
    finally:
        if conn:
            postgreSQL_pool.putconn(conn)

# Utility to convert rows+cols to list of dicts
def rows_to_dicts(rows, cols):
    if not rows:
        return []
    if not cols:
        return [list(r) for r in rows]
    return [dict(zip(cols, row)) for row in rows]

# ---------------- Auth endpoints (demo helpers) ----------------
# NOTE: For real OAuth, implement proper OAuth flow. These demo endpoints
# let you test the frontend quickly: session-based or token-based.

# Demo /auth/google: sets a demo session and redirects back with token
@app.route("/auth/google", methods=["GET"])
def auth_google():
    # This is a placeholder "demo" flow. Replace with real OAuth.
    demo_user = {
        "displayName": "Demo User",
        "email": "demo@example.com",
        "photoURL": "https://i.pravatar.cc/150?u=demo"
    }
    # set session cookie
    session["user"] = demo_user
    # demo token (in real OAuth you'd generate/issue a JWT)
    demo_token = "demo-token"
    redirect_to = request.args.get("redirect_uri", "/")
    if "?" in redirect_to:
        redirect_url = f"{redirect_to}&token={demo_token}"
    else:
        redirect_url = f"{redirect_to}?token={demo_token}"
    return redirect(redirect_url)

# /auth/me: returns current user if session cookie or demo token is provided
@app.route("/auth/me", methods=["GET"])
def auth_me():
    # 1) session-based
    user = session.get("user")
    if user:
        return jsonify(user)

    # 2) bearer token - e.g., Authorization: Bearer demo-token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth.split(" ", 1)[1].strip()
        # Demo token check
        if token == "demo-token":
            demo_user = {
                "displayName": "Demo User",
                "email": "demo@example.com",
                "photoURL": "https://i.pravatar.cc/150?u=demo-token"
            }
            return jsonify(demo_user)

    # not authenticated
    return jsonify({"error": "Unauthorized"}), 401

# /auth/logout: clears session
@app.route("/auth/logout", methods=["POST", "GET"])
def auth_logout():
    session.pop("user", None)
    return jsonify({"ok": True})

# ---------------- API endpoints ----------------

@app.route("/api/farmers", methods=["GET"])
def get_farmers():
    """Return list of farmers"""
    try:
        rows, cols = run_query("SELECT * FROM farmers")
        if rows is None:
            return jsonify({"error": "DB query failed"}), 500
        farmers_list = rows_to_dicts(rows, cols)
        return jsonify(farmers_list)
    except Exception as e:
        print(f"Error fetching farmers: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

# Single /api/orders endpoint with optional status query param
@app.route("/api/orders", methods=["GET"])
def get_orders():
    """Return orders. Optional ?status=ongoing|completed"""
    try:
        status = request.args.get("status")
        if status:
            # sanitize: we use params to avoid SQL injection
            rows, cols = run_query("SELECT * FROM orders WHERE LOWER(status) = LOWER(%s)", (status,))
        else:
            rows, cols = run_query("SELECT * FROM orders")
        if rows is None:
            return jsonify({"error": "DB query failed"}), 500
        orders_list = rows_to_dicts(rows, cols)
        return jsonify(orders_list)
    except Exception as e:
        print(f"Error fetching orders: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

# convenience endpoints for compatibility with earlier frontend attempts
@app.route("/api/orders/ongoing", methods=["GET"])
def get_ongoing_orders():
    rows, cols = run_query("SELECT * FROM orders WHERE LOWER(status) = 'ongoing'")
    if rows is None:
        return jsonify({"error": "DB query failed"}), 500
    return jsonify(rows_to_dicts(rows, cols))

@app.route("/api/orders/previous", methods=["GET"])
def get_previous_orders():
    rows, cols = run_query("SELECT * FROM orders WHERE LOWER(status) = 'completed'")
    if rows is None:
        return jsonify({"error": "DB query failed"}), 500
    return jsonify(rows_to_dicts(rows, cols))

# Root or health-check
@app.route("/", methods=["GET"])
def index():
    return jsonify({"ok": True, "message": "FarmSync API running"}), 200

if __name__ == "__main__":
    # When deployed to Railway, the $PORT env var will be set.
    app.run(host="0.0.0.0", port=port, debug=True)
