# app.py
"""
Enhanced FarmSync Flask app with improved features:
- Input validation and sanitization
- Better error handling and logging
- Enhanced UI with search/filter capabilities
- Proper pagination
- Data export functionality
- Better security headers
- Improved database queries with proper indexing
- Analytics dashboard
"""
import os
import re
import csv
import logging
from datetime import datetime, date, timedelta
from functools import wraps
from io import StringIO

from flask import (
    Flask, request, session, redirect, url_for, jsonify,
    render_template_string, flash, make_response, send_file
)
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

# ---------------- Config & Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")
PORT = int(os.environ.get("PORT", 3000))
NEON_DB_URL = os.environ.get(
    "NEON_DB_URL",
    "postgresql://neondb_owner:npg_jgROvpDtrm03@ep-hidden-truth-aev5l7a7-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
)

# Security headers
@app.after_request
def after_request(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

# ---------------- DB Pool ----------------
postgreSQL_pool = None
try:
    postgreSQL_pool = psycopg2.pool.SimpleConnectionPool(
        minconn=2, maxconn=25, dsn=NEON_DB_URL,
        cursor_factory=RealDictCursor
    )
    logger.info("Postgres pool created successfully")
except Exception as e:
    logger.error(f"Failed to create pool: {e}")
    postgreSQL_pool = None

def db_get_conn():
    if postgreSQL_pool is None:
        raise RuntimeError("DB pool not available")
    return postgreSQL_pool.getconn()

def db_put_conn(conn):
    if conn:
        postgreSQL_pool.putconn(conn)

# ---------------- Enhanced DB Helpers ----------------
def run_query_fetchall(query, params=None):
    conn = None
    cur = None
    try:
        conn = db_get_conn()
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        return [dict(row) for row in rows] if rows else []
    except Exception as e:
        logger.error(f"DB fetchall error: {e}, Query: {query}, Params: {params}")
        if cur:
            try: cur.close()
            except: pass
        return None
    finally:
        if conn:
            db_put_conn(conn)

def run_query_fetchone(query, params=None):
    conn = None
    cur = None
    try:
        conn = db_get_conn()
        cur = conn.cursor()
        cur.execute(query, params)
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"DB fetchone error: {e}")
        if cur:
            try: cur.close()
            except: pass
        return None
    finally:
        if conn:
            db_put_conn(conn)

def run_query_commit(query, params=None, return_id=False):
    conn = None
    cur = None
    try:
        conn = db_get_conn()
        cur = conn.cursor()
        cur.execute(query, params)
        result_id = None
        if return_id:
            result_id = cur.fetchone()
            if result_id:
                result_id = result_id[0]
        conn.commit()
        cur.close()
        return result_id if return_id else True
    except Exception as e:
        logger.error(f"DB commit error: {e}, Query: {query}")
        if conn:
            try: conn.rollback()
            except: pass
        if cur:
            try: cur.close()
            except: pass
        return None if return_id else False
    finally:
        if conn:
            db_put_conn(conn)

# ---------------- Input Validation & Sanitization ----------------
EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
PHONE_RE = re.compile(r"^\+?[\d\s\-\(\)]{10,15}$")

def sanitize_input(text, max_length=None):
    """Basic input sanitization"""
    if not text:
        return ""
    text = text.strip()
    if max_length:
        text = text[:max_length]
    # Remove potentially harmful characters
    text = re.sub(r'[<>"\';{}]', '', text)
    return text

def valid_email(email):
    return bool(email and EMAIL_RE.match(email.strip()))

def valid_password(password):
    return password and len(password) >= 8 and any(c.isdigit() for c in password)

def valid_phone(phone):
    return bool(phone and PHONE_RE.match(phone.strip()))

# ---------------- Authentication ----------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to access this page.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated

# ---------------- Database Schema & Migrations ----------------
def ensure_tables_and_columns():
    """Enhanced table creation with proper indexes"""
    migrations = [
        # Users table
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP,
            is_active BOOLEAN DEFAULT TRUE
        )
        """,
        # Farmers table
        """
        CREATE TABLE IF NOT EXISTS farmers (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            location TEXT,
            contact TEXT,
            email TEXT,
            products TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT TRUE
        )
        """,
        # Orders table
        """
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            farmer_id INTEGER REFERENCES farmers(id) ON DELETE SET NULL,
            farmer_name TEXT,
            items TEXT NOT NULL,
            quantity TEXT,
            unit_price DECIMAL(10,2),
            total_amount DECIMAL(10,2),
            status TEXT DEFAULT 'pending',
            priority TEXT DEFAULT 'medium',
            order_date DATE,
            delivery_date DATE,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        # Indexes for better performance
        "CREATE INDEX IF NOT EXISTS idx_farmers_name ON farmers(name)",
        "CREATE INDEX IF NOT EXISTS idx_farmers_location ON farmers(location)",
        "CREATE INDEX IF NOT EXISTS idx_orders_farmer_id ON orders(farmer_id)",
        "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
        "CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date)",
        "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)",
    ]
    
    for migration in migrations:
        if not run_query_commit(migration):
            logger.error(f"Migration failed: {migration}")

