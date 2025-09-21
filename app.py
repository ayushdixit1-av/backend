# app.py
"""
Farm Marketplace - single-file Flask app
Features:
- Gmail OAuth-only login (Authlib)
- Role selection after first login (Farmer / Buyer / Admin)
- Farmer and Buyer dashboards
- Listings with multiple images
- Cart managed client-side (localStorage)
- Orders, order_items, reviews, messages
- Basic analytics for farmers
- Uses Neon Postgres (DSN hard-coded per user request)
WARNING: This file contains DB credentials in code. Do NOT publish this.
"""

import os
import re
import uuid
import json
import logging
from datetime import datetime, date
from functools import wraps
from io import BytesIO

from flask import (
    Flask, request, session, redirect, url_for, jsonify,
    render_template_string, flash, send_from_directory, make_response
)
from werkzeug.utils import secure_filename
from authlib.integrations.flask_client import OAuth
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

# ---------------- Config ----------------
APP_NAME = "Farm Marketplace"
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 8MB file uploads
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-please-change")
PORT = int(os.environ.get("PORT", 3000))
APP_BASE_URL = os.environ.get("APP_BASE_URL", "")  # e.g. https://your-app.up.railway.app

# Neon Postgres DSN (embedded as requested)
NEON_DSN = "postgresql://neondb_owner:npg_jgROvpDtrm03@ep-hidden-truth-aev5l7a7-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

# Uploads
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_IMAGES = {"png", "jpg", "jpeg", "gif"}

# Google OAuth config (must set in env for real Gmail OAuth)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- Database Pool ----------------
try:
    pg_pool = psycopg2.pool.SimpleConnectionPool(minconn=1, maxconn=20, dsn=NEON_DSN, cursor_factory=RealDictCursor)
    logger.info("Postgres pool created.")
except Exception as e:
    logger.exception("Failed to create Postgres pool. Exiting.")
    raise

def get_conn():
    return pg_pool.getconn()

def put_conn(conn):
    if conn:
        pg_pool.putconn(conn)

# DB helpers
def db_fetchall(query, params=None):
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(query, params or ())
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.error("db_fetchall error: %s -- SQL: %s -- params: %s", e, query, params)
        if cur:
            try: cur.close()
            except: pass
        return None
    finally:
        if conn:
            put_conn(conn)

def db_fetchone(query, params=None):
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(query, params or ())
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error("db_fetchone error: %s -- SQL: %s -- params: %s", e, query, params)
        if cur:
            try: cur.close()
            except: pass
        return None
    finally:
        if conn:
            put_conn(conn)

def db_commit(query, params=None, returning=False):
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(query, params or ())
        rv = None
        if returning:
            maybe = cur.fetchone()
            if maybe:
                try:
                    rv = maybe[0]
                except Exception:
                    try:
                        rv = list(maybe.values())[0]
                    except:
                        rv = None
        conn.commit()
        cur.close()
        return rv if returning else True
    except Exception as e:
        logger.error("db_commit error: %s -- SQL: %s -- params: %s", e, query, params)
        try:
            if conn: conn.rollback()
        except: pass
        if cur:
            try: cur.close()
            except: pass
        return None if returning else False
    finally:
        if conn:
            put_conn(conn)

