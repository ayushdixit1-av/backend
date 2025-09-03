const express = require("express");
const bodyParser = require("body-parser");
const pg = require("pg");
const bcrypt = require("bcrypt");
const cors = require("cors");
const fetch = require("node-fetch"); // required for calling Google API

const app = express();
const port = process.env.PORT || 3000;

// --- PostgreSQL Pool (NeonDB) ---
const pool = new pg.Pool({
  connectionString:
    "postgresql://neondb_owner:npg_jgROvpDtrm03@ep-hidden-truth-aev5l7a7-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
  ssl: {
    rejectUnauthorized: false, // Important for NeonDB SSL connections
  },
});

// --- Middleware ---
app.use(cors());
app.use(bodyParser.json());

// --- Test DB Connection ---
async function testDBConnection() {
  try {
    await pool.query("SELECT NOW()");
    console.log("✅ Successfully connected to NeonDB PostgreSQL");
  } catch (err) {
    console.error("❌ Failed to connect to NeonDB PostgreSQL:", err);
    process.exit(1);
  }
}

// --- Create Tables ---
async function initializeDB() {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS users (
      id SERIAL PRIMARY KEY,
      name VARCHAR(100) NOT NULL,
      email VARCHAR(255) UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      role VARCHAR(50) DEFAULT 'user',
      created_at TIMESTAMP DEFAULT NOW()
    );
  `);

  await pool.query(`
    CREATE TABLE IF NOT EXISTS products (
      id SERIAL PRIMARY KEY,
      name VARCHAR(255) NOT NULL,
      price VARCHAR(50) NOT NULL,
      image_url TEXT NOT NULL,
      created_at TIMESTAMP DEFAULT NOW()
    );
  `);
}

// --- Seed Example Products ---
async function seedProducts() {
  const products = [
    {
      name: "Organic Apples",
      price: "$2.50/lb",
      image_url: "https://placehold.co/400x300/4CAF50/ffffff?text=Apples",
    },
    {
      name: "Fresh Carrots",
      price: "$1.80/lb",
      image_url: "https://placehold.co/400x300/FF5722/ffffff?text=Carrots",
    },
    {
      name: "Farm Eggs",
      price: "$4.00/dozen",
      image_url: "https://placehold.co/400x300/FBC02D/ffffff?text=Eggs",
    },
    {
      name: "Local Honey",
      price: "$8.00/jar",
      image_url: "https://placehold.co/400x300/FF9800/ffffff?text=Honey",
    },
    {
      name: "Red Potatoes",
      price: "$2.00/lb",
      image_url: "https://placehold.co/400x300/BDBDBD/ffffff?text=Potatoes",
    },
    {
      name: "Heirloom Tomatoes",
      price: "$3.50/lb",
      image_url: "https://placehold.co/400x300/F44336/ffffff?text=Tomatoes",
    },
  ];

  for (const product of products) {
    await pool.query(
      `
      INSERT INTO products (name, price, image_url)
      VALUES ($1, $2, $3)
      ON CONFLICT DO NOTHING
    `,
      [product.name, product.price, product.image_url]
    );
  }
}

// --- User Registration ---
app.post("/api/register", async (req, res) => {
  const { name, email, password } = req.body;
  if (!name || !email || !password)
    return res
      .status(400)
      .json({ error: "Name, email, and password are required." });
  try {
    const hashedPassword = await bcrypt.hash(password, 10);
    const result = await pool.query(
      "INSERT INTO users (name, email, password_hash) VALUES ($1, $2, $3) RETURNING id, name, email, role",
      [name, email, hashedPassword]
    );
    res
      .status(201)
      .json({ message: "User registered successfully", user: result.rows[0] });
  } catch (err) {
    if (err.code === "23505") {
      res.status(409).json({ error: "Email already registered" });
    } else {
      console.error(err);
      res.status(500).json({ error: "Internal server error" });
    }
  }
});

// --- User Login ---
app.post("/api/login", async (req, res) => {
  const { email, password } = req.body;
  if (!email || !password)
    return res.status(400).json({ error: "Email and password required" });
  try {
    const userResult = await pool.query(
      "SELECT * FROM users WHERE email=$1",
      [email]
    );
    if (userResult.rows.length === 0)
      return res.status(401).json({ error: "Invalid credentials" });
    const user = userResult.rows[0];
    const isValid = await bcrypt.compare(password, user.password_hash);
    if (!isValid) return res.status(401).json({ error: "Invalid credentials" });
    res.json({
      message: "Login successful",
      user: { id: user.id, name: user.name, email: user.email, role: user.role },
    });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "Internal server error" });
  }
});

// --- Get All Users ---
app.get("/api/users", async (req, res) => {
  try {
    const result = await pool.query(
      "SELECT id, name, email FROM users ORDER BY id"
    );
    res.json(result.rows);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "Internal server error" });
  }
});

// --- Get All Products ---
app.get("/api/products", async (req, res) => {
  try {
    const result = await pool.query(
      "SELECT id, name, price, image_url FROM products ORDER BY id"
    );
    res.json(result.rows);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "Internal server error" });
  }
});

// --- Google Cloud Text-to-Speech Proxy Endpoint ---
app.post("/api/tts", async (req, res) => {
  const { text, lang } = req.body;

  if (!text || !lang) {
    return res.status(400).json({ error: "Text and language are required." });
  }

  try {
    const response = await fetch(
      "https://texttospeech.googleapis.com/v1/text:synthesize?key=AIzaSyCJKiAz4YoRSBuVvyZa-De5iuK4ysAm1ao",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          input: { text },
          voice: { languageCode: lang, ssmlGender: "NEUTRAL" },
          audioConfig: { audioEncoding: "MP3" },
        }),
      }
    );

    const data = await response.json();
    if (data.audioContent) {
      res.json({ audioContent: data.audioContent });
    } else {
      res.status(500).json({ error: "TTS API failed", details: data });
    }
  } catch (err) {
    console.error("TTS Proxy error:", err);
    res.status(500).json({ error: "Internal server error" });
  }
});

// --- Start Server ---
async function startServer() {
  await testDBConnection();
  await initializeDB();
  await seedProducts();
  app.listen(port, () => {
    console.log(`🚀 Server running on http://localhost:${port}`);
  });
}

startServer();
