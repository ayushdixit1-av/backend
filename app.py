# app.py  -- single-file Flask app with inlined templates & CSS/JS
import os
from flask import Flask, request, session, redirect, url_for, jsonify, render_template_string
import psycopg2
from psycopg2 import pool
from datetime import date

# ---------------- Config ----------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")  # set on Railway
PORT = int(os.environ.get("PORT", 3000))
NEON_DB_URL = os.environ.get(
    "NEON_DB_URL",
    "postgresql://neondb_owner:npg_jgROvpDtrm03@ep-hidden-truth-aev5l7a7-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
)

# ---------------- Database pool ----------------
try:
    postgreSQL_pool = psycopg2.pool.SimpleConnectionPool(minconn=1, maxconn=20, dsn=NEON_DB_URL)
    print("Postgres pool created.")
except Exception as e:
    print("Failed to create pool:", e)
    postgreSQL_pool = None

def run_query(query, params=None):
    """
    Runs a query and returns (rows, cols) or (None, None) on error.
    rows is a list of tuples, cols is a list of column names.
    """
    if postgreSQL_pool is None:
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
        cols = [d[0] for d in cur.description] if cur.description else []
        cur.close()
        return rows, cols
    except Exception as e:
        print("DB error in run_query:", e)
        if cur:
            try:
                cur.close()
            except:
                pass
        return None, None
    finally:
        if conn:
            postgreSQL_pool.putconn(conn)

def rows_to_dicts(rows, cols):
    if not rows:
        return []
    if not cols:
        return [list(r) for r in rows]
    return [dict(zip(cols, r)) for r in rows]