# ---------------- Migrations: create tables ----------------
def ensure_schema():
    stmts = [
        # users
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            name TEXT,
            role TEXT NOT NULL DEFAULT 'buyer',
            location TEXT,
            profile_image TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # farmers - optional extra profile for farmers
        """
        CREATE TABLE IF NOT EXISTS farmers (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            farm_name TEXT,
            description TEXT,
            location TEXT,
            certifications TEXT,
            rating NUMERIC DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # products
        """
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            farmer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            name TEXT NOT NULL,
            category TEXT,
            price NUMERIC(12,2) NOT NULL DEFAULT 0,
            unit TEXT,
            quantity INTEGER DEFAULT 0,
            description TEXT,
            status TEXT DEFAULT 'active',
            location TEXT,
            is_organic BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # product images
        """
        CREATE TABLE IF NOT EXISTS product_images (
            id SERIAL PRIMARY KEY,
            product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
            path TEXT NOT NULL
        );
        """,
        # orders
        """
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            buyer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            farmer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            total_amount NUMERIC(12,2) DEFAULT 0,
            status TEXT DEFAULT 'pending',
            delivery_date DATE,
            address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # order_items
        """
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
            product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
            quantity INTEGER,
            price NUMERIC(12,2)
        );
        """,
        # reviews
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id SERIAL PRIMARY KEY,
            buyer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            farmer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
            rating INTEGER CHECK (rating >=1 AND rating <= 5),
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # messages (simple farmer-buyer messaging)
        """
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            sender_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            receiver_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            subject TEXT,
            body TEXT,
            read BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # cart: lightweight server-side persistent cart (also client uses localStorage)
        """
        CREATE TABLE IF NOT EXISTS carts (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            product_id INTEGER REFERENCES products(id),
            quantity INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # indexes
        "CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);",
        "CREATE INDEX IF NOT EXISTS idx_products_farmer ON products(farmer_id);",
        "CREATE INDEX IF NOT EXISTS idx_orders_buyer ON orders(buyer_id);"
    ]
    for s in stmts:
        ok = db_commit(s)
        if not ok:
            logger.error("Migration failed for statement: %s", s)

ensure_schema()

# ---------------- Utilities ----------------
EMAIL_RE = re.compile(r"^[^@]+@gmail\.com$")  # enforce Gmail-only
def is_gmail(email):
    return bool(email and EMAIL_RE.match(email.strip().lower()))

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGES

def save_upload(file):
    if not file or file.filename == "":
        return None
    if not allowed_file(file.filename):
        return None
    name = secure_filename(file.filename)
    unique = f"{uuid.uuid4().hex}_{name}"
    dest = os.path.join(UPLOAD_DIR, unique)
    file.save(dest)
    return unique

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please sign in.")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

def role_required(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("user_id"):
                flash("Please sign in.")
                return redirect(url_for("login"))
            if session.get("role") not in roles:
                flash("Access denied for your role.")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapper
    return deco

# ---------------- OAuth (Google Gmail-only) ----------------
oauth = OAuth(app)
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and APP_BASE_URL:
    oauth.register(
        name='google',
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'}
    )
    OAUTH_READY = True
else:
    OAUTH_READY = False
    logger.warning("Google OAuth not configured. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and APP_BASE_URL env vars for Gmail OAuth.")

# ---------------- Single-file HTML template ----------------
# To keep single-file requirement: embed CSS and JS in this template and render via render_template_string
BASE_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{{ title or 'Farm Marketplace' }}</title>
<style>
:root{--green:#2f8f3a;--dark:#0f172a;--muted:#64748b;--card:#fff}
body{font-family:Inter, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial; margin:0;background:#f4faf6;color:var(--dark)}
.topbar{background:var(--card);border-bottom:1px solid #e6eef2;padding:12px 20px;display:flex;align-items:center;justify-content:space-between}
.brand{font-weight:700;color:var(--green);font-size:1.2rem}
.nav{display:flex;gap:10px;align-items:center}
.btn{background:var(--green);color:white;padding:8px 12px;border-radius:8px;border:none;cursor:pointer}
.card{background:var(--card);padding:16px;border-radius:12px;border:1px solid #e6eef2;box-shadow:0 6px 18px rgba(15,23,42,0.03)}
.container{max-width:1100px;margin:18px auto;padding:0 12px}
.grid{display:grid;gap:16px}
@media(min-width:900px){.grid{grid-template-columns:300px 1fr}}
.listing-grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.product-card img{width:100%;height:160px;object-fit:cover;border-radius:8px}
.search-row{display:flex;gap:8px;align-items:center;margin-bottom:12px}
.input{padding:8px;border:1px solid #e6eef2;border-radius:8px;width:100%}
.small{font-size:0.9rem;color:var(--muted)}
.badge{display:inline-block;padding:4px 8px;border-radius:999px;background:#e6faf0;color:var(--green);font-weight:600}
.notice{padding:8px;background:#fffbeb;border-radius:8px;border:1px solid #fce6b5;color:#7a4b00}
.footer{padding:28px 0;text-align:center;color:var(--muted);font-size:0.9rem}
.list-item{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px dashed #eef6ef}
.form-row{margin-bottom:10px}
.label{display:block;margin-bottom:6px;font-weight:600}
.btn-secondary{background:#fff;border:1px solid #e6eef2;color:var(--dark)}
.kv{display:flex;gap:8px;align-items:center}
</style>
</head>
<body>
  <div class="topbar">
    <div class="brand">🚜 Farm Marketplace</div>
    <div class="nav">
      <a href="{{ url_for('index') }}" class="small">Home</a>
      <a href="{{ url_for('market') }}" class="small">Marketplace</a>
      {% if session.user_id %}
        {% if session.role == 'farmer' %}
        <a href="{{ url_for('farmer_dashboard') }}" class="small">Farmer</a>
        {% endif %}
        {% if session.role == 'buyer' %}
        <a href="{{ url_for('cart') }}" class="small">Cart</a>
        {% endif %}
        {% if session.role == 'admin' %}
        <a href="{{ url_for('admin') }}" class="small">Admin</a>
        {% endif %}
        <span class="small">{{ session.user_email }}</span>
        <a href="{{ url_for('logout') }}" class="btn btn-secondary">Logout</a>
      {% else %}
        {% if oauth_ready %}
          <a href="{{ url_for('login') }}" class="btn">Sign in with Google</a>
        {% else %}
          <a href="{{ url_for('login') }}" class="btn">Sign in (configure Google)</a>
        {% endif %}
      {% endif %}
    </div>
  </div>

  <div class="container">
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        <div style="margin:12px 0">
          {% for m in messages %}<div class="notice">{{ m }}</div>{% endfor %}
        </div>
      {% endif %}
    {% endwith %}
    {{ body|safe }}
  </div>

  <div class="footer">Built with care • No payments integrated • Use Gmail OAuth for sign-in.</div>

<script>
// Client-side helper: localStorage cart management
window.app = {
  addToCart: function(product, qty){
    qty = parseInt(qty)||1;
    const key = 'cart_v1';
    let cart = JSON.parse(localStorage.getItem(key) || '{}');
    cart[product] = (cart[product]||0) + qty;
    localStorage.setItem(key, JSON.stringify(cart));
    alert('Added to cart');
  },
  getCart: function(){ return JSON.parse(localStorage.getItem('cart_v1')||'{}'); },
  setCart: function(c){ localStorage.setItem('cart_v1', JSON.stringify(c)); },
  clearCart: function(){ localStorage.removeItem('cart_v1'); }
};

// on pages where server wants to read local cart, it can POST /cart/sync with JSON
</script>
</body>
</html>
"""

