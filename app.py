# app.py
"""
FarmSync single-file Flask app (fixed migrations + improved UI)
- Safe migrations: create tables and ALTER to add missing columns (non-destructive)
- Proper param queries and VALUES keyword
- Improved UI/CSS and better flash messaging
"""
import os
import re
from datetime import datetime, date
from functools import wraps

from flask import (
    Flask, request, session, redirect, url_for, jsonify,
    render_template_string, flash, get_flashed_messages
)
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2 import pool

# ---------------- Config ----------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")
PORT = int(os.environ.get("PORT", 3000))
NEON_DB_URL = os.environ.get(
    "NEON_DB_URL",
    # override on Railway with your real connection string
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
        print("DB fetchall error:", e, "Query:", query, "Params:", params)
        if cur:
            try: cur.close()
            except: pass
        return None, None
    finally:
        if conn:
            db_put_conn(conn)

def run_query_commit(query, params=None):
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
        print("DB commit error:", e, "Query:", query, "Params:", params)
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
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    return decorated

# ----------------- Validation -----------------
EMAIL_RE = re.compile(r"^[^@]+@[^@]+\.[^@]+$")
def valid_email(e): return bool(EMAIL_RE.match(e or ""))
def valid_password(p): return p and len(p) >= 6

# ----------------- Migrations: safe (create and alter) -----------------
def ensure_tables_and_columns():
    """
    Create tables if not present and add missing columns using ALTER TABLE ... ADD COLUMN IF NOT EXISTS
    This helps existing DBs that were created earlier to be upgraded without errors.
    """
    # Create core tables with essential columns (if not exists)
    create_users = """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    create_farmers = """
    CREATE TABLE IF NOT EXISTS farmers (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    create_orders = """
    CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY,
        farmer_id INTEGER REFERENCES farmers(id) ON DELETE SET NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    run_query_commit(create_users)
    run_query_commit(create_farmers)
    run_query_commit(create_orders)

    # Add optional/missing columns to users, farmers, orders safely
    alter_statements = [
        # users
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMP",
        # farmers
        "ALTER TABLE farmers ADD COLUMN IF NOT EXISTS location TEXT",
        "ALTER TABLE farmers ADD COLUMN IF NOT EXISTS contact TEXT",
        "ALTER TABLE farmers ADD COLUMN IF NOT EXISTS products TEXT",
        # orders
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS farmer_name TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS items TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'ongoing'",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_date DATE"
    ]
    for stmt in alter_statements:
        run_query_commit(stmt)

# run migrations at startup
try:
    ensure_tables_and_columns()
except Exception as e:
    print("Migration error:", e)

# ---------------- Templates (improved UI) ----------------
BASE_HEAD = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>FarmSync</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    :root{
      --bg:#f5f7fb;--card:#ffffff;--accent:#2f9d5a;--muted:#6b7280;
      --danger:#b00020;--success:#1b6b20;
    }
    *{box-sizing:border-box}
    body{font-family:'Inter',sans-serif;background:var(--bg);color:#111;margin:0;padding:18px}
    .wrap{max-width:1100px;margin:0 auto}
    .topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}
    .brand{font-weight:700;font-size:1.25rem}
    .userbox{display:flex;align-items:center;gap:12px}
    .avatar{width:40px;height:40px;border-radius:50%;object-fit:cover}
    .card{background:var(--card);padding:16px;border-radius:12px;border:1px solid #eee;box-shadow:0 6px 24px rgba(15,23,42,0.04)}
    .grid{display:grid;grid-template-columns:1fr;gap:14px}
    @media(min-width:900px){.grid{grid-template-columns:320px 1fr}}
    h2{margin:0 0 8px 0}
    form .row{display:flex;flex-direction:column;margin-bottom:10px}
    label{font-size:.9rem;color:var(--muted);margin-bottom:6px;font-weight:600}
    input[type="text"], input[type="email"], input[type="password"], input[type="date"], select, textarea{
      padding:10px;border-radius:8px;border:1px solid #e6e9ee;font-size:1rem
    }
    button.btn{background:var(--accent);color:#fff;border:none;padding:10px 12px;border-radius:8px;cursor:pointer}
    a.small{font-size:.85rem;color:var(--muted);text-decoration:none}
    .muted{color:var(--muted);font-size:.95rem}
    .list{display:flex;flex-direction:column;gap:8px;margin-top:8px}
    .item{padding:10px;border-radius:10px;border:1px solid #f0f2f5;background:#fbfdff;display:flex;justify-content:space-between;align-items:center}
    .pill{padding:6px 8px;border-radius:999px;font-weight:600;font-size:.85rem}
    .pill.ongoing{background:#e8f8ff;color:#0366a6}
    .pill.completed{background:#eaf8ee;color:#196619}
    .flash{padding:10px;border-radius:8px;margin-bottom:10px}
    .flash.error{background:#fff4f4;color:var(--danger);border:1px solid #ffd5d5}
    .flash.success{background:#f0fff3;color:var(--success);border:1px solid #cdeecf}
    .controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  </style>
</head>
<body>
  <div class="wrap">
"""

BASE_FOOT = """
  </div>
</body>
</html>
"""

INDEX_HTML = BASE_HEAD + """
  <div class="topbar">
    <div class="brand">FarmSync</div>
    <div class="muted">Build & manage farmers & orders</div>
  </div>

  <div class="card">
    {% for m in get_flashed_messages(category_filter=['error']) %}<div class="flash error">{{ m }}</div>{% endfor %}
    {% for m in get_flashed_messages(category_filter=['success']) %}<div class="flash success">{{ m }}</div>{% endfor %}

    <div style="display:flex;gap:18px;flex-wrap:wrap">
      <div style="flex:1;min-width:260px">
        <h2>Create account</h2>
        <form method="post" action="{{ url_for('register') }}">
          <div class="row"><label>Email</label><input type="email" name="email" required></div>
          <div class="row"><label>Display name</label><input type="text" name="display_name"></div>
          <div class="row"><label>Password (min 6 chars)</label><input type="password" name="password" required></div>
          <div style="display:flex;gap:8px"><button class="btn" type="submit">Register</button></div>
        </form>
      </div>

      <div style="flex:1;min-width:260px">
        <h2>Login</h2>
        <form method="post" action="{{ url_for('login') }}">
          <div class="row"><label>Email</label><input type="email" name="email" required></div>
          <div class="row"><label>Password</label><input type="password" name="password" required></div>
          <div style="display:flex;gap:8px"><button class="btn" type="submit">Login</button></div>
        </form>
        <div style="margin-top:10px" class="muted">You can register first, then login. Tables are created automatically.</div>
      </div>
    </div>
  </div>
""" + BASE_FOOT

DASH_HTML = BASE_HEAD + """
  <div class="topbar">
    <div class="brand">FarmSync</div>
    <div class="userbox">
      <div class="muted">Logged in as <strong>{{ user.display_name or user.email }}</strong></div>
      <img class="avatar" src="{{ user.avatar or default_avatar }}" alt="avatar"/>
      <a href="{{ url_for('logout') }}" class="small">Logout</a>
    </div>
  </div>

  {% for m in get_flashed_messages(category_filter=['error']) %}<div class="flash error">{{ m }}</div>{% endfor %}
  {% for m in get_flashed_messages(category_filter=['success']) %}<div class="flash success">{{ m }}</div>{% endfor %}

  <div class="grid">
    <div>
      <div class="card">
        <h2>Add Farmer</h2>
        <form method="post" action="{{ url_for('add_farmer') }}">
          <div class="row"><label>Name</label><input type="text" name="name" required></div>
          <div class="row"><label>Location</label><input type="text" name="location"></div>
          <div class="row"><label>Contact</label><input type="text" name="contact"></div>
          <div class="row"><label>Products (comma separated)</label><input type="text" name="products"></div>
          <div style="display:flex;gap:8px"><button class="btn" type="submit">Add Farmer</button></div>
        </form>
      </div>

      <div style="height:14px"></div>

      <div class="card">
        <h2>Add Order</h2>
        <form method="post" action="{{ url_for('add_order') }}">
          <div class="row"><label>Farmer</label>
            <select name="farmer_id" required>
              <option value="">-- select farmer --</option>
              {% for f in farmers %}
                <option value="{{ f.id }}">{{ f.name }}{% if f.location %} — {{ f.location }}{% endif %}</option>
              {% endfor %}
            </select>
          </div>
          <div class="row"><label>Items / Details</label><input type="text" name="items" required></div>
          <div class="row"><label>Order date</label><input type="date" name="order_date" value="{{ today }}"></div>
          <div style="display:flex;gap:8px"><button class="btn" type="submit">Create Order</button></div>
        </form>
      </div>
    </div>

    <div>
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <h2 style="margin:0">Farmers</h2>
          <div class="controls"><a class="small" href="{{ url_for('dashboard') }}">Refresh</a></div>
        </div>
        <div class="list">
          {% if farmers %}
            {% for f in farmers %}
              <div class="item">
                <div>
                  <div><strong>{{ f.name }}</strong></div>
                  <div class="muted">{{ f.location or '' }} {% if f.contact %}• {{ f.contact }}{% endif %} {% if f.products %}• {{ f.products }}{% endif %}</div>
                </div>
                <div class="muted">ID {{ f.id }}</div>
              </div>
            {% endfor %}
          {% else %}
            <div class="muted">No farmers yet.</div>
          {% endif %}
        </div>
      </div>

      <div style="height:12px"></div>

      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <h2 style="margin:0">Orders</h2>
          <div class="controls">
            <a class="small" href="{{ url_for('dashboard') }}">All</a>
            <a class="small" style="margin-left:6px" href="{{ url_for('orders_filtered', status='ongoing') }}">Ongoing</a>
            <a class="small" style="margin-left:6px" href="{{ url_for('orders_filtered', status='completed') }}">Completed</a>
          </div>
        </div>

        <div class="list">
          {% if orders %}
            {% for o in orders %}
              <div class="item">
                <div style="max-width:70%">
                  <div><strong>Order #{{ o.id }}</strong> <span class="pill {{ o.status }}">{{ o.status }}</span></div>
                  <div class="muted">Farmer: {{ o.farmer_name or '-' }} • Items: {{ o.items or '-' }}</div>
                  <div class="muted">Date: {{ o.order_date or '-' }}</div>
                </div>
                <div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end">
                  {% if o.status != 'completed' %}
                    <form method="post" action="{{ url_for('complete_order', order_id=o.id) }}">
                      <button class="small">Mark completed</button>
                    </form>
                  {% else %}
                    <div class="muted">Done</div>
                  {% endif %}
                </div>
              </div>
            {% endfor %}
          {% else %}
            <div class="muted">No orders yet.</div>
          {% endif %}
        </div>
      </div>
    </div>
  </div>
""" + BASE_FOOT

# ---------------- Routes ----------------

@app.route("/", methods=["GET"])
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template_string(INDEX_HTML)

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

    rows, cols = run_query_fetchall("SELECT id FROM users WHERE email = %s", (email,))
    if rows is None:
        flash("Database error. Try again later.", "error")
        return redirect(url_for("index"))
    if rows:
        flash("Email already registered. Please login.", "error")
        return redirect(url_for("index"))

    pw_hash = generate_password_hash(password)
    ok = run_query_commit(
        "INSERT INTO users (email, password_hash, display_name) VALUES (%s, %s, %s)",
        (email, pw_hash, display_name or None)
    )
    if not ok:
        # Try a fallback: if display_name column is missing, insert without it
        # (migration should have added it, but handle gracefully)
        ok2 = run_query_commit("INSERT INTO users (email, password_hash) VALUES (%s, %s)", (email, pw_hash))
        if not ok2:
            flash("Failed to create account. Try later.", "error")
            return redirect(url_for("index"))
    flash("Account created — please log in.", "success")
    return redirect(url_for("index"))

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
    display_name = user_row[2] if len(user_row) > 2 else None
    if not check_password_hash(pw_hash, password):
        flash("Incorrect password.", "error")
        return redirect(url_for("index"))

    session["user_id"] = user_id
    session["user_email"] = email
    session["user_name"] = display_name or email
    # update last_login for bookkeeping (safe: add column earlier)
    run_query_commit("UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = %s", (user_id,))
    flash("Logged in successfully.", "success")
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("index"))

# Dashboard and actions
@app.route("/dashboard")
@login_required
def dashboard():
    rows_f, cols_f = run_query_fetchall("SELECT * FROM farmers ORDER BY created_at DESC")
    farmers = rows_to_dicts(rows_f, cols_f) if rows_f is not None else []
    rows_o, cols_o = run_query_fetchall("SELECT * FROM orders ORDER BY created_at DESC")
    orders = rows_to_dicts(rows_o, cols_o) if rows_o is not None else []
    user = {"id": session.get("user_id"), "email": session.get("user_email"), "display_name": session.get("user_name"), "avatar": None}
    default_avatar = "https://i.pravatar.cc/150?u=farm-sync"
    return render_template_string(DASH_HTML, user=user, farmers=farmers, orders=orders, today=date.today().isoformat(), default_avatar=default_avatar)

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
    ok = run_query_commit("INSERT INTO farmers (name, location, contact, products) VALUES (%s, %s, %s, %s)",
                          (name, location or None, contact or None, products or None))
    if not ok:
        flash("Failed to add farmer. Try later.", "error")
    else:
        flash("Farmer added.", "success")
    return redirect(url_for("dashboard"))

@app.route("/add_order", methods=["POST"])
@login_required
def add_order():
    farmer_id = request.form.get("farmer_id")
    items = (request.form.get("items") or "").strip()
    order_date = request.form.get("order_date") or date.today().isoformat()
    if not farmer_id or not items:
        flash("Farmer and items are required.", "error")
        return redirect(url_for("dashboard"))
    # ensure farmer exists
    rows, cols = run_query_fetchall("SELECT id, name FROM farmers WHERE id = %s", (farmer_id,))
    if rows is None:
        flash("DB error verifying farmer.", "error")
        return redirect(url_for("dashboard"))
    if not rows:
        flash("Selected farmer not found.", "error")
        return redirect(url_for("dashboard"))
    farmer_name = rows[0][1]
    ok = run_query_commit("INSERT INTO orders (farmer_id, farmer_name, items, status, order_date) VALUES (%s, %s, %s, %s, %s)",
                          (farmer_id, farmer_name, items, 'ongoing', order_date))
    if not ok:
        flash("Failed to create order.", "error")
    else:
        flash("Order added.", "success")
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

@app.route("/orders/<status>")
@login_required
def orders_filtered(status):
    status = (status or "").lower()
    if status not in ('ongoing', 'completed'):
        flash("Invalid status filter.", "error")
        return redirect(url_for("dashboard"))
    rows_o, cols_o = run_query_fetchall("SELECT * FROM orders WHERE LOWER(status) = %s ORDER BY created_at DESC", (status,))
    orders = rows_to_dicts(rows_o, cols_o) if rows_o is not None else []
    rows_f, cols_f = run_query_fetchall("SELECT * FROM farmers ORDER BY created_at DESC")
    farmers = rows_to_dicts(rows_f, cols_f) if rows_f is not None else []
    user = {"id": session.get("user_id"), "email": session.get("user_email"), "display_name": session.get("user_name"), "avatar": None}
    default_avatar = "https://i.pravatar.cc/150?u=farm-sync"
    return render_template_string(DASH_HTML, user=user, farmers=farmers, orders=orders, today=date.today().isoformat(), default_avatar=default_avatar)

# API endpoints
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
    app.run(host="0.0.0.0", port=PORT, debug=True)