# Initialize database
try:
    ensure_tables_and_columns()
    logger.info("Database migrations completed successfully")
except Exception as e:
    logger.error(f"Migration error: {e}")

# ---------------- Enhanced Templates ----------------
ENHANCED_CSS = """
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    @import url('https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css');
    
    :root{
      --primary:#2563eb;--primary-dark:#1d4ed8;--secondary:#f1f5f9;
      --success:#10b981;--warning:#f59e0b;--danger:#ef4444;
      --bg:#f8fafc;--card:#ffffff;--text:#0f172a;--text-muted:#64748b;
      --border:#e2e8f0;--shadow:0 4px 6px -1px rgb(0 0 0 / 0.1);
    }
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);line-height:1.6}
    .container{max-width:1200px;margin:0 auto;padding:0 1rem}
    
    /* Header */
    .header{background:var(--card);border-bottom:1px solid var(--border);padding:1rem 0;box-shadow:var(--shadow)}
    .header-content{display:flex;justify-content:space-between;align-items:center}
    .brand{font-size:1.5rem;font-weight:700;color:var(--primary)}
    .user-menu{display:flex;align-items:center;gap:1rem}
    .avatar{width:32px;height:32px;border-radius:50%;background:var(--primary)}
    
    /* Main content */
    .main{padding:2rem 0}
    .grid{display:grid;gap:1.5rem;grid-template-columns:1fr}
    @media(min-width:768px){.grid{grid-template-columns:1fr 2fr}}
    @media(min-width:1024px){.grid{grid-template-columns:300px 1fr}}
    
    /* Cards */
    .card{background:var(--card);border:1px solid var(--border);border-radius:0.75rem;padding:1.5rem;box-shadow:var(--shadow)}
    .card h2{margin-bottom:1rem;color:var(--text);font-weight:600}
    .card h3{margin-bottom:0.75rem;color:var(--text);font-weight:500}
    
    /* Forms */
    .form-group{margin-bottom:1rem}
    .form-label{display:block;margin-bottom:0.25rem;font-weight:500;color:var(--text)}
    .form-input{width:100%;padding:0.75rem;border:1px solid var(--border);border-radius:0.5rem;font-size:0.875rem}
    .form-input:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px rgb(37 99 235 / 0.1)}
    .form-select{width:100%;padding:0.75rem;border:1px solid var(--border);border-radius:0.5rem;background:white}
    .form-textarea{width:100%;padding:0.75rem;border:1px solid var(--border);border-radius:0.5rem;min-height:100px;resize:vertical}
    
    /* Buttons */
    .btn{padding:0.75rem 1.5rem;border:none;border-radius:0.5rem;font-weight:500;cursor:pointer;transition:all 0.2s;display:inline-flex;align-items:center;gap:0.5rem}
    .btn-primary{background:var(--primary);color:white}
    .btn-primary:hover{background:var(--primary-dark)}
    .btn-secondary{background:var(--secondary);color:var(--text)}
    .btn-success{background:var(--success);color:white}
    .btn-warning{background:var(--warning);color:white}
    .btn-danger{background:var(--danger);color:white}
    .btn-sm{padding:0.5rem 1rem;font-size:0.875rem}
    
    /* Lists */
    .list{display:flex;flex-direction:column;gap:0.75rem}
    .list-item{padding:1rem;border:1px solid var(--border);border-radius:0.5rem;background:var(--card)}
    .list-item-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.5rem}
    .list-item-title{font-weight:600;color:var(--text)}
    .list-item-meta{color:var(--text-muted);font-size:0.875rem}
    .list-item-actions{display:flex;gap:0.5rem}
    
    /* Status badges */
    .badge{padding:0.25rem 0.75rem;border-radius:9999px;font-size:0.75rem;font-weight:600}
    .badge-pending{background:#fef3c7;color:#92400e}
    .badge-ongoing{background:#dbeafe;color:#1e40af}
    .badge-completed{background:#d1fae5;color:#065f46}
    .badge-high{background:#fee2e2;color:#991b1b}
    .badge-medium{background:#fef3c7;color:#92400e}
    .badge-low{background:#d1fae5;color:#065f46}
    
    /* Flash messages */
    .flash{padding:1rem;border-radius:0.5rem;margin-bottom:1rem;display:flex;align-items:center;gap:0.5rem}
    .flash-success{background:#d1fae5;color:#065f46;border:1px solid #a7f3d0}
    .flash-error{background:#fee2e2;color:#991b1b;border:1px solid #fca5a5}
    .flash-warning{background:#fef3c7;color:#92400e;border:1px solid #fde68a}
    
    /* Search and filters */
    .search-bar{display:flex;gap:0.5rem;margin-bottom:1rem}
    .search-input{flex:1;padding:0.75rem;border:1px solid var(--border);border-radius:0.5rem}
    .filters{display:flex;gap:0.5rem;margin-bottom:1rem;flex-wrap:wrap}
    .filter-btn{padding:0.5rem 1rem;border:1px solid var(--border);border-radius:0.5rem;background:white;cursor:pointer;transition:all 0.2s}
    .filter-btn.active{background:var(--primary);color:white;border-color:var(--primary)}
    
    /* Stats */
    .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem;margin-bottom:2rem}
    .stat-card{background:var(--card);border:1px solid var(--border);border-radius:0.5rem;padding:1.5rem;text-align:center}
    .stat-value{font-size:2rem;font-weight:700;color:var(--primary)}
    .stat-label{color:var(--text-muted);font-size:0.875rem}
    
    /* Responsive */
    @media(max-width:768px){
      .container{padding:0 0.5rem}
      .grid{grid-template-columns:1fr}
      .list-item-header{flex-direction:column;align-items:flex-start}
      .list-item-actions{margin-top:0.5rem}
    }
    
    /* Animations */
    @keyframes fadeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
    .fade-in{animation:fadeIn 0.3s ease-out}
    
    /* Dark mode support */
    @media(prefers-color-scheme:dark){
      :root{
        --bg:#0f172a;--card:#1e293b;--text:#f1f5f9;--text-muted:#94a3b8;
        --border:#334155;--shadow:0 4px 6px -1px rgb(0 0 0 / 0.3);
      }
      .form-input,.form-select,.form-textarea{background:var(--card);color:var(--text)}
    }
"""

