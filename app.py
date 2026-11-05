import os, re, secrets, sqlite3
from datetime import date
from flask import Flask, g, request, session, jsonify, abort, make_response, send_from_directory
from werkzeug.security import generate_password_hash as gen, check_password_hash as chk

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "bee-usb-dev")
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bee.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores(id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, pw TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('student','store')), store_id INTEGER REFERENCES stores(id));
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, store_id INTEGER NOT NULL REFERENCES stores(id),
  name TEXT NOT NULL, price INTEGER NOT NULL, available INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  store_id INTEGER NOT NULL REFERENCES stores(id), pickup TEXT NOT NULL, total INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pendiente', paid INTEGER NOT NULL DEFAULT 1, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS order_items(order_id INTEGER NOT NULL REFERENCES orders(id),
  product_id INTEGER NOT NULL REFERENCES products(id), qty INTEGER NOT NULL, price INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cards(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), brand TEXT, last4 TEXT NOT NULL, holder TEXT, exp TEXT);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(id), user_id INTEGER NOT NULL REFERENCES users(id),
  card_id INTEGER REFERENCES cards(id), amount INTEGER NOT NULL, status TEXT NOT NULL, ref TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
"""
SEED = {
 ("Cafetería Central", "Cafetería", "cafeteria@bee.test"): [("Arepa con queso", 4500), ("Jugo natural", 3000), ("Café americano", 2500), ("Almuerzo ejecutivo", 12000)],
 ("Panadería La Espiga", "Panadería", "panaderia@bee.test"): [("Pan de queso", 2000), ("Croissant", 3500), ("Pastel de pollo", 4000)],
 ("Jugos Frescos", "Bebidas", "jugos@bee.test"): [("Jugo de mora", 3500), ("Limonada de coco", 5000), ("Smoothie de fresa", 6000)],
 ("Kiosco Snacks", "Snacks", "snacks@bee.test"): [("Papas", 2500), ("Galletas", 1500), ("Agua", 2000)],
}

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close(_):
    c = g.pop("db", None)
    if c: c.close()

def init():
    c = sqlite3.connect(DB); c.executescript(SCHEMA)
    if not c.execute("SELECT 1 FROM stores").fetchone():
        for (name, cat, mail), prods in SEED.items():
            sid = c.execute("INSERT INTO stores(name,category) VALUES(?,?)", (name, cat)).lastrowid
            c.execute("INSERT INTO users(name,email,pw,role,store_id) VALUES(?,?,?,?,?)", (name, mail, gen("bee123"), "store", sid))
            c.executemany("INSERT INTO products(store_id,name,price) VALUES(?,?,?)", [(sid, n, p) for n, p in prods])
    c.commit(); c.close()

def err(msg, code=400): return make_response(jsonify(error=msg), code)

def need(role=None):
    u = db().execute("SELECT * FROM users WHERE id=?", (session.get("uid"),)).fetchone()
    if not u: abort(err("Inicia sesión", 401))
    if role and u["role"] != role: abort(err("No autorizado", 403))
    return u

def pub(u): return dict(id=u["id"], name=u["name"], role=u["role"], store_id=u["store_id"])

@app.get("/")
def home(): return send_from_directory("static", "index.html")

@app.post("/api/register")
def register():
    d = request.json or {}
    email = (d.get("email") or "").strip().lower()
    if not email.endswith("@usb.edu.co"): return err("Usa tu correo institucional @usb.edu.co")
    if len(d.get("password") or "") < 6 or not d.get("name"): return err("Nombre y contraseña (mín. 6) obligatorios")
    try:
        uid = db().execute("INSERT INTO users(name,email,pw,role) VALUES(?,?,?,'student')", (d["name"], email, gen(d["password"]))).lastrowid
        db().commit()
    except sqlite3.IntegrityError: return err("Ese correo ya está registrado")
    session["uid"] = uid
    return jsonify(user=pub(need()))

@app.post("/api/login")
def login():
    d = request.json or {}
    u = db().execute("SELECT * FROM users WHERE email=?", ((d.get("email") or "").strip().lower(),)).fetchone()
    if not u or not chk(u["pw"], d.get("password") or ""): return err("Correo o contraseña incorrectos", 401)
    session["uid"] = u["id"]
    return jsonify(user=pub(u))

@app.post("/api/logout")
def logout(): session.clear(); return jsonify(ok=1)

@app.get("/api/me")
def me(): return jsonify(user=pub(need()))

@app.get("/api/stores")
def stores():
    need(); c = db(); out = []
    for s in c.execute("SELECT * FROM stores"):
        s = dict(s); s["products"] = [dict(p) for p in c.execute("SELECT * FROM products WHERE store_id=?", (s["id"],))]
        out.append(s)
    return jsonify(out)

@app.post("/api/products/<int:pid>/toggle")
def toggle(pid):
    u = need("store"); c = db()
    c.execute("UPDATE products SET available=1-available WHERE id=? AND store_id=?", (pid, u["store_id"])); c.commit()
    return jsonify(ok=1)

@app.post("/api/orders")
def new_order():
    u = need("student"); d = request.json or {}; c = db(); total = 0; rows = []
    if not d.get("items") or not d.get("pickup"): return err("Elige productos y hora de recogida")
    for it in d["items"]:
        qty = int(it.get("qty", 0))
        p = c.execute("SELECT * FROM products WHERE id=? AND store_id=? AND available=1", (it.get("product_id"), d.get("store_id"))).fetchone()
        if not p or qty < 1: return err("Algún producto ya no está disponible")
        total += p["price"] * qty; rows.append((p["id"], qty, p["price"]))
    card = c.execute("SELECT * FROM cards WHERE id=? AND user_id=?", (d.get("card_id"), u["id"])).fetchone()
    if not card: return err("Elige un método de pago")
    if not re.fullmatch(r"\d{3,4}", str(d.get("cvv", ""))): return err("CVV inválido")
    ref = "BEE-" + secrets.token_hex(4).upper()
    if card["last4"] == "0002":  # tarjeta de prueba que simula rechazo
        c.execute("INSERT INTO payments(user_id,card_id,amount,status,ref) VALUES(?,?,?,?,?)", (u["id"], card["id"], total, "rechazado", ref)); c.commit()
        return err("Pago rechazado por el banco. Prueba con otra tarjeta.")
    oid = c.execute("INSERT INTO orders(user_id,store_id,pickup,total) VALUES(?,?,?,?)", (u["id"], d["store_id"], d["pickup"], total)).lastrowid
    c.executemany("INSERT INTO order_items VALUES(?,?,?,?)", [(oid, *r) for r in rows])
    c.execute("INSERT INTO payments(order_id,user_id,card_id,amount,status,ref) VALUES(?,?,?,?,?,?)", (oid, u["id"], card["id"], total, "aprobado", ref)); c.commit()
    return jsonify(id=oid, total=total, ref=ref)

@app.get("/api/orders")
def orders():
    u = need(); c = db()
    col, val = ("store_id", u["store_id"]) if u["role"] == "store" else ("user_id", u["id"])
    rows = c.execute(f"SELECT o.*, s.name store, us.name student, p.ref, p.status pay FROM orders o JOIN stores s ON s.id=o.store_id JOIN users us ON us.id=o.user_id LEFT JOIN payments p ON p.order_id=o.id WHERE o.{col}=? ORDER BY o.id DESC", (val,)).fetchall()
    out = []
    for o in rows:
        o = dict(o); o["items"] = [dict(i) for i in c.execute("SELECT i.qty, p.name FROM order_items i JOIN products p ON p.id=i.product_id WHERE i.order_id=?", (o["id"],))]
        out.append(o)
    return jsonify(out)

def luhn(n):
    d = [int(x) for x in n][::-1]
    return (sum(d[0::2]) + sum(sum(divmod(x * 2, 10)) for x in d[1::2])) % 10 == 0

@app.get("/api/cards")
def cards():
    u = need("student")
    return jsonify([dict(r) for r in db().execute("SELECT id,brand,last4,holder,exp FROM cards WHERE user_id=?", (u["id"],))])

@app.post("/api/cards")
def add_card():
    u = need("student"); d = request.json or {}
    n = re.sub(r"\D", "", d.get("number") or ""); m = re.fullmatch(r"(\d\d)/(\d\d)", (d.get("exp") or "").strip())
    if not (13 <= len(n) <= 19 and luhn(n)): return err("Número de tarjeta inválido")
    if not m or not 1 <= int(m[1]) <= 12 or (2000 + int(m[2]), int(m[1])) < (date.today().year, date.today().month): return err("Fecha inválida o vencida (MM/AA)")
    if not (d.get("holder") or "").strip(): return err("Escribe el nombre del titular")
    brand = "Visa" if n[0] == "4" else "Mastercard" if n[0] in "25" else "Tarjeta"
    c = db(); c.execute("INSERT INTO cards(user_id,brand,last4,holder,exp) VALUES(?,?,?,?,?)", (u["id"], brand, n[-4:], d["holder"].strip(), d["exp"])); c.commit()
    return jsonify(ok=1)

FLOW = {"pendiente": "preparando", "preparando": "listo", "listo": "entregado"}

@app.post("/api/orders/<int:oid>/status")
def status(oid):
    u = need(); c = db(); s = (request.json or {}).get("status")
    o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o: return err("Pedido no existe", 404)
    ok_store = u["role"] == "store" and o["store_id"] == u["store_id"] and FLOW.get(o["status"]) == s
    ok_stud = u["role"] == "student" and o["user_id"] == u["id"] and o["status"] == "pendiente" and s == "cancelado"
    if not (ok_store or ok_stud): return err("Cambio no permitido", 403)
    c.execute("UPDATE orders SET status=? WHERE id=?", (s, oid))
    if s == "cancelado": c.execute("UPDATE payments SET status='reembolsado' WHERE order_id=?", (oid,))
    c.commit()
    return jsonify(ok=1)

def _op(tag, summary, body=None, path_id=False, auth=True):
    o = {"tags": [tag], "summary": summary, "responses": {"200": {"description": "OK"}, "400": {"description": "Datos inválidos"}}}
    if auth: o["responses"]["401"] = {"description": "Sin sesión: primero usa /api/login"}
    if body: o["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {k: {"type": t, "example": e} for k, (t, e) in body.items()}}}}}
    if path_id: o["parameters"] = [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}]
    return o

_A, _T, _P, _C = "Autenticación", "Tiendas y menú", "Pedidos", "Pagos y tarjetas"
SPEC = {"openapi": "3.0.3",
  "info": {"title": "Bee API", "version": "1.0", "description": "API REST de Bee (Universidad de San Buenaventura): pedir antes y recoger sin fila. Autenticación por cookie de sesión: primero ejecuta POST /api/login (demo tienda: cafeteria@bee.test / bee123) y luego prueba el resto con Try it out. Autores: Neyder Arenas, Nicole Lamprea, Daniel Muñoz."},
  "paths": {
    "/api/register": {"post": _op(_A, "Registrar estudiante (solo correos @usb.edu.co)", {"name": ("string", "Ana Pérez"), "email": ("string", "ana@usb.edu.co"), "password": ("string", "123456")}, auth=False)},
    "/api/login": {"post": _op(_A, "Iniciar sesión (estudiante o tienda)", {"email": ("string", "cafeteria@bee.test"), "password": ("string", "bee123")}, auth=False)},
    "/api/logout": {"post": _op(_A, "Cerrar sesión")},
    "/api/me": {"get": _op(_A, "Datos del usuario con sesión activa")},
    "/api/stores": {"get": _op(_T, "Listar establecimientos con sus productos y disponibilidad")},
    "/api/products/{id}/toggle": {"post": _op(_T, "(Tienda) Marcar un producto disponible/agotado", path_id=True)},
    "/api/cards": {"get": _op(_C, "(Estudiante) Listar tarjetas guardadas (solo marca y últimos 4 dígitos)"),
                   "post": _op(_C, "(Estudiante) Guardar tarjeta; valida Luhn y vencimiento", {"number": ("string", "4242 4242 4242 4242"), "holder": ("string", "Ana Pérez"), "exp": ("string", "12/30")})},
    "/api/orders": {"get": _op(_P, "Listar pedidos: los del estudiante o los de la tienda, con estado de pago"),
                    "post": _op(_P, "(Estudiante) Crear pedido y cobrar con pasarela simulada; el total lo calcula el servidor", {"store_id": ("integer", 1), "pickup": ("string", "10:40"), "card_id": ("integer", 1), "cvv": ("string", "123"), "items": ("array", [{"product_id": 1, "qty": 2}])})},
    "/api/orders/{id}/status": {"post": _op(_P, "Cambiar estado: tienda (pendiente→preparando→listo→entregado) o estudiante (cancelar si está pendiente, genera reembolso)", {"status": ("string", "preparando")}, path_id=True)}}}

DOCS = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Bee API</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui.css"></head><body>
<div id="ui"></div><script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({url:"/api/openapi.json",dom_id:"#ui"})</script></body></html>"""

@app.get("/api/openapi.json")
def openapi(): return jsonify(SPEC)

@app.get("/docs")
def docs(): return DOCS

init()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("DEBUG", "1") == "1")
