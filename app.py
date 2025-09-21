# app.py
"""
Full single-file Flask app with:
- user registration & login (session cookie)
- CRUD for farmers and orders (add/list/mark complete)
- DB connection pool (Postgres via NEON_DB_URL)
- auto-create tables if missing (avoids 500 on fresh DB)
- defensive error handling & validation
- templates inlined with render_template_string (no templates/ folder)
"""
import os
import re
from datetime import datetime, date
from functools import wraps

from flask import (
    Flask, request, session, redirect, url_for, jsonify,
    render_template_string, flash
)
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2 import pool, sql

# ---------------- Config ----------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")  # set on Railway
PORT = int(os.environ.get("PORT", 3000))
NEON_DB_URL = os.environ.get(
    "NEON_DB_URL",
    # placeholder - override in environment for production
    "postgresql://neondb_owner:npg_jgROvpDtrm03@ep-hidden-truth-aev5l7a7-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
)

# ---------------- DB Pool ----------------
postgreSQL_pool = None
try:
    postgreSQL_pool = psycopg2.pool.SimpleConnectionPool(minconn=1, maxconn=20, dsn=NEON_DB_URL)
    print("Postgres pool created.")
except Exception as e:
    print("Failed to create pool:", e)
    postgreSQL_pool = None

def db_get_conn():
    if postgreSQL_pool is None:
        raise RuntimeError("DB pool not available")
    return postgreSQL_pool.getconn()

def db_put_conn(conn):
    if conn:
        postgreSQL_pool.putconn(conn)

# ---------------- Helpers ----------------
def run_query_fetchall(query, params=None):
    """
    Returns (rows, columns) where rows is list of tuples and columns is list of col names.
    On error returns (None, None) and prints error.
    """
    conn = None
    cur = None
    try:
        conn = db_get_conn()
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        cur.close()
        return rows, cols
    except Exception as e:
        print("DB fetchall error:", e)
        if cur:
            try: cur.close()
            except: pass
        return None, None
    finally:
        if conn:
            db_put_conn(conn)

def run_query_commit(query, params=None):
    """
    Executes INSERT/UPDATE/DELETE and commits. Returns True on success, False on error.
    """
    conn = None
    cur = None
    try:
        conn = db_get_conn()
        cur = conn.cursor()
        cur.execute(query, params)
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print("DB commit error:", e)
        if conn:
            try: conn.rollback()
            except: pass
        if cur:
            try: cur.close()
            except: pass
        return False
    finally:
        if conn:
            db_put_conn(conn)

def rows_to_dicts(rows, cols):
    if not rows:
        return []
    if not cols:
        return [list(r) for r in rows]
    return [dict(zip(cols, r)) for r in rows]

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated

# ----------------- Validation -----------------
EMAIL_RE = re.compile(r"^[^@]+@[^@]+\.[^@]+$")
def valid_email(e): return bool(EMAIL_RE.match(e or ""))
def valid_password(p): return p and len(p) >= 6

# ----------------- Auto-create tables -----------------
def ensure_tables():
    """
    Creates users, farmers, orders tables if they don't exist.
    Safe to call multiple times.
    """
    queries = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS farmers (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            location TEXT,
            contact TEXT,
            products TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            farmer_id INTEGER REFERENCES farmers(id) ON DELETE SET NULL,
            farmer_name TEXT,
            items TEXT,
            status TEXT DEFAULT 'ongoing',
            order_date DATE DEFAULT CURRENT_DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    ]
    for q in queries:
        ok = run_query_commit(q)
        if not ok:
            print("Failed to create tables or run migration query, check DB permissions.")

# Ensure tables at startup
try:
    ensure_tables()
except Exception as e:
    print("Error ensuring tables:", e)