# ---------------- Templates (inlined) ----------------
# For readability, templates are stored as Python triple-quoted strings.
LOGIN_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>FarmSync — Login</title>
  <style>
    /* minimal CSS (you can paste your full CSS here) */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    :root{--bg:#f4f7f9;--card:#fff;--primary:#4CAF50;--text:#333;--muted:#666}
    body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);margin:0;display:flex;align-items:center;justify-content:center;height:100vh}
    .login-card{background:var(--card);padding:2.5rem;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,0.06);width:420px;max-width:94%}
    h2{margin:0 0 .5rem 0}
    .muted{color:var(--muted);margin-bottom:1rem}
    .login-btn{display:inline-block;background:var(--primary);color:#fff;padding:.85rem 1.2rem;border-radius:8px;text-decoration:none;font-weight:600}
    .note{font-size:.9rem;color:var(--muted);margin-top:.75rem}
  </style>
</head>
<body>
  <div class="login-card">
    <h2>FarmSync</h2>
    <p class="muted">Sign in to manage your farmers and orders (demo).</p>
    <a class="login-btn" href="{{ auth_url }}">Sign in with Google (demo)</a>
    <p class="note">This demo sets a session cookie and redirects to the dashboard. Replace <code>/auth/google</code> with a real OAuth flow for production.</p>
  </div>
</body>
</html>
"""

DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>FarmSync Dashboard</title>
  <style>
    /* Inline CSS (use your full CSS if desired) */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    :root {
      --bg-color:#f4f7f9;--card-bg:#fff;--primary-color:#4CAF50;--primary-dark:#388E3C;
      --text-color:#333;--secondary-text:#666;--border-color:#e0e0e0;
      --shadow-light:rgba(0,0,0,.05);--shadow-medium:rgba(0,0,0,.1)
    }
    *{box-sizing:border-box}
    body{font-family:'Inter',sans-serif;background:var(--bg-color);color:var(--text-color);margin:0}
    .dashboard-container{display:flex;flex-direction:column;min-height:100vh;padding:1.5rem;max-width:1200px;margin:0 auto;gap:1.5rem}
    .header{display:flex;justify-content:space-between;align-items:center;padding:1rem;background:var(--card-bg);border-radius:10px;border:1px solid var(--border-color)}
    .user-profile{display:flex;align-items:center;gap:.75rem}
    .user-profile img{width:40px;height:40px;border-radius:50%}
    .main-content{display:grid;grid-template-columns:1fr;gap:1rem}
    @media(min-width:768px){.main-content{grid-template-columns:1fr 2fr}}
    .sidebar{background:var(--card-bg);padding:1rem;border-radius:10px;border:1px solid var(--border-color)}
    .tabs{display:flex;flex-direction:column;gap:.5rem;margin-bottom:1rem}
    .tab-button{padding:.6rem;border-radius:8px;border:none;text-align:left;cursor:pointer;background:transparent}
    .tab-button.active{background:var(--primary-color);color:#fff}
    .filters{margin-top:1rem}
    .search-container input{width:100%;padding:.6rem;border:1px solid var(--border-color);border-radius:8px}
    .content-area{background:var(--card-bg);padding:1rem;border-radius:10px;border:1px solid var(--border-color)}
    .list-container{display:grid;gap:.75rem}
    .card{background:var(--bg-color);padding:.9rem;border-radius:8px}
    .card-title{font-weight:600}
    .muted{color:var(--secondary-text)}
    .pill{display:inline-block;padding:.25rem .5rem;border-radius:999px;font-weight:500}
    .pill.ongoing{background:#e1f5fe;color:#039be5}
    .pill.completed{background:#e8f5e9;color:#388e3c}
    .logout-btn{padding:.4rem .6rem;border-radius:8px;background:#eee;text-decoration:none;color:#333}
    .error{background:#fff3f3;padding:.6rem;border-radius:8px;border:1px solid #ffc7c7;color:#a40000}
  </style>
</head>
<body>
  <div class="dashboard-container">
    <header class="header">
      <h1>FarmSync Dashboard</h1>
      <div class="user-profile">
        <span>{{ user.displayName }}</span>
        <img src="{{ user.photoURL }}" alt="avatar"/>
        <a class="logout-btn" href="{{ url_for('auth_logout') }}">Logout</a>
      </div>
    </header>

    <main class="main-content">
      <aside class="sidebar">
        <nav class="tabs">
          <button class="tab-button active" data-tab="farmers">Farmers List</button>
          <button class="tab-button" data-tab="ongoing">Ongoing Orders</button>
          <button class="tab-button" data-tab="previous">Previous Orders</button>
        </nav>

        <div class="filters">
          <h3>Filters</h3>
          <div class="search-container">
            <input id="filterInput" placeholder="Filter by name..." />
          </div>
        </div>
      </aside>

      <section class="content-area">
        <h2 id="contentTitle">Farmers List</h2>
        <div id="contentList" class="list-container">
          {% if farmers|length == 0 %}
            <div class="muted">No farmers found.</div>
          {% else %}
            {% for f in farmers %}
              <div class="card" data-type="farmer">
                <div class="card-title">{{ f.name }}</div>
                <div class="card-text muted">{{ f.location }} {% if f.contact %}- {{ f.contact }}{% endif %}</div>
                <div class="card-text"><strong>Products:</strong> {{ f.products }}</div>
              </div>
            {% endfor %}
          {% endif %}
        </div>
      </section>
    </main>
  </div>

  <script>
    // Client-side tab + filter behavior
    const tabs = document.querySelectorAll('.tab-button');
    const contentTitle = document.getElementById('contentTitle');
    const contentList = document.getElementById('contentList');
    const filterInput = document.getElementById('filterInput');

    function showFarmers() {
      contentTitle.textContent = 'Farmers List';
      // show only cards that were server-rendered as farmers (data-type="farmer")
      // If contentList was overwritten with orders, re-rendering would be handled via fetching
      const cards = contentList.querySelectorAll('.card');
      cards.forEach(c => c.style.display = c.getAttribute('data-type') === 'farmer' ? '' : 'none');
    }

    async function showOrders(status) {
      contentTitle.textContent = status === 'ongoing' ? 'Ongoing Orders' : 'Previous Orders';
      contentList.innerHTML = '<div class="muted">Loading...</div>';
      try {
        const res = await fetch('/api/orders?status=' + encodeURIComponent(status));
        if (!res.ok) {
          const txt = await res.text();
          contentList.innerHTML = '<div class="error">Failed to load orders: ' + res.status + '</div>';
          console.error('Orders fetch failed', res.status, txt);
          return;
        }
        const data = await res.json();
        if (!data || data.length === 0) {
          contentList.innerHTML = '<div class="muted">No orders found.</div>';
          return;
        }
        contentList.innerHTML = '';
        data.forEach(item => {
          const div = document.createElement('div');
          div.className = 'card';
          div.setAttribute('data-type','order');
          // try common possible column names (flexible mapping)
          const farmerName = item.farmer_name || item.farmerName || item.farmer || item.farmername || '-';
          const items = item.items || '-';
          const date = item.order_date || item.orderDate || item.orderDate || '-';
          const statusStr = item.status || '-';
          div.innerHTML = `<div class="card-title">Order #${item.id || ''}</div>
            <div class="card-text muted"><strong>Farmer:</strong> ${farmerName}</div>
            <div class="card-text"><strong>Items:</strong> ${items}</div>
            <div class="card-text"><strong>Date:</strong> ${date}</div>
            <span class="pill ${statusStr}">${(statusStr+'').replace(/^./, c => c.toUpperCase())}</span>`;
          contentList.appendChild(div);
        });
      } catch (err) {
        contentList.innerHTML = '<div class="error">Failed to load orders</div>';
        console.error(err);
      }
    }

    tabs.forEach(btn => {
      btn.addEventListener('click', () => {
        tabs.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const tab = btn.dataset.tab;
        if (tab === 'farmers') showFarmers();
        else if (tab === 'ongoing') showOrders('ongoing');
        else showOrders('completed');
      });
    });

    filterInput.addEventListener('input', (e) => {
      const q = (e.target.value || '').toLowerCase();
      const cards = contentList.querySelectorAll('.card');
      cards.forEach(card => {
        const text = (card.textContent || '').toLowerCase();
        card.style.display = text.includes(q) ? '' : 'none';
      });
    });
  </script>
</body>
</html>
"""

# ---------------- Routes ----------------

@app.route("/")
def index():
    # If logged in, go to dashboard
    if session.get("user"):
        return redirect(url_for("dashboard"))
    # build demo auth URL that redirects back to /dashboard
    auth_url = url_for("auth_google", redirect_uri=url_for("dashboard", _external=True))
    return render_template_string(LOGIN_HTML, auth_url=auth_url)

@app.route("/dashboard")
def dashboard():
    if not session.get("user"):
        return redirect(url_for("index"))
    user = session.get("user")
    # fetch farmers
    rows, cols = run_query("SELECT * FROM farmers")
    farmers = rows_to_dicts(rows, cols) if rows is not None else []
    # fetch orders (all)
    rows2, cols2 = run_query("SELECT * FROM orders ORDER BY COALESCE(order_date, CURRENT_DATE) DESC")
    orders = rows_to_dicts(rows2, cols2) if rows2 is not None else []
    # render template with server-side data (orders initially not shown until user clicks tab)
    return render_template_string(DASHBOARD_HTML, user=user, farmers=farmers, orders=orders)

# API endpoints (still useful for AJAX calls by the page)
@app.route("/api/farmers")
def api_farmers():
    rows, cols = run_query("SELECT * FROM farmers")
    if rows is None:
        return jsonify([]), 500
    return jsonify(rows_to_dicts(rows, cols))

@app.route("/api/orders")
def api_orders():
    status = request.args.get("status")
    if status:
        rows, cols = run_query("SELECT * FROM orders WHERE LOWER(status)=LOWER(%s) ORDER BY COALESCE(order_date, CURRENT_DATE) DESC", (status,))
    else:
        rows, cols = run_query("SELECT * FROM orders ORDER BY COALESCE(order_date, CURRENT_DATE) DESC")
    if rows is None:
        return jsonify([]), 500
    return jsonify(rows_to_dicts(rows, cols))

# Demo auth endpoints (replace with real OAuth)
@app.route("/auth/google")
def auth_google():
    demo_user = {
        "displayName": os.environ.get("DEMO_NAME", "Demo User"),
        "email": os.environ.get("DEMO_EMAIL", "demo@example.com"),
        "photoURL": os.environ.get("DEMO_AVATAR", "https://i.pravatar.cc/150?u=demo")
    }
    session["user"] = demo_user
    # redirect back to provided or dashboard
    redirect_to = request.args.get("redirect_uri") or url_for("dashboard")
    return redirect(redirect_to)

@app.route("/auth/logout", methods=["GET", "POST"])
def auth_logout():
    session.pop("user", None)
    return redirect(url_for("index"))

# Health check
@app.route("/health")
def health():
    return jsonify({"ok": True})

# ---------------- Run ----------------
if __name__ == "__main__":
    # Local dev
    app.run(host="0.0.0.0", port=PORT, debug=True)