BASE_HEAD = f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>FarmSync - Farm Management System</title>
  <style>{ENHANCED_CSS}</style>
</head>
<body>
"""

BASE_FOOT = """
<script>
// Enhanced JavaScript functionality
document.addEventListener('DOMContentLoaded', function() {
    // Auto-hide flash messages
    setTimeout(() => {
        document.querySelectorAll('.flash').forEach(el => {
            el.style.transition = 'opacity 0.3s ease';
            el.style.opacity = '0';
            setTimeout(() => el.remove(), 300);
        });
    }, 5000);
    
    // Search functionality
    const searchInput = document.querySelector('.search-input');
    if (searchInput) {
        searchInput.addEventListener('input', function() {
            const query = this.value.toLowerCase();
            document.querySelectorAll('.list-item').forEach(item => {
                const text = item.textContent.toLowerCase();
                item.style.display = text.includes(query) ? 'block' : 'none';
            });
        });
    }
    
    // Filter functionality
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
        });
    });
    
    // Confirm dangerous actions
    document.querySelectorAll('[data-confirm]').forEach(el => {
        el.addEventListener('click', function(e) {
            if (!confirm(this.dataset.confirm)) {
                e.preventDefault();
            }
        });
    });
});
</script>
</body>
</html>
"""

# Enhanced Dashboard Template
ENHANCED_DASH_HTML = BASE_HEAD + """
<div class="header">
    <div class="container">
        <div class="header-content">
            <div class="brand">🚜 FarmSync</div>
            <div class="user-menu">
                <span>{{ user.display_name or user.email }}</span>
                <div class="avatar"></div>
                <a href="{{ url_for('export_data') }}" class="btn btn-sm btn-secondary">
                    <i class="fas fa-download"></i> Export
                </a>
                <a href="{{ url_for('logout') }}" class="btn btn-sm btn-secondary">Logout</a>
            </div>
        </div>
    </div>
</div>