# ---------------- Routes: auth, role selection ----------------
@app.route("/login")
def login():
    if not OAUTH_READY:
        # show instructions and fallback to mock sign-in
        body = """
        <div class="card"><h2>Google OAuth not configured</h2>
        <p class="small">To enable Gmail-only OAuth sign-in, set environment variables GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and APP_BASE_URL (your deployed URL) and redeploy.</p>
        <p>If you want to mock-sign in for testing, use the form below (Gmail enforced).</p>
        <form method="post" action="/auth/mock">
          <div class="form-row"><label class="label">Gmail address</label><input name="email" class="input" required></div>
          <div class="form-row"><label class="label">Display name</label><input name="name" class="input"></div>
          <div class="form-row"><label class="label">Role</label><select name="role" class="input"><option>buyer</option><option>farmer</option><option>admin</option></select></div>
          <button class="btn">Mock Sign in</button>
        </form></div>
        """
        return render_template_string(BASE_HTML, title="Login", body=body, oauth_ready=False)
    redirect_uri = APP_BASE_URL.rstrip("/") + url_for('auth_callback')
    return oauth.google.authorize_redirect(redirect_uri)

@app.route("/auth/callback")
def auth_callback():
    if not OAUTH_READY:
        flash("OAuth not configured.")
        return redirect(url_for("login"))
    token = oauth.google.authorize_access_token()
    userinfo = oauth.google.parse_id_token(token)
    # userinfo contains 'email', 'email_verified', 'name', 'picture'
    email = userinfo.get('email')
    if not email or not is_gmail(email):
        flash("Please sign in with a Gmail address.")
        return redirect(url_for('index'))
    name = userinfo.get('name') or email.split('@')[0]
    # upsert user
    existing = db_fetchone("SELECT * FROM users WHERE email = %s", (email,))
    if not existing:
        uid = db_commit("INSERT INTO users (email, name) VALUES (%s,%s) RETURNING id", (email, name), returning=True)
        if not uid:
            flash("DB error creating user.")
            return redirect(url_for("index"))
        # default role = buyer; after first login we prompt for role selection
        session['user_id'] = uid
        session['user_email'] = email
        session['role'] = None
        session['user_name'] = name
        return redirect(url_for('choose_role'))
    # existing user
    session['user_id'] = existing['id']
    session['user_email'] = existing['email']
    session['role'] = existing.get('role')
    session['user_name'] = existing.get('name') or existing.get('email')
    flash(f"Welcome back, {session['user_name']}")
    if not session['role']:
        return redirect(url_for('choose_role'))
    return redirect(url_for('index'))

@app.route("/auth/mock", methods=['POST'])
def auth_mock():
    # only used if OAuth not configured — create user if gmail pattern, allow selecting role
    email = (request.form.get('email') or '').strip().lower()
    name = (request.form.get('name') or '').strip()
    role = (request.form.get('role') or 'buyer')
    if not is_gmail(email):
        flash("Please use a Gmail address.")
        return redirect(url_for('login'))
    existing = db_fetchone("SELECT * FROM users WHERE email = %s", (email,))
    if not existing:
        uid = db_commit("INSERT INTO users (email, name, role) VALUES (%s,%s,%s) RETURNING id", (email, name or None, role), returning=True)
        if not uid:
            flash("DB error.")
            return redirect(url_for('login'))
        session['user_id'] = uid
    else:
        # update role if provided
        db_commit("UPDATE users SET role = %s, name = %s WHERE id = %s", (role, name or existing.get('name'), existing['id']))
        session['user_id'] = existing['id']
    session['user_email'] = email
    session['user_name'] = name or email.split('@')[0]
    session['role'] = role
    flash("Signed in (mock).")
    return redirect(url_for('index'))

@app.route("/choose-role", methods=['GET', 'POST'])
@login_required
def choose_role():
    if request.method == 'POST':
        role = request.form.get('role')
        if role not in ('farmer', 'buyer', 'admin'):
            flash("Invalid role.")
            return redirect(url_for('choose_role'))
        db_commit("UPDATE users SET role = %s WHERE id = %s", (role, session['user_id']))
        session['role'] = role
        flash(f"Role set to {role}")
        return redirect(url_for('profile'))
    body = """
    <div class="card"><h2>Choose your role</h2>
    <p class="small">Are you a farmer selling produce, a buyer, or an admin?</p>
    <form method="post">
      <div class="form-row"><label class="label">Role</label>
        <select name="role" class="input"><option value="farmer">Farmer</option><option value="buyer">Buyer</option><option value="admin">Admin</option></select>
      </div>
      <button class="btn">Continue</button>
    </form></div>
    """
    return render_template_string(BASE_HTML, body=body, title="Choose role")

