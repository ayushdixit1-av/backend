# app.py
"""
Robust Farm Marketplace - single-file Flask app with safe migrations.
Fix: ensure missing columns are added (ALTER TABLE ... ADD COLUMN IF NOT EXISTS)
so indexes and queries won't fail when the DB has older schema.
WARNING: contains Neon DSN inline for convenience (you asked). Use env var in production.
"""

import os
import re
import uuid
import time
import logging
from functools import wraps
from datetime import datetime
from typing import Optional, Callable, Dict

from flask import (
    Flask, request, session, redirect, url_for, jsonify,
    render_template_string, flash, send_from_directory
)
from werkzeug.utils import secure_filename
import psycopg2
from psycopg2 import pool, DatabaseError as Psycopg2DBError
from psycopg2.extras import DictCursor
from flask_wtf import CSRFProtect

# ----- Config -----
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")
PORT = int(os.environ.get("PORT", 3000))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_IMAGES = {"png", "jpg", "jpeg", "gif"}
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
csrf = CSRFProtect(app)

# Neon DSN (embedded as requested)
NEON_DSN = "postgresql://neondb_owner:npg_jgROvpDtrm03@ep-hidden-truth-aev5l7a7-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

# ----- DB pool (DictCursor) -----
try:
    pg_pool = psycopg2.pool.SimpleConnectionPool(minconn=1, maxconn=20, dsn=NEON_DSN, cursor_factory=DictCursor)
    logger.info("Postgres pool created successfully.")
except Exception as e:
    logger.exception("Failed to create Postgres pool: %s", e)
    raise

def get_conn():
    return pg_pool.getconn()

def put_conn(conn):
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
        rv = None
        if returning:
            maybe = cur.fetchone()
            if maybe:
                try:
                    # DictCursor returns mapping-like row — extract first value
                    rv = list(maybe.values())[0] if hasattr(maybe, "keys") else maybe[0]
                except Exception:
                    try: rv = maybe[0]
                    except: rv = None
        conn.commit()
        cur.close()
        return rv if returning else True
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

# ----- Safe sort whitelist -----
SORT_WHITELIST = {
    "newest": "p.created_at DESC",
    "price_asc": "p.price ASC",
    "price_desc": "p.price DESC",
    "rating": "avg_rating DESC NULLS LAST"
}
DEFAULT_SORT_SQL = SORT_WHITELIST["newest"]

# ----- Helpers -----
EMAIL_RE = re.compile(r"^[^@]+@gmail\.com$")
def is_gmail(email: str) -> bool:
    return bool(email and EMAIL_RE.match(email.strip().lower()))

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGES

def save_image(file_storage):
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_file(file_storage.filename):
        return None
    fname = secure_filename(file_storage.filename)
    unique = f"{uuid.uuid4().hex}_{fname}"
    dest = os.path.join(UPLOAD_DIR, unique)
    file_storage.save(dest)
    return unique

