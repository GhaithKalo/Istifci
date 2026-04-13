import os
import re
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from app import (
    _process_tags,
    app,
    clean_text,
    db,
    generate_component_code,
    get_locations,
    is_fixed_asset,
    tr_date,
)
from models import BorrowLog, Component, Request, RequestItem, Tag, User


class BaseDbTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls._tmpdir.name, "test.db")
        app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{db_path}",
        )
        with app.app_context():
            db.drop_all()
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.session.remove()
            db.drop_all()
        cls._tmpdir.cleanup()

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.session.remove()
        db.drop_all()
        db.create_all()

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        self.ctx.pop()


class HelperFunctionTests(BaseDbTestCase):
    def test_is_fixed_asset_variants(self):
        self.assertTrue(is_fixed_asset("demirbas"))
        self.assertTrue(is_fixed_asset(" DemirBaş "))
        self.assertTrue(is_fixed_asset("gerecler"))
        self.assertFalse(is_fixed_asset(""))
        self.assertFalse(is_fixed_asset(None))
        self.assertFalse(is_fixed_asset("sarf"))

    @patch("app.uuid.uuid4")
    def test_generate_component_code_with_part_number(self, mock_uuid4):
        mock_uuid4.return_value = SimpleNamespace(hex="abc12345ffff")
        code = generate_component_code("motor", "Nema 17", "pn-42")
        self.assertEqual(code, "MO-PN-42-ABC12345")

    @patch("app.uuid.uuid4")
    def test_generate_component_code_without_part_number(self, mock_uuid4):
        mock_uuid4.return_value = SimpleNamespace(hex="0123456789ab")
        code = generate_component_code("sensor", "Arduino Uno R3", None)
        self.assertEqual(code, "SE-AU3-01234567")

    @patch("app.uuid.uuid4")
    def test_generate_component_code_empty_name_defaults(self, mock_uuid4):
        mock_uuid4.return_value = SimpleNamespace(hex="deadbeefcafefeed")
        code = generate_component_code(None, "", None)
        self.assertEqual(code, "GE--DEADBEEF")
        self.assertRegex(code.split("-")[-1], r"^[A-F0-9]{8}$")

    def test_clean_text_removes_expected_characters(self):
        self.assertEqual(clean_text("('Örnek' \"metin\")"), "Örnek metin")

    def test_tr_date_formats_with_and_without_time(self):
        dt = datetime(2026, 4, 13, 20, 11)
        self.assertEqual(tr_date(dt), "13 Nisan 2026")
        self.assertEqual(tr_date(dt, with_time=True), "13 Nisan 2026 20:11")
        self.assertEqual(tr_date(None), "")


class UserModelTests(unittest.TestCase):
    def test_local_user_password_and_permissions(self):
        user = User(
            username="local-user",
            password="secret123",
            can_add_product=True,
            can_delete_product=False,
        )
        self.assertFalse(user.is_admin())
        self.assertTrue(user.check_password("secret123"))
        self.assertFalse(user.check_password("wrong-pass"))
        self.assertTrue(user.has_add_permission())
        self.assertFalse(user.has_delete_permission())

    def test_ldap_user_has_no_local_password_check(self):
        user = User(username="ldap-user", password=None, is_ldap=True)
        self.assertTrue(user.is_ldap)
        self.assertIsNone(user.password_hash)
        self.assertFalse(user.check_password("anything"))

    def test_admin_user_permissions(self):
        admin = User(username="admin", password="adminpass", role="admin")
        self.assertTrue(admin.is_admin())
        self.assertTrue(admin.has_add_permission())
        self.assertTrue(admin.has_delete_permission())


class TagAndLocationTests(BaseDbTestCase):
    def test_process_tags_handles_new_existing_and_duplicates(self):
        existing = Tag(name="Existing Tag")
        db.session.add(existing)
        db.session.commit()

        processed = _process_tags(
            [
                str(existing.id),
                "new_New-Tag",
                "new_new tag",
                "new_existing tag",
                "invalid",
            ]
        )
        db.session.commit()

        names = sorted(t.name for t in processed)
        self.assertEqual(names, ["Existing Tag", "Existing Tag", "New Tag"])
        self.assertEqual(Tag.query.filter_by(name="New Tag").count(), 1)
        self.assertEqual(Tag.query.filter_by(name="Existing Tag").count(), 1)

    def test_get_locations_returns_sorted_unique_cleaned_values(self):
        component = Component(
            name="Multimeter",
            category="gerec",
            type="tool",
            quantity=1,
            location=" Lab A ",
            code="TO-MM-0001",
        )
        db.session.add(component)
        db.session.flush()

        db.session.add(
            BorrowLog(
                user_id=None,
                username="u1",
                comp_id=component.id,
                action="borrow",
                amount=1,
                location="Storage",
            )
        )
        db.session.add(
            BorrowLog(
                user_id=None,
                username="u2",
                comp_id=component.id,
                action="return",
                amount=1,
                location="Lab A",
            )
        )
        db.session.commit()

        self.assertEqual(get_locations(), ["Lab A", "Storage"])

    def test_get_locations_returns_empty_list_on_query_error(self):
        with patch("app.db.session.query", side_effect=Exception("boom")):
            self.assertEqual(get_locations(), [])


class RequestModelTests(BaseDbTestCase):
    def test_request_total_items_price_uses_items_total(self):
        req = Request(
            name="Satın Alma",
            req_type="satin_alma",
            req_status="beklemede",
            total_price=100.0,
        )
        db.session.add(req)
        db.session.flush()

        db.session.add_all(
            [
                RequestItem(request_id=req.id, name="A", quantity=1, total_price=40.0),
                RequestItem(request_id=req.id, name="B", quantity=2, total_price=60.0),
                RequestItem(request_id=req.id, name="C", quantity=1, total_price=None),
            ]
        )
        db.session.commit()

        self.assertEqual(req.items_count, 3)
        self.assertEqual(req.total_items_price, 100.0)

    def test_request_total_items_price_falls_back_to_request_total(self):
        req = Request(
            name="Satın Alma",
            req_type="satin_alma",
            req_status="beklemede",
            total_price=250.0,
        )
        db.session.add(req)
        db.session.flush()
        db.session.add(RequestItem(request_id=req.id, name="A", quantity=1, total_price=None))
        db.session.commit()

        self.assertEqual(req.items_count, 1)
        self.assertEqual(req.total_items_price, 250.0)


if __name__ == "__main__":
    unittest.main()
