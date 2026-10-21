import re
import secrets
from decimal import Decimal, InvalidOperation
from functools import wraps

from pymysql.err import IntegrityError

from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.security import check_password_hash, generate_password_hash

try:  # Supports both `python app.py` in Docker and `import backend.app` in tests.
    from .db import get_db, init_db
except ImportError:
    from db import get_db, init_db

app = Flask(__name__)
CORS(
    app,
    resources={r"/api/*": {"origins": ["http://localhost:3000", "http://127.0.0.1:3000"]}},
)


# Keep parsing/validation consistent across routes. Never accept booleans or
# fractional floats as integer IDs/quantities (int(1.9), for example, is 1).
def json_object():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def parse_integer(value):
    if isinstance(value, bool):
        raise ValueError("Expected a whole number.")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        return int(value.strip())
    raise ValueError("Expected a whole number.")


def parse_price(value):
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Price must be a valid non-negative amount with at most two decimal places.")
    if not price.is_finite() or price < 0 or price > Decimal("999999.99"):
        raise ValueError("Price must be between 0 and 999999.99.")
    if price != price.quantize(Decimal("0.01")):
        raise ValueError("Price may have at most two decimal places.")
    return price


def validate_menu_payload(data):
    if not isinstance(data, dict):
        return None, (jsonify({"error": "A JSON object is required."}), 400)
    name = data.get("name")
    category = data.get("category")
    description = data.get("description", "")
    if not isinstance(name, str) or not name.strip() or not isinstance(category, str) or not category.strip():
        return None, (jsonify({"error": "Name and category are required."}), 400)
    if not isinstance(description, str):
        return None, (jsonify({"error": "Description must be text."}), 400)
    if len(name.strip()) > 150 or len(category.strip()) > 100 or len(description) > 5000:
        return None, (jsonify({"error": "Name, category, or description exceeds the allowed length."}), 400)
    try:
        price = parse_price(data.get("price"))
    except ValueError as error:
        return None, (jsonify({"error": str(error)}), 400)
    return {"name": name.strip(), "category": category.strip(), "description": description.strip(), "price": price}, None


# ---------- Helpers for figuring out who is making the request ----------

