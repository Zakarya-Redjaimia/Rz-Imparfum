import os, io, datetime, urllib.parse
from functools import wraps
import psycopg2, psycopg2.extras, jwt, qrcode, qrcode.image.svg
from flask import Flask, request, jsonify, Response, g
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.lib.pagesizes import A5
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

DATABASE_URL = os.environ["DATABASE_URL"]      # Neon connection string (sslmode=require)
JWT_SECRET = os.environ["JWT_SECRET"]          # long random string, set in HF Space secrets
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "https://rz-imparfum.vercel.app")  # e.g. https://your-app.vercel.app

STORE_BRAND = "Luxury Impact Parfum RZ"
STORE_PHONE = "0542932846"
STORE_EMAIL = "contact@luxuryimpactparfum.com"
CURRENCIES = {"DA": {"rate": 220.0, "symbol": "DA"},
              "EUR": {"rate": 1.0, "symbol": "€"},
              "USD": {"rate": 1.08, "symbol": "$"}}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024
CORS(app, origins=FRONTEND_ORIGIN, allow_headers=["Authorization", "Content-Type"])

# ---------- DB ----------
def q(sql, args=(), fetch=None):
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            if fetch == "all": return cur.fetchall()
            if fetch == "one": return cur.fetchone()
    finally:
        conn.close()

def init_db():
    q("""CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL, name TEXT NOT NULL, role TEXT DEFAULT 'user')""")
    q("""CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, price DOUBLE PRECISION NOT NULL,
            currency TEXT NOT NULL, image BYTEA, image_type TEXT)""")
    q("""CREATE TABLE IF NOT EXISTS cart (
            id SERIAL PRIMARY KEY,
            user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            product_id INT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            quantity INT NOT NULL CHECK (quantity > 0), UNIQUE (user_id, product_id))""")
init_db()

# ---------- Auth ----------
def make_token(u):
    payload = {"id": u["id"], "role": u["role"],
               "exp": datetime.datetime.utcnow() + datetime.timedelta(days=7)}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def auth(admin=False):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            try:
                tok = request.headers.get("Authorization", "").replace("Bearer ", "")
                g.user = jwt.decode(tok, JWT_SECRET, algorithms=["HS256"])
            except Exception:
                return jsonify(error="Login required"), 401
            if admin and g.user["role"] != "admin":
                return jsonify(error="Admin only"), 403
            return f(*a, **kw)
        return wrapper
    return deco

def public_user(u):
    return {k: u[k] for k in ("id", "name", "username", "email", "role")}

@app.post("/api/register")
def register():
    d = request.get_json(force=True)
    for k in ("name", "username", "email", "password"):
        if not str(d.get(k, "")).strip():
            return jsonify(error=f"Missing {k}"), 400
    role = "user"
    if d.get("role") == "admin" and not q("SELECT id FROM users WHERE role='admin' LIMIT 1", fetch="one"):
        role = "admin"
    try:
        q("INSERT INTO users (name, username, email, password, role) VALUES (%s,%s,%s,%s,%s)",
          (d["name"].strip(), d["username"].strip(), d["email"].strip().lower(),
           generate_password_hash(d["password"].strip()), role))
    except psycopg2.errors.UniqueViolation:
        return jsonify(error="Username or email already used"), 409
    return jsonify(ok=True, role=role)

@app.post("/api/login")
def login():
    d = request.get_json(force=True)
    lid = str(d.get("login_id", "")).strip()
    u = q("SELECT * FROM users WHERE username=%s OR email=%s", (lid, lid.lower()), "one")
    if not u or not check_password_hash(u["password"], str(d.get("password", "")).strip()):
        return jsonify(error="Wrong credentials"), 401
    return jsonify(token=make_token(u), user=public_user(u))

# ---------- Products ----------
@app.get("/api/config")
def config():
    return jsonify(brand=STORE_BRAND, currencies=CURRENCIES)

@app.get("/api/products")
def products():
    return jsonify(q("SELECT id, name, price, currency FROM products ORDER BY id DESC", fetch="all"))

@app.get("/api/products/<int:pid>/image")
def product_image(pid):
    r = q("SELECT image, image_type FROM products WHERE id=%s", (pid,), "one")
    if not r or not r["image"]:
        return "", 404
    resp = Response(bytes(r["image"]), mimetype=r["image_type"] or "image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp

@app.get("/api/products/<int:pid>/qr")
def product_qr(pid):
    r = q("SELECT name FROM products WHERE id=%s", (pid,), "one")
    if not r: return "", 404
    link = "https://www.google.com/search?q=" + urllib.parse.quote_plus(f"{r['name']} perfume ingredients notes")
    payload = (f"Brand: {STORE_BRAND}\nPerfume: {r['name']}\nPhone: {STORE_PHONE}\n"
               f"Email: {STORE_EMAIL}\nIngredients Link: {link}")
    img = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage, box_size=4, border=1)
    buf = io.BytesIO(); img.save(buf)
    return Response(buf.getvalue(), mimetype="image/svg+xml")

