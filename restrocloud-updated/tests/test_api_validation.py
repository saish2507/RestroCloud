"""Fast, database-free API validation and role-authorization tests.

Run from the project root with: python -m unittest discover -s tests
"""
import unittest
from decimal import Decimal
from unittest.mock import patch

from backend import app as app_module


class ApiValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_module.app.config.update(TESTING=True)
        cls.client = app_module.app.test_client()

    def test_parse_integer_rejects_fractional_values_and_booleans(self):
        for value in (True, False, 1.5, "1.5", None, ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    app_module.parse_integer(value)
        self.assertEqual(app_module.parse_integer(3), 3)
        self.assertEqual(app_module.parse_integer("003"), 3)

    def test_parse_price_accepts_currency_precision(self):
        self.assertEqual(app_module.parse_price("12.50"), Decimal("12.50"))
        for value in ("-1", "NaN", "Infinity", "1.234", "1000000.00", "bad"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    app_module.parse_price(value)

    def test_registration_rejects_non_object_json_without_database(self):
        response = self.client.post("/api/register", json=["not", "an", "object"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON object", response.get_json()["error"])

    def test_customer_registration_requires_delivery_address(self):
        response = self.client.post("/api/register", json={
            "name": "Test Customer",
            "email": "test.customer@example.com",
            "password": "safe-password",
            "role": "customer",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("delivery address", response.get_json()["error"].lower())

    def test_fractional_order_quantity_is_rejected_before_database_write(self):
        customer = {"id": 7, "name": "Customer", "role": "customer", "address": "1 Test Road"}
        with patch.object(app_module, "get_logged_in_user", return_value=customer), \
             patch.object(app_module, "get_db", side_effect=AssertionError("Database should not be used")):
            response = self.client.post("/api/orders", json={
                "restaurant_id": 3,
                "delivery_address": "1 Test Road",
                "items": [{"menu_item_id": 4, "quantity": 1.5}],
            }, headers={"Authorization": "Bearer test-token"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("whole numbers", response.get_json()["error"])

    def test_owners_cannot_use_customer_order_history_or_place_orders(self):
        owner = {"id": 8, "name": "Owner", "role": "owner", "address": None}
        with patch.object(app_module, "get_logged_in_user", return_value=owner):
            history = self.client.get("/api/orders", headers={"Authorization": "Bearer test-token"})
            placing = self.client.post("/api/orders", json={"restaurant_id": 1, "items": []},
                                       headers={"Authorization": "Bearer test-token"})
        self.assertEqual(history.status_code, 403)
        self.assertEqual(placing.status_code, 403)

    def test_customers_cannot_access_owner_queue_or_decision_endpoints(self):
        customer = {"id": 9, "name": "Customer", "role": "customer", "address": "1 Test Road"}
        with patch.object(app_module, "get_logged_in_user", return_value=customer):
            queue = self.client.get("/api/owner/orders", headers={"Authorization": "Bearer test-token"})
            decision = self.client.patch("/api/owner/orders/42", json={"status": "accepted"},
                                         headers={"Authorization": "Bearer test-token"})
        self.assertEqual(queue.status_code, 403)
        self.assertEqual(decision.status_code, 403)

    def test_owner_decision_rejects_unknown_status_and_non_object_json(self):
        owner = {"id": 10, "name": "Owner", "role": "owner", "address": None}
        with patch.object(app_module, "get_logged_in_user", return_value=owner), \
             patch.object(app_module, "get_db", side_effect=AssertionError("Database should not be used")):
            bad_status = self.client.patch("/api/owner/orders/1", json={"status": "delivered"},
                                           headers={"Authorization": "Bearer test-token"})
            bad_body = self.client.patch("/api/owner/orders/1", json=["accepted"],
                                         headers={"Authorization": "Bearer test-token"})
        self.assertEqual(bad_status.status_code, 400)
        self.assertEqual(bad_body.status_code, 400)


if __name__ == "__main__":
    unittest.main()
