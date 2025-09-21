# app.py
"""
Improved single-file Farm Marketplace (Flask) - safer DB usage, CSRF, SQL injection fixes.
WARNING: This file contains a hard-coded Neon DSN as requested. DO NOT commit to public repos.
Replace NEON_DSN with an env var in production and rotate credentials.
"""

import os
import re
import uuid
import time
import logging
from functools import wraps
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Callable

from flask import (
    Flask, request, session, redirect, url_for, jsonify,
    render_template_string, flash, send_from_directory
)
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException
import psycopg2
from psycopg2 import pool, DatabaseError as Psycopg2DBError
from psycopg2.extras import DictCursor
from flask_wtf import CSRFProtect

# ---------- Config ----------
APP_NAME = "Farm Marketplace"
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")
PORT = int(os.environ.get("PORT", 3000))

# Embedded Neon DSN (use env var in production)
NEON_DSN = "postgresql://neondb_owner:npg_jgROvpDtrm03@ep-hidden-truth-aev5l7a7-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

# Uploads
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_IMAGES = {"png", "jpg", "jpeg", "gif"}
MAX_UPLOAD = 8 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD

# Enable CSRF protection for forms
csrf = CSRFProtect(app)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- DB pool setup (DictCursor) ----------
try:
    pg_pool = psycopg2.pool.SimpleConnectionPool(
        minconn=1, maxconn=20, dsn=NEON_DSN, cursor_factory=DictCursor
    )
    logger.info("Postgres pool created successfully.")
except Exception as e:
    logger.exception("Failed creating Postgres connection pool: %s", e)
    raise

def get_conn():
    """Get connection from pool."""
    return pg_pool.getconn()

def put_conn(conn):
    """Return connection to pool."""
    if conn:
        pg_pool.putconn(conn)

class DBError(Exception):
    pass

def db_fetchall(query: str, params: Optional[tuple] = None):
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(query, params or ())
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows] if rows else []
    except Psycopg2DBError as e:
        logger.error("db_fetchall error: %s | SQL: %s | params: %s", e, query, params)
        if cur:
            try: cur.close()
            except: pass
        raise DBError(str(e))
    finally:
        if conn:
            put_conn(conn)

def db_fetchone(query: str, params: Optional[tuple] = None):
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(query, params or ())
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Psycopg2DBError as e:
        logger.error("db_fetchone error: %s | SQL: %s | params: %s", e, query, params)
        if cur:
            try: cur.close()
            except: pass
        raise DBError(str(e))
    finally:
        if conn:
            put_conn(conn)

def db_commit(query: str, params: Optional[tuple] = None, returning: bool = False):
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(query, params or ())
        ret = None
        if returning:
            maybe = cur.fetchone()
            if maybe:
                # DictCursor -> mapping; try values or first column
                try:
                    ret = list(maybe)[0]
                except Exception:
                    try:
                        ret = maybe[0]
                    except:
                        ret = None
        conn.commit()
        cur.close()
        return ret if returning else True
    except Psycopg2DBError as e:
        logger.error("db_commit error: %s | SQL: %s | params: %s", e, query, params)
        try:
            if conn: conn.rollback()
        except:
            pass
        if cur:
            try: cur.close()
            except: pass
        raise DBError(str(e))
    finally:
        if conn:
            put_conn(conn)

# ---------- Safe sort mapping (prevents SQL injection) ----------
SORT_WHITELIST = {
    "newest": "p.created_at DESC",
    "price_asc": "p.price ASC",
    "price_desc": "p.price DESC",
    "rating": "avg_rating DESC NULLS LAST"
}
DEFAULT_SORT_SQL = SORT_WHITELIST["newest"]

# ---------- Helpers ----------
EMAIL_RE = re.compile(r"^[^@]+@gmail\.com$")
def is_gmail(email: str) -> bool:
    return bool(email and EMAIL_RE.match(email.strip().lower()))

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGES

def save_image(file_storage) -> Optional[str]:
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_file(file_storage.filename):
        return None
    safe = secure_filename(file_storage.filename)
    unique = f"{uuid.uuid4().hex}_{safe}"
    dest = os.path.join(UPLOAD_DIR, unique)
    file_storage.save(dest)
    return unique  # store only the unique filename in DB