def read_image():
    f = request.files.get("image")
    if f and f.filename:
        if f.mimetype not in ("image/jpeg", "image/png", "image/webp"):
            return None, None, "Only JPG/PNG/WEBP"
        return f.read(), f.mimetype, None
    return None, None, None

@app.post("/api/products")
@auth(admin=True)
def add_product():
    img, typ, err = read_image()
    if err or not img: return jsonify(error=err or "Image required"), 400
    f = request.form
    if f.get("currency") not in CURRENCIES: return jsonify(error="Bad currency"), 400
    q("INSERT INTO products (name, price, currency, image, image_type) VALUES (%s,%s,%s,%s,%s)",
      (f["name"].strip(), float(f["price"]), f["currency"], psycopg2.Binary(img), typ))
    return jsonify(ok=True)

@app.put("/api/products/<int:pid>")
@auth(admin=True)
def edit_product(pid):
    f = request.form
    img, typ, err = read_image()
    if err: return jsonify(error=err), 400
    q("UPDATE products SET name=%s, price=%s, currency=%s WHERE id=%s",
      (f["name"].strip(), float(f["price"]), f["currency"], pid))
    if img:
        q("UPDATE products SET image=%s, image_type=%s WHERE id=%s", (psycopg2.Binary(img), typ, pid))
    return jsonify(ok=True)

@app.delete("/api/products/<int:pid>")
@auth(admin=True)
def delete_product(pid):
    q("DELETE FROM products WHERE id=%s", (pid,))
    return jsonify(ok=True)

# ---------- Cart ----------
def cart_rows(uid):
    return q("""SELECT c.id, c.product_id, c.quantity, p.name, p.price, p.currency
                FROM cart c JOIN products p ON p.id=c.product_id WHERE c.user_id=%s ORDER BY c.id""",
             (uid,), "all")

@app.get("/api/cart")
@auth()
def get_cart():
    return jsonify(cart_rows(g.user["id"]))

@app.post("/api/cart")
@auth()
def add_cart():
    d = request.get_json(force=True)
    qty = max(1, int(d.get("quantity", 1)))
    q("""INSERT INTO cart (user_id, product_id, quantity) VALUES (%s,%s,%s)
         ON CONFLICT (user_id, product_id) DO UPDATE SET quantity = cart.quantity + EXCLUDED.quantity""",
      (g.user["id"], int(d["product_id"]), qty))
    return jsonify(ok=True)

@app.delete("/api/cart")
@auth()
def clear_cart():
    q("DELETE FROM cart WHERE user_id=%s", (g.user["id"],))
    return jsonify(ok=True)

def convert(amount, a, b):
    return amount / CURRENCIES[a]["rate"] * CURRENCIES[b]["rate"]

@app.get("/api/invoice")
@auth()
def invoice():
    cur = request.args.get("currency", "DA")
    if cur not in CURRENCIES: cur = "DA"
    rows = cart_rows(g.user["id"])
    if not rows: return jsonify(error="Cart is empty"), 400
    sym = cur  # use ISO code: built-in PDF fonts can't render all symbols
    data, total = [["Item", "Price", "Qty", "Total"]], 0.0
    for r in rows:
        p = convert(r["price"], r["currency"], cur); t = p * r["quantity"]; total += t
        data.append([r["name"], f"{p:,.2f} {sym}", str(r["quantity"]), f"{t:,.2f} {sym}"])
    data.append(["Grand Total", "", "", f"{total:,.2f} {sym}"])
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A5, leftMargin=20, rightMargin=20, topMargin=20, bottomMargin=20)
    st = getSampleStyleSheet()
    tbl = Table(data, colWidths=[140, 70, 30, 80])
    tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a202c")),
                             ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                             ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                             ("GRID", (0, 0), (-1, -1), .5, colors.HexColor("#cbd5e0")),
                             ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]))
    doc.build([Paragraph(f"<b>{STORE_BRAND}</b>", st["Heading1"]), Paragraph("OFFICIAL INVOICE", st["Normal"]),
               Spacer(1, 10), tbl])
    return Response(buf.getvalue(), mimetype="application/pdf")

@app.get("/healthz")
def health():
    return jsonify(status="ok")