def get_logged_in_user():
    """Looks up the user tied to the token sent in the Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.replace("Bearer ", "")

    db = get_db()
    try:
        with db.cursor() as cursor:
            cursor.execute(
                """SELECT users.* FROM sessions
                   JOIN users ON users.id = sessions.user_id
                   WHERE sessions.token = %s""",
                (token,),
            )
            return cursor.fetchone()
    finally:
        db.close()


def login_required(view_function):
    """Rejects the request with 401 Unauthorized if no valid token was sent."""
    @wraps(view_function)
    def wrapper(*args, **kwargs):
        user = get_logged_in_user()
        if user is None:
            return jsonify({"error": "Please log in first."}), 401
        request.current_user = user
        return view_function(*args, **kwargs)
    return wrapper


def owner_required(view_function):
    """Rejects the request with 403 Forbidden if the user is not an owner."""
    @wraps(view_function)
    def wrapper(*args, **kwargs):
        if request.current_user["role"] != "owner":
            return jsonify({"error": "Only restaurant owners can do this."}), 403
        return view_function(*args, **kwargs)
    return wrapper


def customer_required(view_function):
    """Allow only customer accounts to place and track customer orders."""
    @wraps(view_function)
    def wrapper(*args, **kwargs):
        if request.current_user["role"] != "customer":
            return jsonify({"error": "Only customer accounts can place or view customer orders."}), 403
        return view_function(*args, **kwargs)
    return wrapper


# ---------- Auth endpoints ----------

@app.route("/api/register", methods=["POST"])
def register():
    """Create a customer or restaurant-owner account with a hashed password."""
    data = json_object()
    if data is None:
        return jsonify({"error": "A JSON object is required."}), 400

    required_fields = ("name", "email", "password", "role")
    if any(not isinstance(data.get(field), str) or not data[field].strip() for field in required_fields):
        return jsonify({"error": "Name, email, password, and role are required."}), 400

    name = data["name"].strip()
    email = data["email"].strip().lower()
    password = data["password"]
    role = data["role"]
    address_value = data.get("address", "")
    if not isinstance(address_value, str):
        return jsonify({"error": "Address must be text."}), 400
    address = address_value.strip()

    if role not in ("customer", "owner"):
        return jsonify({"error": "Role must be customer or owner."}), 400
    if len(name) > 100 or len(email) > 150 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify({"error": "Enter a valid name and email address (maximum 100 and 150 characters)."}), 400
    if role == "customer" and not address:
        return jsonify({"error": "A delivery address is required for customer accounts."}), 400
    if len(address) > 255:
        return jsonify({"error": "Address must be 255 characters or fewer."}), 400
    if len(password) < 6 or len(password) > 128:
        return jsonify({"error": "Password must be between 6 and 128 characters."}), 400

    db = get_db()
    try:
        with db.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cursor.fetchone():
                return jsonify({"error": "An account with this email already exists."}), 409
            cursor.execute(
                "INSERT INTO users (name, email, password, role, address) VALUES (%s, %s, %s, %s, %s)",
                (name, email, generate_password_hash(password), role, address or None),
            )
        db.commit()
        return jsonify({"message": "Account created."}), 201
    except IntegrityError:
        db.rollback()
        return jsonify({"error": "An account with this email already exists."}), 409
    finally:
        db.close()


@app.route("/api/login", methods=["POST"])
def login():
    """Check credentials and issue a bearer token."""
    data = json_object()
    if data is None or not isinstance(data.get("email"), str) or not isinstance(data.get("password"), str):
        return jsonify({"error": "Email and password are required."}), 400
    email = data["email"].strip().lower()
    password = data["password"]
    if not email or not password:
        return jsonify({"error": "Email and password are required."}), 400

    db = get_db()
    try:
        with db.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            if user is None or not check_password_hash(user["password"], password):
                return jsonify({"error": "Incorrect email or password."}), 401
            token = secrets.token_hex(32)
            cursor.execute("INSERT INTO sessions (token, user_id) VALUES (%s, %s)", (token, user["id"]))
        db.commit()
        return jsonify({
            "token": token,
            "user": {
                "id": user["id"], "name": user["name"], "role": user["role"],
                "address": user.get("address") or "",
            },
        })
    finally:
        db.close()


@app.route("/api/logout", methods=["POST"])
@login_required
def logout():
    """Deletes the current token so it can no longer be used."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM sessions WHERE token = %s", (token,))
    db.commit()
    db.close()

    return jsonify({"message": "Logged out."})


# ---------- Restaurant endpoints ----------

@app.route("/api/restaurants", methods=["GET"])
def list_restaurants():
    """Lists every restaurant, optionally filtered by a search term (name or cuisine)."""
    search_term = request.args.get("search", "").strip()

    db = get_db()
    cursor = db.cursor()
    if search_term:
        like_pattern = f"%{search_term}%"
        cursor.execute(
            "SELECT * FROM restaurants WHERE name LIKE %s OR cuisine LIKE %s",
            (like_pattern, like_pattern),
        )
    else:
        cursor.execute("SELECT * FROM restaurants")
    restaurants = cursor.fetchall()
    db.close()

    return jsonify(restaurants)


@app.route("/api/restaurants/<int:restaurant_id>", methods=["GET"])
def get_restaurant(restaurant_id):
    """Shows one restaurant's info plus its available (non-hidden) menu items."""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM restaurants WHERE id = %s", (restaurant_id,))
    restaurant = cursor.fetchone()

    if restaurant is None:
        db.close()
        return jsonify({"error": "Restaurant not found."}), 404

    cursor.execute(
        "SELECT * FROM menu_items WHERE restaurant_id = %s AND is_available = 1",
        (restaurant_id,),
    )
    menu_items = cursor.fetchall()
    db.close()

    return jsonify({"restaurant": restaurant, "menu_items": menu_items})


@app.route("/api/my-restaurant", methods=["GET"])
@login_required
@owner_required
def get_my_restaurant():
    """Returns the logged-in owner's restaurant, if they've created one."""
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "SELECT * FROM restaurants WHERE owner_id = %s", (request.current_user["id"],)
    )
    restaurant = cursor.fetchone()
    db.close()

    if restaurant is None:
        return jsonify({"error": "You haven't created a restaurant yet."}), 404

    return jsonify(restaurant)