@app.route("/profile", methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        name = request.form.get('name')
        location = request.form.get('location')
        profile_image = None
        f = request.files.get('profile_image')
        if f:
            profile_image = save_upload(f)
        db_commit("UPDATE users SET name=%s, location=%s, profile_image=%s WHERE id=%s", (name, location, profile_image, session['user_id']))
        flash("Profile updated.")
        return redirect(url_for('index'))
    user = db_fetchone("SELECT * FROM users WHERE id = %s", (session['user_id'],))
    body = f"""
    <div class="card"><h2>Complete your profile</h2>
      <form method="post" enctype="multipart/form-data">
        <div class="form-row"><label class="label">Display name</label><input class="input" name="name" value="{user.get('name') if user else ''}"></div>
        <div class="form-row"><label class="label">Location</label><input class="input" name="location" value="{user.get('location') if user else ''}"></div>
        <div class="form-row"><label class="label">Profile image</label><input type="file" name="profile_image"></div>
        <button class="btn">Save</button>
      </form>
    </div>
    """
    return render_template_string(BASE_HTML, body=body, title="Profile")

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for('index'))

# ---------------- Public pages ----------------
@app.route("/")
def index():
    # featured products + farmers
    prods = db_fetchall("SELECT p.*, u.name as farmer_name FROM products p LEFT JOIN users u ON p.farmer_id = u.id WHERE p.status='active' ORDER BY p.created_at DESC LIMIT 6") or []
    farmers = db_fetchall("SELECT u.id,u.name, f.farm_name FROM users u JOIN farmers f ON u.id=f.user_id ORDER BY f.created_at DESC LIMIT 6") or []
    # build HTML
    cards = ""
    for p in prods:
        img_row = db_fetchall("SELECT path FROM product_images WHERE product_id = %s ORDER BY id LIMIT 1", (p['id'],)) or []
        img = img_row[0]['path'] if img_row else ''
        cards += f"""
        <div class="product-card card">
          <img src="{url_for('uploaded_file', filename=img)}" onerror="this.src='https://via.placeholder.com/400x300?text=No+image'">
          <h3>{p['name']}</h3>
          <div class="small">By {p.get('farmer_name') or 'Unknown'}</div>
          <div class="kv" style="margin-top:8px"><div class="badge">₹{float(p['price']):.2f}</div><div class="small">Stock: {p.get('quantity',0)}</div></div>
        </div>
        """
    farmers_html = "".join([f"<div class='card small'>{f.get('farm_name') or ''} — {f.get('name')}</div>" for f in farmers])
    body = f"""
    <div class="grid">
      <div class="card">
        <h2>Featured Products</h2>
        <div class="listing-grid">{cards}</div>
      </div>
      <div>
        <div class="card"><h3>Featured Farmers</h3>{farmers_html}</div>
        <div style="height:12px"></div>
        <div class="card"><h3>Categories</h3>
          <div class="small">Fresh Vegetables • Fruits • Grains & Cereals • Dairy • Meat & Poultry • Organic • Herbs & Spices</div>
        </div>
      </div>
    </div>
    """
    return render_template_string(BASE_HTML, body=body, title="Home", oauth_ready=OAUTH_READY)

# ---------------- Marketplace: catalog, product view, search ----------------
@app.route("/market")
def market():
    q = (request.args.get('q') or '').strip()
    category = (request.args.get('category') or '').strip()
    location = (request.args.get('location') or '').strip()
    organic = request.args.get('organic')
    sort = request.args.get('sort')  # price_asc, price_desc, newest
    params = []
    sql = "SELECT p.*, u.name as farmer_name FROM products p LEFT JOIN users u ON p.farmer_id = u.id WHERE p.status='active'"
    if q:
        sql += " AND (p.name ILIKE %s OR p.description ILIKE %s OR u.name ILIKE %s)"
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if category:
        sql += " AND p.category = %s"; params.append(category)
    if location:
        sql += " AND p.location ILIKE %s"; params.append(f"%{location}%")
    if organic:
        sql += " AND p.is_organic = TRUE"
    if sort == 'price_asc':
        sql += " ORDER BY p.price ASC"
    elif sort == 'price_desc':
        sql += " ORDER BY p.price DESC"
    else:
        sql += " ORDER BY p.created_at DESC"
    sql += " LIMIT 200"
    rows = db_fetchall(sql, tuple(params))
    # render product cards
    cards = ""
    for p in rows or []:
        imgs = db_fetchall("SELECT path FROM product_images WHERE product_id=%s ORDER BY id LIMIT 1", (p['id'],)) or []
        img = imgs[0]['path'] if imgs else ''
        cards += f"""
        <div class="product-card card">
          <img src="{url_for('uploaded_file', filename=img)}" onerror="this.src='https://via.placeholder.com/400x300?text=No+image'">
          <h3>{p['name']}</h3>
          <div class="small">By {p.get('farmer_name') or 'Unknown'}</div>
          <div class="kv" style="margin-top:8px">
            <div class="badge">₹{float(p['price']):.2f}</div>
            <div class="small">Stock: {p.get('quantity',0)}</div>
          </div>
          <div style="margin-top:8px">
            <a href="{url_for('product_view', product_id=p['id'])}">View</a>
            <button onclick="app.addToCart({p['id']},1)" class="btn btn-secondary">Add</button>
          </div>
        </div>
        """
    body = f"""
    <div class="card">
      <form method="get" class="search-row">
        <input class="input" name="q" placeholder="Search products or farmers" value="{q}">
        <input class="input" name="location" placeholder="Location" value="{location}">
        <select name="category" class="input">
          <option value="">All categories</option>
          <option value="Fresh Vegetables" {"selected" if category=="Fresh Vegetables" else ""}>Fresh Vegetables</option>
          <option value="Fruits" {"selected" if category=="Fruits" else ""}>Fruits</option>
          <option value="Grains & Cereals" {"selected" if category=="Grains & Cereals" else ""}>Grains & Cereals</option>
          <option value="Dairy Products" {"selected" if category=="Dairy Products" else ""}>Dairy Products</option>
          <option value="Meat & Poultry" {"selected" if category=="Meat & Poultry" else ""}>Meat & Poultry</option>
          <option value="Organic Products" {"selected" if category=="Organic Products" else ""}>Organic Products</option>
          <option value="Herbs & Spices" {"selected" if category=="Herbs & Spices" else ""}>Herbs & Spices</option>
        </select>
        <label class="small">Organic <input type="checkbox" name="organic" value="1" {"checked" if organic else ""}></label>
        <select name="sort" class="input"><option value="">Newest</option><option value="price_asc" {"selected" if sort=='price_asc' else ""}>Price low→high</option><option value="price_desc" {"selected" if sort=='price_desc' else ""}>Price high→low</option></select>
        <button class="btn">Filter</button>
      </form>
      <div class="listing-grid" style="margin-top:12px">{cards}</div>
    </div>
    """
    return render_template_string(BASE_HTML, body=body, title="Marketplace")

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)

