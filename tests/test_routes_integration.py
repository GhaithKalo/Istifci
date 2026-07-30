import io
import os
import tempfile
import unittest
from contextlib import contextmanager

from flask import template_rendered

TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "istifci_unittest.db")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"

from app import app, db
from models import BorrowLog, Component, InventoryItem, Request, User


@contextmanager
def captured_templates(flask_app):
    recorded = []

    def record(sender, template, context, **extra):
        recorded.append(template.name)

    template_rendered.connect(record, flask_app)
    try:
        yield recorded
    finally:
        template_rendered.disconnect(record, flask_app)


class RouteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        self.upload_dir = tempfile.mkdtemp(prefix="istifci_test_upload_")
        app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            UPLOAD_FOLDER=self.upload_dir,
        )
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

        for name in os.listdir(self.upload_dir):
            try:
                os.remove(os.path.join(self.upload_dir, name))
            except FileNotFoundError:
                pass
        os.rmdir(self.upload_dir)

    def _create_user(
        self,
        username,
        password,
        role="user",
        can_add_product=False,
        can_delete_product=False,
        is_ldap=False,
    ):
        user = User(
            username=username,
            password=password,
            role=role,
            can_add_product=can_add_product,
            can_delete_product=can_delete_product,
            is_ldap=is_ldap,
        )
        db.session.add(user)
        db.session.commit()
        return user

    def _login(self, username, password):
        return self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )

    def test_login_route_success_and_failure(self):
        self._create_user("alice", "alice-pass")

        ok_resp = self._login("alice", "alice-pass")
        self.assertEqual(ok_resp.status_code, 302)
        self.assertTrue(ok_resp.location.endswith("/"))

        bad_resp = self._login("alice", "wrong")
        self.assertEqual(bad_resp.status_code, 200)
        self.assertIn("Geçersiz kullanıcı adı veya şifre", bad_resp.get_data(as_text=True))

    def test_admin_dashboard_access_control_and_template(self):
        self._create_user("normal", "pass")
        self._create_user("admin", "pass", role="admin")

        anon_resp = self.client.get("/admin/dashboard", follow_redirects=False)
        self.assertEqual(anon_resp.status_code, 302)
        self.assertIn("/login", anon_resp.location)

        self._login("normal", "pass")
        forbidden_resp = self.client.get("/admin/dashboard", follow_redirects=False)
        self.assertEqual(forbidden_resp.status_code, 403)

        self.client.get("/logout", follow_redirects=False)
        self._login("admin", "pass")
        with captured_templates(app) as templates:
            ok_resp = self.client.get("/admin/dashboard")
        self.assertEqual(ok_resp.status_code, 200)
        self.assertIn("admin/dashboard.html", templates)

    def test_user_page_renders_expected_template(self):
        self._create_user("u1", "pass")
        self._login("u1", "pass")

        with captured_templates(app) as templates:
            resp = self.client.get("/my_borrowed")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("my_borrowed.html", templates)

    def test_add_component_with_photo_upload_integration(self):
        self._create_user("editor", "pass", can_add_product=True)
        self._login("editor", "pass")

        resp = self.client.post(
            "/add",
            data={
                "category": "sarf",
                "name": "Flux",
                "type": "kimyasal",
                "location": "Lab-1",
                "description": "Lehim",
                "quantity": "3",
                "part_number": "FLX-01",
                "serial_numbers": "",
                "owner_prefix": "",
                "photo": (io.BytesIO(b"fake-image-bytes"), "flux.png"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.location.endswith("/"))

        component = Component.query.filter_by(name="Flux").first()
        self.assertIsNotNone(component)
        self.assertIsNotNone(component.image_url)
        self.assertTrue(component.image_url.startswith("/static/uploads/"))

        uploaded_name = os.path.basename(component.image_url)
        uploaded_path = os.path.join(self.upload_dir, uploaded_name)
        self.assertTrue(os.path.exists(uploaded_path))

    def test_borrow_then_return_fixed_asset_workflow(self):
        user = self._create_user("borrower", "pass")
        component = Component(
            name="Laptop",
            category="demirbas",
            type="cihaz",
            quantity=1,
            code="CI-L-0001",
        )
        db.session.add(component)
        db.session.flush()
        item = InventoryItem(component_id=component.id, serial_number="LAB-001")
        db.session.add(item)
        db.session.commit()

        self._login("borrower", "pass")
        borrow_resp = self.client.post(
            f"/process_item/{component.id}",
            data={
                "amount": "1",
                "action": "borrow",
                "serial_number": "LAB-001",
                "location": "Makerspace",
            },
            follow_redirects=False,
        )
        self.assertEqual(borrow_resp.status_code, 302)
        db.session.refresh(component)
        db.session.refresh(item)
        self.assertEqual(component.quantity, 0)
        self.assertEqual(item.assigned_to, user.username)
        self.assertEqual(
            BorrowLog.query.filter_by(comp_id=component.id, action="borrow").count(), 1
        )

        return_resp = self.client.post(
            f"/return/{component.id}",
            data={"amount": "1", "serial_number": "LAB-001", "next": "/my_borrowed"},
            follow_redirects=False,
        )
        self.assertEqual(return_resp.status_code, 302)
        db.session.refresh(component)
        db.session.refresh(item)
        self.assertEqual(component.quantity, 1)
        self.assertIsNone(item.assigned_to)
        self.assertEqual(
            BorrowLog.query.filter_by(comp_id=component.id, action="return").count(), 1
        )

    def test_admin_set_request_status_and_guarded_transition(self):
        admin = self._create_user("admin2", "pass", role="admin")
        req = Request(
            name="Yeni Lehim İstasyonu",
            req_type="satin_alma",
            req_status="beklemede",
            created_by=admin.id,
        )
        db.session.add(req)
        db.session.commit()

        self._login("admin2", "pass")
        accept_resp = self.client.post(
            f"/admin/request/{req.id}/set_status",
            data={"status": "kabul", "admin_note": "Onaylandı"},
            follow_redirects=False,
        )
        self.assertEqual(accept_resp.status_code, 302)
        db.session.refresh(req)
        self.assertEqual(req.req_status, "kabul")
        self.assertEqual(req.admin_note, "Onaylandı")

        reject_resp = self.client.post(
            f"/admin/request/{req.id}/set_status",
            data={"status": "reddedildi", "admin_note": "Reddedilemez"},
            follow_redirects=False,
        )
        self.assertEqual(reject_resp.status_code, 302)
        db.session.refresh(req)
        self.assertEqual(req.req_status, "kabul")


if __name__ == "__main__":
    unittest.main()