@app.route("/api/my-restaurant", methods=["POST"])
@login_required
@owner_required
def save_my_restaurant():
    """Create the owner's restaurant profile or update it in place."""
    data = json_object()
    if data is None:
        return jsonify({"error": "A JSON object is required."}), 400

    fields = {"name": 150, "description": 5000, "address": 255, "phone": 20, "cuisine": 100}
    values = {}
    for field, max_length in fields.items():
        value = data.get(field, "")
        if not isinstance(value, str):
            return jsonify({"error": f"{field.capitalize()} must be text."}), 400
        value = value.strip()
        if field in ("name", "address", "phone") and not value:
            return jsonify({"error": f"{field.capitalize()} is required."}), 400
        if len(value) > max_length:
            return jsonify({"error": f"{field.capitalize()} must be {max_length} characters or fewer."}), 400
        values[field] = value or None

    owner_id = request.current_user["id"]
    db = get_db()
    try:
        with db.cursor() as cursor:
            cursor.execute("SELECT id FROM restaurants WHERE owner_id = %s", (owner_id,))
            existing = cursor.fetchone()
            if existing:
                cursor.execute(
                    """UPDATE restaurants
                       SET name = %s, description = %s, address = %s, phone = %s, cuisine = %s
                       WHERE owner_id = %s""",
                    (values["name"], values["description"], values["address"], values["phone"], values["cuisine"], owner_id),
                )
            else:
                cursor.execute(
                    """INSERT INTO restaurants (owner_id, name, description, address, phone, cuisine)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (owner_id, values["name"], values["description"], values["address"], values["phone"], values["cuisine"]),
                )
            cursor.execute("SELECT * FROM restaurants WHERE owner_id = %s", (owner_id,))
            restaurant = cursor.fetchone()
        db.commit()
        return jsonify(restaurant)
    finally:
        db.close()


# ---------- ( owner-only) ----------

@app.route("/api/menu", methods=["GET"])
@login_required
@owner_required
def get_my_menu():
    """Returns every menu item (including hidden ones) for the owner's restaurant."""
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "SELECT id FROM restaurants WHERE owner_id = %s", (request.current_user["id"],)
    )
    restaurant = cursor.fetchone()

    if restaurant is None:
        db.close()
        return jsonify([])

    cursor.execute("SELECT * FROM menu_items WHERE restaurant_id = %s", (restaurant["id"],))
    items = cursor.fetchall()
    db.close()

    return jsonify(items)


@app.route("/api/menu", methods=["POST"])
@login_required
@owner_required
def add_menu_item():
    """Add a menu item belonging to the signed-in owner's restaurant."""
    payload, error = validate_menu_payload(json_object())
    if error:
        return error

    db = get_db()
    try:
        with db.cursor() as cursor:
            cursor.execute("SELECT id FROM restaurants WHERE owner_id = %s", (request.current_user["id"],))
            restaurant = cursor.fetchone()
            if restaurant is None:
                return jsonify({"error": "Create your restaurant before adding menu items."}), 400
            cursor.execute(
                """INSERT INTO menu_items (restaurant_id, name, description, price, category)
                   VALUES (%s, %s, %s, %s, %s)""",
                (restaurant["id"], payload["name"], payload["description"], payload["price"], payload["category"]),
            )
            item_id = cursor.lastrowid
            cursor.execute("SELECT * FROM menu_items WHERE id = %s", (item_id,))
            item = cursor.fetchone()
        db.commit()
        return jsonify(item), 201
    finally:
        db.close()


@app.route("/api/menu/<int:item_id>", methods=["PUT"])
@login_required
@owner_required
def edit_menu_item(item_id):
    """Update a menu item only when it belongs to the signed-in owner."""
    payload, error = validate_menu_payload(json_object())
    if error:
        return error

    db = get_db()
    try:
        with db.cursor() as cursor:
            cursor.execute(
                """SELECT mi.id FROM menu_items mi
                   JOIN restaurants r ON r.id = mi.restaurant_id
                   WHERE mi.id = %s AND r.owner_id = %s""",
                (item_id, request.current_user["id"]),
            )
            if cursor.fetchone() is None:
                return jsonify({"error": "Menu item not found in your restaurant."}), 404
            cursor.execute(
                """UPDATE menu_items mi
                   JOIN restaurants r ON r.id = mi.restaurant_id
                   SET mi.name = %s, mi.description = %s, mi.price = %s, mi.category = %s
                   WHERE mi.id = %s AND r.owner_id = %s""",
                (payload["name"], payload["description"], payload["price"], payload["category"], item_id, request.current_user["id"]),
            )
            cursor.execute(
                """SELECT mi.* FROM menu_items mi JOIN restaurants r ON r.id = mi.restaurant_id
                   WHERE mi.id = %s AND r.owner_id = %s""",
                (item_id, request.current_user["id"]),
            )
            item = cursor.fetchone()
        db.commit()
        return jsonify(item)
    finally:
        db.close()