@app.route("/product/<int:product_id>")
def product_view(product_id):
    p = db_fetchone("SELECT p.*, u.name as farmer_name, u.email as farmer_email FROM products p LEFT JOIN users u ON p.farmer_id = u.id WHERE p.id = %s", (product_id,))
    if not p:
        flash("Product not found.")
        return redirect(url_for('market'))
    imgs = db_fetchall("SELECT path FROM product_images WHERE product_id = %s ORDER BY id", (product_id,)) or []
    images_html = "".join([f'<img src="{url_for("uploaded_file", filename=i["path"])}" style="max-width:200px;margin-right:6px">' for i in imgs])
    # reviews
    reviews = db_fetchall("SELECT r.*, u.name as buyer_name FROM reviews r LEFT JOIN users u ON r.buyer_id = u.id WHERE r.product_id = %s ORDER BY r.created_at DESC LIMIT 50", (product_id,)) or []
    reviews_html = "".join([f'<div class="card small">{r["buyer_name"]} — {r["rating"]}/5<br>{r["comment"]}</div>' for r in reviews])
    body = f"""
    <div class="grid">
      <div class="card">
        <div style="display:flex;gap:12px">
          <div style="min-width:320px">{images_html or '<div style=color:#aaa>no images</div>'}</div>
          <div>
            <h2>{p['name']}</h2>
            <div class="small">By {p['farmer_name']} • {p.get('category') or ''}</div>
            <div style="margin-top:8px"><strong>₹{float(p['price']):.2f}</strong> • Stock: {p.get('quantity',0)}</div>
            <p class="small">{p.get('description') or ''}</p>
            <div style="margin-top:10px">
              <button onclick="app.addToCart({p['id']},1)" class="btn">Add to cart</button>
              {% if session.role == 'buyer' %}<a class="btn btn-secondary" href="{{ url_for('cart') }}">View Cart</a>{% endif %}
            </div>
          </div>
        </div>
      </div>

      <div>
        <div class="card"><h3>Farmer</h3><div class="small">{p.get('farmer_name')} • {p.get('location') or ''} • {p.get('is_organic') and 'Organic' or ''}</div></div>
        <div style="height:12px"></div>
        <div class="card"><h3>Reviews</h3>{reviews_html or '<div class=small>No reviews yet.</div>'}</div>
      </div>
    </div>
    """
    return render_template_string(BASE_HTML, body=body, title=p['name'])

# ---------------- Farmer dashboard: manage products, orders, analytics ----------------
@app.route("/farmer")
@login_required
@role_required('farmer')
def farmer_dashboard():
    uid = session['user_id']
    # products
    products = db_fetchall("SELECT * FROM products WHERE farmer_id = %s ORDER BY created_at DESC", (uid,)) or []
    prod_rows = ""
    for p in products:
        prod_rows += f"<div class='list-item'><div><strong>{p['name']}</strong><div class='small'>Stock: {p['quantity']} • ₹{float(p['price']):.2f}</div></div><div><a href='{url_for('edit_product', product_id=p['id'])}'>Edit</a></div></div>"
    # orders
    orders = db_fetchall("SELECT * FROM orders WHERE farmer_id = %s ORDER BY created_at DESC LIMIT 50", (uid,)) or []
    order_rows = ""
    for o in orders:
        order_rows += f"<div class='list-item'><div>Order #{o['id']} • ₹{float(o['total_amount']):.2f} • {o['status']}</div><div><a href='{url_for('view_order', order_id=o['id'])}'>View</a></div></div>"
    # simple analytics
    total_sales = db_fetchone("SELECT COALESCE(SUM(total_amount),0) as s FROM orders WHERE farmer_id=%s AND status='completed'", (uid,))
    total_products = db_fetchone("SELECT COUNT(*) as c FROM products WHERE farmer_id=%s", (uid,))
    body = f"""
    <div class="grid">
      <div class="card">
        <h2>Farmer Dashboard</h2>
        <div class="small">Earnings: ₹{float(total_sales['s'] or 0):.2f} • Products: {total_products['c']}</div>
        <h3 style="margin-top:12px">Add Product</h3>
        <form method="post" action="{url_for('create_product')}" enctype="multipart/form-data">
          <div class="form-row"><input class="input" name="name" placeholder="Product name" required></div>
          <div class="form-row"><input class="input" name="category" placeholder="Category"></div>
          <div class="form-row"><input class="input" name="price" placeholder="Price"></div>
          <div class="form-row"><input class="input" name="unit" placeholder="Unit (kg, piece)"></div>
          <div class="form-row"><input class="input" name="quantity" placeholder="Quantity"></div>
          <div class="form-row"><textarea class="input" name="description" placeholder="Description"></textarea></div>
          <div class="form-row"><label>Images (multiple)</label><input type="file" name="images" multiple></div>
          <button class="btn">Create</button>
        </form>
      </div>

      <div>
        <div class="card"><h3>Your Products</h3>{prod_rows or '<div class=small>No products yet.</div>'}</div>
        <div style="height:12px"></div>
        <div class="card"><h3>Recent Orders</h3>{order_rows or '<div class=small>No orders yet.</div>'}</div>
      </div>
    </div>
    """
    return render_template_string(BASE_HTML, body=body, title="Farmer Dashboard")

