# app.py
"""
Farm Marketplace single-file Flask app
- Gmail-only sign-in (no external OAuth required)
- Role: farmer / buyer / admin
- Listings with multiple images
- Search + filters + sorting
- Cart & mock checkout + invoices
- Ratings & reviews
- Admin panel for management
- Uses PostgreSQL via NEON_DB_URL (env)
"""
import os
import re
import uuid
from datetime import datetime, date
from functools import wraps
from io import StringIO

from flask import (
    Flask, request, session, redirect, url_for, jsonify,
    render_template_string, flash, send_from_directory, make_response
)
from werkzeug.utils import secure_filename
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------- Config ----------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")
PORT = int(os.environ.get("PORT", 3000))
NEON_DB_URL = os.environ.get("NEON_DB_URL", None)

# Uploads
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif"}
MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8 MB
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

# ---------------- DB pool ----------------
postgreSQL_pool = None
if not NEON_DB_URL:
    raise RuntimeError("Please set NEON_DB_URL environment variable (Postgres connection).")

try:
    postgreSQL_pool = psycopg2.pool.SimpleConnectionPool(minconn=1, maxconn=20, dsn=NEON_DB_URL)
    app.logger.info("Postgres pool created.")
except Exception as e:
    app.logger.error("Failed to create Postgres pool: %s", e)
    raise

def db_conn():
    return postgreSQL_pool.getconn()

def db_put(conn):
    postgreSQL_pool.putconn(conn)

# ---------------- DB helpers ----------------
def run_fetchall(query, params=None):
    conn = None
    cur = None
    try:
        conn = db_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows] if rows else []
    except Exception as e:
        app.logger.error("DB fetchall error: %s Query:%s Params:%s", e, query, params)
        if cur:
            try: cur.close()
            except: pass
        return None
    finally:
        if conn:
            db_put(conn)

def run_fetchone(query, params=None):
    conn = None
    cur = None
    try:
        conn = db_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(query, params)
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        app.logger.error("DB fetchone error: %s Query:%s Params:%s", e, query, params)
        if cur:
            try: cur.close()
            except: pass
        return None
    finally:
        if conn:
            db_put(conn)

def run_commit(query, params=None, returning=False):
    conn = None
    cur = None
    try:
        conn = db_conn()
        cur = conn.cursor()
        cur.execute(query, params)
        result_id = None
        if returning:
            maybe = cur.fetchone()
            if maybe:
                try:
                    result_id = maybe[0]
                except Exception:
                    try:
                        result_id = list(maybe.values())[0]
                    except Exception:
                        result_id = None
        conn.commit()
        cur.close()
        return result_id if returning else True
    except Exception as e:
        app.logger.error("DB commit error: %s Query:%s Params:%s", e, query, params)
        if conn:
            try: conn.rollback()
            except: pass
        if cur:
            try: cur.close()
            except: pass
        return None if returning else False
    finally:
        if conn:
            db_put(conn)

def column_exists(table_name, column_name):
    q = """
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s
    """
    r = run_fetchone(q, (table_name, column_name))
    return bool(r)