@app.route("/api/menu/<int:item_id>/toggle", methods=["POST"])
@login_required
@owner_required
def toggle_menu_item(item_id):
    """Flips one menu item between available and hidden (from customers)."""
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        """SELECT mi.is_available FROM menu_items mi
           JOIN restaurants r ON r.id = mi.restaurant_id
           WHERE mi.id = %s AND r.owner_id = %s""",
        (item_id, request.current_user["id"]),
    )
    item = cursor.fetchone()
    if item is None:
        db.close()
        return jsonify({"error": "Menu item not found in your restaurant."}), 404

    new_status = 0 if item["is_available"] else 1
    cursor.execute("UPDATE menu_items SET is_available = %s WHERE id = %s", (new_status, item_id))
    db.commit()
    db.close()

    return jsonify({"is_available": new_status})


@app.route("/api/menu/<int:item_id>", methods=["DELETE"])
@login_required
@owner_required
def delete_menu_item(item_id):
    """Permanently removes one menu item."""
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        """DELETE mi FROM menu_items mi
           JOIN restaurants r ON r.id = mi.restaurant_id
           WHERE mi.id = %s AND r.owner_id = %s""",
        (item_id, request.current_user["id"]),
    )
    if cursor.rowcount == 0:
        db.close()
        return jsonify({"error": "Menu item not found in your restaurant."}), 404
    db.commit()
    db.close()

    return jsonify({"message": "Item deleted."})


# ---------- Customer ordering and order tracking ----------