# ---------------- Templates (inlined) ----------------
# Keep templates simple and safe. Use flash messages to display validation errors.
BASE_HEAD = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>FarmSync</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    :root{--bg:#f4f7f9;--card:#fff;--primary:#4CAF50;--muted:#666}
    body{font-family:'Inter',sans-serif;background:var(--bg);color:#222;margin:0;padding:0}
    .container{max-width:1100px;margin:20px auto;padding:16px}
    .card{background:var(--card);padding:14px;border-radius:10px;border:1px solid #eee;box-shadow:0 6px 24px rgba(0,0,0,0.03)}
    header.site{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
    a.btn{display:inline-block;padding:8px 12px;border-radius:8px;background:var(--primary);color:#fff;text-decoration:none}
    a.small{background:#eee;color:#333;padding:6px 8px}
    form.field{display:flex;flex-direction:column;gap:8px}
    input[type="text"], input[type="email"], input[type="password"], textarea{padding:8px;border:1px solid #ddd;border-radius:8px}
    label{font-weight:600}
    .muted{color:var(--muted)}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
    .list .item{padding:10px;border-radius:8px;border:1px solid #f0f0f0;margin-bottom:8px;background:#fafafa}
    .pill{display:inline-block;padding:4px 8px;border-radius:999px;background:#f0f0f0}
    .error{background:#fff3f3;border:1px solid #ffc7c7;padding:8px;border-radius:8px;color:#900;margin-bottom:8px}
    .success{background:#e8f5e9;border:1px solid #b6e2b6;padding:8px;border-radius:8px;color:#155724;margin-bottom:8px}
  </style>
</head>
<body>
  <div class="container">
"""

BASE_FOOT = """
  </div>
</body>
</html>
"""

LOGIN_HTML = BASE_HEAD + """
  <div class="card">
    <header class="site">
      <h2>FarmSync — Register / Login</h2>
      <div>
        <a href="{{ url_for('index') }}" class="small">Home</a>
      </div>
    </header>

    {% with messages = get_flashed_messages(category_filter=['error']) %}
      {% if messages %}
        {% for m in messages %}<div class="error">{{ m }}</div>{% endfor %}
      {% endif %}
    {% endwith %}
    {% with messages = get_flashed_messages(category_filter=['success']) %}
      {% if messages %}
        {% for m in messages %}<div class="success">{{ m }}</div>{% endfor %}
      {% endif %}
    {% endwith %}

    <div style="display:flex;gap:18px;flex-wrap:wrap">
      <div style="flex:1;min-width:260px">
        <h3>Register</h3>
        <form method="post" action="{{ url_for('register') }}" class="field">
          <label>Email</label>
          <input type="email" name="email" required />
          <label>Display name (optional)</label>
          <input type="text" name="display_name" />
          <label>Password (min 6 chars)</label>
          <input type="password" name="password" required />
          <button class="btn" type="submit">Register</button>
        </form>
      </div>

      <div style="flex:1;min-width:260px">
        <h3>Login</h3>
        <form method="post" action="{{ url_for('login') }}" class="field">
          <label>Email</label>
          <input type="email" name="email" required />
          <label>Password</label>
          <input type="password" name="password" required />
          <button class="btn" type="submit">Login</button>
        </form>
      </div>
    </div>
  </div>
""" + BASE_FOOT

DASHBOARD_HTML = BASE_HEAD + """
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
    <h2>FarmSync Dashboard</h2>
    <div>
      <span class="muted">Logged in as: <strong>{{ user.display_name or user.email }}</strong></span>
      <a href="{{ url_for('logout') }}" class="small" style="margin-left:8px">Logout</a>
    </div>
  </div>

  {% with messages = get_flashed_messages(category_filter=['error']) %}
    {% if messages %}
      {% for m in messages %}<div class="error">{{ m }}</div>{% endfor %}
    {% endif %}
  {% endwith %}
  {% with messages = get_flashed_messages(category_filter=['success']) %}
    {% if messages %}
      {% for m in messages %}<div class="success">{{ m }}</div>{% endfor %}
    {% endif %}
  {% endwith %}

  <div class="grid" style="grid-template-columns: 1fr 1fr; margin-bottom:12px">
    <div class="card">
      <h3>Farmers</h3>
      <form method="post" action="{{ url_for('add_farmer') }}" class="field">
        <label>Name</label>
        <input type="text" name="name" required />
        <label>Location</label>
        <input type="text" name="location" />
        <label>Contact</label>
        <input type="text" name="contact" />
        <label>Products (comma-separated)</label>
        <input type="text" name="products" />
        <button class="btn" type="submit">Add Farmer</button>
      </form>
    </div>

    <div class="card">
      <h3>Add Order</h3>
      <form method="post" action="{{ url_for('add_order') }}" class="field">
        <label>Farmer (choose)</label>
        <select name="farmer_id" required>
          <option value="">-- select farmer --</option>
          {% for f in farmers %}
            <option value="{{ f.id }}">{{ f.name }}{% if f.location %} — {{ f.location }}{% endif %}</option>
          {% endfor %}
        </select>
        <label>Items / Details</label>
        <input type="text" name="items" required />
        <label>Order date</label>
        <input type="date" name="order_date" value="{{ today }}" />
        <button class="btn" type="submit">Add Order</button>
      </form>
    </div>
  </div>

  <div class="grid" style="grid-template-columns:1fr 1fr; gap:12px; margin-bottom:12px">
    <div class="card">
      <h3>All Farmers</h3>
      <div class="list">
        {% if farmers %}
          {% for f in farmers %}
            <div class="item">
              <div><strong>{{ f.name }}</strong> {% if f.location %}<span class="muted">— {{ f.location }}</span>{% endif %}</div>
              <div class="muted">{{ f.contact or '' }} {% if f.products %} • {{ f.products }}{% endif %}</div>
            </div>
          {% endfor %}
        {% else %}
          <div class="muted">No farmers yet.</div>
        {% endif %}
      </div>
    </div>

    <div class="card">
      <h3>Orders</h3>
      <div style="margin-bottom:8px">
        <a href="{{ url_for('dashboard') }}" class="small">All</a>
        <a href="{{ url_for('orders_filtered', status='ongoing') }}" class="small" style="margin-left:8px">Ongoing</a>
        <a href="{{ url_for('orders_filtered', status='completed') }}" class="small" style="margin-left:8px">Completed</a>
      </div>
      <div class="list">
        {% if orders %}
          {% for o in orders %}
            <div class="item">
              <div><strong>Order #{{ o.id }}</strong> <span class="pill">{{ o.status }}</span></div>
              <div class="muted">Farmer: {{ o.farmer_name or '—' }} • Items: {{ o.items or '—' }} • Date: {{ o.order_date or '—' }}</div>
              {% if o.status != 'completed' %}
                <form method="post" action="{{ url_for('complete_order', order_id=o.id) }}" style="margin-top:6px">
                  <button type="submit" class="small">Mark Completed</button>
                </form>
              {% endif %}
            </div>
          {% endfor %}
        {% else %}
          <div class="muted">No orders yet.</div>
        {% endif %}
      </div>
    </div>
  </div>

  <div style="margin-top:8px" class="muted">Tip: Use "Add Farmer" above before creating orders.</div>
""" + BASE_FOOT

# ---------------- Routes ----------------

@app.route("/", methods=["GET"])
def index():
    # If logged in redirect to dashboard
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template_string(LOGIN_HTML)

# ---- Registration ----
@app.route("/register", methods=["POST"])
def register():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    display_name = (request.form.get("display_name") or "").strip()

    if not valid_email(email):
        flash("Please provide a valid email address.", "error")
        return redirect(url_for("index"))
    if not valid_password(password):
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("index"))

    # check if user exists
    rows, cols = run_query_fetchall("SELECT id FROM users WHERE email = %s", (email,))
    if rows is None:
        flash("Database error. Try again later.", "error")
        return redirect(url_for("index"))
    if rows:
        flash("Email already registered. Please login or use another email.", "error")
        return redirect(url_for("index"))

    pw_hash = generate_password_hash(password)
    ok = run_query_commit(
        "INSERT INTO users (email, password_hash, display_name) VALUES (%s, %s, %s)",
        (email, pw_hash, display_name or None)
    )
    if not ok:
        flash("Failed to create account. Try again later.", "error")
        return redirect(url_for("index"))
    flash("Account created — please log in.", "success")
    return redirect(url_for("index"))

# ---- Login ----
@app.route("/login", methods=["POST"])
def login():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    if not email or not password:
        flash("Provide email and password.", "error")
        return redirect(url_for("index"))

    rows, cols = run_query_fetchall("SELECT id, password_hash, display_name FROM users WHERE email = %s", (email,))
    if rows is None:
        flash("Database error. Try again later.", "error")
        return redirect(url_for("index"))
    if not rows:
        flash("No account found for that email.", "error")
        return redirect(url_for("index"))

    user_row = rows[0]
    user_id = user_row[0]
    pw_hash = user_row[1]
    display_name = user_row[2] or email

    if not check_password_hash(pw_hash, password):
        flash("Incorrect password.", "error")
        return redirect(url_for("index"))

    # successful login -> set session
    session["user_id"] = user_id
    session["user_email"] = email
    session["user_name"] = display_name
    flash("Logged in successfully.", "success")
    return redirect(url_for("dashboard"))

# ---- Logout ----
@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("index"))

# ---- Dashboard & actions ----
@app.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    # fetch farmers and orders
    rows, cols = run_query_fetchall("SELECT * FROM farmers ORDER BY created_at DESC")
    farmers = rows_to_dicts(rows, cols) if rows is not None else []
    rows2, cols2 = run_query_fetchall("SELECT * FROM orders ORDER BY created_at DESC")
    orders = rows_to_dicts(rows2, cols2) if rows2 is not None else []
    user = {"id": session.get("user_id"), "email": session.get("user_email"), "display_name": session.get("user_name")}
    return render_template_string(DASHBOARD_HTML, user=user, farmers=farmers, orders=orders, today=date.today().isoformat())

@app.route("/add_farmer", methods=["POST"])
@login_required
def add_farmer():
    name = (request.form.get("name") or "").strip()
    location = (request.form.get("location") or "").strip()
    contact = (request.form.get("contact") or "").strip()
    products = (request.form.get("products") or "").strip()
    if not name:
        flash("Farmer name is required.", "error")
        return redirect(url_for("dashboard"))

    ok = run_query_commit(
        "INSERT INTO farmers (name, location, contact, products) VALUES (%s, %s, %s, %s)",
        (name, location or None, contact or None, products or None)
    )
    if not ok:
        flash("Failed to add farmer. Try again later.", "error")
    else:
        flash("Farmer added.", "success")
    return redirect(url_for("dashboard"))

@app.route("/add_order", methods=["POST"])
@login_required
def add_order():
    try:
        farmer_id = request.form.get("farmer_id")
        items = (request.form.get("items") or "").strip()
        order_date = request.form.get("order_date") or date.today().isoformat()
        # validate
        if not farmer_id or not items:
            flash("Farmer and items are required to create an order.", "error")
            return redirect(url_for("dashboard"))
        # ensure farmer exists to get name
        rows, cols = run_query_fetchall("SELECT id, name FROM farmers WHERE id = %s", (farmer_id,))
        if rows is None:
            flash("Database error when verifying farmer.", "error")
            return redirect(url_for("dashboard"))
        if not rows:
            flash("Selected farmer not found.", "error")
            return redirect(url_for("dashboard"))
        farmer_name = rows[0][1]
        # insert order
        ok = run_query_commit(
            "INSERT INTO orders (farmer_id, farmer_name, items, status, order_date) VALUES (%s, %s, %s, %s, %s)",
            (farmer_id, farmer_name, items, 'ongoing', order_date)
        )
        if not ok:
            flash("Failed to create order.", "error")
        else:
            flash("Order added.", "success")
    except Exception as e:
        print("add_order error:", e)
        flash("Error adding order.", "error")
    return redirect(url_for("dashboard"))

@app.route("/complete_order/<int:order_id>", methods=["POST"])
@login_required
def complete_order(order_id):
    ok = run_query_commit("UPDATE orders SET status = %s WHERE id = %s", ('completed', order_id))
    if not ok:
        flash("Failed to update order.", "error")
    else:
        flash("Order marked completed.", "success")
    return redirect(url_for("dashboard"))

@app.route("/orders/<status>", methods=["GET"])
@login_required
def orders_filtered(status):
    status = (status or "").lower()
    if status not in ('ongoing', 'completed'):
        flash("Invalid status filter.", "error")
        return redirect(url_for("dashboard"))
    rows, cols = run_query_fetchall("SELECT * FROM orders WHERE LOWER(status) = %s ORDER BY created_at DESC", (status,))
    orders = rows_to_dicts(rows, cols) if rows is not None else []
    rows_f, cols_f = run_query_fetchall("SELECT * FROM farmers ORDER BY created_at DESC")
    farmers = rows_to_dicts(rows_f, cols_f) if rows_f is not None else []
    user = {"id": session.get("user_id"), "email": session.get("user_email"), "display_name": session.get("user_name")}
    return render_template_string(DASHBOARD_HTML, user=user, farmers=farmers, orders=orders, today=date.today().isoformat())

# ---- API endpoints (optional, JSON) ----
@app.route("/api/me")
def api_me():
    if not session.get("user_id"):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"id": session.get("user_id"), "email": session.get("user_email"), "display_name": session.get("user_name")})

@app.route("/api/farmers")
def api_farmers():
    rows, cols = run_query_fetchall("SELECT * FROM farmers ORDER BY created_at DESC")
    if rows is None:
        return jsonify({"error": "db"}), 500
    return jsonify(rows_to_dicts(rows, cols))

@app.route("/api/orders")
def api_orders():
    status = request.args.get("status")
    if status:
        rows, cols = run_query_fetchall("SELECT * FROM orders WHERE LOWER(status) = %s ORDER BY created_at DESC", (status.lower(),))
    else:
        rows, cols = run_query_fetchall("SELECT * FROM orders ORDER BY created_at DESC")
    if rows is None:
        return jsonify({"error": "db"}), 500
    return jsonify(rows_to_dicts(rows, cols))

@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat()})

# ---------------- Run ----------------
if __name__ == "__main__":
    # Local development
    app.run(host="0.0.0.0", port=PORT, debug=True)
