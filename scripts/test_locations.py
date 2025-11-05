import sqlalchemy
from app import app, db
from models import Component, BorrowLog  # modellerin yolunu kendi projenle eşleştir

def test_locations():
    print("SQLAlchemy version:", sqlalchemy.__version__)

    try:
        comp_locs = [row[0] for row in db.session.query(Component.location)
                     .filter(Component.location.isnot(None))
                     .distinct()
                     .all()]

        borrow_locs = [row[0] for row in db.session.query(BorrowLog.location)
                       .filter(BorrowLog.location.isnot(None))
                       .distinct()
                       .all()]

        print("Component:", comp_locs)
        print("BorrowLog:", borrow_locs)

        loc_set = {v.strip() for v in comp_locs + borrow_locs if v and v.strip()}
        locations = sorted(loc_set)

        print("Cleaned locations:", locations)

    except Exception as e:
        print("Hata:", e)

if __name__ == "__main__":
    with app.app_context():
        test_locations()