@app.route("/api/orders", methods=["POST"])
@login_required
@customer_required
def place_order():
    """Create an order with server-verified items/prices and a delivery snapshot."""
    data = json_object()
    if data is None:
        return jsonify({"error": "A JSON object is required."}), 400

    try:
        restaurant_id = parse_integer(data.get("restaurant_id"))
    except ValueError:
        return jsonify({"error": "A valid restaurant_id is required."}), 400
    if restaurant_id <= 0:
        return jsonify({"error": "A valid restaurant_id is required."}), 400

    request_key = data.get("request_key")
    if request_key is not None:
        if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", request_key):
            return jsonify({"error": "request_key must be 16–64 letters, numbers, hyphens, or underscores."}), 400

    submitted_address = data.get("delivery_address", "")
    if not isinstance(submitted_address, str):
        return jsonify({"error": "Delivery address must be text."}), 400
    submitted_address = submitted_address.strip()
    if len(submitted_address) > 255:
        return jsonify({"error": "Delivery address must be 255 characters or fewer."}), 400
    delivery_address = submitted_address or (request.current_user.get("address") or "").strip()
    if not delivery_address:
        return jsonify({"error": "Enter a delivery address before placing your order."}), 400

    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return jsonify({"error": "Choose at least one menu item."}), 400
    if len(raw_items) > 100:
        return jsonify({"error": "An order may contain at most 100 item entries."}), 400

    quantities = {}
    for entry in raw_items:
        if not isinstance(entry, dict):
            return jsonify({"error": "Each order item must include menu_item_id and quantity."}), 400
        try:
            menu_item_id = parse_integer(entry.get("menu_item_id"))
            quantity = parse_integer(entry.get("quantity"))
        except ValueError:
            return jsonify({"error": "Menu item IDs and quantities must be whole numbers."}), 400
        if menu_item_id <= 0 or quantity < 1 or quantity > 99:
            return jsonify({"error": "Menu item IDs must be positive and quantities must be between 1 and 99."}), 400
        quantities[menu_item_id] = quantities.get(menu_item_id, 0) + quantity
        if quantities[menu_item_id] > 99:
            return jsonify({"error": "The maximum quantity for an item is 99."}), 400

    db = get_db()
    try:
        db.begin()
        with db.cursor() as cursor:
            # A retry with the same key returns the order already created by this customer.
            if request_key:
                cursor.execute(
                    """SELECT o.id, o.status, o.total_amount, r.name AS restaurant_name
                       FROM orders o JOIN restaurants r ON r.id = o.restaurant_id
                       WHERE o.customer_id = %s AND o.request_key = %s FOR UPDATE""",
                    (request.current_user["id"], request_key),
                )
                previous_order = cursor.fetchone()
                if previous_order:
                    db.commit()
                    return jsonify({
                        "message": "Order was already placed.",
                        "order_id": previous_order["id"],
                        "restaurant_name": previous_order["restaurant_name"],
                        "status": previous_order["status"],
                        "total_amount": str(previous_order["total_amount"]),
                        "duplicate": True,
                    }), 200

            cursor.execute("SELECT id, name FROM restaurants WHERE id = %s", (restaurant_id,))
            restaurant = cursor.fetchone()
            if restaurant is None:
                db.rollback()
                return jsonify({"error": "Restaurant not found."}), 404

            placeholders = ",".join(["%s"] * len(quantities))
            cursor.execute(
                f"""SELECT id, name, price FROM menu_items
                    WHERE restaurant_id = %s AND is_available = 1 AND id IN ({placeholders})
                    ORDER BY id FOR UPDATE""",
                (restaurant_id, *quantities.keys()),
            )
            menu_items = cursor.fetchall()
            if len(menu_items) != len(quantities):
                db.rollback()
                return jsonify({"error": "One or more selected items are unavailable or belong to another restaurant. Refresh the menu and try again."}), 400

            item_by_id = {item["id"]: item for item in menu_items}
            total = Decimal("0.00")
            order_lines = []
            for item_id, quantity in quantities.items():
                menu_item = item_by_id[item_id]
                unit_price = Decimal(str(menu_item["price"]))
                line_total = unit_price * quantity
                total += line_total
                order_lines.append((menu_item["name"], unit_price, quantity, line_total))
            if total > Decimal("99999999.99"):
                db.rollback()
                return jsonify({"error": "Order total exceeds the maximum supported amount."}), 400

            if submitted_address:
                cursor.execute("UPDATE users SET address = %s WHERE id = %s", (delivery_address, request.current_user["id"]))
            cursor.execute(
                """INSERT INTO orders
                   (customer_id, restaurant_id, delivery_address, request_key, status, total_amount)
                   VALUES (%s, %s, %s, %s, 'pending', %s)""",
                (request.current_user["id"], restaurant_id, delivery_address, request_key, total),
            )
            order_id = cursor.lastrowid
            cursor.executemany(
                """INSERT INTO order_items
                   (order_id, item_name, unit_price, quantity, line_total)
                   VALUES (%s, %s, %s, %s, %s)""",
                [(order_id, name, price, quantity, line_total) for name, price, quantity, line_total in order_lines],
            )
        db.commit()
        return jsonify({
            "message": "Order placed successfully.",
            "order_id": order_id,
            "restaurant_name": restaurant["name"],
            "status": "pending",
            "total_amount": str(total),
        }), 201
    except IntegrityError:
        db.rollback()
        # Another concurrent retry may have inserted the same key first.
        if request_key:
            with db.cursor() as cursor:
                cursor.execute(
                    """SELECT o.id, o.status, o.total_amount, r.name AS restaurant_name
                       FROM orders o JOIN restaurants r ON r.id = o.restaurant_id
                       WHERE o.customer_id = %s AND o.request_key = %s""",
                    (request.current_user["id"], request_key),
                )
                previous_order = cursor.fetchone()
            if previous_order:
                return jsonify({
                    "message": "Order was already placed.",
                    "order_id": previous_order["id"],
                    "restaurant_name": previous_order["restaurant_name"],
                    "status": previous_order["status"],
                    "total_amount": str(previous_order["total_amount"]),
                    "duplicate": True,
                }), 200
        app.logger.exception("Order integrity error")
        return jsonify({"error": "Unable to place this order due to a data conflict. Please refresh and try again."}), 409
    except Exception:
        db.rollback()
        app.logger.exception("Could not place order")
        return jsonify({"error": "Unable to place order right now. Please try again."}), 500
    finally:
        db.close()



def attach_order_items(cursor, orders):
    """Attach line items to a list of already-authorized orders."""
    if not orders:
        return orders
    order_ids = [order["id"] for order in orders]
    placeholders = ",".join(["%s"] * len(order_ids))
    cursor.execute(
        f"SELECT id, order_id, item_name, unit_price, quantity, line_total FROM order_items WHERE order_id IN ({placeholders}) ORDER BY id",
        tuple(order_ids),
    )
    items_by_order = {order_id: [] for order_id in order_ids}
    for item in cursor.fetchall():
        items_by_order[item["order_id"]].append(item)
    for order in orders:
        order["items"] = items_by_order[order["id"]]
    return orders