<div class="container main">
    {% for message in get_flashed_messages(with_categories=true) %}
        <div class="flash flash-{{ message[0] }} fade-in">
            <i class="fas fa-{{ 'check-circle' if message[0] == 'success' else 'exclamation-triangle' }}"></i>
            {{ message[1] }}
        </div>
    {% endfor %}
    
    <!-- Analytics Dashboard -->
    <div class="stats">
        <div class="stat-card">
            <div class="stat-value">{{ stats.total_farmers }}</div>
            <div class="stat-label">Total Farmers</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{{ stats.total_orders }}</div>
            <div class="stat-label">Total Orders</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{{ stats.pending_orders }}</div>
            <div class="stat-label">Pending Orders</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${{ "%.2f"|format(stats.total_revenue) }}</div>
            <div class="stat-label">Total Revenue</div>
        </div>
    </div>
    
    <div class="grid">
        <!-- Left Sidebar -->
        <div>
            <div class="card">
                <h2><i class="fas fa-user-plus"></i> Add Farmer</h2>
                <form method="post" action="{{ url_for('add_farmer') }}">
                    <div class="form-group">
                        <label class="form-label">Name *</label>
                        <input type="text" name="name" class="form-input" required maxlength="100">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Location</label>
                        <input type="text" name="location" class="form-input" maxlength="100">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Contact</label>
                        <input type="text" name="contact" class="form-input" maxlength="20">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Email</label>
                        <input type="email" name="email" class="form-input">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Products</label>
                        <input type="text" name="products" class="form-input" placeholder="e.g., Tomatoes, Wheat, Rice">
                    </div>
                    <button type="submit" class="btn btn-primary">
                        <i class="fas fa-plus"></i> Add Farmer
                    </button>
                </form>
            </div>
            
            <div style="height:1rem"></div>
            
            <div class="card">
                <h2><i class="fas fa-shopping-cart"></i> Create Order</h2>
                <form method="post" action="{{ url_for('add_order') }}">
                    <div class="form-group">
                        <label class="form-label">Farmer *</label>
                        <select name="farmer_id" class="form-select" required>
                            <option value="">Select farmer...</option>
                            {% for f in farmers %}
                                <option value="{{ f.id }}">{{ f.name }}{% if f.location %} - {{ f.location }}{% endif %}</option>
                            {% endfor %}
                        </select>
                    </div>
                    <div class="form-group">
                        <label class="form-label">Items *</label>
                        <input type="text" name="items" class="form-input" required>
                    </div>
                    <div class="form-group">
                        <label class="form-label">Quantity</label>
                        <input type="text" name="quantity" class="form-input" placeholder="e.g., 100 kg">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Unit Price</label>
                        <input type="number" name="unit_price" class="form-input" step="0.01" min="0">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Priority</label>
                        <select name="priority" class="form-select">
                            <option value="low">Low</option>
                            <option value="medium" selected>Medium</option>
                            <option value="high">High</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label class="form-label">Order Date</label>
                        <input type="date" name="order_date" class="form-input" value="{{ today }}">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Expected Delivery</label>
                        <input type="date" name="delivery_date" class="form-input">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Notes</label>
                        <textarea name="notes" class="form-textarea" placeholder="Additional notes..."></textarea>
                    </div>
                    <button type="submit" class="btn btn-primary">
                        <i class="fas fa-plus"></i> Create Order
                    </button>
                </form>
            </div>
        </div>
        
        <!-- Main Content -->
        <div>
            <!-- Farmers Section -->
            <div class="card">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
                    <h2><i class="fas fa-users"></i> Farmers ({{ farmers|length }})</h2>
                    <div class="search-bar" style="width:300px">
                        <input type="text" class="search-input" placeholder="Search farmers...">
                    </div>
                </div>
                
                <div class="list">
                    {% for farmer in farmers %}
                        <div class="list-item fade-in">
                            <div class="list-item-header">
                                <div>
                                    <div class="list-item-title">{{ farmer.name }}</div>
                                    <div class="list-item-meta">
                                        {% if farmer.location %}<i class="fas fa-map-marker-alt"></i> {{ farmer.location }}{% endif %}
                                        {% if farmer.contact %} • <i class="fas fa-phone"></i> {{ farmer.contact }}{% endif %}
                                        {% if farmer.email %} • <i class="fas fa-envelope"></i> {{ farmer.email }}{% endif %}
                                    </div>
                                    {% if farmer.products %}
                                        <div class="list-item-meta" style="margin-top:0.25rem">
                                            <i class="fas fa-seedling"></i> {{ farmer.products }}
                                        </div>
                                    {% endif %}
                                </div>
                                <div class="list-item-actions">
                                    <span class="badge badge-{{ 'completed' if farmer.is_active else 'pending' }}">
                                        {{ 'Active' if farmer.is_active else 'Inactive' }}
                                    </span>
                                </div>
                            </div>
                        </div>
                    {% else %}
                        <div class="list-item">
                            <p style="text-align:center;color:var(--text-muted)">No farmers added yet.</p>
                        </div>
                    {% endfor %}
                </div>
            </div>
            
            <div style="height:1.5rem"></div>
            
            <!-- Orders Section -->
            <div class="card">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
                    <h2><i class="fas fa-clipboard-list"></i> Recent Orders</h2>
                    <div class="filters">
                        <button class="filter-btn active" onclick="filterOrders('all')">All</button>
                        <button class="filter-btn" onclick="filterOrders('pending')">Pending</button>
                        <button class="filter-btn" onclick="filterOrders('ongoing')">Ongoing</button>
                        <button class="filter-btn" onclick="filterOrders('completed')">Completed</button>
                    </div>
                </div>
                
                <div class="list">
                    {% for order in orders %}
                        <div class="list-item fade-in" data-status="{{ order.status }}">
                            <div class="list-item-header">
                                <div style="flex:1">
                                    <div class="list-item-title">Order #{{ order.id }}</div>
                                    <div class="list-item-meta">
                                        <i class="fas fa-user"></i> {{ order.farmer_name or 'Unknown Farmer' }} • 
                                        <i class="fas fa-box"></i> {{ order.items }}
                                        {% if order.quantity %} ({{ order.quantity }}){% endif %}
                                    </div>
                                    <div class="list-item-meta">
                                        <i class="fas fa-calendar"></i> {{ order.order_date or 'No date' }}
                                        {% if order.delivery_date %} → {{ order.delivery_date }}{% endif %}
                                        {% if order.total_amount %} • <i class="fas fa-dollar-sign"></i> ${{ "%.2f"|format(order.total_amount) }}{% endif %}
                                    </div>
                                    {% if order.notes %}
                                        <div class="list-item-meta"><i class="fas fa-sticky-note"></i> {{ order.notes }}</div>
                                    {% endif %}
                                </div>
                                <div class="list-item-actions">
                                    <span class="badge badge-{{ order.priority }}">{{ order.priority|title }}</span>
                                    <span class="badge badge-{{ order.status }}">{{ order.status|title }}</span>
                                    {% if order.status != 'completed' %}
                                        <form method="post" action="{{ url_for('update_order_status', order_id=order.id) }}" style="display:inline">
                                            <input type="hidden" name="status" value="completed">
                                            <button type="submit" class="btn btn-sm btn-success">
                                                <i class="fas fa-check"></i> Complete
                                            </button>
                                        </form>
                                    {% endif %}
                                </div>
                            </div>
                        </div>
                    {% else %}
                        <div class="list-item">
                            <p style="text-align:center;color:var(--text-muted)">No orders created yet.</p>
                        </div>
                    {% endfor %}
                </div>
            </div>
        </div>
    </div>
