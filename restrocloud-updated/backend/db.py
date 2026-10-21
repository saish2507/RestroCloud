import os
import time

import pymysql
import pymysql.cursors


DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", 3306))
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "restrocloud")
DB_NAME = os.environ.get("DB_NAME", "restrocloud")


def get_db():
    """Open a connection to MySQL and return it."""
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
    )


def wait_for_db(retries=30, delay=2):
    """Wait until MySQL is ready to accept connections."""
    for attempt in range(1, retries + 1):
        try:
            connection = get_db()
            connection.close()
            return
        except pymysql.err.OperationalError:
            print(f"Waiting for MySQL to be ready... (attempt {attempt}/{retries})")
            time.sleep(delay)
    raise RuntimeError("Could not connect to MySQL after several attempts.")


def init_db():
    """Create all tables used by RestroCloud if they don't already exist."""
    wait_for_db()
    db = get_db()
    try:
        with db.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    email VARCHAR(150) UNIQUE NOT NULL,
                    password VARCHAR(255) NOT NULL,
                    role VARCHAR(20) NOT NULL CHECK (role IN ('customer', 'owner')),
                    address VARCHAR(255) NULL
                )
            """)

            # Existing volumes may already contain a users table created before
            # customer addresses were added. Migrate it in place without deleting data.
            cursor.execute("SHOW COLUMNS FROM users LIKE 'address'")
            if cursor.fetchone() is None:
                cursor.execute("ALTER TABLE users ADD COLUMN address VARCHAR(255) NULL")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS restaurants (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    owner_id INT NOT NULL,
                    name VARCHAR(150) NOT NULL,
                    description TEXT,
                    address VARCHAR(255),
                    phone VARCHAR(20),
                    cuisine VARCHAR(100),
                    UNIQUE KEY uq_restaurant_owner (owner_id),
                    FOREIGN KEY (owner_id) REFERENCES users (id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS menu_items (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    restaurant_id INT NOT NULL,
                    name VARCHAR(150) NOT NULL,
                    description TEXT,
                    price DECIMAL(8, 2) NOT NULL,
                    category VARCHAR(100) NOT NULL,
                    is_available TINYINT(1) NOT NULL DEFAULT 1,
                    FOREIGN KEY (restaurant_id) REFERENCES restaurants (id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token VARCHAR(64) PRIMARY KEY,
                    user_id INT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
                )
            """)

            # Order headers retain their own status and total. The MySQL volume
            # in docker-compose.yml persists these tables and all order history.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    customer_id INT NOT NULL,
                    restaurant_id INT NOT NULL,
                    delivery_address VARCHAR(255) NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'accepted', 'rejected')),
                    total_amount DECIMAL(10, 2) NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_orders_customer_created (customer_id, created_at),
                    INDEX idx_orders_restaurant_status (restaurant_id, status, created_at),
                    FOREIGN KEY (customer_id) REFERENCES users (id),
                    FOREIGN KEY (restaurant_id) REFERENCES restaurants (id)
                )
            """)

            # Add the delivery address snapshot for orders created before/after this feature.
            cursor.execute("SHOW COLUMNS FROM orders LIKE 'delivery_address'")
            if cursor.fetchone() is None:
                cursor.execute("ALTER TABLE orders ADD COLUMN delivery_address VARCHAR(255) NULL AFTER restaurant_id")

            # Optional reason shown to the customer when an owner rejects an order.
            cursor.execute("SHOW COLUMNS FROM orders LIKE 'rejection_reason'")
            if cursor.fetchone() is None:
                cursor.execute("ALTER TABLE orders ADD COLUMN rejection_reason VARCHAR(255) NULL")

            # Optional client-generated key makes a retried checkout request idempotent.
            # Existing orders remain valid because the new column is nullable.
            cursor.execute("SHOW COLUMNS FROM orders LIKE 'request_key'")
            if cursor.fetchone() is None:
                cursor.execute("ALTER TABLE orders ADD COLUMN request_key VARCHAR(64) NULL")

            cursor.execute("SHOW INDEX FROM orders WHERE Key_name = 'uq_orders_customer_request_key'")
            if cursor.fetchone() is None:
                cursor.execute("CREATE UNIQUE INDEX uq_orders_customer_request_key ON orders (customer_id, request_key)")

            # Snapshot the dish name/price at order time so menu edits don't
            # silently rewrite historical orders.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS order_items (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    order_id BIGINT NOT NULL,
                    item_name VARCHAR(150) NOT NULL,
                    unit_price DECIMAL(8, 2) NOT NULL,
                    quantity INT NOT NULL,
                    line_total DECIMAL(10, 2) NOT NULL,
                    INDEX idx_order_items_order (order_id),
                    FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE
                )
            """)

            # Upgrade older order_items tables in place. Defaults apply only to legacy
            # rows; new order creation always writes exact item/price snapshots.
            legacy_item_columns = {
                "item_name": "VARCHAR(150) NOT NULL DEFAULT ''",
                "unit_price": "DECIMAL(8, 2) NOT NULL DEFAULT 0.00",
                "quantity": "INT NOT NULL DEFAULT 1",
                "line_total": "DECIMAL(10, 2) NOT NULL DEFAULT 0.00",
            }
            for column_name, column_definition in legacy_item_columns.items():
                cursor.execute(f"SHOW COLUMNS FROM order_items LIKE '{column_name}'")
                if cursor.fetchone() is None:
                    cursor.execute(f"ALTER TABLE order_items ADD COLUMN {column_name} {column_definition}")

            # If legacy rows retained menu_item_id, recover names/prices where possible
            # without touching or deleting any order records.
            cursor.execute("SHOW COLUMNS FROM order_items LIKE 'menu_item_id'")
            has_legacy_menu_item_id = cursor.fetchone() is not None
            if has_legacy_menu_item_id:
                cursor.execute("""UPDATE order_items oi
                    JOIN menu_items mi ON mi.id = oi.menu_item_id
                    SET oi.item_name = mi.name,
                        oi.unit_price = mi.price,
                        oi.line_total = mi.price * oi.quantity
                    WHERE oi.item_name = '' OR oi.unit_price = 0 OR oi.line_total = 0""")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