@app.route("/api/orders", methods=["GET"])
@login_required
@customer_required
def get_my_orders():
    """Return only orders belonging to the signed-in customer."""
    db = get_db()
    try:
        with db.cursor() as cursor:
            cursor.execute(
                """SELECT o.id, o.restaurant_id, o.delivery_address, r.name AS restaurant_name,
                          o.status, o.rejection_reason, o.total_amount, o.created_at, o.updated_at
                   FROM orders o JOIN restaurants r ON r.id = o.restaurant_id
                   WHERE o.customer_id = %s
                   ORDER BY o.created_at DESC, o.id DESC""",
                (request.current_user["id"],),
            )
            orders = cursor.fetchall()
            attach_order_items(cursor, orders)
        return jsonify(orders)
    finally:
        db.close()


@app.route("/api/owner/orders", methods=["GET"])
@login_required
@owner_required
def get_restaurant_order_queue():
    """Return the signed-in owner's restaurant orders, pending orders first."""
    db = get_db()
    try:
        with db.cursor() as cursor:
            cursor.execute("SELECT id FROM restaurants WHERE owner_id = %s", (request.current_user["id"],))
            restaurant = cursor.fetchone()
            if restaurant is None:
                return jsonify([])

            cursor.execute(
                """SELECT o.id, o.restaurant_id, o.delivery_address, o.status, o.rejection_reason, o.total_amount,
                          o.created_at, o.updated_at, u.name AS customer_name, u.email AS customer_email
                   FROM orders o JOIN users u ON u.id = o.customer_id
                   WHERE o.restaurant_id = %s
                   ORDER BY CASE o.status WHEN 'pending' THEN 0 ELSE 1 END,
                            o.created_at DESC, o.id DESC""",
                (restaurant["id"],),
            )
            orders = cursor.fetchall()
            attach_order_items(cursor, orders)
        return jsonify(orders)
    finally:
        db.close()


@app.route("/api/owner/orders/<int:order_id>", methods=["PATCH"])
@login_required
@owner_required
def decide_order(order_id):
    """Accept or reject a pending order belonging to this owner's restaurant."""
    data = json_object()
    if data is None:
        return jsonify({"error": "A JSON object is required."}), 400
    new_status = data.get("status")
    if new_status not in ("accepted", "rejected"):
        return jsonify({"error": "Status must be accepted or rejected."}), 400

    # Optional reason, only stored for rejections and shown to the customer.
    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        return jsonify({"error": "Reason must be text."}), 400
    reason = reason.strip() if isinstance(reason, str) else ""
    if len(reason) > 255:
        return jsonify({"error": "Reason must be 255 characters or fewer."}), 400
    reason = reason if (new_status == "rejected" and reason) else None

    db = get_db()
    try:
        db.begin()
        with db.cursor() as cursor:
            cursor.execute("SELECT id FROM restaurants WHERE owner_id = %s", (request.current_user["id"],))
            restaurant = cursor.fetchone()
            if restaurant is None:
                db.rollback()
                return jsonify({"error": "Create your restaurant before managing orders."}), 400

            cursor.execute(
                "SELECT id, status FROM orders WHERE id = %s AND restaurant_id = %s FOR UPDATE",
                (order_id, restaurant["id"]),
            )
            order = cursor.fetchone()
            if order is None:
                db.rollback()
                return jsonify({"error": "Order not found in your restaurant."}), 404
            if order["status"] != "pending":
                db.rollback()
                return jsonify({"error": "This order has already been decided."}), 409

            cursor.execute(
                "UPDATE orders SET status = %s, rejection_reason = %s WHERE id = %s AND restaurant_id = %s AND status = 'pending'",
                (new_status, reason, order_id, restaurant["id"]),
            )
            if cursor.rowcount != 1:
                db.rollback()
                return jsonify({"error": "This order has already been decided."}), 409
        db.commit()
        return jsonify({"id": order_id, "status": new_status, "message": f"Order {new_status}."})
    except Exception:
        db.rollback()
        app.logger.exception("Could not update order status")
        return jsonify({"error": "Unable to update this order right now."}), 500
    finally:
        db.close()


if __name__ == "__main__":
    # Initialize/migrate the schema at process startup, not on module import,
    # so test discovery and WSGI tooling do not require a live database.
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
