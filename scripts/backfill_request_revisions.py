#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mevcut istekler için başlangıç revision kayıtlarını üretir.
Kullanım:
  python scripts/backfill_request_revisions.py
"""

from app import app, db, Request, RequestRevision, create_request_revision


def main():
    with app.app_context():
        requests = Request.query.all()
        created = 0

        for req in requests:
            has_revision = db.session.query(RequestRevision.id).filter_by(request_id=req.id).first()
            if has_revision:
                continue
            create_request_revision(req, submitted_by=req.created_by, status_at_submit=req.req_status)
            created += 1

        db.session.commit()
        print(f"Tamamlandı. Oluşturulan revision sayısı: {created}")


if __name__ == '__main__':
    main()