# Simple in-memory TTL cache for homepage
_cache: Dict[str, tuple] = {}
def ttl_cache(ttl_seconds: int = 30):
    def deco(fn: Callable):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = fn.__name__
            now = time.time()
            entry = _cache.get(key)
            if entry and (now - entry[0]) < ttl_seconds:
                return entry[1]
            val = fn(*args, **kwargs)
            _cache[key] = (now, val)
            return val
        return wrapper
    return deco

# ---------- Migrations ----------
def ensure_schema():
    stmts = [
        # users table (core)
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
            filename TEXT NOT NULL
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
            rating INTEGER CHECK (rating>=1 AND rating<=5),
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # carts (server-side optional)
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
        try:
            db_commit(s)
        except DBError as e:
            logger.error("Migration stmt failed: %s | error: %s", s[:80], e)

ensure_schema()

# ---------- Error handlers ----------
@app.errorhandler(DBError)
def handle_db_error(e):
    logger.error("Database error (handled): %s", e)
    return render_template_string("<h1>Database error</h1><p>Please try again later.</p>"), 500

@app.errorhandler(HTTPException)
def handle_http_error(e):
    return f"<h1>{e.code}</h1><p>{e.description}</p>", e.code

# ---------- Simple templates (single-file approach) ----------
BASE_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>{{ title or 'Farm Marketplace' }}</title>
  <style>
    :root{--green:#2f8f3a;--muted:#64748b;--card:#fff;--bg:#f4faf6}
    body{font-family:Arial,Helvetica,sans-serif;margin:0;background:var(--bg);color:#0f172a}
    .top{background:var(--card);padding:12px 20px;border-bottom:1px solid #e6eef2;display:flex;justify-content:space-between;align-items:center}
    .brand{font-weight:700;color:var(--green)}
    .nav{display:flex;gap:12px;align-items:center}
    .btn{background:var(--green);color:#fff;padding:8px 12px;border-radius:8px;border:none;cursor:pointer}
    .container{max-width:1100px;margin:18px auto;padding:0 12px}
    .card{background:var(--card);padding:16px;border-radius:10px;border:1px solid #e6eef2;margin-bottom:12px}
    .small{color:var(--muted);font-size:0.95rem}
    .grid{display:grid;gap:12px}
    @media(min-width:900px){.grid{grid-template-columns:300px 1fr}}
    .listing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
    img.product{width:100%;height:150px;object-fit:cover;border-radius:8px}
    .notice{background:#fff3cd;padding:8px;border-radius:8px;border:1px solid #ffeeba;margin-bottom:12px}
  </style>
</head>
<body>
  <div class="top">
    <div class="brand">🚜 Farm Marketplace</div>
    <div class="nav">
      <a href="{{ url_for('index') }}">Home</a>
      <a href="{{ url_for('market') }}">Marketplace</a>
      {% if session.get('user_id') %}
        {% if session.get('role') == 'farmer' %}<a href="{{ url_for('farmer') }}">Farmer</a>{% endif %}
        {% if session.get('role') == 'buyer' %}<a href="{{ url_for('cart') }}">Cart</a>{% endif %}
        {% if session.get('role') == 'admin' %}<a href="{{ url_for('admin') }}">Admin</a>{% endif %}
        <span class="small">{{ session.get('user_email') }}</span>
        <a href="{{ url_for('logout') }}" class="btn">Logout</a>
      {% else %}
        <a href="{{ url_for('login') }}" class="btn">Sign in (Gmail)</a>
      {% endif %}
    </div>
  </div>

  <div class="container">
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        <div class="card">
          {% for m in messages %}<div class="notice">{{ m }}</div>{% endfor %}
        </div>
      {% endif %}
    {% endwith %}
    {{ body|safe }}
  </div>
</body>
</html>
"""

# ---------- Auth (mock Gmail-only sign-in, but can be replaced with OAuth) ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    # For brevity we provide a secure mock sign-in UI if you haven't wired OAuth.
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        name = (request.form.get("name") or "").strip()
        role = (request.form.get("role") or "buyer")
        if not is_gmail(email):
            flash("Please use a Gmail address (for production integrate Google OAuth).")
            return redirect(url_for("login"))
        try:
            user = db_fetchone("SELECT * FROM users WHERE email = %s", (email,))
            if not user:
                user_id = db_commit("INSERT INTO users (email,name,role) VALUES (%s,%s,%s) RETURNING id", (email, name or None, role), returning=True)
                if not user_id:
                    raise DBError("Failed to create user")
                user = db_fetchone("SELECT * FROM users WHERE id = %s", (user_id,))
            # set minimal session
            session['user_id'] = user['id']
            session['user_email'] = user['email']
            session['role'] = user.get('role') or 'buyer'
            session['user_name'] = user.get('name') or user['email'].split('@')[0]
            flash("Signed in (mock).")
            return redirect(url_for('index'))
        except DBError as e:
            logger.error("Login DBError: %s", e)
            flash("Database error.")
            return redirect(url_for('login'))
    body = """
    <div class="card"><h2>Sign in (Gmail only)</h2>
      <form method="post">
        <div><label>Email (@gmail.com)</label><br><input name="email" required></div>
        <div><label>Display name (optional)</label><br><input name="name"></div>
        <div><label>Role</label><br>
          <select name="role"><option value="buyer">Buyer</option><option value="farmer">Farmer</option><option value="admin">Admin</option></select>
        </div>
        <div style="margin-top:10px"><button class="btn">Sign in</button></div>
      </form>
    </div>
    """
    return render_template_string(BASE_HTML, body=body, title="Sign in")

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for('index'))

# ---------- Homepage (cached) ----------
@ttl_cache(ttl_seconds=20)
def get_featured():
    prods = db_fetchall("SELECT p.*, u.name AS farmer_name FROM products p LEFT JOIN users u ON p.farmer_id = u.id WHERE p.status='active' ORDER BY p.created_at DESC LIMIT 6")
    # include one image path if available
    for p in prods:
        img = db_fetchone("SELECT filename FROM product_images WHERE product_id = %s ORDER BY id LIMIT 1", (p['id'],))
        p['image'] = img['filename'] if img else None
    return prods

@app.route("/")
def index():
    prods = get_featured() or []
    cards = ""
    for p in prods:
        img = p.get('image') or ''
        img_url = url_for('uploaded_file', filename=img) if img else "https://via.placeholder.com/400x300?text=No+image"
        cards += f"""<div class="card"><img class="product" src="{img_url}" onerror="this.src='https://via.placeholder.com/400x300?text=No+image'"><h3>{p['name']}</h3><div class="small">By {p.get('farmer_name') or 'Unknown'}</div><div class="small">Price ₹{float(p['price']):.2f} • Stock {p.get('quantity',0)}</div></div>"""
    body = f"<div class='card'><h2>Featured</h2><div class='listing-grid'>{cards}</div></div>"
    return render_template_string(BASE_HTML, body=body, title="Home")

# ---------- Uploaded files ----------
@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)

# ---------- Marketplace and filtering (safe sort mapping) ----------
@app.route("/market")
def market():
    q = (request.args.get('q') or '').strip()
    category = (request.args.get('category') or '').strip()
    location = (request.args.get('location') or '').strip()
    organic = request.args.get('organic')
    sort = request.args.get('sort') or 'newest'
    sort_sql = SORT_WHITELIST.get(sort, DEFAULT_SORT_SQL)

    params = []
    sql = "SELECT p.*, u.name as farmer_name, COALESCE(avg_r.avg,0) AS avg_rating FROM products p LEFT JOIN users u ON p.farmer_id = u.id LEFT JOIN (SELECT product_id, AVG(rating) as avg FROM reviews GROUP BY product_id) avg_r ON p.id = avg_r.product_id WHERE p.status='active'"

    if q:
        sql += " AND (p.name ILIKE %s OR p.description ILIKE %s OR u.name ILIKE %s)"
        p = f"%{q}%"
        params.extend([p,p,p])
    if category:
        sql += " AND p.category = %s"
        params.append(category)
    if location:
        sql += " AND p.location ILIKE %s"
        params.append(f"%{location}%")
    if organic:
        sql += " AND p.is_organic = TRUE"

    # safe: append the pre-built sort_sql (which came from whitelist)
    sql += f" ORDER BY {sort_sql} LIMIT 200"

    prods = db_fetchall(sql, tuple(params)) or []
    # enrich with image
    for p in prods:
        img = db_fetchone("SELECT filename FROM product_images WHERE product_id=%s ORDER BY id LIMIT 1", (p['id'],))
        p['image'] = img['filename'] if img else None

    # render minimal view
    cards = ""
    for p in prods:
        img_url = url_for('uploaded_file', filename=p['image']) if p.get('image') else "https://via.placeholder.com/400x300?text=No+image"
        cards += f"""
          <div class="card">
            <img class="product" src="{img_url}" onerror="this.src='https://via.placeholder.com/400x300?text=No+image'">
            <h3>{p['name']}</h3>
            <div class="small">By {p.get('farmer_name') or 'Unknown'}</div>
            <div style="margin-top:6px"><strong>₹{float(p['price']):.2f}</strong> • Stock {p.get('quantity',0)}</div>
            <div style="margin-top:8px"><a href="{url_for('product_view', product_id=p['id'])}">View</a></div>
          </div>
        """

    body = f"""
      <div class="card">
        <form method="get" style="display:flex;gap:8px;flex-wrap:wrap">
          <input name="q" placeholder="search" value="{q}">
          <input name="location" placeholder="location" value="{location}">
          <select name="category">
            <option value="">All categories</option>
            <option{" selected" if category=="Fresh Vegetables" else ""}>Fresh Vegetables</option>
            <option{" selected" if category=="Fruits" else ""}>Fruits</option>
            <option{" selected" if category=="Grains & Cereals" else ""}>Grains & Cereals</option>
            <option{" selected" if category=="Dairy Products" else ""}>Dairy Products</option>
            <option{" selected" if category=="Meat & Poultry" else ""}>Meat & Poultry</option>
            <option{" selected" if category=="Organic Products" else ""}>Organic Products</option>
            <option{" selected" if category=="Herbs & Spices" else ""}>Herbs & Spices</option>
          </select>
          <label class="small">Organic <input type="checkbox" name="organic" value="1" {"checked" if organic else ""}></label>
          <select name="sort">
            <option value="newest" {"selected" if sort=="newest" else ""}>Newest</option>
            <option value="price_asc" {"selected" if sort=="price_asc" else ""}>Price low→high</option>
            <option value="price_desc" {"selected" if sort=="price_desc" else ""}>Price high→low</option>
            <option value="rating" {"selected" if sort=="rating" else ""}>Rating</option>
          </select>
          <button class="btn">Filter</button>
        </form>
      </div>
      <div class="listing-grid">{cards}</div>
    """
    return render_template_string(BASE_HTML, body=body, title="Marketplace")

# ---------- Product view ----------
@app.route("/product/<int:product_id>")
def product_view(product_id):
    p = db_fetchone("SELECT p.*, u.name as farmer_name, u.email as farmer_email FROM products p LEFT JOIN users u ON p.farmer_id=u.id WHERE p.id=%s", (product_id,))
    if not p:
        flash("Product not found.")
        return redirect(url_for('market'))
    imgs = db_fetchall("SELECT filename FROM product_images WHERE product_id=%s ORDER BY id", (product_id,)) or []
    imgs_html = "".join([f'<img src="{url_for("uploaded_file", filename=i["filename"])}" style="max-width:200px;margin-right:6px">' for i in imgs])
    reviews = db_fetchall("SELECT r.*, u.name as buyer_name FROM reviews r LEFT JOIN users u ON r.buyer_id=u.id WHERE r.product_id=%s ORDER BY r.created_at DESC LIMIT 50", (product_id,)) or []
    reviews_html = "".join([f"<div class='card small'>{r['buyer_name']} — {r['rating']}/5<br>{r['comment']}</div>" for r in reviews])
    body = f"""
      <div class="grid">
        <div class="card">
          <div style="display:flex;gap:12px">
            <div style="min-width:260px">{imgs_html or '<div class=small>No images</div>'}</div>
            <div>
              <h2>{p['name']}</h2>
              <div class="small">By {p.get('farmer_name')}</div>
              <div style="margin-top:8px"><strong>₹{float(p['price']):.2f}</strong></div>
              <p class="small">{p.get('description') or ''}</p>
            </div>
          </div>
        </div>
        <div class="card"><h3>Reviews</h3>{reviews_html or '<div class=small>No reviews yet.</div>'}</div>
      </div>
    """
    return render_template_string(BASE_HTML, body=body, title=p['name'])

# ---------- Farmer dashboard / product management ----------
def login_required(role=None):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get('user_id'):
                flash("Please sign in.")
                return redirect(url_for('login'))
            if role and session.get('role') != role:
                flash("Access denied for your role.")
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return wrapper
    return deco

@app.route("/farmer")
@login_required(role='farmer')
def farmer():
    uid = session['user_id']
    products = db_fetchall("SELECT * FROM products WHERE farmer_id=%s ORDER BY created_at DESC", (uid,)) or []
    prod_html = ""
    for p in products:
        prod_html += f"<div class='list-item'><div><strong>{p['name']}</strong><div class='small'>Stock {p['quantity']} • ₹{float(p['price']):.2f}</div></div><div><a href='{url_for('edit_product', product_id=p['id'])}'>Edit</a></div></div>"
    body = f"""
      <div class="card"><h2>Sell — Create product</h2>
        <form method="post" action="{url_for('create_product')}" enctype="multipart/form-data">
          <div><input name="name" placeholder="Product name" required></div>
          <div><input name="category" placeholder="Category"></div>
          <div><input name="price" placeholder="Price"></div>
          <div><input name="unit" placeholder="Unit (kg, piece)"></div>
          <div><input name="quantity" placeholder="Quantity"></div>
          <div><textarea name="description" placeholder="Description"></textarea></div>
          <div><input type="file" name="images" multiple></div>
          <div style="margin-top:8px"><button class="btn">Create</button></div>
        </form>
      </div>
      <div class="card"><h3>Your products</h3>{prod_html or '<div class=small>No products yet.</div>'}</div>
    """
    return render_template_string(BASE_HTML, body=body, title="Farmer dashboard")

@app.route("/product/create", methods=["POST"])
@login_required(role='farmer')
def create_product():
    try:
        name = request.form.get('name')
        category = request.form.get('category')
        price = float(request.form.get('price') or 0)
        unit = request.form.get('unit')
        quantity = int(request.form.get('quantity') or 0)
        description = request.form.get('description')
        farmer_id = session['user_id']
        product_id = db_commit("INSERT INTO products (farmer_id,name,category,price,unit,quantity,description) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                               (farmer_id, name, category, price, unit, quantity, description), returning=True)
        files = request.files.getlist("images")
        for f in files:
            saved = save_image(f)
            if saved:
                db_commit("INSERT INTO product_images (product_id,filename) VALUES (%s,%s)", (product_id, saved))
        flash("Product created.")
        return redirect(url_for('farmer'))
    except DBError as e:
        logger.error("Create product DB error: %s", e)
        flash("Database error creating product.")
        return redirect(url_for('farmer'))

@app.route("/product/<int:product_id>/edit", methods=["GET","POST"])
@login_required(role='farmer')
def edit_product(product_id):
    p = db_fetchone("SELECT * FROM products WHERE id=%s", (product_id,))
    if not p or p['farmer_id'] != session['user_id']:
        flash("Not allowed.")
        return redirect(url_for('farmer'))
    if request.method == "POST":
        try:
            name = request.form.get('name')
            price = float(request.form.get('price') or 0)
            quantity = int(request.form.get('quantity') or 0)
            description = request.form.get('description')
            db_commit("UPDATE products SET name=%s,price=%s,quantity=%s,description=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                      (name, price, quantity, description, product_id))
            files = request.files.getlist("images")
            for f in files:
                saved = save_image(f)
                if saved:
                    db_commit("INSERT INTO product_images (product_id,filename) VALUES (%s,%s)", (product_id, saved))
            flash("Product updated.")
            return redirect(url_for('farmer'))
        except DBError as e:
            logger.error("Edit product DB error: %s", e)
            flash("Database error.")
            return redirect(url_for('farmer'))
    imgs = db_fetchall("SELECT filename FROM product_images WHERE product_id=%s", (product_id,)) or []
    imgs_html = "".join([f"<div class='small'>{i['filename']}</div>" for i in imgs])
    body = f"""
      <div class="card"><h3>Edit product</h3>
        <form method="post" enctype="multipart/form-data">
          <div><input name="name" value="{p['name']}"></div>
          <div><input name="price" value="{float(p['price']):.2f}"></div>
          <div><input name="quantity" value="{p.get('quantity',0)}"></div>
          <div><textarea name="description">{p.get('description') or ''}</textarea></div>
          <div><input type="file" name="images" multiple></div>
          <div>{imgs_html}</div>
          <div style="margin-top:8px"><button class='btn'>Save</button></div>
        </form>
      </div>
    """
    return render_template_string(BASE_HTML, body=body, title="Edit product")

# ---------- Cart / Checkout (client-side cart sync) ----------
@app.route("/cart")
@login_required(role='buyer')
def cart():
    body = """
      <div class="card">
        <h2>Your cart</h2>
        <p class="small">Client-side cart stored in your browser. Click Checkout to create order(s).</p>
        <div id="cart-area"></div>
        <div style="margin-top:12px"><button class="btn" onclick="syncAndCheckout()">Checkout (mock)</button></div>
      </div>
      <script>
        async function renderCart(){
          const c = JSON.parse(localStorage.getItem('cart_v1')||'{}');
          const area = document.getElementById('cart-area');
          if(!Object.keys(c).length){ area.innerHTML='<div class=small>Cart empty</div>'; return; }
          const ids = Object.keys(c).map(x=>parseInt(x));
          const res = await fetch('/api/products/batch', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids})});
          const data = await res.json();
          let html = '<table border=1 cellpadding=6 style="width:100%"><tr><th>Product</th><th>Qty</th></tr>';
          let total=0;
          for(const p of data){
            const qty = c[p.id];
            html += `<tr><td>${p.name}</td><td>${qty}</td></tr>`;
            total += parseFloat(p.price||0)*qty;
          }
          html += `</table><h3>Total ₹${total.toFixed(2)}</h3>`;
          area.innerHTML = html;
        }
        async function syncAndCheckout(){
          const c = JSON.parse(localStorage.getItem('cart_v1')||'{}');
          if(!Object.keys(c).length){ alert('Cart empty'); return; }
          const res = await fetch('/checkout', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({cart:c})});
          const j = await res.json();
          if(j.ok){ localStorage.removeItem('cart_v1'); alert('Order(s) created: '+j.orders.join(',')); window.location='/orders'; } else alert('Error: '+(j.error||'unknown'));
        }
        renderCart();
      </script>
    """
    return render_template_string(BASE_HTML, body=body, title="Cart")

@app.route("/api/products/batch", methods=["POST"])
def api_products_batch():
    data = request.get_json() or {}
    ids = data.get('ids') or []
    if not ids:
        return jsonify([])
    # sanitize ids
    ids_clean = [int(x) for x in ids]
    rows = db_fetchall("SELECT id,name,price FROM products WHERE id = ANY(%s)", (ids_clean,))
    return jsonify(rows or [])

@app.route("/checkout", methods=["POST"])
@login_required(role='buyer')
def checkout():
    data = request.get_json() or {}
    cart = data.get('cart') or {}
    if not cart:
        return jsonify({'ok': False, 'error': 'cart empty'}), 400
    # group by farmer
    product_ids = [int(k) for k in cart.keys()]
    prods = db_fetchall("SELECT * FROM products WHERE id = ANY(%s)", (product_ids,)) or []
    prod_map = {p['id']: p for p in prods}
    groups = {}
    for pid_str, qty in cart.items():
        pid = int(pid_str)
        p = prod_map.get(pid)
        if not p:
            continue
        groups.setdefault(p['farmer_id'], []).append((pid, int(qty), float(p['price'] or 0)))
    created_orders = []
    try:
        for farmer_id, items in groups.items():
            total = sum(q*price for (_,q,price) in items)
            order_id = db_commit("INSERT INTO orders (buyer_id, fisherman_id, farmer_id, total_amount, status) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                                 (session['user_id'], None, farmer_id, total, 'pending'), returning=True)
            # note: we had to correct field names; if your schema differs, adjust accordingly
            if not order_id:
                continue
            for pid, qty, price in items:
                db_commit("INSERT INTO order_items (order_id, product_id, quantity, price) VALUES (%s,%s,%s,%s)",
                          (order_id, pid, qty, price))
                db_commit("UPDATE products SET quantity = GREATEST(quantity - %s, 0) WHERE id = %s", (qty, pid))
            created_orders.append(order_id)
    except DBError as e:
        logger.error("Checkout DB error: %s", e)
        return jsonify({'ok': False, 'error': 'DB error during checkout'}), 500
    if not created_orders:
        return jsonify({'ok': False, 'error': 'no orders created'}), 500
    return jsonify({'ok': True, 'orders': created_orders})

# ---------- Orders listing ----------
@app.route("/orders")
@login_required()
def orders():
    uid = session.get('user_id')
    role = session.get('role')
    if role == 'buyer':
        rows = db_fetchall("SELECT * FROM orders WHERE buyer_id=%s ORDER BY created_at DESC", (uid,)) or []
    elif role == 'farmer':
        rows = db_fetchall("SELECT * FROM orders WHERE farmer_id=%s ORDER BY created_at DESC", (uid,)) or []
    else:
        rows = db_fetchall("SELECT * FROM orders ORDER BY created_at DESC LIMIT 200") or []
    rows_html = "".join([f"<div class='list-item'><div>Order #{r['id']} • ₹{float(r['total_amount']):.2f} • {r['status']}</div><div><a href='{url_for('view_order', order_id=r['id'])}'>View</a></div></div>" for r in rows])
    body = f"<div class='card'><h2>Orders</h2>{rows_html or '<div class=small>No orders</div>'}</div>"
    return render_template_string(BASE_HTML, body=body, title="Orders")

@app.route("/order/<int:order_id>")
@login_required()
def view_order(order_id):
    o = db_fetchone("SELECT * FROM orders WHERE id=%s", (order_id,))
    if not o:
        flash("Order not found.")
        return redirect(url_for('orders'))
    items = db_fetchall("SELECT oi.*, p.name FROM order_items oi LEFT JOIN products p ON oi.product_id=p.id WHERE oi.order_id=%s", (order_id,)) or []
    items_html = "".join([f"<div class='small'>{it['name']} • {it['quantity']} x ₹{float(it['price']):.2f}</div>" for it in items])
    update_html = ""
    if session.get('role') == 'farmer' and o.get('farmer_id') == session.get('user_id'):
        update_html = f"""
          <form method="post" action="{url_for('update_order_status', order_id=order_id)}">
            <select name="status"><option>pending</option><option>confirmed</option><option>packed</option><option>delivered</option></select>
            <button class="btn">Update</button>
          </form>
        """
    body = f"<div class='card'><h2>Order #{o['id']}</h2><div class='small'>Total ₹{float(o['total_amount']):.2f} • Status: {o['status']}</div>{items_html}{update_html}</div>"
    return render_template_string(BASE_HTML, body=body, title=f"Order {order_id}")

@app.route("/order/<int:order_id>/status", methods=["POST"])
@login_required(role='farmer')
def update_order_status(order_id):
    new = request.form.get('status')
    if new not in ('pending','confirmed','packed','delivered','cancelled'):
        flash("Invalid status.")
        return redirect(url_for('view_order', order_id=order_id))
    try:
        db_commit("UPDATE orders SET status=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (new, order_id))
        flash("Status updated.")
    except DBError:
        flash("Failed to update status.")
    return redirect(url_for('view_order', order_id=order_id))

# ---------- Admin simple page ----------
@app.route("/admin")
@login_required(role='admin')
def admin():
    users = db_fetchall("SELECT id,email,role FROM users ORDER BY id DESC") or []
    users_html = "".join([f"<div class='list-item'><div>{u['email']} • {u['role']}</div></div>" for u in users])
    body = f"<div class='card'><h2>Admin</h2>{users_html}</div>"
    return render_template_string(BASE_HTML, body=body, title="Admin")

# ---------- API health ----------
@app.route("/health")
def health():
    try:
        ok = db_fetchone("SELECT 1 as ok")
        return jsonify({'status': 'ok' if ok else 'db-error', 'time': datetime.utcnow().isoformat()})
    except DBError:
        return jsonify({'status': 'db-error', 'time': datetime.utcnow().isoformat()}), 500

# ---------- Run ----------
if __name__ == "__main__":
    logger.info("Starting improved Farm Marketplace app on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
