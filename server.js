const express = require("express");
const bodyParser = require("body-parser");
const pg = require("pg");
const bcrypt = require("bcrypt");
const cors = require("cors");
const path = require("path");
const session = require('express-session');
const pgSession = require('connect-pg-simple')(session);

const app = express();
const port = process.env.PORT || 3000;

// --- PostgreSQL Pool (NeonDB) ---
// The connection string is now hardcoded as per your request.
const pool = new pg.Pool({
  connectionString: "postgresql://neondb_owner:npg_jgROvpDtrm03@ep-hidden-truth-aev5l7a7-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
  ssl: {
    rejectUnauthorized: false, // Required for NeonDB SSL connections
  },
});

// --- Middleware ---
app.use(cors());
app.use(bodyParser.json());
app.use(bodyParser.urlencoded({ extended: true })); // For parsing form data

// Corrected the path to the 'views' directory to fix the "lookup view" error.
app.set('views', path.join(__dirname, 'views'));
app.set('view engine', 'ejs'); // Set EJS as the templating engine

app.use(express.static(path.join(__dirname, 'public'))); // Serve static files (if any)

// --- Session Middleware ---
// The session secret is now hardcoded as per your request.
app.use(
  session({
    store: new pgSession({
      pool: pool,
      tableName: 'session',
    }),
    secret: 'super_secret_key_that_should_be_in_env_file', // Hardcoded secret
    resave: false,
    saveUninitialized: false,
    cookie: { maxAge: 30 * 24 * 60 * 60 * 1000 }, // 30 days
  })
);

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

  await pool.query(`
    CREATE TABLE IF NOT EXISTS "session" (
      "sid" varchar NOT NULL COLLATE "default" PRIMARY KEY,
      "sess" json NOT NULL,
      "expire" timestamp(6) NOT NULL
    );
    CREATE INDEX "IDX_session_expire" ON "session" ("expire");
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

// --- Frontend Routes ---
app.get("/", (req, res) => {
  if (req.session.user) {
    if (req.session.user.role === 'farm') {
      return res.redirect("/farm/dashboard");
    }
    return res.redirect("/user/dashboard");
  }
  res.render("login.ejs", { error: null });
});

app.get("/register", (req, res) => {
  res.render("register.ejs", { error: null });
});

app.get("/login", (req, res) => {
  const error = req.query.error;
  res.render("login.ejs", { error: error ? 'Invalid credentials or email already in use.' : null });
});

app.get("/user/dashboard", async (req, res) => {
  if (!req.session.user || req.session.user.role !== 'user') {
    return res.redirect("/login?error=Unauthorized access");
  }
  try {
    const productsResult = await pool.query("SELECT * FROM products ORDER BY id");
    res.render("user_dashboard.ejs", { user: req.session.user, products: productsResult.rows });
  } catch (err) {
    console.error(err);
    res.status(500).send("Internal server error");
  }
});

app.get("/farm/dashboard", async (req, res) => {
  if (!req.session.user || req.session.user.role !== 'farm') {
    return res.redirect("/login?error=Unauthorized access");
  }
  try {
    const usersResult = await pool.query("SELECT id, name, email, role FROM users ORDER BY id");
    res.render("farm_dashboard.ejs", { user: req.session.user, users: usersResult.rows });
  } catch (err) {
    console.error(err);
    res.status(500).send("Internal server error");
  }
});

app.post("/logout", (req, res) => {
  req.session.destroy((err) => {
    if (err) {
      return res.status(500).send("Could not log out.");
    }
    res.redirect("/login");
  });
});

// --- User Registration ---
app.post("/register", async (req, res) => {
  const { name, email, password, role } = req.body;
  if (!name || !email || !password) {
    return res.redirect("/register?error=Name, email, and password are required.");
  }
  try {
    const hashedPassword = await bcrypt.hash(password, 10);
    const result = await pool.query(
      "INSERT INTO users (name, email, password_hash, role) VALUES ($1, $2, $3, $4) RETURNING id, name, email, role",
      [name, email, hashedPassword, role || 'user'] // default to 'user'
    );
    req.session.user = result.rows[0];
    res.redirect("/user/dashboard");
  } catch (err) {
    if (err.code === "23505") {
      return res.redirect("/register?error=Email already registered");
    } else {
      console.error(err);
      res.redirect("/register?error=Internal server error");
    }
  }
});

// --- User Login ---
app.post("/login", async (req, res) => {
  const { email, password } = req.body;
  if (!email || !password) {
    return res.redirect("/login?error=Email and password required");
  }
  try {
    const userResult = await pool.query("SELECT * FROM users WHERE email=$1", [email]);
    if (userResult.rows.length === 0) {
      return res.redirect("/login?error=Invalid credentials");
    }
    const user = userResult.rows[0];
    const isValid = await bcrypt.compare(password, user.password_hash);
    if (!isValid) {
      return res.redirect("/login?error=Invalid credentials");
    }

    req.session.user = { id: user.id, name: user.name, email: user.email, role: user.role };

    if (user.role === 'farm') {
      res.redirect("/farm/dashboard");
    } else {
      res.redirect("/user/dashboard");
    }
  } catch (err) {
    console.error(err);
    res.redirect("/login?error=Internal server error");
  }
});

// --- Add New Product (Farm-only) ---
app.post("/add-product", async (req, res) => {
  if (!req.session.user || req.session.user.role !== 'farm') {
    return res.status(403).send("Unauthorized");
  }
  const { name, price, image_url } = req.body;
  if (!name || !price || !image_url) {
    return res.status(400).send("Name, price, and image URL are required.");
  }
  try {
    await pool.query(
      "INSERT INTO products (name, price, image_url) VALUES ($1, $2, $3)",
      [name, price, image_url]
    );
    res.redirect("/farm/dashboard");
  } catch (err) {
    console.error(err);
    res.status(500).send("Internal server error");
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
