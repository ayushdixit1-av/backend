/**
 * @file server.js
 * @description This file sets up a Node.js server with Express to handle API requests
 * and connect to a Neon PostgreSQL database.
 */

// Import necessary libraries
const express = require('express');
const cors = require('cors');
const { Pool } = require('pg');
const dotenv = require('dotenv');

// Load environment variables from the .env file
dotenv.config();

// Initialize the Express application
const app = express();

// Set the port, using the environment variable or defaulting to 5000
const port = process.env.PORT || 5000;

// Middleware setup
// Use CORS to allow requests from your React frontend
app.use(cors());
// Use express.json() to parse incoming JSON requests
app.use(express.json());

// Check if the Neon database URL is set
if (!process.env.NEON_DATABASE_URL) {
  console.error('Error: NEON_DATABASE_URL not found in .env file.');
  process.exit(1);
}

// Create a new PostgreSQL client pool using the Neon database URL
const pool = new Pool({
  connectionString: process.env.NEON_DATABASE_URL,
  ssl: {
    // Required for secure connections to Neon, which enforces SSL
    rejectUnauthorized: false
  }
});

/**
 * @route GET /api/test
 * @description A simple test route to check if the server is running and the database connection is working.
 * This query fetches the current database timestamp.
 */
app.get('/api/test', async (req, res) => {
  try {
    const result = await pool.query('SELECT NOW()');
    res.status(200).json({
      message: 'Server is running and connected to the database!',
      databaseTime: result.rows[0].now,
    });
  } catch (err) {
    console.error('Database connection error:', err);
    res.status(500).json({ error: 'Database connection failed.' });
  }
});

/**
 * @route GET /api/projects
 * @description Fetches a list of projects from the database.
 * This is a placeholder example. In a real-world scenario, you would
 * have a 'projects' table and query it.
 *
 * Example of a real database query:
 * `const result = await pool.query('SELECT * FROM projects ORDER BY id');`
 */
app.get('/api/projects', async (req, res) => {
  try {
    // For this example, we'll return hardcoded data.
    // In a production app, you would fetch this from your database.
    const projects = [
      { id: 1, title: 'Portfolio Website', description: 'Built with React and Tailwind CSS.', imageUrl: 'https://placehold.co/400x300/a855f7/ffffff?text=Project+1', link: '#' },
      { id: 2, title: 'Data Visualization Dashboard', description: 'Interactive charts and graphs.', imageUrl: 'https://placehold.co/400x300/6366f1/ffffff?text=Project+2', link: '#' },
    ];
    res.status(200).json(projects);
  } catch (err) {
    console.error('Error fetching projects:', err);
    res.status(500).json({ error: 'Failed to fetch projects.' });
  }
});

/**
 * @route POST /api/contact
 * @description Handles form submissions. This is a placeholder for a contact form.
 * It demonstrates how you would insert data into a 'messages' table.
 */
app.post('/api/contact', async (req, res) => {
  const { name, email, message } = req.body;
  if (!name || !email || !message) {
    return res.status(400).json({ error: 'All fields are required.' });
  }

  try {
    // Example of inserting data into a database table
    // await pool.query(
    //   'INSERT INTO messages (name, email, message) VALUES ($1, $2, $3) RETURNING *',
    //   [name, email, message]
    // );
    
    // For this example, we'll just log the data received.
    console.log('Received new contact message:', { name, email, message });
    
    res.status(201).json({ message: 'Message received successfully!' });
  } catch (err) {
    console.error('Error saving contact message:', err);
    res.status(500).json({ error: 'Failed to send message.' });
  }
});

// Start the server and listen on the specified port
app.listen(port, () => {
  console.log(`Server is listening on http://localhost:${port}`);
});