# ---------------- Migrations (safe) ----------------
def ensure_schema():
    # base create tables
    create_stmts = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            display_name TEXT,
            role TEXT NOT NULL DEFAULT 'buyer',
            password_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """
        CREATE TABLE IF NOT EXISTS listings (
            id SERIAL PRIMARY KEY,
            seller_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            title TEXT NOT NULL,
            category TEXT,
            description TEXT,
            price NUMERIC(12,2) NOT NULL DEFAULT 0.0,
            stock INTEGER NOT NULL DEFAULT 0,
            delivery_options TEXT,
            is_organic BOOLEAN DEFAULT FALSE,
            freshness TEXT,
            location TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """
        CREATE TABLE IF NOT EXISTS listing_images (
            id SERIAL PRIMARY KEY,
            listing_id INTEGER REFERENCES listings(id) ON DELETE CASCADE,
            image_path TEXT NOT NULL
        )""",
        """
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            buyer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            total_amount NUMERIC(12,2) DEFAULT 0.0,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
            listing_id INTEGER REFERENCES listings(id) ON DELETE SET NULL,
            seller_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            listing_title TEXT,
            unit_price NUMERIC(12,2),
            quantity INTEGER,
            line_total NUMERIC(12,2)
        )""",
        """
        CREATE TABLE IF NOT EXISTS ratings (
            id SERIAL PRIMARY KEY,
            listing_id INTEGER REFERENCES listings(id) ON DELETE CASCADE,
            buyer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
            review TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    ]
    for s in create_stmts:
        ok = run_commit(s)
        if not ok:
            app.logger.error("Migration failed for create: %s", s)
    # optional columns (safe)
    optional_alters = [
        ("users","last_login","TIMESTAMP"),
        ("listings","updated_at","TIMESTAMP"),
        ("orders","updated_at","TIMESTAMP"),
        # add more if needed
    ]
    for t,c,typ in optional_alters:
        try:
            run_commit(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {c} {typ}")
        except Exception as e:
            app.logger.error("Migration alter error: %s", e)

ensure_schema()

# ---------------- Utilities ----------------
EMAIL_RE = re.compile(r"^[^@]+@gmail\.com$")  # gmail only for now
def is_valid_gmail(email):
    return bool(email and EMAIL_RE.match(email.strip().lower()))

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT

def save_image(file_storage):
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_file(file_storage.filename):
        return None
    name = secure_filename(file_storage.filename)
    unique = f"{uuid.uuid4().hex[:12]}-{name}"
    dest = os.path.join(UPLOAD_DIR, unique)
    file_storage.save(dest)
    return unique

# role decorator
def roles_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("user_id"):
                flash("Please login.", "error")
                return redirect(url_for("auth"))
            user_role = session.get("user_role")
            if user_role not in roles:
                flash("Access denied for your role.", "error")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapper
    return decorator

# ---------------- Templates (concise but functional) ----------------
# For brevity keep templates inline. You can expand CSS/JS later.
NAV_HTML = """
<nav style="display:flex;gap:12px;align-items:center;margin-bottom:12px">
  <a href="{{ url_for('index') }}">Home</a>
  <a href="{{ url_for('marketplace') }}">Marketplace</a>
  {% if session.get('user_id') %}
    {% if session.get('user_role') == 'farmer' %}
      <a href="{{ url_for('farmer_dashboard') }}">Farmer Dashboard</a>
    {% endif %}
    {% if session.get('user_role') == 'buyer' %}
      <a href="{{ url_for('cart') }}">Cart ({{ session.get('cart')|length if session.get('cart') else 0 }})</a>
    {% endif %}
    {% if session.get('user_role') == 'admin' %}
      <a href="{{ url_for('admin_dashboard') }}">Admin</a>
    {% endif %}
    <a href="{{ url_for('logout') }}">Logout</a>
  {% else %}
    <a href="{{ url_for('auth') }}">Sign in (Gmail)</a>
  {% endif %}
</nav>
"""

INDEX_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Farm Marketplace</title></head><body>
<div style="max-width:1100px;margin:20px auto;">
  """ + NAV_HTML + """
  <h1>Welcome to Farm Marketplace</h1>
  <p>Gmail only sign-in. Choose a role (farmer/buyer/admin) at first sign-in.</p>
  <p><a href="{{ url_for('marketplace') }}">Browse Marketplace</a></p>
</div></body></html>
"""

AUTH_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Sign in</title></head><body>
<div style="max-width:700px;margin:20px auto;">
  """ + NAV_HTML + """
  <h2>Sign in with Gmail (mock)</h2>
  {% for m in get_flashed_messages() %}
    <div style="background:#fee;padding:8px;border-radius:6px;margin-bottom:8px">{{ m }}</div>
  {% endfor %}
  <form method="post" action="{{ url_for('auth') }}">
    <label>Email (Gmail only):</label><br>
    <input type="email" name="email" required><br><br>
    <label>Display name:</label><br>
    <input type="text" name="display_name"><br><br>
    <label>Role:</label><br>
    <select name="role">
      <option value="buyer">Buyer</option>
      <option value="farmer">Farmer</option>
      <!-- admin option included but real-world you would restrict this -->
      <option value="admin">Admin</option>
    </select><br><br>
    <button type="submit">Sign in</button>
  </form>
</div></body></html>
"""

MARKET_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Marketplace</title></head><body>
<div style="max-width:1100px;margin:20px auto;">
  """ + NAV_HTML + """
  <h2>Marketplace</h2>
  <form method="get" action="{{ url_for('marketplace') }}">
    <input name="q" placeholder="search product or farmer" value="{{ q or '' }}">
    <select name="category">
      <option value="">All categories</option>
      {% for c in categories %}<option value="{{ c }}" {% if c==category %}selected{% endif %}>{{ c }}</option>{% endfor %}
    </select>
    <label>Organic <input type="checkbox" name="organic" value="1" {% if organic %}checked{% endif %}></label>
    <label>Price from <input type="number" name="min_price" step="0.01" value="{{ min_price or '' }}"></label>
    <label>to <input type="number" name="max_price" step="0.01" value="{{ max_price or '' }}"></label>
    <select name="sort">
      <option value="">Sort</option>
      <option value="price_asc" {% if sort=='price_asc' %}selected{% endif %}>Price low→high</option>
      <option value="price_desc" {% if sort=='price_desc' %}selected{% endif %}>Price high→low</option>
      <option value="newest" {% if sort=='newest' %}selected{% endif %}>Newest</option>
      <option value="rating" {% if sort=='rating' %}selected{% endif %}>Rating</option>
    </select>
    <button type="submit">Filter</button>
  </form>

  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-top:12px">
    {% for l in listings %}
      <div style="border:1px solid #ddd;padding:8px;border-radius:8px">
        <div style="height:140px;overflow:hidden;background:#f8f8f8;text-align:center">
          {% if l.images and l.images|length>0 %}
            <img src="{{ url_for('uploaded_file', filename=l.images[0]) }}" style="max-height:140px">
          {% else %}
            <div style="padding:40px;color:#aaa">No image</div>
          {% endif %}
        </div>
        <h3>{{ l.title }}</h3>
        <div>Seller: {{ l.seller_name or '—' }}</div>
        <div>Category: {{ l.category or '—' }}</div>
        <div>Price: ₹{{ "%.2f"|format(l.price) }}</div>
        <div>Stock: {{ l.stock }}</div>
        <div>Organic: {{ 'Yes' if l.is_organic else 'No' }}</div>
        <div>Avg Rating: {{ l.avg_rating or '—' }}</div>
        <div style="margin-top:6px">
          <a href="{{ url_for('view_listing', listing_id=l.id) }}">View</a>
          {% if session.get('user_role') == 'buyer' %}
            <form method="post" action="{{ url_for('add_to_cart', listing_id=l.id) }}" style="display:inline">
              <input name="qty" value="1" size="2">
              <button type="submit">Add to cart</button>
            </form>
          {% endif %}
        </div>
      </div>
    {% else %}
      <div>No listings found.</div>
    {% endfor %}
  </div>
</div></body></html>
"""

LISTING_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>{{ listing.title }}</title></head><body>
<div style="max-width:900px;margin:20px auto;">""" + NAV_HTML + """
  <h2>{{ listing.title }}</h2>
  <div style="display:flex;gap:12px">
    <div style="min-width:320px">
      {% for img in images %}
        <img src="{{ url_for('uploaded_file', filename=img) }}" style="max-width:320px;display:block;margin-bottom:8px">
      {% endfor %}
    </div>
    <div>
      <div>Seller: {{ seller.display_name or seller.email }}</div>
      <div>Category: {{ listing.category or '—' }}</div>
      <div>Price: ₹{{ "%.2f"|format(listing.price) }}</div>
      <div>Stock: {{ listing.stock }}</div>
      <div>Delivery: {{ listing.delivery_options or '—' }}</div>
      <div>Organic: {{ 'Yes' if listing.is_organic else 'No' }}</div>
      <div>Freshness: {{ listing.freshness or '—' }}</div>
      <p>{{ listing.description or '' }}</p>

      {% if session.get('user_role') == 'buyer' %}
        <form method="post" action="{{ url_for('add_to_cart', listing_id=listing.id) }}">
          <label>Qty</label><input name="qty" value="1" size="3">
          <button type="submit">Add to cart</button>
        </form>
      {% endif %}
    </div>
  </div>

  <h3>Reviews</h3>
  <div>
    {% for r in reviews %}
      <div style="border-top:1px solid #eee;padding:8px 0;">
        <strong>{{ r.buyer_name }}</strong> — {{ r.rating }}/5<br>
        {{ r.review }}
      </div>
    {% else %}
      <div>No reviews yet.</div>
    {% endfor %}
  </div>
</div></body></html>
"""

CART_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Cart</title></head><body>
<div style="max-width:900px;margin:20px auto;">""" + NAV_HTML + """
  <h2>Your Cart</h2>
  {% if cart_items %}
    <table border="1" cellpadding="8" cellspacing="0" style="width:100%;border-collapse:collapse">
      <tr><th>Item</th><th>Unit</th><th>Qty</th><th>Line total</th><th>Action</th></tr>
      {% for it in cart_items %}
        <tr>
          <td>{{ it.title }}</td>
          <td>₹{{ "%.2f"|format(it.price) }}</td>
          <td>{{ it.qty }}</td>
          <td>₹{{ "%.2f"|format(it.line_total) }}</td>
          <td>
            <form method="post" action="{{ url_for('remove_from_cart', listing_id=it.listing_id) }}">
              <button type="submit">Remove</button>
            </form>
          </td>
        </tr>
      {% endfor %}
    </table>
    <h3>Total: ₹{{ "%.2f"|format(total) }}</h3>
    <form method="post" action="{{ url_for('checkout') }}">
      <button type="submit">Checkout (Mock Payment)</button>
    </form>
  {% else %}
    <p>Your cart is empty.</p>
  {% endif %}
</div></body></html>
"""

FARMER_DASH_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Farmer Dashboard</title></head><body>
<div style="max-width:1100px;margin:20px auto;">""" + NAV_HTML + """
  <h2>Farmer dashboard</h2>
  <h3>Add Listing</h3>
  <form method="post" action="{{ url_for('create_listing') }}" enctype="multipart/form-data">
    <input name="title" placeholder="Title" required><br>
    <input name="category" placeholder="Category"><br>
    <textarea name="description" placeholder="Description"></textarea><br>
    <input name="price" type="number" step="0.01" value="0"><br>
    <input name="stock" type="number" value="0"><br>
    <input name="delivery_options" placeholder="Delivery options"><br>
    <label>Organic <input name="is_organic" type="checkbox" value="1"></label><br>
    <input name="freshness" placeholder="Freshness text"><br>
    <input name="location" placeholder="Location"><br>
    <label>Images (multiple): <input name="images" type="file" multiple accept="image/*"></label><br>
    <button type="submit">Create Listing</button>
  </form>

  <h3>Your Listings</h3>
  {% for l in my_listings %}
    <div style="border:1px solid #ddd;padding:8px;margin-bottom:6px">
      <strong>{{ l.title }}</strong> — ₹{{ "%.2f"|format(l.price) }} — Stock: {{ l.stock }} — <a href="{{ url_for('view_listing', listing_id=l.id) }}">View</a>
      <form method="post" action="{{ url_for('delete_listing', listing_id=l.id) }}" style="display:inline" onsubmit="return confirm('Delete listing?')">
        <button type="submit">Delete</button>
      </form>
    </div>
  {% else %}
    <div>You have no listings yet.</div>
  {% endfor %}
</div></body></html>
"""

ADMIN_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Admin</title></head><body>
<div style="max-width:1100px;margin:20px auto;">""" + NAV_HTML + """
  <h2>Admin Dashboard</h2>

  <h3>Users</h3>
  {% for u in users %}
    <div style="border:1px solid #ddd;padding:8px;margin-bottom:6px">
      {{ u.id }} — {{ u.email }} — {{ u.display_name or '' }} — {{ u.role }}
      <form method="post" action="{{ url_for('admin_change_role', user_id=u.id) }}" style="display:inline">
        <select name="role">
          <option value="buyer" {% if u.role=='buyer' %}selected{% endif %}>Buyer</option>
          <option value="farmer" {% if u.role=='farmer' %}selected{% endif %}>Farmer</option>
          <option value="admin" {% if u.role=='admin' %}selected{% endif %}>Admin</option>
        </select>
        <button type="submit">Change</button>
      </form>
    </div>
  {% endfor %}

  <h3>Orders</h3>
  {% for o in orders %}
    <div style="border:1px solid #ddd;padding:8px;margin-bottom:6px">
      Order #{{ o.id }} — Buyer: {{ o.buyer_email or '—' }} — ₹{{ "%.2f"|format(o.total_amount or 0) }} — Status: {{ o.status }}
      <form method="post" action="{{ url_for('admin_update_order', order_id=o.id) }}" style="display:inline">
        <select name="status">
          <option value="pending" {% if o.status=='pending' %}selected{% endif %}>pending</option>
          <option value="paid" {% if o.status=='paid' %}selected{% endif %}>paid</option>
          <option value="shipped" {% if o.status=='shipped' %}selected{% endif %}>shipped</option>
          <option value="completed" {% if o.status=='completed' %}selected{% endif %}>completed</option>
          <option value="cancelled" {% if o.status=='cancelled' %}selected{% endif %}>cancelled</option>
        </select>
        <button type="submit">Update</button>
      </form>
    </div>
  {% endfor %}
</div></body></html>
"""

# ---------------- Routes ----------------

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

@app.route("/auth", methods=["GET", "POST"])
def auth():
    # simple Gmail-only sign-in
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        display_name = (request.form.get("display_name") or "").strip()
        role = (request.form.get("role") or "buyer")
        if not is_valid_gmail(email):
            flash("Please use a valid Gmail address (ends with @gmail.com).")
            return redirect(url_for("auth"))
        # create user if not exists
        user = run_fetchone("SELECT * FROM users WHERE email = %s", (email,))
        if not user:
            # insert
            ok = run_commit("INSERT INTO users (email, display_name, role) VALUES (%s,%s,%s) RETURNING id", (email, display_name or None, role), returning=True)
            if not ok:
                flash("Failed to create user — DB error.")
                return redirect(url_for("auth"))
            user_id = ok
            user = run_fetchone("SELECT * FROM users WHERE id = %s", (user_id,))
        # set session
        session["user_id"] = user["id"]
        session["user_email"] = user["email"]
        session["user_name"] = user.get("display_name") or user["email"]
        session["user_role"] = user.get("role") or "buyer"
        flash(f"Signed in as {session['user_name']} ({session['user_role']})")
        return redirect(url_for("marketplace"))
    return render_template_string(AUTH_HTML)

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("index"))

# ---------------- Marketplace & Listings ----------------

# helper: categories present in listings
def get_categories():
    rows = run_fetchall("SELECT DISTINCT category FROM listings WHERE category IS NOT NULL AND category <> ''")
    if not rows:
        return []
    return [r['category'] for r in rows]

@app.route("/marketplace")
def marketplace():
    q = (request.args.get("q") or "").strip()
    category = (request.args.get("category") or "").strip()
    organic = request.args.get("organic")
    min_price = request.args.get("min_price")
    max_price = request.args.get("max_price")
    sort = request.args.get("sort")

    # build query
    base = "SELECT l.*, u.display_name AS seller_name, COALESCE(avg_r.avg,0)::numeric(3,2) AS avg_rating FROM listings l LEFT JOIN users u ON l.seller_id = u.id LEFT JOIN (SELECT listing_id, AVG(rating) AS avg FROM ratings GROUP BY listing_id) avg_r ON l.id = avg_r.listing_id WHERE 1=1"
    params = []
    if q:
        base += " AND (l.title ILIKE %s OR u.display_name ILIKE %s OR l.description ILIKE %s)"
        p = f"%{q}%"
        params.extend([p,p,p])
    if category:
        base += " AND l.category = %s"
        params.append(category)
    if organic:
        base += " AND l.is_organic = TRUE"
    if min_price:
        try:
            base += " AND l.price >= %s"; params.append(float(min_price))
        except: pass
    if max_price:
        try:
            base += " AND l.price <= %s"; params.append(float(max_price))
        except: pass

    # sorting
    order_by = " ORDER BY l.created_at DESC"
    if sort == "price_asc":
        order_by = " ORDER BY l.price ASC"
    elif sort == "price_desc":
        order_by = " ORDER BY l.price DESC"
    elif sort == "newest":
        order_by = " ORDER BY l.created_at DESC"
    elif sort == "rating":
        order_by = " ORDER BY avg_r.avg DESC NULLS LAST"

    sql = base + order_by + " LIMIT 200"
    rows = run_fetchall(sql, params)
    if rows is None:
        rows = []
    # attach images
    for r in rows:
        imgs = run_fetchall("SELECT image_path FROM listing_images WHERE listing_id = %s ORDER BY id", (r['id'],))
        r['images'] = [i['image_path'] for i in imgs] if imgs else []
        # coerce fields
        r['price'] = float(r['price']) if r.get('price') is not None else 0.0
        r['stock'] = int(r['stock']) if r.get('stock') is not None else 0
        r['is_organic'] = bool(r.get('is_organic'))
        r['avg_rating'] = float(r['avg_rating']) if r.get('avg_rating') is not None else None

    return render_template_string(MARKET_HTML, listings=rows, categories=get_categories(),
                                  q=q, category=category, organic=organic, min_price=min_price, max_price=max_price, sort=sort)

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)

@app.route("/listing/<int:listing_id>")
def view_listing(listing_id):
    l = run_fetchone("SELECT l.*, u.display_name AS seller_name, u.email as seller_email FROM listings l LEFT JOIN users u ON l.seller_id = u.id WHERE l.id = %s", (listing_id,))
    if not l:
        flash("Listing not found.")
        return redirect(url_for("marketplace"))
    imgs = run_fetchall("SELECT image_path FROM listing_images WHERE listing_id = %s ORDER BY id", (listing_id,))
    images = [i['image_path'] for i in imgs] if imgs else []
    seller = run_fetchone("SELECT id,email,display_name FROM users WHERE id = %s", (l['seller_id'],)) or {}
    reviews = run_fetchall("SELECT r.*, u.display_name AS buyer_name FROM ratings r LEFT JOIN users u ON r.buyer_id = u.id WHERE r.listing_id = %s ORDER BY r.created_at DESC", (listing_id,))
    return render_template_string(LISTING_HTML, listing=l, images=images, seller=seller, reviews=reviews)

# ---------------- Cart & Checkout ----------------
def get_cart():
    return session.get("cart", {})  # {listing_id: qty}

def save_cart(cart):
    session['cart'] = cart
    session.modified = True

@app.route("/cart")
def cart():
    cart = get_cart()
    if not cart:
        return render_template_string(CART_HTML, cart_items=[], total=0)
    # fetch listing details
    items = []
    total = 0.0
    for lid, qty in cart.items():
        l = run_fetchone("SELECT id,title,price,stock FROM listings WHERE id = %s", (lid,))
        if not l:
            continue
        unit = float(l['price'])
        line = unit * int(qty)
        total += line
        items.append({
            'listing_id': l['id'],
            'title': l['title'],
            'price': unit,
            'qty': int(qty),
            'line_total': line
        })
    return render_template_string(CART_HTML, cart_items=items, total=total)

@app.route("/cart/add/<int:listing_id>", methods=["POST"])
def add_to_cart(listing_id):
    # only buyers can add
    if session.get("user_role") != "buyer":
        flash("Only buyers can add to cart.")
        return redirect(url_for("view_listing", listing_id=listing_id))
    qty = int(request.form.get("qty") or 1)
    if qty < 1: qty = 1
    cart = get_cart()
    cart[str(listing_id)] = cart.get(str(listing_id), 0) + qty
    save_cart(cart)
    flash("Added to cart.")
    return redirect(url_for("marketplace"))

@app.route("/cart/remove/<int:listing_id>", methods=["POST"])
def remove_from_cart(listing_id):
    cart = get_cart()
    cart.pop(str(listing_id), None)
    save_cart(cart)
    flash("Removed from cart.")
    return redirect(url_for("cart"))

@app.route("/checkout", methods=["POST"])
def checkout():
    # mock checkout: create order with status 'pending', then immediately mark 'paid' on confirmation page
    if session.get("user_role") != "buyer":
        flash("Only buyers can checkout.")
        return redirect(url_for("cart"))
    cart = get_cart()
    if not cart:
        flash("Cart empty.")
        return redirect(url_for("cart"))
    # compute totals and verify stock
    total = 0.0
    order_items = []
    for lid_str, qty in cart.items():
        lid = int(lid_str)
        l = run_fetchone("SELECT id,title,price,stock,seller_id FROM listings WHERE id = %s", (lid,))
        if not l:
            flash("Listing missing; please update cart.")
            return redirect(url_for("cart"))
        if int(qty) > (l['stock'] or 0):
            flash(f"Not enough stock for {l['title']}.")
            return redirect(url_for("cart"))
        unit = float(l['price'])
        line = unit * int(qty)
        total += line
        order_items.append((lid, l['title'], l['seller_id'], unit, int(qty), line))
    # create order
    order_id = run_commit("INSERT INTO orders (buyer_id, total_amount, status) VALUES (%s,%s,%s) RETURNING id",
                          (session.get("user_id"), total, "pending"), returning=True)
    if not order_id:
        flash("Failed to create order.")
        return redirect(url_for("cart"))
    # insert order items & decrement stock
    for lid, title, seller_id, unit, qty, line in order_items:
        run_commit("INSERT INTO order_items (order_id, listing_id, seller_id, listing_title, unit_price, quantity, line_total) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                   (order_id, lid, seller_id, title, unit, qty, line))
        # decrement stock
        run_commit("UPDATE listings SET stock = GREATEST(stock - %s, 0) WHERE id = %s", (qty, lid))
    # clear cart
    save_cart({})
    # redirect to payment simulation page (mock)
    flash("Order created. Proceed to payment (mock).")
    return redirect(url_for("pay", order_id=order_id))

@app.route("/pay/<int:order_id>", methods=["GET","POST"])
def pay(order_id):
    order = run_fetchone("SELECT o.*, u.email AS buyer_email FROM orders o LEFT JOIN users u ON o.buyer_id = u.id WHERE o.id = %s", (order_id,))
    if not order:
        flash("Order not found.")
        return redirect(url_for("marketplace"))
    if request.method == "POST":
        # simulate payment success
        run_commit("UPDATE orders SET status = %s WHERE id = %s", ("paid", order_id))
        flash("Payment successful (mock). Order marked as paid.")
        return redirect(url_for("order_receipt", order_id=order_id))
    # show simple payment page
    return """
    <h2>Mock Payment</h2>
    <p>Order #{} — Total: ₹{:.2f}</p>
    <form method="post"><button type="submit">Pay (mock)</button></form>
    """.format(order_id, float(order.get("total_amount") or 0.0))

@app.route("/order/<int:order_id>/receipt")
def order_receipt(order_id):
    order = run_fetchone("SELECT o.*, u.email AS buyer_email, u.display_name AS buyer_name FROM orders o LEFT JOIN users u ON o.buyer_id = u.id WHERE o.id = %s", (order_id,))
    if not order:
        flash("Order not found.")
        return redirect(url_for("marketplace"))
    items = run_fetchall("SELECT * FROM order_items WHERE order_id = %s", (order_id,))
    html = "<h2>Invoice</h2>"
    html += f"<div>Order #{order_id}</div><div>Buyer: {order.get('buyer_name') or order.get('buyer_email')}</div>"
    html += "<table border=1 cellpadding=6><tr><th>Item</th><th>Qty</th><th>Unit</th><th>Line</th></tr>"
    for it in items:
        html += f"<tr><td>{it['listing_title']}</td><td>{it['quantity']}</td><td>₹{float(it['unit_price'])}</td><td>₹{float(it['line_total'])}</td></tr>"
    html += "</table>"
    html += f"<p>Total: ₹{float(order.get('total_amount') or 0.0)}</p>"
    html += f"<p>Status: {order.get('status')}</p>"
    return html

# ---------------- Farmer actions ----------------

@app.route("/farmer")
@roles_required("farmer")
def farmer_dashboard():
    user_id = session.get("user_id")
    my_listings = run_fetchall("SELECT * FROM listings WHERE seller_id = %s ORDER BY created_at DESC", (user_id,))
    return render_template_string(FARMER_DASH_HTML, my_listings=my_listings)

@app.route("/listing/create", methods=["POST"])
@roles_required("farmer")
def create_listing():
    title = (request.form.get("title") or "").strip()
    category = (request.form.get("category") or "").strip()
    description = (request.form.get("description") or "").strip()
    try:
        price = float(request.form.get("price") or 0.0)
    except:
        price = 0.0
    try:
        stock = int(request.form.get("stock") or 0)
    except:
        stock = 0
    delivery_options = (request.form.get("delivery_options") or "").strip()
    is_organic = bool(request.form.get("is_organic"))
    freshness = (request.form.get("freshness") or "").strip()
    location = (request.form.get("location") or "").strip()
    seller_id = session.get("user_id")
    listing_id = run_commit("INSERT INTO listings (seller_id,title,category,description,price,stock,delivery_options,is_organic,freshness,location) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                            (seller_id, title, category, description, price, stock, delivery_options, is_organic, freshness, location), returning=True)
    if not listing_id:
        flash("Failed to create listing.")
        return redirect(url_for("farmer_dashboard"))
    # handle multiple files
    files = request.files.getlist("images")
    for f in files:
        if f and f.filename:
            saved = save_image(f)
            if saved:
                run_commit("INSERT INTO listing_images (listing_id, image_path) VALUES (%s,%s)", (listing_id, saved))
    flash("Listing created.")
    return redirect(url_for("farmer_dashboard"))

@app.route("/listing/<int:listing_id>/delete", methods=["POST"])
@roles_required("farmer")
def delete_listing(listing_id):
    # ensure ownership
    li = run_fetchone("SELECT * FROM listings WHERE id = %s", (listing_id,))
    if not li or li.get('seller_id') != session.get("user_id"):
        flash("Not allowed.")
        return redirect(url_for("farmer_dashboard"))
    # delete images from disk
    imgs = run_fetchall("SELECT image_path FROM listing_images WHERE listing_id = %s", (listing_id,))
    for i in imgs or []:
        path = os.path.join(UPLOAD_DIR, i['image_path'])
        try:
            if os.path.exists(path):
                os.remove(path)
        except:
            pass
    run_commit("DELETE FROM listing_images WHERE listing_id = %s", (listing_id,))
    run_commit("DELETE FROM listings WHERE id = %s", (listing_id,))
    flash("Listing deleted.")
    return redirect(url_for("farmer_dashboard"))

# ---------------- Ratings ----------------
@app.route("/listing/<int:listing_id>/rate", methods=["POST"])
@roles_required("buyer")
def rate_listing(listing_id):
    rating = int(request.form.get("rating") or 0)
    review = (request.form.get("review") or "").strip()
    if rating < 1 or rating > 5:
        flash("Invalid rating.")
        return redirect(url_for("view_listing", listing_id=listing_id))
    run_commit("INSERT INTO ratings (listing_id, buyer_id, rating, review) VALUES (%s,%s,%s,%s)",
               (listing_id, session.get("user_id"), rating, review))
    flash("Thanks for your review.")
    return redirect(url_for("view_listing", listing_id=listing_id))

# ---------------- Admin ----------------

@app.route("/admin")
@roles_required("admin")
def admin_dashboard():
    users = run_fetchall("SELECT id,email,display_name,role FROM users ORDER BY id DESC")
    orders = run_fetchall("SELECT o.*, u.email as buyer_email FROM orders o LEFT JOIN users u ON o.buyer_id = u.id ORDER BY o.created_at DESC LIMIT 100")
    return render_template_string(ADMIN_HTML, users=users or [], orders=orders or [])

@app.route("/admin/user/<int:user_id>/change_role", methods=["POST"])
@roles_required("admin")
def admin_change_role(user_id):
    new_role = request.form.get("role")
    if new_role not in ("buyer","farmer","admin"):
        flash("Invalid role.")
        return redirect(url_for("admin_dashboard"))
    run_commit("UPDATE users SET role = %s WHERE id = %s", (new_role, user_id))
    flash("Role updated.")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/order/<int:order_id>/update", methods=["POST"])
@roles_required("admin")
def admin_update_order(order_id):
    status = request.form.get("status")
    if status not in ("pending","paid","shipped","completed","cancelled"):
        flash("Invalid status.")
        return redirect(url_for("admin_dashboard"))
    run_commit("UPDATE orders SET status = %s WHERE id = %s", (status, order_id))
    flash("Order updated.")
    return redirect(url_for("admin_dashboard"))

# ---------------- API endpoints (simple) ----------------
@app.route("/api/me")
def api_me():
    if not session.get("user_id"):
        return jsonify({"error":"unauthenticated"}), 401
    return jsonify({"id":session.get("user_id"),"email":session.get("user_email"),"role":session.get("user_role")})

@app.route("/api/listings")
def api_listings():
    rows = run_fetchall("SELECT * FROM listings ORDER BY created_at DESC LIMIT 200")
    return jsonify(rows or [])

# ---------------- Run ----------------
if __name__ == "__main__":
    # debug False in production; Railway uses gunicorn normally
    app.run(host="0.0.0.0", port=PORT, debug=True)