@app.route("/product/create", methods=['POST'])
@login_required
@role_required('farmer')
def create_product():
    name = request.form.get('name')
    category = request.form.get('category')
    price = float(request.form.get('price') or 0)
    unit = request.form.get('unit')
    quantity = int(request.form.get('quantity') or 0)
    description = request.form.get('description')
    is_organic = bool(request.form.get('is_organic'))
    location = request.form.get('location') or session.get('user_name')
    farmer_id = session['user_id']
    pid = db_commit("INSERT INTO products (farmer_id,name,category,price,unit,quantity,description,is_organic,location) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (farmer_id, name, category, price, unit, quantity, description, is_organic, location), returning=True)
    if not pid:
        flash("Failed to create product.")
        return redirect(url_for('farmer_dashboard'))
    files = request.files.getlist('images')
    for f in files:
        saved = save_upload(f)
        if saved:
            db_commit("INSERT INTO product_images (product_id, path) VALUES (%s,%s)", (pid, saved))
    flash("Product created.")
    return redirect(url_for('farmer_dashboard'))

@app.route("/product/<int:product_id>/edit", methods=['GET','POST'])
@login_required
@role_required('farmer')
def edit_product(product_id):
    p = db_fetchone("SELECT * FROM products WHERE id = %s", (product_id,))
    if not p or p['farmer_id'] != session['user_id']:
        flash("Not allowed.")
        return redirect(url_for('farmer_dashboard'))
    if request.method == 'POST':
        name = request.form.get('name')
        price = float(request.form.get('price') or 0)
        quantity = int(request.form.get('quantity') or 0)
        description = request.form.get('description')
        db_commit("UPDATE products SET name=%s, price=%s, quantity=%s, description=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                  (name, price, quantity, description, product_id))
        files = request.files.getlist('images')
        for f in files:
            saved = save_upload(f)
            if saved:
                db_commit("INSERT INTO product_images (product_id, path) VALUES (%s,%s)", (product_id, saved))
        flash("Product updated.")
        return redirect(url_for('farmer_dashboard'))
    imgs = db_fetchall("SELECT * FROM product_images WHERE product_id = %s", (product_id,)) or []
    imgs_html = "".join([f"<div class='small'>{i['path']}</div>" for i in imgs])
    body = f"""
    <div class="card"><h3>Edit Product</h3>
      <form method="post" enctype="multipart/form-data">
        <div class="form-row"><input class="input" name="name" value="{p['name']}"></div>
        <div class="form-row"><input class="input" name="price" value="{float(p['price']):.2f}"></div>
        <div class="form-row"><input class="input" name="quantity" value="{p.get('quantity',0)}"></div>
        <div class="form-row"><textarea class="input" name="description">{p.get('description') or ''}</textarea></div>
        <div class="form-row"><label>Images</label><input type="file" name="images" multiple></div>
        <div>{imgs_html}</div>
        <button class="btn">Save</button>
      </form>
    </div>
    """
    return render_template_string(BASE_HTML, body=body, title="Edit Product")

# ---------------- Buyer: cart (client-side), checkout (mock), orders, reviews ----------------
@app.route("/cart")
@login_required
@role_required('buyer')
def cart():
    # server will not store cart (client localStorage). Provide sync endpoints to persist optional server-side cart
    body = """
    <div class="card">
      <h2>Your Cart</h2>
      <p class="small">This demo uses client-side cart stored in your browser. On checkout we create an order using current cart state.</p>
      <div style="margin:12px 0">
        <button onclick="syncAndCheckout()" class="btn">Proceed to Checkout (mock)</button>
      </div>
      <div id="cart-contents"></div>
    </div>
    <script>
    function renderCart(){
      const c = JSON.parse(localStorage.getItem('cart_v1')||'{}');
      const container = document.getElementById('cart-contents');
      if(!Object.keys(c).length){ container.innerHTML='<div class=small>Your cart is empty.</div>'; return; }
      let html = '<table border=1 cellpadding=6 style="width:100%"><tr><th>Item</th><th>Qty</th><th>Action</th></tr>';
      const ids = Object.keys(c);
      fetch('/api/products/batch', {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids:ids})
      }).then(r=>r.json()).then(data=>{
        let total=0;
        for(const it of data){
          const qty = c[it.id];
          html += '<tr><td>'+it.name+'</td><td>'+qty+'</td><td><button onclick="remove('+it.id+')">Remove</button></td></tr>';
          total += parseFloat(it.price||0)*qty;
        }
        html += '</table><h3>Total: ₹'+total.toFixed(2)+'</h3>';
        container.innerHTML = html;
      });
    }
    function remove(id){ const c=JSON.parse(localStorage.getItem('cart_v1')||'{}'); delete c[id]; localStorage.setItem('cart_v1',JSON.stringify(c)); renderCart(); }
    function syncAndCheckout(){
      const c = JSON.parse(localStorage.getItem('cart_v1')||'{}');
      if(!Object.keys(c).length){ alert('Cart empty'); return; }
      fetch('/checkout', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({cart:c})}).then(r=>r.json()).then(j=>{
        if(j.ok){ localStorage.removeItem('cart_v1'); alert('Order created: '+j.order_id); window.location='/orders'; } else alert('Error: '+(j.error||'unknown')) ;
      });
    }
    renderCart();
    </script>
    """
    return render_template_string(BASE_HTML, body=body, title="Cart")

@app.route("/api/products/batch", methods=['POST'])
def api_products_batch():
    data = request.get_json() or {}
    ids = data.get('ids') or []
    if not ids:
        return jsonify([])
    # sanitize ids as ints
    clean = [int(x) for x in ids]
    rows = db_fetchall("SELECT id,name,price FROM products WHERE id = ANY(%s)", (clean,))
    return jsonify(rows or [])

@app.route("/checkout", methods=['POST'])
@login_required
@role_required('buyer')
def checkout_api():
    data = request.get_json() or {}
    cart = data.get('cart') or {}
    if not cart:
        return jsonify({'ok': False, 'error': 'Cart empty'}), 400
    # build order per farmer: group by farmer_id
    items = []
    product_ids = [int(k) for k in cart.keys()]
    prods = db_fetchall("SELECT * FROM products WHERE id = ANY(%s)", (product_ids,)) or []
    prod_map = {p['id']: p for p in prods}
    # group by farmer
    groups = {}
    total_amount = 0
    for pid_str, qty in cart.items():
        pid = int(pid_str)
        p = prod_map.get(pid)
        if not p:
            continue
        farmer_id = p['farmer_id']
        line = float(p['price'] or 0) * int(qty)
        total_amount += line
        groups.setdefault(farmer_id, []).append((pid, int(qty), float(p['price'] or 0)))
    created_orders = []
    for farmer_id, items_list in groups.items():
        # create order
        order_id = db_commit("INSERT INTO orders (buyer_id, farmer_id, total_amount, status) VALUES (%s,%s,%s,%s) RETURNING id",
                             (session['user_id'], farmer_id, sum(q*price for (_,q,price) in items_list), 'pending'), returning=True)
        if not order_id:
            continue
        for pid, qty, price in items_list:
            db_commit("INSERT INTO order_items (order_id, product_id, quantity, price) VALUES (%s,%s,%s,%s)", (order_id, pid, qty, price))
            # decrement stock
            db_commit("UPDATE products SET quantity = GREATEST(quantity - %s, 0) WHERE id = %s", (qty, pid))
        created_orders.append(order_id)
    if not created_orders:
        return jsonify({'ok': False, 'error': 'Failed to create orders'}), 500
    return jsonify({'ok': True, 'order_id': created_orders[0], 'orders': created_orders})

@app.route("/orders")
@login_required
def orders():
    uid = session['user_id']
    if session['role']=='buyer':
        rows = db_fetchall("SELECT * FROM orders WHERE buyer_id = %s ORDER BY created_at DESC", (uid,)) or []
    elif session['role']=='farmer':
        rows = db_fetchall("SELECT * FROM orders WHERE farmer_id = %s ORDER BY created_at DESC", (uid,)) or []
    else:
        rows = db_fetchall("SELECT * FROM orders ORDER BY created_at DESC LIMIT 200") or []
    rows_html = ""
    for o in rows:
        rows_html += f"<div class='list-item'><div>Order #{o['id']} • ₹{float(o['total_amount']):.2f} • {o['status']}</div><div><a href='{url_for('view_order', order_id=o['id'])}'>View</a></div></div>"
    body = f"<div class='card'><h2>Your Orders</h2>{rows_html or '<div class=small>No orders found.</div>'}</div>"
    return render_template_string(BASE_HTML, body=body, title="Orders")

@app.route("/order/<int:order_id>")
@login_required
def view_order(order_id):
    o = db_fetchone("SELECT * FROM orders WHERE id = %s", (order_id,))
    if not o:
        flash("Order not found.")
        return redirect(url_for('orders'))
    items = db_fetchall("SELECT oi.*, p.name FROM order_items oi LEFT JOIN products p ON oi.product_id = p.id WHERE oi.order_id = %s", (order_id,)) or []
    items_html = "".join([f"<div class='small'>{it['name']} • {it['quantity']} x ₹{float(it['price']):.2f}</div>" for it in items])
    # allow farmer to update status
    update_btn = ""
    if session['role']=='farmer' and o['farmer_id']==session['user_id']:
        update_btn = f"""
        <form method="post" action="{url_for('update_order_status', order_id=order_id)}">
          <select name="status" class="input"><option>pending</option><option>confirmed</option><option>packed</option><option>delivered</option></select>
          <button class="btn">Update status</button>
        </form>
        """
    body = f"<div class='card'><h2>Order #{o['id']}</h2><div class='small'>Total ₹{float(o['total_amount']):.2f} • Status: {o['status']}</div><div style='margin-top:12px'>{items_html}</div>{update_btn}</div>"
    return render_template_string(BASE_HTML, body=body, title=f"Order {order_id}")

@app.route("/order/<int:order_id>/status", methods=['POST'])
@login_required
@role_required('farmer')
def update_order_status(order_id):
    new = request.form.get('status')
    if new not in ('pending','confirmed','packed','delivered','cancelled'):
        flash("Invalid status.")
        return redirect(url_for('view_order', order_id=order_id))
    db_commit("UPDATE orders SET status=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (new, order_id))
    flash("Status updated.")
    return redirect(url_for('view_order', order_id=order_id))

@app.route("/review/<int:product_id>", methods=['POST'])
@login_required
@role_required('buyer')
def post_review(product_id):
    rating = int(request.form.get('rating') or 0)
    comment = request.form.get('comment') or ''
    prod = db_fetchone("SELECT * FROM products WHERE id = %s", (product_id,))
    if not prod:
        flash("Product missing.")
        return redirect(url_for('market'))
    db_commit("INSERT INTO reviews (buyer_id, farmer_id, product_id, rating, comment) VALUES (%s,%s,%s,%s,%s)", (session['user_id'], prod['farmer_id'], product_id, rating, comment))
    flash("Thanks for your review.")
    return redirect(url_for('product_view', product_id=product_id))

# ---------------- Messaging ----------------
@app.route("/messages", methods=['GET','POST'])
@login_required
def messages():
    if request.method=='POST':
        to = int(request.form.get('to'))
        subject = request.form.get('subject')
        body = request.form.get('body')
        db_commit("INSERT INTO messages (sender_id, receiver_id, subject, body) VALUES (%s,%s,%s,%s)", (session['user_id'], to, subject, body))
        flash("Message sent.")
        return redirect(url_for('messages'))
    # show inbox
    inbox = db_fetchall("SELECT m.*, u.name as sender_name FROM messages m LEFT JOIN users u ON m.sender_id = u.id WHERE m.receiver_id = %s ORDER BY m.created_at DESC", (session['user_id'],)) or []
    rows = "".join([f"<div class='list-item'><div><strong>{r['subject']}</strong><div class='small'>From {r['sender_name']} • {r['created_at']}</div></div></div>" for r in inbox])
    body = f"<div class='card'><h2>Messages</h2>{rows}</div>"
    return render_template_string(BASE_HTML, body=body, title="Messages")

# ---------------- Admin ----------------
@app.route("/admin")
@login_required
@role_required('admin')
def admin():
    users = db_fetchall("SELECT * FROM users ORDER BY id DESC") or []
    orders = db_fetchall("SELECT * FROM orders ORDER BY created_at DESC LIMIT 100") or []
    users_html = "".join([f"<div class='list-item'><div>{u['email']} • {u.get('role')}</div><div><form method='post' action='{url_for('admin_change_role', user_id=u['id'])}'><select name='role'><option value='buyer' {'selected' if u.get('role')=='buyer' else ''}>buyer</option><option value='farmer' {'selected' if u.get('role')=='farmer' else ''}>farmer</option><option value='admin' {'selected' if u.get('role')=='admin' else ''}>admin</option></select><button class='btn btn-secondary'>Change</button></form></div></div>" for u in users])
    orders_html = "".join([f"<div class='list-item'><div>Order #{o['id']} • {o['status']}</div></div>" for o in orders])
    body = f"<div class='grid'><div class='card'><h3>Users</h3>{users_html}</div><div class='card'><h3>Recent Orders</h3>{orders_html}</div></div>"
    return render_template_string(BASE_HTML, body=body, title="Admin")

@app.route("/admin/user/<int:user_id>/role", methods=['POST'])
@login_required
@role_required('admin')
def admin_change_role(user_id):
    new = request.form.get('role')
    db_commit("UPDATE users SET role=%s WHERE id=%s", (new, user_id))
    flash("Role updated.")
    return redirect(url_for('admin'))

# ---------------- API: basic endpoints for front-end JS if needed ----------------
@app.route("/api/products/<int:product_id>/images")
def api_product_images(product_id):
    imgs = db_fetchall("SELECT path FROM product_images WHERE product_id=%s ORDER BY id", (product_id,)) or []
    return jsonify(imgs)

@app.route("/health")
def health():
    ok = db_fetchone("SELECT 1 as ok")
    return jsonify({'status':'ok' if ok else 'db-error', 'time': datetime.utcnow().isoformat()})

# ---------------- Run ----------------
if __name__ == "__main__":
    logger.info("Starting app on port %s", PORT)
    # For local testing you must set APP_BASE_URL to http://localhost:3000 for OAuth redirect building.
    app.run(host="0.0.0.0", port=PORT, debug=True)