# ----- Robust migrations (add missing columns if necessary) -----
def ensure_schema_robust():
    """
    Create tables if not exists, then ALTER TABLE ADD COLUMN IF NOT EXISTS
    for columns our app expects. Finally create indexes (only after columns exist).
    This avoids failing index creation when legacy tables are missing columns.
    """
    statements = [
        # Ensure core tables exist (basic shape)
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
        """
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            farmer_id INTEGER, -- added via ALTER below if needed
            name TEXT,
            price NUMERIC(12,2),
            quantity INTEGER DEFAULT 0,
            description TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS product_images (
            id SERIAL PRIMARY KEY,
            product_id INTEGER,
            filename TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            buyer_id INTEGER,
            farmer_id INTEGER,
            total_amount NUMERIC(12,2) DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id INTEGER,
            product_id INTEGER,
            quantity INTEGER,
            price NUMERIC(12,2)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id SERIAL PRIMARY KEY,
            buyer_id INTEGER,
            farmer_id INTEGER,
            product_id INTEGER,
            rating INTEGER,
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # carts
        """
        CREATE TABLE IF NOT EXISTS carts (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            product_id INTEGER,
            quantity INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    ]
    # Run create-if-not-exists statements
    for s in statements:
        try:
            db_commit(s)
        except DBError as e:
            logger.error("Migration (create) failed: %s", e)

    # Now ensure specific columns we rely on exist (ALTER TABLE ... ADD COLUMN IF NOT EXISTS)
    # products: add category, unit, location, is_organic
    alters = [
        ("products", "farmer_id", "INTEGER"),
        ("products", "category", "TEXT"),
        ("products", "unit", "TEXT"),
        ("products", "location", "TEXT"),
        ("products", "is_organic", "BOOLEAN DEFAULT FALSE"),
        ("products", "price", "NUMERIC(12,2) DEFAULT 0"),
        ("products", "name", "TEXT"),
        ("product_images", "product_id", "INTEGER"),
        ("product_images", "filename", "TEXT"),
        ("orders", "buyer_id", "INTEGER"),
        ("orders", "farmer_id", "INTEGER"),
        ("orders", "total_amount", "NUMERIC(12,2) DEFAULT 0"),
        ("orders", "status", "TEXT DEFAULT 'pending'"),
        ("order_items", "order_id", "INTEGER"),
        ("order_items", "product_id", "INTEGER"),
        ("order_items", "quantity", "INTEGER"),
        ("order_items", "price", "NUMERIC(12,2)"),
        ("users", "role", "TEXT DEFAULT 'buyer'"),
        ("users", "name", "TEXT"),
    ]
    for table, col, definition in alters:
        stmt = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {definition};"
        try:
            db_commit(stmt)
        except DBError as e:
            logger.error("Migration (alter) failed: %s | SQL: %s", e, stmt)

    # Finally create indexes only now (columns exist)
    idxs = [
        "CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);",
        "CREATE INDEX IF NOT EXISTS idx_products_farmer ON products(farmer_id);",
        "CREATE INDEX IF NOT EXISTS idx_orders_buyer ON orders(buyer_id);"
    ]
    for idx in idxs:
        try:
            db_commit(idx)
        except DBError as e:
            logger.error("Failed creating index: %s | error: %s", idx, e)

ensure_schema_robust()

# ----- Error handlers -----
@app.errorhandler(DBError)
def handle_db_error(e):
    logger.error("Database error (handled): %s", e)
    return render_template_string("<h1>Database error</h1><p>Please try again later.</p>"), 500

# ----- Basic single-file templates (small) -----
BASE_HTML = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{title or 'Farm Marketplace'}}</title>
<style>
:root{--green:#2f8f3a;--bg:#f4faf6;--card:#fff;--muted:#64748b}
body{font-family:Arial,Helvetica,sans-serif;margin:0;background:var(--bg);color:#0f172a}
.top{background:var(--card);padding:12px 20px;border-bottom:1px solid #e6eef2;display:flex;justify-content:space-between}
.brand{font-weight:700;color:var(--green)}
.container{max-width:1100px;margin:18px auto;padding:0 12px}
.card{background:var(--card);padding:16px;border-radius:8px;border:1px solid #e6eef2;margin-bottom:12px}
.btn{background:var(--green);color:#fff;padding:8px 12px;border-radius:8px;border:none;cursor:pointer}
.small{color:var(--muted);font-size:0.95rem}
.listing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
img.product{width:100%;height:150px;object-fit:cover;border-radius:8px}
.notice{background:#fff3cd;padding:8px;border-radius:8px;border:1px solid #ffeeba;margin-bottom:12px}
</style></head><body>
<div class="top"><div class="brand">🚜 Farm Marketplace</div><div>
{% if session.get('user_id') %}
  <span class="small">{{session.get('user_email')}}</span> <a href="{{url_for('logout')}}" class="btn">Logout</a>
{% else %}
  <a href="{{url_for('login')}}" class="btn">Sign in (mock Gmail)</a>
{% endif %}
</div></div>
<div class="container">
{% with messages = get_flashed_messages() %}
  {% if messages %}
    <div class="card">{% for m in messages %}<div class="notice">{{m}}</div>{% endfor %}</div>
  {% endif %}
{% endwith %}
{{body|safe}}
</div></body></html>
"""

# ----- Simple routes (homepage, login mock, marketplace) -----
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        name = (request.form.get("name") or "").strip()
        role = (request.form.get("role") or "buyer")
        if not EMAIL_RE.match(email) and not re.match(r"^[^@]+@gmail\.com$", email):
            # allow only gmail addresses
            if not is_gmail(email):
                flash("Use a Gmail address (mock sign-in). For production, configure Google OAuth.")
                return redirect(url_for("login"))
        try:
            user = db_fetchone("SELECT * FROM users WHERE email=%s", (email,))
            if not user:
                uid = db_commit("INSERT INTO users (email,name,role) VALUES (%s,%s,%s) RETURNING id", (email, name or None, role), returning=True)
                user = db_fetchone("SELECT * FROM users WHERE id=%s", (uid,))
            session['user_id'] = user['id']
            session['user_email'] = user['email']
            session['role'] = user.get('role') or 'buyer'
            session['user_name'] = user.get('name') or user['email'].split('@')[0]
            flash("Signed in (mock).")
            return redirect(url_for('index'))
        except DBError as e:
            logger.error("Login DB error: %s", e)
            flash("Database error.")
            return redirect(url_for("login"))
    body = """
    <div class="card"><h2>Sign in (Gmail-only mock)</h2>
      <form method="post">
        <div><label>Email</label><br><input name="email" required></div>
        <div><label>Name</label><br><input name="name"></div>
        <div><label>Role</label><br><select name="role"><option value="buyer">Buyer</option><option value="farmer">Farmer</option><option value="admin">Admin</option></select></div>
        <div style="margin-top:8px"><button class="btn">Sign in</button></div>
      </form>
    </div>
    """
    return render_template_string(BASE_HTML, body=body, title="Sign in")

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for('index'))

# Homepage: use safe JOIN but only if columns exist (we added them above)
@app.route("/")
def index():
    try:
        prods = db_fetchall("SELECT p.id, p.name, COALESCE(p.price,0) AS price, COALESCE(p.quantity,0) AS quantity FROM products p WHERE p.status='active' ORDER BY p.created_at DESC LIMIT 6") or []
    except DBError as e:
        logger.error("Homepage products query failed: %s", e)
        prods = []
    cards = ""
    for p in prods:
        img = None
        try:
            img_row = db_fetchone("SELECT filename FROM product_images WHERE product_id=%s ORDER BY id LIMIT 1", (p['id'],))
            img = img_row['filename'] if img_row else None
        except DBError:
            img = None
        img_url = url_for('uploaded_file', filename=img) if img else "https://via.placeholder.com/400x300?text=No+image"
        cards += f"""<div class="card"><img class="product" src="{img_url}"><h3>{p['name']}</h3><div class="small">₹{float(p['price']):.2f} • Stock {p['quantity']}</div></div>"""
    body = f"<div class='card'><h2>Featured</h2><div class='listing-grid'>{cards}</div></div>"
    return render_template_string(BASE_HTML, body=body, title="Home")

# Uploads serving
@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)

# Marketplace (safe sort mapping)
@app.route("/market")
def market():
    q = (request.args.get('q') or "").strip()
    category = (request.args.get('category') or "").strip()
    location = (request.args.get('location') or "").strip()
    organic = request.args.get('organic')
    sort = request.args.get('sort') or 'newest'
    sort_sql = SORT_WHITELIST.get(sort, DEFAULT_SORT_SQL)

    params = []
    # Build safe SQL — only fields referenced by our schema (we added them)
    sql = "SELECT p.id, p.name, COALESCE(p.price,0) AS price, COALESCE(p.quantity,0) AS quantity, p.category, p.location FROM products p WHERE p.status='active'"

    if q:
        sql += " AND (p.name ILIKE %s OR p.description ILIKE %s)"
        params.extend([f"%{q}%", f"%{q}%"])
    if category:
        sql += " AND p.category = %s"; params.append(category)
    if location:
        sql += " AND p.location ILIKE %s"; params.append(f"%{location}%")
    if organic:
        sql += " AND p.is_organic = TRUE"

    sql += f" ORDER BY {sort_sql} LIMIT 200"

    try:
        prods = db_fetchall(sql, tuple(params)) or []
    except DBError as e:
        logger.error("Market query failed: %s", e)
        prods = []

    cards = ""
    for p in prods:
        img = None
        try:
            img_row = db_fetchone("SELECT filename FROM product_images WHERE product_id=%s ORDER BY id LIMIT 1", (p['id'],))
            img = img_row['filename'] if img_row else None
        except DBError:
            img = None
        img_url = url_for('uploaded_file', filename=img) if img else "https://via.placeholder.com/400x300?text=No+image"
        cards += f"""<div class="card"><img class="product" src="{img_url}"><h3>{p['name']}</h3><div class="small">₹{float(p['price']):.2f} • Stock {p['quantity']}</div><div style="margin-top:8px"><a href="{url_for('product_view', product_id=p['id'])}">View</a></div></div>"""
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

@app.route("/product/<int:product_id>")
def product_view(product_id):
    try:
        p = db_fetchone("SELECT id,name,price,quantity,description FROM products WHERE id=%s", (product_id,))
    except DBError:
        flash("Product lookup failed.")
        return redirect(url_for('market'))
    if not p:
        flash("Product not found.")
        return redirect(url_for('market'))
    imgs = db_fetchall("SELECT filename FROM product_images WHERE product_id=%s ORDER BY id", (product_id,)) or []
    imgs_html = "".join([f'<img src="{url_for("uploaded_file", filename=i["filename"])}" style="max-width:200px;margin-right:6px">' for i in imgs])
    body = f"""
      <div class="card">
        <div style="display:flex;gap:10px">
          <div style="min-width:260px">{imgs_html or '<div class=small>No images</div>'}</div>
          <div>
            <h2>{p['name']}</h2>
            <div class="small">Price: ₹{float(p['price']):.2f}</div>
            <p class="small">{p.get('description') or ''}</p>
            <div style="margin-top:8px"><button class="btn" onclick="alert('Add-to-cart client-side only in this demo')">Add to cart</button></div>
          </div>
        </div>
      </div>
    """
    return render_template_string(BASE_HTML, body=body, title=p['name'])

# Minimal create/edit product routes for farmers (robustly catch DBError)
def role_required(required_role):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get('user_id'):
                flash("Please sign in.")
                return redirect(url_for('login'))
            if session.get('role') != required_role:
                flash("Access denied.")
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return wrapper
    return deco

@app.route("/farmer", methods=["GET","POST"])
@role_required('farmer')
def farmer_dashboard():
    if request.method == "POST":
        # create product
        try:
            name = request.form.get('name')
            price = float(request.form.get('price') or 0)
            quantity = int(request.form.get('quantity') or 0)
            category = request.form.get('category')
            description = request.form.get('description')
            farmer_id = session['user_id']
            pid = db_commit("INSERT INTO products (farmer_id,name,price,quantity,category,description) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                            (farmer_id, name, price, quantity, category, description), returning=True)
            files = request.files.getlist('images')
            for f in files:
                saved = save_image(f)
                if saved:
                    db_commit("INSERT INTO product_images (product_id,filename) VALUES (%s,%s)", (pid, saved))
            flash("Product created.")
        except DBError as e:
            logger.error("Create product failed: %s", e)
            flash("Failed to create product.")
        return redirect(url_for('farmer_dashboard'))
    # GET: list products
    try:
        products = db_fetchall("SELECT id,name,price,quantity FROM products WHERE farmer_id=%s ORDER BY created_at DESC", (session['user_id'],)) or []
    except DBError:
        products = []
    rows = "".join([f"<div class='list-item'><div><strong>{p['name']}</strong><div class='small'>₹{float(p['price']):.2f} • {p['quantity']}</div></div></div>" for p in products])
    body = f"""
      <div class="card"><h2>Create product</h2>
        <form method="post" enctype="multipart/form-data">
          <div><input name="name" placeholder="Product name" required></div>
          <div><input name="price" placeholder="Price"></div>
          <div><input name="quantity" placeholder="Quantity"></div>
          <div><input name="category" placeholder="Category"></div>
          <div><textarea name="description" placeholder="Description"></textarea></div>
          <div><input type="file" name="images" multiple></div>
          <div style="margin-top:8px"><button class="btn">Create</button></div>
        </form>
      </div>
      <div class="card"><h3>Your products</h3>{rows or '<div class=small>No products yet</div>'}</div>
    """
    return render_template_string(BASE_HTML, body=body, title="Farmer")

# Admin and health endpoints (simplified)
@app.route("/admin")
@role_required('admin')
def admin():
    try:
        users = db_fetchall("SELECT id,email,role FROM users ORDER BY id DESC") or []
    except DBError:
        users = []
    rows = "".join([f"<div class='list-item'><div>{u['email']} • {u['role']}</div></div>" for u in users])
    body = f"<div class='card'><h2>Admin</h2>{rows}</div>"
    return render_template_string(BASE_HTML, body=body, title="Admin")

@app.route("/health")
def health():
    try:
        ok = db_fetchone("SELECT 1 as ok")
        return jsonify({"status":"ok" if ok else "db-error", "time": datetime.utcnow().isoformat()})
    except DBError:
        return jsonify({"status":"db-error", "time": datetime.utcnow().isoformat()}), 500

# ----- Run -----
if __name__ == "__main__":
    logger.info("Starting app on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
