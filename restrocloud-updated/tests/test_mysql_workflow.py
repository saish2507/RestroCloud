"""Opt-in end-to-end tests against a disposable MySQL database.

Set DB_NAME=restrocloud_test and RESTROCLOUD_RUN_MYSQL_TESTS=1 before running.
The suite refuses to run against a database whose name does not end in `_test`.
It creates uniquely named records and deletes only the records it created.
"""
import os
import unittest
import uuid

from backend import app as app_module
from backend.db import get_db, init_db

RUN_INTEGRATION = os.environ.get("RESTROCLOUD_RUN_MYSQL_TESTS") == "1"


@unittest.skipUnless(RUN_INTEGRATION, "Set RESTROCLOUD_RUN_MYSQL_TESTS=1 to run MySQL integration tests")
class MySqlOrderWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database_name = os.environ.get("DB_NAME", "restrocloud")
        if not database_name.lower().endswith("_test"):
            raise RuntimeError("Refusing integration tests unless DB_NAME ends with '_test'.")
        init_db()
        app_module.app.config.update(TESTING=True)
        cls.client = app_module.app.test_client()

    def setUp(self):
        self.created_users = []
        self.created_restaurants = []
        self.created_orders = []
        self.prefix = uuid.uuid4().hex[:12]

    def register_and_login(self, role, suffix, address=""):
        email = f"rc-{self.prefix}-{suffix}@example.test"
        response = self.client.post("/api/register", json={
            "name": f"RC Test {suffix}",
            "email": email,
            "password": "safe-test-password",
            "role": role,
            "address": address,
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        login = self.client.post("/api/login", json={"email": email, "password": "safe-test-password"})
        self.assertEqual(login.status_code, 200, login.get_json())
        user = login.get_json()["user"]
        self.created_users.append(user["id"])
        return user, {"Authorization": f"Bearer {login.get_json()['token']}"}

    def tearDown(self):
        db = get_db()
        try:
            with db.cursor() as cursor:
                for order_id in self.created_orders:
                    cursor.execute("DELETE FROM order_items WHERE order_id = %s", (order_id,))
                    cursor.execute("DELETE FROM orders WHERE id = %s", (order_id,))
                for restaurant_id in self.created_restaurants:
                    cursor.execute("DELETE FROM menu_items WHERE restaurant_id = %s", (restaurant_id,))
                    cursor.execute("DELETE FROM restaurants WHERE id = %s", (restaurant_id,))
                for user_id in self.created_users:
                    cursor.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
                for user_id in self.created_users:
                    cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
            db.commit()
        finally:
            db.close()

    def test_order_price_snapshot_idempotency_owner_decision_and_customer_isolation(self):
        _, owner_headers = self.register_and_login("owner", "owner")
        _, other_owner_headers = self.register_and_login("owner", "other-owner")
        customer, customer_headers = self.register_and_login("customer", "customer", "10 Demo Street")
        _, other_customer_headers = self.register_and_login("customer", "other-customer", "20 Demo Street")

        restaurant_response = self.client.post("/api/my-restaurant", headers=owner_headers, json={
            "name": f"RC Test Restaurant {self.prefix}",
            "description": "Integration-test restaurant",
            "address": "10 Kitchen Road",
            "phone": "1234567890",
            "cuisine": "Test cuisine",
        })
        self.assertEqual(restaurant_response.status_code, 200, restaurant_response.get_json())
        restaurant_id = restaurant_response.get_json()["id"]
        self.created_restaurants.append(restaurant_id)

        other_restaurant_response = self.client.post("/api/my-restaurant", headers=other_owner_headers, json={
            "name": f"RC Other Restaurant {self.prefix}",
            "description": "Second restaurant",
            "address": "20 Kitchen Road",
            "phone": "0987654321",
            "cuisine": "Other cuisine",
        })
        self.assertEqual(other_restaurant_response.status_code, 200, other_restaurant_response.get_json())
        other_restaurant_id = other_restaurant_response.get_json()["id"]
        self.created_restaurants.append(other_restaurant_id)

        item_response = self.client.post("/api/menu", headers=owner_headers, json={
            "name": "Test Dish", "description": "Price snapshot dish", "price": "12.50", "category": "Mains"
        })
        self.assertEqual(item_response.status_code, 201, item_response.get_json())
        item_id = item_response.get_json()["id"]
        request_key = f"rc-test-{self.prefix}-{uuid.uuid4().hex}"
        body = {
            "restaurant_id": restaurant_id,
            "items": [{"menu_item_id": item_id, "quantity": 2, "unit_price": 0}],
            "delivery_address": "New 30 Demo Street",
            "request_key": request_key,
            "total_amount": "0.00",
        }
        placed = self.client.post("/api/orders", headers=customer_headers, json=body)
        self.assertEqual(placed.status_code, 201, placed.get_json())
        order = placed.get_json()
        self.created_orders.append(order["order_id"])
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["total_amount"], "25.00")

        duplicate = self.client.post("/api/orders", headers=customer_headers, json=body)
        self.assertEqual(duplicate.status_code, 200, duplicate.get_json())
        self.assertEqual(duplicate.get_json()["order_id"], order["order_id"])

        # Changing the live menu must not alter already placed order snapshots.
        edited = self.client.put(f"/api/menu/{item_id}", headers=owner_headers, json={
            "name": "Renamed Dish", "description": "New menu price", "price": "99.00", "category": "Mains"
        })
        self.assertEqual(edited.status_code, 200, edited.get_json())

        owner_orders = self.client.get("/api/owner/orders", headers=owner_headers)
        self.assertEqual(owner_orders.status_code, 200)
        self.assertEqual(len(owner_orders.get_json()), 1)
        owner_order = owner_orders.get_json()[0]
        self.assertEqual(owner_order["customer_email"], f"rc-{self.prefix}-customer@example.test")
        self.assertEqual(owner_order["items"][0]["item_name"], "Test Dish")
        self.assertEqual(str(owner_order["items"][0]["unit_price"]), "12.50")

        # Other restaurant owners cannot see or decide this order.
        other_queue = self.client.get("/api/owner/orders", headers=other_owner_headers)
        self.assertEqual(other_queue.status_code, 200)
        self.assertEqual(other_queue.get_json(), [])
        forbidden_decision = self.client.patch(f"/api/owner/orders/{order['order_id']}",
                                                headers=other_owner_headers, json={"status": "accepted"})
        self.assertEqual(forbidden_decision.status_code, 404)

        accepted = self.client.patch(f"/api/owner/orders/{order['order_id']}",
                                     headers=owner_headers, json={"status": "accepted"})
        self.assertEqual(accepted.status_code, 200, accepted.get_json())
        self.assertEqual(accepted.get_json()["status"], "accepted")
        second_decision = self.client.patch(f"/api/owner/orders/{order['order_id']}",
                                            headers=owner_headers, json={"status": "rejected"})
        self.assertEqual(second_decision.status_code, 409)

        customer_orders = self.client.get("/api/orders", headers=customer_headers)
        self.assertEqual(customer_orders.status_code, 200)
        self.assertEqual(len(customer_orders.get_json()), 1)
        self.assertEqual(customer_orders.get_json()[0]["status"], "accepted")
        self.assertEqual(customer_orders.get_json()[0]["delivery_address"], "New 30 Demo Street")
        self.assertEqual(customer_orders.get_json()[0]["items"][0]["item_name"], "Test Dish")
        self.assertEqual(self.client.get("/api/orders", headers=other_customer_headers).get_json(), [])


if __name__ == "__main__":
    unittest.main()