</div>

<script>
function filterOrders(status) {
    document.querySelectorAll('[data-status]').forEach(item => {
        if (status === 'all' || item.dataset.status === status) {
            item.style.display = 'block';
        } else {
            item.style.display = 'none';
        }
    });
}
</script>
""" + BASE_FOOT

# Simple Index/Login Template
INDEX_HTML = BASE_HEAD + """
<div class="container" style="max-width:800px;margin-top:4rem">
    <div class="card" style="text-align:center;margin-bottom:2rem">
        <h1 style="font-size:3rem;margin-bottom:0.5rem">🚜 FarmSync</h1>
        <p style="color:var(--text-muted);font-size:1.125rem">Modern Farm Management System</p>
    </div>
    
    {% for message in get_flashed_messages(with_categories=true) %}
        <div class="flash flash-{{ message[0] }} fade-in">
            <i class="fas fa-{{ 'check-circle' if message[0] == 'success' else 'exclamation-triangle' }}"></i>
            {{ message[1] }}
        </div>
    {% endfor %}
    
    <div class="grid" style="grid-template-columns:1fr 1fr;gap:2rem">
        <div class="card">
            <h2><i class="fas fa-user-plus"></i> Create Account</h2>
            <form method="post" action="{{ url_for('register') }}">
                <div class="form-group">
                    <label class="form-label">Email Address</label>
                    <input type="email" name="email" class="form-input" required>
                </div>
                <div class="form-group">
                    <label class="form-label">Display Name</label>
                    <input type="text" name="display_name" class="form-input" placeholder="Your name">
                </div>
                <div class="form-group">
                    <label class="form-label">Password (min 8 chars, 1 number)</label>
                    <input type="password" name="password" class="form-input" required minlength="8">
                </div>
                <button type="submit" class="btn btn-primary" style="width:100%">
                    <i class="fas fa-user-plus"></i> Create Account
                </button>
            </form>
        </div>
        
        <div class="card">
            <h2><i class="fas fa-sign-in-alt"></i> Login</h2>
            <form method="post" action="{{ url_for('login') }}">
                <div class="form-group">
                    <label class="form-label">Email Address</label>
                    <input type="email" name="email" class="form-input" required>
                </div>
                <div class="form-group">
                    <label class="form-label">Password</label>
                    <input type="password" name="password" class="form-input" required>
                </div>
                <button type="submit" class="btn btn-primary" style="width:100%">
                    <i class="fas fa-sign-in-alt"></i> Sign In
                </button>
            </form>
            
            <div style="margin-top:1rem;padding-top:1rem;border-top:1px solid var(--border)">
                <p style="color:var(--text-muted);font-size:0.875rem;text-align:center">
                    First time? Create an account to get started.<br>
                    All data is automatically synced and backed up.
                </p>
            </div>
        </div>
    </div>
    
    <div class="card" style="margin-top:2rem">
        <h3><i class="fas fa-star"></i> Features</h3>
        <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem;margin-top:1rem">
            <div style="text-align:center">
                <i class="fas fa-users" style="font-size:2rem;color:var(--primary);margin-bottom:0.5rem"></i>
                <h4>Farmer Management</h4>
                <p style="color:var(--text-muted);font-size:0.875rem">Organize farmer contacts, locations, and specialties</p>
            </div>
            <div style="text-align:center">
                <i class="fas fa-clipboard-list" style="font-size:2rem;color:var(--primary);margin-bottom:0.5rem"></i>
                <h4>Order Tracking</h4>
                <p style="color:var(--text-muted);font-size:0.875rem">Track orders from creation to completion</p>
            </div>
            <div style="text-align:center">
                <i class="fas fa-chart-line" style="font-size:2rem;color:var(--primary);margin-bottom:0.5rem"></i>
                <h4>Analytics</h4>
                <p style="color:var(--text-muted);font-size:0.875rem">Monitor performance and revenue metrics</p>
            </div>
            <div style="text-align:center">
                <i class="fas fa-download" style="font-size:2rem;color:var(--primary);margin-bottom:0.5rem"></i>
                <h4>Export Data</h4>
                <p style="color:var(--text-muted);font-size:0.875rem">Download reports in CSV format</p>
            </div>
        </div>
    </div>
