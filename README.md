RestroCloud — local food-ordering MVP
RestroCloud is an existing Flask/MySQL/vanilla-JavaScript app extended to support the core food-ordering flow: customers register and browse restaurants, select dishes and quantities, confirm a delivery address, place an order, and see its status; restaurant owners manage their restaurant/menu and accept or reject orders from My Dashboard.

Stack
Frontend: HTML, CSS, vanilla JavaScript (fetch() API calls)
Backend: Python 3.11, Flask, Flask-CORS
Database: MySQL 8.0 through PyMySQL
Local runtime: Docker Desktop + Docker Compose
Persistence: Compose named volume db_data (the project does not reset or remove it)
Run on Windows 11 with Docker Desktop
Open Command Prompt (CMD) and run these commands from the folder where you downloaded the ZIP. The archive extracts into restrocloud-updated:

cd /d "%USERPROFILE%\Downloads"
tar -xf restrocloud-updated.zip
cd /d "%USERPROFILE%\Downloads\restrocloud-updated"
copy .env.example .env
notepad .env
docker compose up --build
In Notepad, keep MYSQL_ROOT_PASSWORD=restrocloud when reusing a database volume initialized with this project's previous default. For a new database volume, you can choose another local password before first startup. The environment variable does not automatically change a password already stored inside an existing MySQL volume.

Open the app at:

Website: http://localhost:3000
Flask API: http://localhost:5000/api
MySQL host port (for database tools): localhost:3307
Compose waits for MySQL's health check before starting Flask. The backend also retries the database connection and performs safe, additive schema initialization. To stop the app, press Ctrl+C and run docker compose down; this keeps the named volume. Do not run docker compose down -v if you want to keep your data.

Test the workflow manually
Register as Restaurant Owner and log in.
In My Dashboard, create a restaurant profile and add at least one available menu item.
Register a separate Customer account with a delivery address and log in as that customer.
Browse restaurants, open the restaurant, choose dishes and quantities, review the address, then select Place Order · Cash on delivery.
Open My Orders as the customer. The order should initially be pending.
Log back in as the restaurant owner. The order should be visible in Order Queue with the customer contact, delivery address, line prices, total, and timestamps. Accept or reject the pending order.
Return to My Orders as the customer. The updated status should appear automatically (about every 10 seconds) or after pressing Refresh.
Customers can only list their own orders. Owners can only view and decide orders for their own restaurant. Only pending orders may transition to accepted or rejected.

API routes
Method	Route	Access	Purpose
POST	/api/register	Public	Create customer/owner account
POST	/api/login	Public	Log in and receive bearer token
POST	/api/logout	Authenticated	Revoke the current session
GET	/api/restaurants?search=	Public	Browse/search restaurants
GET	/api/restaurants/<id>	Public	Restaurant details and available menu
GET / POST	/api/my-restaurant	Owner	Read/create/update restaurant profile
GET / POST	/api/menu	Owner	List/add menu items
PUT / DELETE	/api/menu/<id>	Owner	Edit/delete a menu item
POST	/api/menu/<id>/toggle	Owner	Hide/show a menu item
POST	/api/orders	Customer	Place an order; server computes the prices and total
GET	/api/orders	Customer	View own orders and status
GET	/api/owner/orders	Owner	View orders for own restaurant
PATCH	/api/owner/orders/<id>	Owner	Accept/reject a pending order
Order placement stores a delivery-address snapshot and item name/unit-price/quantity/line-total snapshots. The checkout also sends a client request key; retries with the same key are idempotent for that customer. No real payment gateway is integrated. Schema upgrades are additive and do not reset data. If an exceptionally old order_items table has no saved name/price fields but still has menu_item_id, the migration best-effort backfills from the current menu record; those legacy lines cannot be guaranteed to show their original historical price if it was never stored.

Inspect orders in MySQL
Run these commands in a second CMD window from the project directory. MySQL prompts for the root password configured in .env:

docker compose exec db mysql -uroot -p restrocloud -e "SELECT o.id, u.email AS customer_email, r.name AS restaurant, o.status, o.total_amount, o.delivery_address, o.created_at, o.updated_at FROM orders o JOIN users u ON u.id = o.customer_id JOIN restaurants r ON r.id = o.restaurant_id ORDER BY o.id DESC;"
docker compose exec db mysql -uroot -p restrocloud -e "SELECT oi.order_id, oi.item_name, oi.unit_price, oi.quantity, oi.line_total FROM order_items oi ORDER BY oi.order_id DESC, oi.id;"
Useful status values are pending, accepted, and rejected.

Automated tests
Database-free tests
Install the backend dependencies, then run from the project root:

python -m pip install -r backend\requirements.txt
python -m unittest discover -s tests -v
These tests cover request validation and role gates without connecting to MySQL. The MySQL workflow test is skipped unless explicitly enabled.

End-to-end MySQL workflow tests (optional)
Run these only against a separate database named restrocloud_test, not the app's regular restrocloud database. The tests create unique test records and delete only those records when they complete.

First create the test database:

docker compose exec db mysql -uroot -p -e "CREATE DATABASE IF NOT EXISTS restrocloud_test;"
Then in CMD set the local database connection and enable the integration test:

set DB_HOST=127.0.0.1
set DB_PORT=3307
set DB_USER=root
set DB_PASSWORD=restrocloud
set DB_NAME=restrocloud_test
set RESTROCLOUD_RUN_MYSQL_TESTS=1
python -m unittest tests.test_mysql_workflow -v
If the password in .env differs from restrocloud, set DB_PASSWORD to that value. The integration test checks registration/login, restaurant/menu creation, price validation against client-supplied totals, idempotent placement, historical price snapshots, customer isolation, owner isolation, and accept/reject transition rules.

Project layout
restrocloud-updated/
├── backend/
│   ├── app.py
│   ├── db.py
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── index.html
│   ├── login.html
│   ├── register.html
│   ├── restaurants.html
│   ├── menu.html
│   ├── orders.html
│   ├── dashboard.html
│   ├── api.js
│   ├── nav.js
│   ├── style.css
│   └── Dockerfile
├── tests/
│   ├── test_api_validation.py
│   └── test_mysql_workflow.py
├── docker-compose.yml
├── .env.example
└── README.md
Scope and production note
This iteration intentionally does not include delivery partners, live GPS, real payments, coupons, loyalty points, reviews, or platform-admin analytics. It is a local MVP/learning project, not production-hardened. Before exposing it publicly, add HTTPS, managed secrets, production-grade token/session controls, rate limiting, stricter deployment/network settings, and broader monitoring. The development Compose password is only for local use.

