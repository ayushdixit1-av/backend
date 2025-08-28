/**
 * Main server file for the Node.js backend.
 * This server uses Express to handle API requests and 'pg' to connect to a PostgreSQL database.
 * The database connection URL is pulled from the environment variables for security.
 */

// Load environment variables in development mode only
if (process.env.NODE_ENV !== 'production') {
  require('dotenv').config();
}

// Import necessary modules
const express = require('express');
const { Pool } = require('pg');
const cors = require('cors');

// Initialize Express app
const app = express();
const port = process.env.PORT || 3000;

// Middleware setup
app.use(cors()); // Enable CORS for cross-origin requests
app.use(express.json()); // Enable JSON body parsing

// --- Database Connection Configuration ---
// The connection string is read from the environment variable.
// This is a crucial step for production deployment on platforms like Railway.
const neonDatabaseUrl = process.env.NEON_DATABASE_URL;

if (!neonDatabaseUrl) {
    console.error('NEON_DATABASE_URL is not set. Please set this environment variable.');
    process.exit(1); // Exit if the database URL is not configured
}

const pool = new Pool({
    connectionString: neonDatabaseUrl,
});

/**
 * Test database connection.
 * This is a simple function to check if the database is reachable.
 */
async function testDbConnection() {
    try {
        await pool.query('SELECT 1 + 1 AS solution');
        console.log('Successfully connected to the PostgreSQL database!');
    } catch (err) {
        console.error('Failed to connect to the database:', err.message);
        process.exit(1);
    }
}

testDbConnection();

// --- API Endpoints ---

/**
 * Health check endpoint.
 * Responds with a simple message to confirm the server is running.
 */
app.get('/', (req, res) => {
    res.send('Backend server is running successfully!');
});

/**
 * Example endpoint to fetch data from the database.
 * This endpoint queries a 'users' table and returns the results.
 * You can modify this to fit your portfolio's data needs.
 */
app.get('/api/users', async (req, res) => {
    try {
        const result = await pool.query('SELECT * FROM users');
        res.json(result.rows);
    } catch (err) {
        console.error('Error executing query:', err.stack);
        res.status(500).json({ error: 'Internal server error' });
    }
});

// Start the server
app.listen(port, () => {
    console.log(`Server is running on http://localhost:${port}`);
});