</div>
""" + BASE_FOOT

# ---------------- Enhanced Routes ----------------

@app.route("/", methods=["GET"])
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template_string(INDEX_HTML)

@app.route("/register", methods=["POST"])
def register():
    email = sanitize_input(request.form.get("email", "").strip().lower(), 100)
    password = request.form.get("password", "")
    display_name = sanitize_input(request.form.get("display_name", "").strip(), 50)

    # Enhanced validation
    if not valid_email(email):
        flash("Please provide a valid email address.", "error")
        return redirect(url_for("index"))
    
    if not valid_password(password):
        flash("Password must be at least 8 characters and contain at least one number.", "error")
        return redirect(url_for("index"))

    # Check if user exists
    existing_user = run_query_fetchone("SELECT id FROM users WHERE email = %s", (email,))
    if existing_user is None:
        flash("Database error. Please try again.", "error")
        return redirect(url_for("index"))
    
    if existing_user:
        flash("Email already registered. Please login instead.", "error")
        return redirect(url_for("index"))

    # Create user
    pw_hash = generate_password_hash(password)
    user_id = run_query_commit(
        "INSERT INTO users (email, password_hash, display_name) VALUES (%s, %s, %s) RETURNING id",
        (email, pw_hash, display_name or None),
        return_id=True
    )
    
    if not user_id:
        flash("Failed to create account. Please try again.", "error")
        return redirect(url_for("index"))

    logger.info(f"New user registered: {email}")
    flash("Account created successfully! Please log in.", "success")
    return redirect(url_for("index"))

@app.route("/login", methods=["POST"])
def login():
    email = sanitize_input(request.form.get("email", "").strip().lower(), 100)
    password = request.form.get("password", "")
    
    if not email or not password:
        flash("Please provide both email and password.", "error")
        return redirect(url_for("index"))

    user = run_query_fetchone(
        "SELECT id, password_hash, display_name, is_active FROM users WHERE email = %s",
        (email,)
    )
    
    if not user:
        flash("No account found with that email address.", "error")
        return redirect(url_for("index"))
    
    if not user.get('is_active', True):
        flash("Account is deactivated. Please contact support.", "error")
        return redirect(url_for("index"))

    if not check_password_hash(user['password_hash'], password):
        flash("Incorrect password.", "error")
        return redirect(url_for("index"))

    # Set session
    session["user_id"] = user['id']
    session["user_email"] = email
    session["user_name"] = user.get('display_name') or email
    
    # Update last login
    run_query_commit("UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = %s", (user['id'],))
    
    logger.info(f"User logged in: {email}")
    flash("Welcome back!", "success")
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    email = session.get("user_email", "unknown")
    session.clear()
    logger.info(f"User logged out: {email}")
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    # Get analytics data
    stats = {}
    stats['total_farmers'] = run_query_fetchone("SELECT COUNT(*) as count FROM farmers WHERE is_active = TRUE")
    stats['total_farmers'] = stats['total_farmers']['count'] if stats['total_farmers'] else 0
    
    stats['total_orders'] = run_query_fetchone("SELECT COUNT(*) as count FROM orders")
    stats['total_orders'] = stats['total_orders']['count'] if stats['total_orders'] else 0
    
    stats['pending_orders'] = run_query_fetchone("SELECT COUNT(*) as count FROM orders WHERE status IN ('pending', 'ongoing')")
    stats['pending_orders'] = stats['pending_orders']['count'] if stats['pending_orders'] else 0
    
    revenue = run_query_fetchone("SELECT COALESCE(SUM(total_amount), 0) as total FROM orders WHERE status = 'completed'")
    stats['total_revenue'] = float(revenue['total']) if revenue and revenue['total'] else 0.0

    # Get farmers and orders
    farmers = run_query_fetchall("SELECT * FROM farmers WHERE is_active = TRUE ORDER BY name")
    orders = run_query_fetchall("SELECT * FROM orders ORDER BY created_at DESC LIMIT 20")
    
    if farmers is None:
        farmers = []
    if orders is None:
        orders = []

    user = {
        "id": session.get("user_id"),
        "email": session.get("user_email"),
        "display_name": session.get("user_name"),
        "avatar": None
    }
    
    return render_template_string(
        ENHANCED_DASH_HTML,
        user=user,
        farmers=farmers,
        orders=orders,
        stats=stats,
        today=date.today().isoformat()
    )

@app.route("/add_farmer", methods=["POST"])
@login_required
def add_farmer():
    name = sanitize_input(request.form.get("name", "").strip(), 100)
    location = sanitize_input(request.form.get("location", "").strip(), 100)
    contact = sanitize_input(request.form.get("contact", "").strip(), 20)
    email = sanitize_input(request.form.get("email", "").strip().lower(), 100)
    products = sanitize_input(request.form.get("products", "").strip(), 200)
    
    if not name:
        flash("Farmer name is required.", "error")
        return redirect(url_for("dashboard"))
    
    # Validate email if provided
    if email and not valid_email(email):
        flash("Please provide a valid email address.", "error")
        return redirect(url_for("dashboard"))
    
    # Validate contact if provided
    if contact and not valid_phone(contact):
        flash("Please provide a valid contact number.", "error")
        return redirect(url_for("dashboard"))
    
    success = run_query_commit(
        """INSERT INTO farmers (name, location, contact, email, products, updated_at) 
           VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)""",
        (name, location or None, contact or None, email or None, products or None)
    )
    
    if success:
        logger.info(f"New farmer added: {name}")
        flash(f"Farmer '{name}' added successfully!", "success")
    else:
        flash("Failed to add farmer. Please try again.", "error")
    
    return redirect(url_for("dashboard"))

@app.route("/add_order", methods=["POST"])
@login_required
def add_order():
    farmer_id = request.form.get("farmer_id")
    items = sanitize_input(request.form.get("items", "").strip(), 200)
    quantity = sanitize_input(request.form.get("quantity", "").strip(), 50)
    unit_price = request.form.get("unit_price")
    priority = request.form.get("priority", "medium")
    order_date = request.form.get("order_date") or date.today().isoformat()
    delivery_date = request.form.get("delivery_date")
    notes = sanitize_input(request.form.get("notes", "").strip(), 500)
    
    if not farmer_id or not items:
        flash("Farmer and items are required.", "error")
        return redirect(url_for("dashboard"))
    
    if priority not in ['low', 'medium', 'high']:
        priority = 'medium'
    
    # Calculate total amount if unit price provided
    total_amount = None
    if unit_price:
        try:
            unit_price = float(unit_price)
            # Simple calculation - could be enhanced with proper quantity parsing
            total_amount = unit_price
        except (ValueError, TypeError):
            unit_price = None
    
    # Get farmer name
    farmer = run_query_fetchone("SELECT name FROM farmers WHERE id = %s", (farmer_id,))
    if not farmer:
        flash("Selected farmer not found.", "error")
        return redirect(url_for("dashboard"))
    
    farmer_name = farmer['name']
    
    success = run_query_commit(
        """INSERT INTO orders (farmer_id, farmer_name, items, quantity, unit_price, 
           total_amount, status, priority, order_date, delivery_date, notes, updated_at) 
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)""",
        (farmer_id, farmer_name, items, quantity or None, unit_price, 
         total_amount, 'pending', priority, order_date, delivery_date or None, notes or None)
    )
    
    if success:
        logger.info(f"New order created for farmer {farmer_name}: {items}")
        flash("Order created successfully!", "success")
    else:
        flash("Failed to create order. Please try again.", "error")
    
    return redirect(url_for("dashboard"))

@app.route("/update_order_status/<int:order_id>", methods=["POST"])
@login_required
def update_order_status(order_id):
    new_status = request.form.get("status", "completed")
    
    if new_status not in ['pending', 'ongoing', 'completed', 'cancelled']:
        flash("Invalid status.", "error")
        return redirect(url_for("dashboard"))
    
    success = run_query_commit(
        "UPDATE orders SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (new_status, order_id)
    )
    
    if success:
        flash(f"Order #{order_id} marked as {new_status}.", "success")
    else:
        flash("Failed to update order status.", "error")
    
    return redirect(url_for("dashboard"))

@app.route("/export_data")
@login_required
def export_data():
    """Export all data as CSV files in a simple format"""
    try:
        # Get all data
        farmers = run_query_fetchall("SELECT * FROM farmers ORDER BY name")
        orders = run_query_fetchall("SELECT * FROM orders ORDER BY created_at DESC")
        
        if farmers is None or orders is None:
            flash("Error accessing database for export.", "error")
            return redirect(url_for("dashboard"))
        
        # Create CSV content
        output = StringIO()
        output.write("=== FARMERS ===\n")
        
        if farmers:
            # Write farmers header
            farmer_keys = farmers[0].keys()
            output.write(",".join(farmer_keys) + "\n")
            
            # Write farmers data
            for farmer in farmers:
                row = [str(farmer.get(key, "") or "") for key in farmer_keys]
                output.write(",".join(f'"{val}"' for val in row) + "\n")
        
        output.write("\n=== ORDERS ===\n")
        
        if orders:
            # Write orders header
            order_keys = orders[0].keys()
            output.write(",".join(order_keys) + "\n")
            
            # Write orders data
            for order in orders:
                row = [str(order.get(key, "") or "") for key in order_keys]
                output.write(",".join(f'"{val}"' for val in row) + "\n")
        
        # Create response
        response = make_response(output.getvalue())
        response.headers['Content-Type'] = 'text/csv'
        response.headers['Content-Disposition'] = f'attachment; filename=farmsync_export_{date.today().isoformat()}.csv'
        
        logger.info(f"Data exported by user {session.get('user_email')}")
        return response
        
    except Exception as e:
        logger.error(f"Export error: {e}")
        flash("Error generating export file.", "error")
        return redirect(url_for("dashboard"))

# Enhanced API endpoints
@app.route("/api/stats")
@login_required
def api_stats():
    """Get dashboard statistics"""
    try:
        stats = {}
        
        # Get counts
        farmer_count = run_query_fetchone("SELECT COUNT(*) as count FROM farmers WHERE is_active = TRUE")
        order_count = run_query_fetchone("SELECT COUNT(*) as count FROM orders")
        pending_count = run_query_fetchone("SELECT COUNT(*) as count FROM orders WHERE status IN ('pending', 'ongoing')")
        
        stats['farmers'] = farmer_count['count'] if farmer_count else 0
        stats['orders'] = order_count['count'] if order_count else 0
        stats['pending'] = pending_count['count'] if pending_count else 0
        
        # Get revenue
        revenue = run_query_fetchone("SELECT COALESCE(SUM(total_amount), 0) as total FROM orders WHERE status = 'completed'")
        stats['revenue'] = float(revenue['total']) if revenue and revenue['total'] else 0.0
        
        # Get recent activity (orders in last 7 days)
        recent = run_query_fetchone(
            "SELECT COUNT(*) as count FROM orders WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'"
        )
        stats['recent_orders'] = recent['count'] if recent else 0
        
        return jsonify(stats)
    except Exception as e:
        logger.error(f"Stats API error: {e}")
        return jsonify({"error": "Failed to fetch statistics"}), 500

@app.route("/api/farmers")
@login_required
def api_farmers():
    """Get farmers with optional filtering"""
    try:
        search = request.args.get("search", "").strip()
        location = request.args.get("location", "").strip()
        
        query = "SELECT * FROM farmers WHERE is_active = TRUE"
        params = []
        
        if search:
            query += " AND (name ILIKE %s OR products ILIKE %s)"
            search_pattern = f"%{search}%"
            params.extend([search_pattern, search_pattern])
        
        if location:
            query += " AND location ILIKE %s"
            params.append(f"%{location}%")
        
        query += " ORDER BY name"
        
        farmers = run_query_fetchall(query, params)
        return jsonify(farmers or [])
    except Exception as e:
        logger.error(f"Farmers API error: {e}")
        return jsonify({"error": "Failed to fetch farmers"}), 500

@app.route("/api/orders")
@login_required
def api_orders():
    """Get orders with filtering and pagination"""
    try:
        status = request.args.get("status", "").strip()
        farmer_id = request.args.get("farmer_id", "").strip()
        limit = min(int(request.args.get("limit", 50)), 100)  # Max 100 items
        offset = int(request.args.get("offset", 0))
        
        query = "SELECT * FROM orders WHERE 1=1"
        params = []
        
        if status:
            query += " AND status = %s"
            params.append(status)
        
        if farmer_id:
            query += " AND farmer_id = %s"
            params.append(farmer_id)
        
        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        
        orders = run_query_fetchall(query, params)
        return jsonify(orders or [])
    except Exception as e:
        logger.error(f"Orders API error: {e}")
        return jsonify({"error": "Failed to fetch orders"}), 500

@app.route("/health")
def health():
    """Health check endpoint"""
    try:
        # Test database connection
        test_query = run_query_fetchone("SELECT 1 as test")
        db_status = "ok" if test_query else "error"
        
        return jsonify({
            "status": "ok",
            "database": db_status,
            "timestamp": datetime.utcnow().isoformat(),
            "version": "2.0"
        })
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return jsonify({
            "status": "error",
            "database": "error",
            "timestamp": datetime.utcnow().isoformat()
        }), 500

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return render_template_string("""
    <div style="text-align:center;padding:4rem">
        <h1>404 - Page Not Found</h1>
        <p><a href="{{ url_for('index') }}">← Back to Home</a></p>
    </div>
    """), 404

@app.errorhandler(500)
def server_error(error):
    logger.error(f"Server error: {error}")
    return render_template_string("""
    <div style="text-align:center;padding:4rem">
        <h1>500 - Server Error</h1>
        <p>Something went wrong. Please try again later.</p>
        <p><a href="{{ url_for('index') }}">← Back to Home</a></p>
    </div>
    """), 500

# ---------------- Run Application ----------------
if __name__ == "__main__":
    logger.info(f"Starting FarmSync application on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
