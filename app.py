from flask import Flask, jsonify, request
import psycopg2
from psycopg2 import pool
import os

app = Flask(__name__)
port = int(os.environ.get('PORT', 3000))

# --- IMPORTANT: Paste your Neon DB URL here ---
# For security reasons, this is not recommended for production.
# Use environment variables instead for a production environment.
neon_db_url = "postgresql://neondb_owner:npg_jgROvpDtrm03@ep-hidden-truth-aev5l7a7-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

# Set up the PostgreSQL connection pool
# The minconn and maxconn are important for managing database connections efficiently.
try:
    postgreSQL_pool = psycopg2.pool.SimpleConnectionPool(
        minconn=1,
        maxconn=20,
        dsn=neon_db_url
    )
    print("Successfully created PostgreSQL connection pool.")
except (Exception, psycopg2.Error) as error:
    print("Failed to create PostgreSQL connection pool:", error)
    
# Function to get a connection from the pool and run a query
def run_query(query, params=None):
    conn = None
    try:
        conn = postgreSQL_pool.getconn()
        cur = conn.cursor()
        if params:
            cur.execute(query, params)
        else:
            cur.execute(query)
        result = cur.fetchall()
        cur.close()
        return result
    except (Exception, psycopg2.Error) as error:
        print("Error while connecting to PostgreSQL or executing query:", error)
        return None
    finally:
        if conn:
            postgreSQL_pool.putconn(conn)

@app.route('/api/farmers', methods=['GET'])
def get_farmers():
    """API endpoint to get all farmers from the database."""
    try:
        # Assuming your table is named 'farmers'
        farmers = run_query('SELECT * FROM farmers')
        
        # Convert the list of tuples to a list of dictionaries for JSON output
        if farmers:
            columns = [desc[0] for desc in psycopg2.cursor.description]
            farmers_list = [dict(zip(columns, row)) for row in farmers]
            return jsonify(farmers_list)
        return jsonify([])
    except Exception as e:
        print(f"Error fetching farmers: {e}")
        return jsonify({'error': 'Internal Server Error'}), 500

@app.route('/api/orders/ongoing', methods=['GET'])
def get_ongoing_orders():
    """API endpoint to get all ongoing orders."""
    try:
        # Assuming your table is named 'orders'
        ongoing_orders = run_query("SELECT * FROM orders WHERE status = 'ongoing'")
        
        # Convert to JSON format
        if ongoing_orders:
            columns = [desc[0] for desc in psycopg2.cursor.description]
            ongoing_orders_list = [dict(zip(columns, row)) for row in ongoing_orders]
            return jsonify(ongoing_orders_list)
        return jsonify([])
    except Exception as e:
        print(f"Error fetching ongoing orders: {e}")
        return jsonify({'error': 'Internal Server Error'}), 500

@app.route('/api/orders/previous', methods=['GET'])
def get_previous_orders():
    """API endpoint to get all previous (completed) orders."""
    try:
        # Assuming your table is named 'orders'
        previous_orders = run_query("SELECT * FROM orders WHERE status = 'completed'")
        
        # Convert to JSON format
        if previous_orders:
            columns = [desc[0] for desc in psycopg2.cursor.description]
            previous_orders_list = [dict(zip(columns, row)) for row in previous_orders]
            return jsonify(previous_orders_list)
        return jsonify([])
    except Exception as e:
        print(f"Error fetching previous orders: {e}")
        return jsonify({'error': 'Internal Server Error'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=port, debug=True)
