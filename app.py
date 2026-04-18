# -*- coding: utf-8 -*-

# ==============================================================================
# GEREKLİ KÜTÜPHANELERİN VE MODÜLLERİN YÜKLENMESİ
# ==============================================================================
import uuid
from flask import Flask, render_template, request, redirect, url_for, flash, abort, g, send_file, jsonify
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_migrate import Migrate
from werkzeug.utils import secure_filename
from flask_wtf import CSRFProtect
from flask_wtf.csrf import generate_csrf
from sqlalchemy import func, or_, event, case, text
from sqlalchemy.orm import joinedload, selectinload, subqueryload
from collections import defaultdict, Counter
from datetime import datetime
from zoneinfo import ZoneInfo # Python 3.9+
import os, re, logging, sys
from dotenv import load_dotenv

load_dotenv()

# LDAP configuration (can be set via environment variables)
USE_LDAP_AUTH = os.environ.get('USE_LDAP_AUTH', '0') in ('1', 'true', 'True')
LDAP_HOST = os.environ.get('LDAP_HOST', '172.28.1.103')
LDAP_PORT = int(os.environ.get('LDAP_PORT', os.environ.get('LDAP_PORT', 3890)))
LDAP_USE_SSL = os.environ.get('LDAP_USE_SSL', '0') in ('1', 'true', 'True')
LDAP_BASE_DN = os.environ.get('LDAP_BASE_DN', '')
LDAP_USER_ATTR = os.environ.get('LDAP_USER_ATTR', 'uid')
# Optional credentials to perform an initial search (if anonymous search is not allowed)
LDAP_SEARCH_BIND_DN = os.environ.get('LDAP_SEARCH_BIND_DN')
LDAP_SEARCH_BIND_PASS = os.environ.get('LDAP_SEARCH_BIND_PASS')

# LDAP Group configuration for role management
# Admin group - users in this group will have admin role
LDAP_ADMIN_GROUP = os.environ.get('LDAP_ADMIN_GROUP', 'istifci_admins')  # Default: istifci_admins
# Group base DN for searching groups (if different from LDAP_BASE_DN)
LDAP_GROUP_BASE_DN = os.environ.get('LDAP_GROUP_BASE_DN', '')
# Group member attribute (memberUid, member, uniqueMember etc.)
LDAP_GROUP_MEMBER_ATTR = os.environ.get('LDAP_GROUP_MEMBER_ATTR', 'memberUid')

from ldap3 import Server, Connection, ALL, SUBTREE

# Proje içi modüller
from models import db, User, Component, BorrowLog, Project, ProjectItem, Tag, Request, InventoryItem, RequestItem, RequestMessage, RequestRevision

# ==============================================================================
# YARDIMCI FONKSİYONLAR
# ==============================================================================

def is_fixed_asset(category: str) -> bool:
    """
    Bir kategorinin demirbaş veya gereç gibi sabit bir varlık olup olmadığını kontrol eder.
    Seri numarası takibi gibi işlemler için kullanılır.
    """
    if not category:
        return False
    c = category.lower().strip()
    # 'gereç' ve 'demirbaş' için farklı yazım şekillerini kabul eder
    fixed_names = {'demirbas', 'demirbaş', 'gereç', 'gerec', 'gereçler', 'gerecler'}
    return c in fixed_names

def clean_text(s):
    return re.sub(r"[\"'()]", "", s)


REQUEST_MESSAGE_MAX_LENGTH = 2000
REQUEST_MESSAGE_UPLOAD_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'static', 'uploads', 'request_messages')
REQUEST_MESSAGE_ALLOWED_EXTENSIONS = {
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp',
    'pdf', 'txt', 'csv', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
    'zip', 'rar'
}
WET_SIGNATURE_PRICE_THRESHOLD = 100000


def status_label(status: str) -> str:
    labels = {
        'beklemede': 'Beklemede',
        'reddedildi': 'Reddedildi',
        'kabul': 'Kabul Edildi',
        'tamamlandi': 'Tamamlandı'
    }
    return labels.get(status or '', status or '-')


def build_request_conversation_map(requests_list):
    """
    Şablonlarda kullanılmak üzere request_id -> conversation entries map üretir.
    Legacy admin_note alanını, ilgili timeline'da admin_note mesajı yoksa sentetik olarak ekler.
    """
    conversation_map = {}
    for req in requests_list:
        entries = []
        has_admin_note_message = False

        for msg in (req.messages or []):
            if msg.message_type == 'admin_note':
                has_admin_note_message = True
            entries.append({
                'id': msg.id,
                'author_user_id': msg.author_user_id,
                'author_role': msg.author_role or 'user',
                'author': msg.author_username_snapshot or ('Sistem' if msg.author_role == 'system' else 'Bilinmiyor'),
                'message_type': msg.message_type or 'chat',
                'body': msg.body or '',
                'attachment_path': msg.attachment_path,
                'attachment_name': msg.attachment_name,
                'attachment_mime': msg.attachment_mime,
                'status_from': msg.status_from,
                'status_to': msg.status_to,
                'created_at': msg.created_at,
                'legacy': False
            })

        if req.admin_note and not has_admin_note_message:
            entries.append({
                'id': None,
                'author_user_id': None,
                'author_role': 'admin',
                'author': 'Admin',
                'message_type': 'admin_note',
                'body': req.admin_note,
                'attachment_path': None,
                'attachment_name': None,
                'attachment_mime': None,
                'status_from': None,
                'status_to': None,
                'created_at': req.created_at,
                'legacy': True
            })

        fallback_date = req.created_at or datetime.utcnow()
        entries.sort(key=lambda e: (e.get('created_at') or fallback_date, e.get('id') or 0))
        conversation_map[req.id] = entries

    return conversation_map


def append_status_event_message(req, old_status: str, new_status: str):
    if old_status == new_status:
        return

    event_message = RequestMessage(
        request_id=req.id,
        author_role='system',
        author_username_snapshot='Sistem',
        message_type='status_event',
        body=f"Durum güncellendi: {status_label(old_status)} → {status_label(new_status)}",
        status_from=old_status,
        status_to=new_status
    )
    db.session.add(event_message)


def append_admin_note_message(req, note: str, admin_user):
    note = (note or '').strip()
    if not note:
        return

    admin_note_message = RequestMessage(
        request_id=req.id,
        author_user_id=admin_user.id if admin_user else None,
        author_username_snapshot=admin_user.username if admin_user else 'Admin',
        author_role='admin',
        message_type='admin_note',
        body=note
    )
    db.session.add(admin_note_message)


def build_request_snapshot(req) -> dict:
    """
    Request + RequestItem verisini karşılaştırılabilir normalize bir snapshot olarak döndürür.
    """
    items = []
    for item in req.items.order_by(RequestItem.id.asc()).all():
        items.append({
            'name': item.name or '',
            'component_id': item.component_id,
            'product_category': item.product_category or '',
            'product_type': item.product_type or '',
            'product_description': item.product_description or '',
            'tags': item.tags or '',
            'quantity': item.quantity or 0,
            'purchase_link': item.purchase_link or '',
            'unit_price': item.unit_price,
            'total_price': item.total_price
        })

    return {
        'req_type': req.req_type or '',
        'name': req.name or '',
        'description': req.description or '',
        'component_id': req.component_id,
        'serial_number': req.serial_number or '',
        'product_category': req.product_category or '',
        'product_type': req.product_type or '',
        'product_description': req.product_description or '',
        'tags': req.tags or '',
        'quantity': req.quantity or 0,
        'purchase_link': req.purchase_link or '',
        'unit_price': req.unit_price,
        'total_price': req.total_price,
        'budget': req.budget or '',
        'items': items
    }


def create_request_revision(req, submitted_by=None, status_at_submit=None):
    """
    İstek için yeni revision/snapshot kaydı oluşturur.
    """
    if not req or not req.id:
        return

    current_max = db.session.query(func.max(RequestRevision.revision_no)).filter(
        RequestRevision.request_id == req.id
    ).scalar() or 0

    revision = RequestRevision(
        request_id=req.id,
        revision_no=current_max + 1,
        submitted_by=submitted_by,
        status_at_submit=status_at_submit or (req.req_status or 'beklemede'),
        snapshot=build_request_snapshot(req)
    )
    db.session.add(revision)


def _normalize_snapshot_value(value):
    if value is None:
        return ''
    return value


def build_snapshot_diff(old_snapshot: dict, new_snapshot: dict) -> dict:
    """
    İki revision snapshot'u arasındaki alan ve item farklarını hesaplar.
    """
    old_snapshot = old_snapshot or {}
    new_snapshot = new_snapshot or {}

    field_labels = {
        'req_type': 'İstek Türü',
        'name': 'Ürün/İstek Adı',
        'description': 'Açıklama',
        'component_id': 'Bileşen ID',
        'serial_number': 'Seri Numarası',
        'product_category': 'Kategori',
        'product_type': 'Ürün Türü',
        'product_description': 'Ürün Açıklaması',
        'tags': 'Etiketler',
        'quantity': 'Adet',
        'purchase_link': 'Satın Alma Linki',
        'unit_price': 'Birim Fiyat',
        'total_price': 'Toplam Fiyat',
        'budget': 'Bütçe'
    }

    changed_fields = []
    for key, label in field_labels.items():
        old_val = _normalize_snapshot_value(old_snapshot.get(key))
        new_val = _normalize_snapshot_value(new_snapshot.get(key))
        if old_val != new_val:
            changed_fields.append({
                'field': key,
                'label': label,
                'old': old_val,
                'new': new_val
            })

    old_items = old_snapshot.get('items') or []
    new_items = new_snapshot.get('items') or []
    max_common = min(len(old_items), len(new_items))

    updated_items = []
    item_field_labels = {
        'name': 'Ürün',
        'component_id': 'Bileşen ID',
        'product_category': 'Kategori',
        'product_type': 'Tür',
        'product_description': 'Açıklama',
        'tags': 'Etiketler',
        'quantity': 'Adet',
        'purchase_link': 'Link',
        'unit_price': 'Birim Fiyat',
        'total_price': 'Toplam'
    }

    for idx in range(max_common):
        old_item = old_items[idx] or {}
        new_item = new_items[idx] or {}
        changes = []
        for key, label in item_field_labels.items():
            old_val = _normalize_snapshot_value(old_item.get(key))
            new_val = _normalize_snapshot_value(new_item.get(key))
            if old_val != new_val:
                changes.append({
                    'field': key,
                    'label': label,
                    'old': old_val,
                    'new': new_val
                })
        if changes:
            updated_items.append({
                'index': idx + 1,
                'old_name': old_item.get('name') or '-',
                'new_name': new_item.get('name') or '-',
                'changes': changes
            })

    removed_items = old_items[max_common:] if len(old_items) > max_common else []
    added_items = new_items[max_common:] if len(new_items) > max_common else []

    return {
        'changed_fields': changed_fields,
        'updated_items': updated_items,
        'removed_items': removed_items,
        'added_items': added_items,
        'has_changes': bool(changed_fields or updated_items or removed_items or added_items)
    }


def build_request_revision_diffs(requests_list):
    """
    Admin ekranı için tüm revision çiftlerinin diff listesini üretir.
    """
    diff_map = {}

    for req in requests_list:
        revisions = sorted(req.revisions or [], key=lambda r: (r.revision_no, r.id))
        entries = []
        for i in range(1, len(revisions)):
            old_rev = revisions[i - 1]
            new_rev = revisions[i]
            diff = build_snapshot_diff(old_rev.snapshot or {}, new_rev.snapshot or {})
            entries.append({
                'from_revision': old_rev.revision_no,
                'to_revision': new_rev.revision_no,
                'from_status': old_rev.status_at_submit,
                'to_status': new_rev.status_at_submit,
                'from_submitted_at': old_rev.submitted_at,
                'to_submitted_at': new_rev.submitted_at,
                'diff': diff
            })
        diff_map[req.id] = entries

    return diff_map


def build_request_return_url(default_endpoint: str, req_id: int):
    """
    Mesaj sonrası dönüş URL'sini güvenli şekilde oluşturur.
    Kullanıcı girdisine bağlı yönlendirme yapılmaz.
    """
    return f"{url_for(default_endpoint)}#request-{req_id}"


def is_allowed_request_message_file(filename: str) -> bool:
    if not filename or '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in REQUEST_MESSAGE_ALLOWED_EXTENSIONS


def save_request_message_attachment(file_storage):
    """
    RequestMessage eklentisini doğrular ve static/uploads/request_messages altına kaydeder.
    """
    if not file_storage or not file_storage.filename:
        return None

    original_name = secure_filename(file_storage.filename)
    if not original_name:
        return None
    if not is_allowed_request_message_file(original_name):
        raise ValueError("Desteklenmeyen dosya uzantısı.")

    os.makedirs(REQUEST_MESSAGE_UPLOAD_DIR, exist_ok=True)
    unique_name = f"{uuid.uuid4().hex}_{original_name}"
    abs_path = os.path.join(REQUEST_MESSAGE_UPLOAD_DIR, unique_name)
    file_storage.save(abs_path)

    rel_static_path = os.path.join('uploads', 'request_messages', unique_name).replace('\\', '/')
    return {
        'path': rel_static_path,
        'name': original_name,
        'mime': getattr(file_storage, 'mimetype', None)
    }


def validate_request_message_content(message_body: str, has_attachment: bool) -> str | None:
    if not message_body and not has_attachment:
        return 'Mesaj veya dosya eklemelisiniz.'
    if len(message_body) > REQUEST_MESSAGE_MAX_LENGTH:
        return f'Mesaj en fazla {REQUEST_MESSAGE_MAX_LENGTH} karakter olabilir.'
    return None

def get_locations() -> list[str]:
    """
    Component ve BorrowLog tablolarından konumları alır,
    boş değerleri temizler, tekilleştirir ve sıralar.
    """
    try:
        # İki sorguyu UNION ile birleştirerek tek bir veritabanı çağrısı yap
        q1 = db.session.query(Component.location).filter(Component.location.isnot(None))
        q2 = db.session.query(BorrowLog.location).filter(BorrowLog.location.isnot(None))
        
        # UNION, otomatik olarak distinct sonuçlar döndürür
        all_locations = q1.union(q2).all()
        
        loc_set = {loc[0].strip() for loc in all_locations if loc[0] and loc[0].strip()}
        return sorted(loc_set)

    except Exception as e:
        logging.exception("Location fetch error")
        return []


def utility_processor():
    """
    Tüm şablonlara (template) ortak değişkenleri göndermek için kullanılır.
    Örneğin, tüm konumları, temel URL'yi ve logo URL'sini buradan sağlar.
    """
    base_url = os.environ.get('BASE_URL')
    logo_url = os.environ.get('LOGO_URL')
    locations = get_locations()

    return dict(
        is_fixed_asset=is_fixed_asset,
        locations=locations,
        BASE_URL=base_url,
        LOGO_URL=logo_url,
    )

# ==============================================================================
# FLASK UYGULAMA KURULUMU VE YAPILANDIRMASI
# ==============================================================================
app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'CHANGE_ME_DEV_ONLY')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(os.path.abspath(os.path.dirname(__file__)), 'instance', 'database.db')
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Fotoğraf yükleme ayarları
DEFAULT_UPLOAD = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'static', 'uploads')
# Ortam değişkenleri ile yükleme klasörünü ve maksimum dosya boyutunu esnek bir şekilde ayarlama imkanı
UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', DEFAULT_UPLOAD)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
# MAX_CONTENT_LENGTH, byte cinsinden ayarlanabilir, varsayılan 2MB
try:
    app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH', 2 * 1024 * 1024))
except ValueError:
    app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024

# Veritabanı ve migrate ayarları
db.init_app(app)
migrate = Migrate(app, db) # Veritabanı şema göçleri için

# CSRF (Cross-Site Request Forgery) koruması
csrf = CSRFProtect()
csrf.init_app(app)

# Geliştirme ortamında unutulmaması gereken SECRET_KEY için uyarı
try:
    if not app.debug and app.secret_key in (None, '', 'CHANGE_ME_DEV_ONLY'):
        # Servis loglarında operatörlerin görebilmesi için standart hataya (stderr) net bir uyarı yazdır.
        print("WARNING: SECRET_KEY is not set or using the development placeholder. Set SECRET_KEY via environment in production.", file=sys.stderr)
except Exception:
    pass

try:

    # SQLite için daha iyi performans ve eşzamanlılık ayarları
    @event.listens_for(db.engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        try:
            cursor = dbapi_connection.cursor()
            # Use WAL journal mode for better concurrency
            cursor.execute('PRAGMA journal_mode = WAL;')
            # Set busy timeout to 30 seconds so writers wait briefly instead of failing immediately
            cursor.execute('PRAGMA busy_timeout = 30000;')
            cursor.close()
        except Exception:
            # If anything goes wrong (e.g., not SQLite), skip silently
            pass
except Exception:
    pass

def ensure_request_schema_columns():
    """Eksik istek sütunlarını geriye uyumlu şekilde ekler."""
    try:
        with app.app_context():
            req_cols = {
                row[1]
                for row in db.session.execute(text("PRAGMA table_info('request')")).fetchall()
            }
            if 'project_number' not in req_cols:
                db.session.execute(text("ALTER TABLE request ADD COLUMN project_number VARCHAR(120)"))
            if 'requires_wet_signature' not in req_cols:
                db.session.execute(text("ALTER TABLE request ADD COLUMN requires_wet_signature BOOLEAN DEFAULT 0"))

            req_item_cols = {
                row[1]
                for row in db.session.execute(text("PRAGMA table_info('request_item')")).fetchall()
            }
            if 'requires_wet_signature' not in req_item_cols:
                db.session.execute(text("ALTER TABLE request_item ADD COLUMN requires_wet_signature BOOLEAN DEFAULT 0"))

            db.session.commit()
    except Exception:
        db.session.rollback()

ensure_request_schema_columns()

# ==============================================================================
# FLASK-LOGIN YAPILANDIRMASI
# ==============================================================================
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    """Kullanıcı ID'sine göre kullanıcıyı veritabanından yükler."""
    return User.query.get(int(user_id))

# ==============================================================================
# TEMPLATE (ŞABLON) YARDIMCILARI VE CONTEXT PROCESSOR'LAR
# ==============================================================================

@app.context_processor
def inject_csrf_token():
    """Tüm formlara CSRF token'ı ekler."""
    return dict(csrf_token=generate_csrf())

@app.context_processor
def inject_timezone():
    """Tarih/saat gösterimleri için zaman dilimini şablonlara ekler."""
    return dict(tz=ZoneInfo("Europe/Istanbul"))

@app.context_processor
def inject_datetime():
    """Template'lerde datetime.now() kullanabilmek için."""
    from datetime import datetime
    return dict(now=datetime.now)

# `utility_processor` fonksiyonunu context processor olarak kaydet
app.context_processor(utility_processor)

@app.template_filter('get_user_by_username')
def get_user_by_username(username):
    """
    Kullanıcı adına göre User nesnesini döndüren bir template filtresi.
    """
    return User.query.filter_by(username=username).first()


@app.template_filter('tr_date')
def tr_date(value, with_time=False):
    """Tarihleri Türkçe formatlar ve relatif zaman ekler."""
    if not value:
        return ''

    month_names = {
        1: 'Ocak', 2: 'Subat', 3: 'Mart', 4: 'Nisan', 5: 'Mayis', 6: 'Haziran',
        7: 'Temmuz', 8: 'Agustos', 9: 'Eylul', 10: 'Ekim', 11: 'Kasim', 12: 'Aralik'
    }
    month_name = month_names.get(value.month, '')

    now_utc = datetime.now(ZoneInfo("UTC")).replace(tzinfo=None)
    dt_value = value
    if getattr(dt_value, "tzinfo", None):
        try:
            dt_value = dt_value.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        except Exception:
            dt_value = dt_value.replace(tzinfo=None)

    delta_seconds = int((now_utc - dt_value).total_seconds())
    future = delta_seconds < 0
    delta_seconds = abs(delta_seconds)

    def rel_suffix(txt: str) -> str:
        return f"{txt} sonra" if future else f"{txt} önce"

    if delta_seconds < 60:
        relative = "birazdan" if future else "az önce"
    elif delta_seconds < 3600:
        minutes = delta_seconds // 60
        relative = rel_suffix(f"{minutes} dakika")
    elif delta_seconds < 86400:
        hours = delta_seconds // 3600
        relative = rel_suffix(f"{hours} saat")
    elif delta_seconds < 86400 * 7:
        days = delta_seconds // 86400
        relative = rel_suffix(f"{days} gün")
    elif delta_seconds < 86400 * 30:
        weeks = max(1, delta_seconds // (86400 * 7))
        relative = rel_suffix(f"{weeks} hafta")
    elif delta_seconds < 86400 * 365:
        earlier, later = (dt_value, now_utc) if dt_value <= now_utc else (now_utc, dt_value)
        months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
        if later.day < earlier.day:
            months -= 1
        months = max(1, months)
        relative = rel_suffix(f"{months} ay")
    else:
        earlier, later = (dt_value, now_utc) if dt_value <= now_utc else (now_utc, dt_value)
        months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
        if later.day < earlier.day:
            months -= 1
        years = max(1, months // 12)
        relative = rel_suffix(f"{years} yıl")

    if with_time:
        absolute = f"{value.day:02d} {month_name} {value.year} {value:%H:%M}"
    else:
        absolute = f"{value.day:02d} {month_name} {value.year}"
    return f"{absolute} ({relative})"

# ==============================================================================
# ANA SAYFA VE KULLANICI İŞLEMLERİ (GİRİŞ, ÇIKIŞ, ŞİFRE DEĞİŞTİRME)
# ==============================================================================
@app.route('/')
@login_required
def index():
    """Ana sayfayı oluşturur. Arama ve kategori filtreleme işlevlerini içerir."""
    search_query = request.args.get('q', '').strip()
    selected_category = request.args.get('category', '').strip()

    # Tüm kategoriler (benzersiz, boş olmayan, sıralı)
    raw_cats = db.session.query(Component.category).distinct().all()
    all_categories = sorted({c[0] for c in raw_cats if c and c[0]})

    # Temel sorgu
    query = Component.query.filter_by(is_deleted=False)

    # Arama filtresi
    if search_query:
        q = f"%{search_query}%"
        query = query.filter(
            or_(
                Component.name.ilike(q),
                Component.type.ilike(q),
                Component.code.ilike(q)
            )
        )

    # Kategori filtresi
    if selected_category:
        query = query.filter(func.lower(Component.category) == selected_category.lower())

    # Bileşenleri veritabanından al
    components = query.order_by(func.coalesce(Component.type, 'ZZ'), Component.name).all()

    # Gruplamayı Python tarafında yap
    grouped_components = defaultdict(list)
    grouped_imaged_components = defaultdict(list)
    grouped_nullIMG_components = defaultdict(list)
    
    # Bileşenleri türe ve gerçek bir resme sahip olup olmadıklarına göre grupla
    for comp in components:
        type_key = comp.type or 'Diğer'
        grouped_components[type_key].append(comp)
        img = (comp.image_url or '').strip()
        if img and 'placeholder' not in img and not img.lower().endswith('no-image'):
            grouped_imaged_components[type_key].append(comp)
        else:
            grouped_nullIMG_components[type_key].append(comp)
    all_types = sorted(grouped_components.keys())

    return render_template(
        'index.html',
        all_categories=all_categories,
        grouped_components=grouped_components,
        grouped_imaged_components=grouped_imaged_components,
        grouped_nullIMG_components=grouped_nullIMG_components,
        all_types=all_types,
        selected_category=selected_category if selected_category else None,
        search_query=search_query
    )

@app.route('/api/components/image-search')
@login_required
def component_image_search():
    """Mevcut envanter görsellerinde dinamik ürün araması."""
    q = request.args.get('q', '').strip()
    category = request.args.get('category', '').strip()
    comp_type = request.args.get('type', '').strip()
    limit = min(max(request.args.get('limit', 24, type=int), 1), 60)

    query = Component.query.filter(Component.is_deleted == False)  # noqa: E712
    if q:
        pattern = f"%{q}%"
        query = query.filter(
            or_(
                Component.name.ilike(pattern),
                Component.type.ilike(pattern),
                Component.category.ilike(pattern)
            )
        )
    if category:
        query = query.filter(Component.category == category)
    if comp_type:
        query = query.filter(Component.type == comp_type)

    components = query.order_by(Component.name.asc()).limit(limit).all()
    return jsonify({
        "items": [
            {
                "id": comp.id,
                "name": comp.name,
                "category": comp.category or 'Diğer',
                "type": comp.type or 'Diğer',
                "image_url": (comp.image_url or '').strip()
            }
            for comp in components
        ]
    })


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Kullanıcı giriş sayfasını yönetir."""
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        # If LDAP auth is enabled, try LDAP first
        if USE_LDAP_AUTH and username and password:
            try:
                def check_ldap_group_membership(usern, user_dn):
                    """Check if user is member of admin group.
                    
                    Returns True if user is in admin group, False otherwise.
                    """
                    if not LDAP_ADMIN_GROUP:
                        return False
                    
                    try:
                        server = Server(LDAP_HOST, port=LDAP_PORT, use_ssl=LDAP_USE_SSL, get_info=ALL)
                        
                        # Connect with search credentials
                        if LDAP_SEARCH_BIND_DN and LDAP_SEARCH_BIND_PASS:
                            conn = Connection(server, user=LDAP_SEARCH_BIND_DN, password=LDAP_SEARCH_BIND_PASS, auto_bind=True)
                        else:
                            conn = Connection(server, auto_bind=True)
                        
                        # Determine group base DN
                        group_base = LDAP_GROUP_BASE_DN if LDAP_GROUP_BASE_DN else LDAP_BASE_DN
                        
                        # Build search filter based on member attribute type
                        if LDAP_GROUP_MEMBER_ATTR == 'memberUid':
                            # For posixGroup - memberUid contains just username
                            search_filter = f'(&(objectClass=*)(cn={LDAP_ADMIN_GROUP})(memberUid={usern}))'
                        elif LDAP_GROUP_MEMBER_ATTR in ('member', 'uniqueMember'):
                            # For groupOfNames/groupOfUniqueNames - member contains full DN
                            search_filter = f'(&(objectClass=*)({LDAP_GROUP_MEMBER_ATTR}={user_dn}))'
                        else:
                            # Generic fallback
                            search_filter = f'(&(objectClass=*)({LDAP_GROUP_MEMBER_ATTR}={usern}))'
                        
                        # If LDAP_ADMIN_GROUP is a full DN, search it directly
                        if '=' in LDAP_ADMIN_GROUP and ',' in LDAP_ADMIN_GROUP:
                            # It's a DN, search for it
                            if LDAP_GROUP_MEMBER_ATTR == 'memberUid':
                                search_filter = f'(memberUid={usern})'
                            else:
                                search_filter = f'({LDAP_GROUP_MEMBER_ATTR}={user_dn})'
                            conn.search(search_base=LDAP_ADMIN_GROUP, search_filter=search_filter, search_scope='BASE', attributes=['cn'])
                        else:
                            # It's just a group name, search under group base
                            conn.search(search_base=group_base, search_filter=search_filter, search_scope=SUBTREE, attributes=['cn'])
                        
                        is_member = len(conn.entries) > 0
                        conn.unbind()
                        return is_member
                        
                    except Exception as e:
                        logging.exception(f"LDAP group check error: {e}")
                        return False
                
                def ldap_authenticate(usern, pwd):
                    """Try to authenticate against LDAP.

                    Returns a tuple (ok: bool, user_dn: str or None, attrs: dict).
                    """
                    server = Server(LDAP_HOST, port=LDAP_PORT, use_ssl=LDAP_USE_SSL, get_info=ALL)

                    # If a search bind is configured, use it to find the user's DN.
                    search_conn = None
                    try:
                        if LDAP_SEARCH_BIND_DN and LDAP_SEARCH_BIND_PASS:
                            search_conn = Connection(server, user=LDAP_SEARCH_BIND_DN, password=LDAP_SEARCH_BIND_PASS, auto_bind=True)
                        else:
                            # anonymous bind or no pre-bind
                            search_conn = Connection(server, auto_bind=False)
                    except Exception:
                        search_conn = None

                    user_dn = None
                    found_attrs = {}
                    try:
                        if search_conn and LDAP_BASE_DN:
                            # search for the user to get DN
                            search_filter = f'({LDAP_USER_ATTR}={usern})'
                            if not search_conn.bound:
                                try:
                                    search_conn.open()
                                except Exception:
                                    pass
                            try:
                                search_conn.search(search_base=LDAP_BASE_DN, search_filter=search_filter, search_scope=SUBTREE, attributes=['*'])
                                if search_conn.entries:
                                    entry = search_conn.entries[0]
                                    user_dn = entry.entry_dn
                                    # convert attributes to dict of simple values
                                    for k in entry.entry_attributes_as_dict:
                                        found_attrs[k] = entry.entry_attributes_as_dict[k]
                            except Exception:
                                # search failed; we'll try a direct DN bind fallback below
                                pass

                    finally:
                        try:
                            if search_conn:
                                search_conn.unbind()
                        except Exception:
                            pass

                    # If we didn't find a DN via search, try a common DN pattern if base is provided
                    if not user_dn and LDAP_BASE_DN:
                        # Try e.g. uid=username,base or cn=username,base
                        possible_dns = [f'{LDAP_USER_ATTR}={usern},{LDAP_BASE_DN}', f'cn={usern},{LDAP_BASE_DN}']
                        for dn_try in possible_dns:
                            try_conn = Connection(server, user=dn_try, password=pwd, auto_bind=True)
                            if try_conn.bound:
                                try_conn.unbind()
                                return True, dn_try, {}
                    # If we have a DN from search, try binding with that DN and the provided password
                    if user_dn:
                        try:
                            user_conn = Connection(server, user=user_dn, password=pwd, auto_bind=True)
                            if user_conn.bound:
                                # success
                                try:
                                    user_conn.unbind()
                                except Exception:
                                    pass
                                return True, user_dn, found_attrs
                        except Exception:
                            return False, None, {}

                    return False, None, {}

                ok, user_dn, ldap_attrs = ldap_authenticate(username, password)
            except Exception as e:
                ok = False
                user_dn = None
                ldap_attrs = {}

            if ok:
                # Check LDAP group membership for role assignment
                is_ldap_admin = check_ldap_group_membership(username, user_dn)
                
                # Create local user record if missing (password_hash left null for LDAP users)
                user = User.query.filter_by(username=username).first()
                if not user:
                    # Create local user record marking it as LDAP-managed
                    user = User(username=username, is_ldap=True)
                    # Set role based on LDAP group membership
                    user.role = 'admin' if is_ldap_admin else 'user'
                    # Optional: populate email or full name if available
                    try:
                        if 'mail' in ldap_attrs and ldap_attrs.get('mail'):
                            # ldap_attrs values may be lists
                            mail_val = ldap_attrs.get('mail')
                            if isinstance(mail_val, (list, tuple)):
                                mail_val = mail_val[0]
                            # If your User model has an email field, set it here.
                            # Example: user.email = mail_val
                    except Exception:
                        pass
                    db.session.add(user)
                    db.session.commit()
                else:
                    # Update existing LDAP user's role based on current group membership
                    if user.is_ldap:
                        new_role = 'admin' if is_ldap_admin else 'user'
                        if user.role != new_role:
                            user.role = new_role
                            db.session.commit()

                login_user(user)
                return redirect(url_for('index'))

        # Fallback: local DB authentication
        user = User.query.filter_by(username=request.form['username']).first()
        if user and user.check_password(request.form['password']):
            login_user(user)
            return redirect(url_for('index'))
        flash("Geçersiz kullanıcı adı veya şifre")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    """Kullanıcı çıkış işlemini yapar."""
    logout_user()
    return redirect(url_for('login'))

@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    """Mevcut kullanıcının şifresini değiştirmesini sağlar."""
    if request.method == 'POST':
        old_password = request.form.get('old_password')
        new_password = request.form.get('new_password')
        new_password2 = request.form.get('new_password2')

        # If the user is LDAP-managed, prevent local password changes here.
        # (Optional: implement LDAP password change separately if desired.)
        try:
            if getattr(current_user, 'is_ldap', False):
                flash('Bu hesap LDAP tarafından yönetildiği için yerel şifre değişikliği desteklenmiyor.', 'warning')
                return redirect(url_for('index'))
        except Exception:
            # If current_user doesn't have attribute, fall back to normal behavior
            pass

        if not current_user.check_password(old_password):
            flash("Mevcut şifreniz yanlış.", "danger")

        elif not new_password or new_password != new_password2:
            flash("Yeni şifreler eşleşmiyor veya boş.", "danger")

        else:
            current_user.set_password(new_password)
            db.session.commit()
            flash("Şifreniz başarıyla güncellendi.", "success")
            return redirect(url_for('index'))

    return render_template('change_password.html')

# ==============================================================================
# BİLEŞEN (COMPONENT) YÖNETİMİ (CRUD - EKLEME, GÖRÜNTÜLEME, DÜZENLEME, SİLME)
# ==============================================================================

@app.route('/admin/users/add', methods=['GET', 'POST'])
@login_required
def add_user():
    if not current_user.is_admin():
        flash("Bu sayfaya erişim yetkiniz yok!")
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role', 'user')  # default user
        can_add_product = bool(request.form.get('can_add_product'))

        if not username or not password:
            flash("Kullanıcı adı ve şifre zorunludur.")
            return redirect(url_for('add_user'))

        if User.query.filter_by(username=username).first():
            flash("Bu kullanıcı adı zaten var.")
            return redirect(url_for('add_user'))

        new_user = User(username=username, password=password, role=role, can_add_product=can_add_product)
        db.session.add(new_user)
        db.session.commit()

        flash(f"{username} başarıyla eklendi.")
        return redirect(url_for('manage_users'))

    return render_template('admin/add_user.html')

@app.route('/components')
@login_required
def component_list():
    """Tüm bileşenleri listeleyen sayfa. Sıralama işlevselliği içerir."""
    # URL'den sıralama parametrelerini al
    sort_by = request.args.get('sort', 'name')  # Varsayılan: isme göre sıralama
    sort_direction = request.args.get('direction', 'asc')  # Varsayılan: A->Z
    
    # Sıralama kriterini belirle
    if sort_by == 'name':
        order_by = Component.name
    elif sort_by == 'type':
        order_by = Component.type
    elif sort_by == 'quantity':
        order_by = Component.quantity
    else:
        order_by = Component.name
    
    # Sıralama yönünü uygula
    if sort_direction == 'desc':
        order_by = order_by.desc()
    
    # Bileşenleri sıralı şekilde getir
    components = Component.query.order_by(order_by).all()
    
    return render_template('admin/component_list.html', 
                         components=components,
                         sort_by=sort_by,
                         sort_direction=sort_direction)

def generate_component_code(type_, name, part_number):
    """Bileşenler için otomatik olarak benzersiz bir kod üretir."""
    # Türün ilk 2 harfi büyük
    type_short = (type_ or "GE")[:2].upper()

    # Ortadaki kısım: part_number varsa onu kullan, yoksa isimden üret
    if part_number:
        middle = part_number.upper()
    else:
        parts = name.split()
        if not parts:
            middle = ""
        else:
            middle = parts[0][0].upper()
            for part in parts[1:]:
                match = re.search(r'\d+', part)
                if match:
                    middle += match.group()
                else:
                    middle += part[0].upper()

    # Yarış koşullarını (race condition) önlemek için benzersiz bir sonek ekle.
    # UUID'nin ilk 8 karakterini kullanmak iyi bir denge sağlar.
    unique_suffix = uuid.uuid4().hex[:8].upper()

    return f"{type_short}-{middle}-{unique_suffix}"

def _handle_photo_upload(photo_file) -> str | None:
    """
    Handles the file upload process for a component photo.
    Generates a unique filename to prevent collisions.
    """
    if not (photo_file and photo_file.filename):
        return None

    try:
        # Generate a unique filename to prevent overwrites
        filename = secure_filename(photo_file.filename)
        # Use UUID to ensure the filename is absolutely unique
        ext = os.path.splitext(filename)[1]
        unique_filename = f"{uuid.uuid4().hex}{ext}"
        
        upload_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
        photo_file.save(upload_path)
        
        # url_for, alt klasörlerle her zaman doğru çalışmayabilir.
        # URL'yi manuel olarak oluşturmak daha güvenilirdir.
        return f"/static/uploads/{unique_filename}"
    except Exception as e:
        logging.exception("Error during photo upload.")
        flash("Fotoğraf yüklenirken bir hata oluştu.", "danger")
        return None


def _process_tags(tags_raw: list[str]) -> list[Tag]:
    """
    Processes a list of raw tag strings from a form, creating new tags
    if they don't exist, and returns a list of Tag objects.
    """
    processed_tags = []
    new_tag_names = set()

    for tag_item in tags_raw:
        if tag_item.startswith('new_'):
            tag_name = tag_item[4:].replace('-', ' ').strip()
            if tag_name and tag_name.lower() not in new_tag_names:
                new_tag_names.add(tag_name.lower())
                # Check if tag already exists in DB
                existing_tag = Tag.query.filter(func.lower(Tag.name) == tag_name.lower()).first()
                if not existing_tag:
                    new_tag = Tag(name=tag_name)
                    db.session.add(new_tag)
                    db.session.flush()  # Ensure the new tag gets an ID immediately
                    processed_tags.append(new_tag)
                else:
                    processed_tags.append(existing_tag)
        elif tag_item.isdigit():
            tag = Tag.query.get(int(tag_item))
            if tag:
                processed_tags.append(tag)
    return processed_tags

@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_component():
    """Yeni bir bileşen (ürün) eklemek için kullanılır."""
    if not current_user.has_add_permission():
        return "Bu işlemi yapmaya yetkiniz yok.", 403

    types = [t[0] for t in db.session.query(Component.type).distinct().all() if t[0]]
    existing_tags = Tag.query.order_by(Tag.name).all()
    selected_tags = []
    owner_prefixes = [i[0] for i in db.session.query(InventoryItem.serial_number).distinct().all() if i[0]]
    owner_prefixes = list({sn.split('-')[0] for sn in owner_prefixes if '-' in sn})  # Prefixleri ayıkla
    locations = [loc[0] for loc in db.session.query(Component.location).distinct().all() if loc[0]]

    # Form verileri için boş başlangıç değerleri veya URL parametrelerinden gelen değerler (istek formundan)
    form_data = {}
    from_request = request.args.get('from_request')
    request_item_id = request.args.get('request_item_id')  # Çoklu ürün içeren isteklerden gelen item ID
    
    # URL parametrelerinden gelen değerleri form_data'ya aktar (satın alma isteğinden geliyorsa)
    if request.method == 'GET' and (from_request or request_item_id):
        form_data = {
            'name': request.args.get('name', ''),
            'category': request.args.get('category', ''),
            'type': request.args.get('type', ''),
            'description': request.args.get('description', ''),
            'quantity': request.args.get('quantity', '1'),
        }
        # Etiketleri işle
        tags_str = request.args.get('tags', '')
        if tags_str:
            selected_tags = [t.strip() for t in tags_str.split(',') if t.strip()]
    
    if request.method == 'POST':
        try:
            # --- 1. Form Verilerini Topla ve Doğrula ---
            form_data = {
                'category': request.form.get('category'),
                'name': request.form['name'].strip(),
                'type': request.form.get('type', '').strip(),
                'location': request.form.get('location', '').strip(),
                'description': request.form.get('description', '').strip(),
                'quantity': request.form['quantity'].strip(),
                'part_number': request.form.get('part_number', '').strip(),
                'serial_numbers_raw': request.form.get('serial_numbers', '').strip(),
                'owner_prefix': request.form.get('owner_prefix', '').strip().upper(),
                'tags_raw': request.form.getlist('tags[]')
            }

            if not (form_data['quantity'].isdigit() and int(form_data['quantity']) >= 0):
                flash("Geçerli pozitif bir miktar giriniz.", "danger")
                return render_template('add.html', types=types, existing_tags=existing_tags, selected_tags=form_data.get('tags_raw', []), owner_prefixes=owner_prefixes, locations=locations, form_data=form_data)
            
            quantity_int = int(form_data['quantity'])

            # --- 2. Seri Numaralarını İşle ---
            serial_numbers = []
            if is_fixed_asset(form_data['category']):
                if not form_data['owner_prefix']:
                    flash("Demirbaşlar için sahiplik kısaltması (prefix) zorunludur.", "danger")
                    return render_template('add.html', types=types, existing_tags=existing_tags, selected_tags=form_data.get('tags_raw', []), owner_prefixes=owner_prefixes, locations=locations, form_data=form_data)

                serial_numbers = [f"{form_data['owner_prefix']}-{sn.strip()}" for sn in form_data['serial_numbers_raw'].split(',') if sn.strip()]
                if len(serial_numbers) != quantity_int:
                    flash("Seri numarası sayısı ile miktar eşleşmeli.", "danger")
                    return render_template('add.html', types=types, existing_tags=existing_tags, selected_tags=form_data.get('tags_raw', []), owner_prefixes=owner_prefixes, locations=locations, form_data=form_data)

            # --- 3. Veritabanı İşlemlerini Başlat ---
            image_url = _handle_photo_upload(request.files.get('photo'))
            
            component = Component(
                name=form_data['name'],
                category=form_data['category'],
                type=form_data['type'],
                location=form_data['location'],
                description=form_data['description'],
                quantity=quantity_int,
                image_url=image_url,
                part_number=form_data['part_number'],
                code=generate_component_code(form_data['type'], form_data['name'], form_data['part_number'])
            )

            component.tags = _process_tags(form_data['tags_raw'])
            db.session.add(component)
            db.session.flush()  # Component ID'si almak için

            if is_fixed_asset(form_data['category']):
                for sn in serial_numbers:
                    if InventoryItem.query.filter_by(serial_number=sn).first():
                        raise ValueError(f"Bu seri numarası zaten var: {sn}")
                    item = InventoryItem(component_id=component.id, serial_number=sn)
                    db.session.add(item)

            # İstek kalemi varsa, envantere eklendiğini işaretle
            request_item_id_post = request.form.get('request_item_id')
            from_request_post = request.form.get('from_request')
            
            if request_item_id_post:
                req_item = RequestItem.query.get(int(request_item_id_post))
                if req_item:
                    req_item.added_to_inventory = True
                    req_item.added_component_id = component.id
            elif from_request_post:
                # Eski tek ürünlü istek durumu için Request modelini güncelle
                req = Request.query.get(int(from_request_post))
                if req:
                    req.added_to_inventory = True
                    req.added_component_id = component.id

            db.session.commit()
            flash(f"{component.name} bileşeni eklendi. Kod: {component.code}", "success")
            return redirect(url_for('index'))

        except ValueError as ve:
            db.session.rollback()
            flash(str(ve), "danger")
            return render_template('add.html', types=types, existing_tags=existing_tags, selected_tags=form_data.get('tags_raw', []), owner_prefixes=owner_prefixes, locations=locations, form_data=form_data)
        except Exception as e:
            db.session.rollback()
            logging.exception("Component eklenirken bir hata oluştu.")
            flash("Bileşen eklenirken beklenmedik bir hata oluştu.", "danger")
            return render_template('add.html', types=types, existing_tags=existing_tags, selected_tags=form_data.get('tags_raw', []), owner_prefixes=owner_prefixes, locations=locations, form_data=form_data)

    return render_template('add.html', types=types, existing_tags=existing_tags, selected_tags=selected_tags, owner_prefixes=owner_prefixes, locations=locations, form_data=form_data)


@app.route('/component/<int:comp_id>')
@login_required
def component_detail(comp_id):
    """Belirli bir bileşenin detaylarını, ödünç alma geçmişini ve envanterini gösterir."""
    # N+1 problemini önlemek için ilişkili verileri eager load ile yükle
    component = Component.query.options(
        selectinload(Component.tags),
        selectinload(Component.inventory_items),
        selectinload(Component.borrow_logs).joinedload(BorrowLog.user)
    ).get_or_404(comp_id)

    logs = sorted(component.borrow_logs, key=lambda log: log.timestamp)

    open_borrows = []

    for log in logs:
        if log.action == 'borrow':
            open_borrows.append({
                'user_id': log.user_id,
                'user_name': log.user.username if log.user else 'Bilinmiyor',
                'borrow_date': log.timestamp,
                'amount': log.amount,
                'return_date': None,
                'location': log.location or '-',
                'serial_number': log.serial_number or '-',  # Seri numarasını göster
                'notes': log.notes  # Notları ekle
            })
        elif log.action == 'return':
            amount_to_return = log.amount
            for borrow in open_borrows:
                if borrow['user_id'] == log.user_id and borrow['return_date'] is None:
                    if amount_to_return >= borrow['amount']:
                        borrow['return_date'] = log.timestamp
                        amount_to_return -= borrow['amount']
                    else:
                        remaining = borrow['amount'] - amount_to_return
                        borrow['amount'] = amount_to_return
                        borrow['return_date'] = log.timestamp
                        open_borrows.append({
                            'user_id': borrow['user_id'],
                            'user_name': borrow['user_name'],
                            'borrow_date': borrow['borrow_date'],
                            'amount': remaining,
                            'return_date': None,
                            'location': borrow['location'],
                            'serial_number': borrow.get('serial_number', '-'),  # devamı
                            'notes': borrow.get('notes') # Notları koru
                        })
                        amount_to_return = 0
                    if amount_to_return == 0:
                        break

    borrow_history = sorted(open_borrows, key=lambda x: x['borrow_date'], reverse=True)

    consume_logs = BorrowLog.query.filter_by(comp_id=comp_id, action='consume').order_by(BorrowLog.timestamp.desc()).all()
    consume_history = [{
        'user_name': rec.user.username if rec.user else 'Bilinmiyor',
        'timestamp': rec.timestamp,
        'amount': rec.amount,
        'notes': rec.notes  # Notları ekle
    } for rec in consume_logs]

    return render_template('component_detail.html',
                           component=component,
                           borrow_history=borrow_history,
                           consume_history=consume_history,
                           inventory_items=component.inventory_items)  # Listeyi template'e gönder

@app.route('/component/<int:comp_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_component(comp_id):
    """Mevcut bir bileşenin bilgilerini düzenler, fotoğraf, stok ve seri numarası yönetimi içerir."""
    if not current_user.is_admin():
        abort(403)

    # İlgili tüm verileri tek seferde yükle (N+1 problemini önle)
    component = Component.query.options(
        selectinload(Component.tags),
        selectinload(Component.inventory_items)
    ).get_or_404(comp_id)

    if request.method == 'POST':
        try:
            # --- 1. Form Verilerini Topla ---
            original_category = component.category
            new_category = request.form.get('category')
            
            component.name = request.form.get('name').strip()
            component.description = request.form.get('description', '').strip()
            component.category = new_category

            # Handle 'new_' prefix for types, if the UI sends it for new types
            raw_type = request.form.get('type', '').strip()
            if raw_type.startswith('new_'):
                component.type = raw_type[4:].replace('-', ' ').strip()
            else:
                component.type = raw_type

            component.part_number = request.form.get('part_number', '').strip()
            component.location = request.form.get('location', '').strip()

            # --- 2. Fotoğraf Güncelleme ---
            if 'photo' in request.files and request.files['photo'].filename:
                # Eski fotoğrafı sil
                if component.image_url:
                    try:
                        old_photo_name = os.path.basename(component.image_url)
                        old_photo_path = os.path.join(app.config['UPLOAD_FOLDER'], old_photo_name)
                        if os.path.exists(old_photo_path):
                            os.remove(old_photo_path)
                    except Exception as e:
                        logging.warning(f"Eski fotoğraf silinemedi: {e}")
                
                # Yeni fotoğrafı yükle
                component.image_url = _handle_photo_upload(request.files['photo'])

            # --- 3. Etiketleri İşle ---
            tags_raw = request.form.getlist('tags[]')
            component.tags = _process_tags(tags_raw)

            # --- 4. Stok ve Seri Numaralarını Yönet ---
            new_quantity_str = request.form.get('quantity', '0').strip()
            if not new_quantity_str.isdigit():
                raise ValueError("Geçersiz miktar değeri.")
            new_quantity = int(new_quantity_str)

            if is_fixed_asset(new_category):
                serial_numbers_raw = request.form.get('serial_numbers', '').strip()
                new_serials = {sn.strip() for sn in serial_numbers_raw.split(',') if sn.strip()}
                
                # Miktarı seri numarası sayısına eşitle
                component.quantity = len(new_serials)

                existing_serials = {item.serial_number for item in component.inventory_items}
                
                # Silinecek seri numaralarını bul
                serials_to_delete = existing_serials - new_serials
                for sn in serials_to_delete:
                    item_to_delete = next((item for item in component.inventory_items if item.serial_number == sn), None)
                    if item_to_delete:
                        if item_to_delete.assigned_to:
                            raise ValueError(f"Seri numarası '{sn}' şu anda bir kullanıcıya atanmış ve silinemez.")
                        db.session.delete(item_to_delete)

                # Eklenecek yeni seri numaralarını bul
                serials_to_add = new_serials - existing_serials
                for sn in serials_to_add:
                    if InventoryItem.query.filter_by(serial_number=sn).first():
                        raise ValueError(f"Seri numarası '{sn}' zaten başka bir bileşen için kayıtlı.")
                    new_item = InventoryItem(component_id=component.id, serial_number=sn)
                    db.session.add(new_item)
            else:
                # Kategori demirbaş değilse, envanterdeki tüm seri numaralarını sil
                if is_fixed_asset(original_category):
                    for item in component.inventory_items:
                        if item.assigned_to:
                            raise ValueError(f"Kategori değiştirilemez çünkü '{item.serial_number}' seri numaralı ürün bir kullanıcıya atanmış.")
                        db.session.delete(item)
                component.quantity = new_quantity

            db.session.commit()
            flash("Bileşen başarıyla güncellendi.", "success")
            return redirect(url_for('component_detail', comp_id=comp_id))

        except ValueError as ve:
            db.session.rollback()
            flash(str(ve), "danger")
        except Exception as e:
            db.session.rollback()
            logging.exception("Bileşen güncellenirken bir hata oluştu.")
            flash("Bileşen güncellenirken beklenmedik bir hata oluştu.", "danger")

    # GET request: Formu doldurmak için verileri hazırla
    types = sorted([t[0] for t in db.session.query(Component.type).distinct().all() if t[0]])
    existing_tags = Tag.query.order_by(Tag.name).all()
    selected_tags = [str(tag.id) for tag in component.tags]

    return render_template(
        'edit_component.html',
        component=component,
        types=types,
        existing_tags=existing_tags,
        selected_tags=selected_tags
    )

@app.route('/delete/<int:comp_id>', methods=['POST'])
@login_required
def delete_component(comp_id):
    """Bir bileşeni ve ilişkili tüm kayıtları (envanter, loglar) siler."""
    if not current_user.is_admin():
        return "Bu işlemi yapmaya yetkiniz yok.", 403

    component = Component.query.get_or_404(comp_id)

    # STOKT MİKTARINI SIFIRLA VE SİLİNDİ OLARAK İŞARETLE
    # InventoryItem ve BorrowLog kayıtlarını silmiyoruz, sadece ilişkiyi koparıyoruz veya olduğu gibi bırakıyoruz.
    # Kullanıcı isteği: "urunler tam olarak silinemesin sadece is_deleted booleni eklensin"
    
    # Ancak stok miktarını 0 yapmalıyız ki sistemde var gibi gözükmesin (veya low_stock'a düşmesin diye ne yapmalı?
    # Eğer low_stock'ta is_deleted kontrolü yaparsak sorun olmaz.)
    
    component.is_deleted = True
    # component.quantity = 0 # İsteğe bağlı: miktar sıfırlanabilir, ama geçmiş takibi için kalması daha iyi olabilir.
    
    db.session.commit()
    flash(f"{component.name} bileşeni silindi (Arşivlendi).")
    return redirect(url_for('index'))

@app.route('/restore/<int:comp_id>', methods=['POST'])
@login_required
def restore_component(comp_id):
    """Soft-delete edilmiş bir bileşeni geri getirir."""
    if not current_user.is_admin():
        return "Bu işlemi yapmaya yetkiniz yok.", 403

    component = Component.query.get_or_404(comp_id)

    if not component.is_deleted:
        flash(f"{component.name} zaten aktif durumda.", "warning")
        return redirect(url_for('component_list'))

    component.is_deleted = False
    db.session.commit()
    flash(f"{component.name} bileşeni başarıyla geri getirildi.")
    return redirect(url_for('component_list'))

# ==============================================================================
# STOK VE ENVANTER İŞLEMLERİ
# ==============================================================================

@app.route('/component/<int:comp_id>/update_stock', methods=['POST'])
@login_required
def update_stock(comp_id):
    """Adminlerin bileşen stoklarını artırmasını veya azaltmasını sağlar."""
    if not current_user.is_admin():
        return "Bu işlemi yapmaya yetkiniz yok.", 403
    component = Component.query.get_or_404(comp_id)
    action = request.form.get('action')
    try:
        amount = int(request.form.get('amount', 0))
    except ValueError:
        flash("Geçersiz miktar.")
        return redirect(url_for('component_detail', comp_id=comp_id))

    if amount <= 0:
        flash("Miktar pozitif olmalıdır.")
        return redirect(url_for('component_detail', comp_id=comp_id))

    serial_number = request.form.get("serial_number", "").strip()

    # For fixed-assets, a serial number is required
    if is_fixed_asset(component.category) and not serial_number:
        flash("Seri numarası gereklidir.", "danger")
        return redirect(url_for('component_detail', comp_id=comp_id))

    if action == 'increase':
        # If fixed-asset, ensure serial number uniqueness and create InventoryItem
        if is_fixed_asset(component.category):
            if InventoryItem.query.filter_by(serial_number=serial_number).first():
                flash("Bu seri numarası zaten kayıtlı.", "danger")
                return redirect(url_for('component_detail', comp_id=comp_id))
            new_item = InventoryItem(component_id=component.id, serial_number=serial_number)
            db.session.add(new_item)

        component.quantity += amount
        
        # İstek kalemi varsa, envantere eklendiğini işaretle
        request_item_id = request.form.get('request_item_id')
        from_request_id = request.form.get('from_request')
        
        if request_item_id:
            req_item = RequestItem.query.get(int(request_item_id))
            if req_item:
                req_item.added_to_inventory = True
                req_item.added_component_id = component.id
        elif from_request_id:
            # Tek ürünlü istek için Request modelini güncelle
            req = Request.query.get(int(from_request_id))
            if req:
                req.added_to_inventory = True
                req.added_component_id = component.id
        
        flash(f"{amount} adet stok eklendi.", "success")

    elif action == 'decrease':
        if component.quantity < amount:
            flash("Yeterli stok yok!", "danger")
            return redirect(url_for('component_detail', comp_id=comp_id))

        # For fixed-assets, find and delete the specific inventory item
        if is_fixed_asset(component.category):
            item = InventoryItem.query.filter_by(component_id=component.id, serial_number=serial_number).first()
            if not item:
                flash("Bu seri numarası mevcut değil.", "danger")
                return redirect(url_for('component_detail', comp_id=comp_id))
            if item.assigned_to:
                flash("Bu seri numarası şu anda bir kullanıcıya atanmış, silemezsiniz.", "danger")
                return redirect(url_for('component_detail', comp_id=comp_id))
            db.session.delete(item)

        component.quantity -= amount
        flash(f"{amount} adet stok azaltıldı.", "success")

    else:
        flash("Geçersiz işlem.")
        return redirect(url_for('component_detail', comp_id=comp_id))

    db.session.commit()
    return redirect(url_for('component_detail', comp_id=comp_id))

@app.route('/azalan_stok')
@login_required
def low_stock():
    """
    Stoku azalan ürünleri listeler.
    - Demirbaş/Gereç: Stok 0 ise gösterilir.
    - Sarf: Stok 5'in altındaysa gösterilir.
    """
    selected_category = request.args.get('category', '').strip()

    # Demirbaş ve gereç kategorileri için koşul (stok bittiğinde)
    fixed_asset_condition = Component.category.in_(['demirbas', 'demirbaş', 'gerec', 'gereç', 'gereçler', 'gerecler']) & (Component.quantity == 0)

    # Sarf malzemeleri için koşul (stok 5'in altına düştüğünde)
    consumable_condition = (Component.category == 'sarf') & (Component.quantity < 5)

    # Temel sorgu - Silinmiş ürünleri hariç tut
    query = Component.query.filter(Component.is_deleted == False).filter(or_(fixed_asset_condition, consumable_condition))

    # Get all categories for the filter buttons, based on the low_stock query
    all_categories_query = query.with_entities(Component.category).distinct().all()
    all_categories = sorted([c[0] for c in all_categories_query if c[0]])

    # Category filtresi
    if selected_category:
        query = query.filter(func.lower(Component.category) == selected_category.lower())

    low_stock_components = query.order_by(Component.quantity).all()
    
    return render_template('admin/low_stock.html', 
                           components=low_stock_components,
                           all_categories=all_categories,
                           selected_category=selected_category)

# ==============================================================================
# ÖDÜNÇ ALMA, İADE VE SARF İŞLEMLERİ
# ==============================================================================

@app.route('/process_item/<int:comp_id>', methods=['POST'])
@login_required
def process_item(comp_id):
    """
    Ödünç alma (borrow) ve sarf etme (consume) işlemlerini yöneten merkezi fonksiyon.
    Formdan gelen 'action' parametresine göre işlem yapar.
    """
    try:
        amount = int(request.form.get('amount', 1))
    except ValueError:
        flash("Geçersiz miktar.")
        return redirect(url_for('index'))

    location = request.form.get('location', '').strip()
    serial_number = request.form.get('serial_number', '').strip()
    action = request.form.get('action', 'borrow') # 'borrow' or 'consume'
    notes = request.form.get('notes', '').strip()

    # Eğer işlem yapan admin ise, işlemi başka bir kullanıcı adına yapabilir
    user_id = current_user.id
    if current_user.is_admin():
        user_id = int(request.form.get('user_id', current_user.id))

    component = Component.query.get_or_404(comp_id)

    if amount <= 0:
        flash("Miktar pozitif olmalıdır.")
        return redirect(url_for('index'))

    if component.quantity < amount:
        flash("Yeterli stok yok!")
        return redirect(url_for('index'))

    # Eğer eylem belirtilmemişse, kategoriye göre eylemi belirle
    if action not in ['borrow', 'consume']:
        if is_fixed_asset(component.category):
            action = 'borrow'
        elif component.category.lower() == 'sarf':
            action = 'consume'
        else: # Default to borrow for other types like 'gerec'
            action = 'borrow'

    # Eğer demirbaş ödünç alınıyorsa, seri numarasını kontrol et ve kullanıcıya ata
    if action == 'borrow' and is_fixed_asset(component.category):
        inventory_item = InventoryItem.query.filter_by(serial_number=serial_number, component_id=comp_id).first()
        if not inventory_item:
            flash("Geçerli bir seri numarası seçilmedi veya seri numarası zorunludur.")
            return redirect(url_for('index'))
        if inventory_item.assigned_to:
            flash(f"Bu seri numarası ({serial_number}) zaten '{inventory_item.assigned_to}' kullanıcısına atanmış.", "danger")
            return redirect(url_for('index'))
        
        # --- YENİ ---
        # Eğer ürün arızalı ise ödünç verilmesini engelle
        if inventory_item.is_defective:
            flash(f"Bu seri numaralı ürün ({serial_number}) arızalı olarak işaretlenmiş ve ödünç alınamaz.", "danger")
            return redirect(url_for('index'))
        # ------------

        inventory_item.assigned_to = User.query.get(user_id).username

    component.quantity -= amount

    # İşlemi logla
    log = BorrowLog(
        user_id=user_id,
        comp_id=comp_id,
        action=action,
        amount=amount,
        location=location,
        serial_number=serial_number if action == 'borrow' and is_fixed_asset(component.category) else None,
        notes=notes if notes else None
    )
    db.session.add(log)
    db.session.commit()

    flash(f"{amount} adet {component.name} {'ödünç alındı' if action == 'borrow' else 'sarf edildi'}.")

    return redirect(url_for('index'))

@app.route('/return/<int:comp_id>', methods=['GET', 'POST'])
@login_required
def return_component(comp_id):
    """Kullanıcıların veya adminlerin ödünç aldıkları ürünleri iade etmesini sağlar."""
    component = Component.query.get_or_404(comp_id)
    users = []
    user_id_to_check = current_user.id

    if current_user.is_admin():
        users = User.query.all()
        # On GET, admin might select a user to view their items
        if request.method == 'GET':
            user_id_to_check = request.args.get('user_id', current_user.id, type=int)
        # On POST, admin is acting, potentially on behalf of another user
        elif request.method == 'POST':
            user_id_to_check = int(request.form.get('user_id', current_user.id))

    # Calculate net borrowed amount for the user
    borrowed_amount = db.session.query(func.sum(BorrowLog.amount)).filter_by(
        user_id=user_id_to_check, comp_id=comp_id, action='borrow').scalar() or 0
    returned_amount = db.session.query(func.sum(BorrowLog.amount)).filter_by(
        user_id=user_id_to_check, comp_id=comp_id, action='return').scalar() or 0
    current_borrowed = borrowed_amount - returned_amount

    # Get serial numbers in possession
    serial_numbers = []
    if is_fixed_asset(component.category):
        borrowed_serials = db.session.query(BorrowLog.serial_number).filter(
            BorrowLog.user_id == user_id_to_check,
            BorrowLog.comp_id == comp_id,
            BorrowLog.action == 'borrow',
            BorrowLog.serial_number.isnot(None)
        ).all()
        returned_serials = db.session.query(BorrowLog.serial_number).filter(
            BorrowLog.user_id == user_id_to_check,
            BorrowLog.comp_id == comp_id,
            BorrowLog.action == 'return',
            BorrowLog.serial_number.isnot(None)
        ).all()

        borrowed_counter = Counter(s[0] for s in borrowed_serials)
        returned_counter = Counter(s[0] for s in returned_serials)
        borrowed_counter.subtract(returned_counter)
        serial_numbers = [sn for sn, count in borrowed_counter.items() if count > 0]


    if request.method == 'POST':
        next_url = request.form.get('next') or url_for('my_borrowed')
        try:
            amount = int(request.form.get('amount', 1))
            notes = request.form.get('notes', '').strip()
            user_id_for_return = user_id_to_check # This is the user we are returning for

            if current_borrowed < amount:
                flash("Bu kadar iade edemezsiniz.", "danger")
                return redirect(next_url)

            serial_number_to_return = None
            if is_fixed_asset(component.category):
                serial_number_to_return = request.form.get('serial_number', '').strip()
                if not serial_number_to_return or serial_number_to_return not in serial_numbers:
                    flash("Geçerli bir seri numarası seçmelisiniz.", "danger")
                    return redirect(next_url)

                # Update inventory item
                item = InventoryItem.query.filter_by(serial_number=serial_number_to_return).first()
                if item:
                    item.assigned_to = None
                    db.session.add(item)

            component.quantity += amount
            log = BorrowLog(
                user_id=user_id_for_return,
                comp_id=comp_id,
                action='return',
                amount=amount,
                serial_number=serial_number_to_return,
                notes=notes
            )
            db.session.add(log)
            db.session.commit()

            flash(f"{amount} adet {component.name} iade edildi.", "success")
            return redirect(next_url)
        except ValueError:
            flash("Geçersiz miktar.", "danger")
            return redirect(next_url)
        except Exception as e:
            db.session.rollback()
            logging.exception("İade işlemi sırasında bir hata oluştu.")
            flash("İade işlemi sırasında beklenmedik bir hata oluştu.", "danger")
            return redirect(next_url)


    # For GET request, we need to calculate the items the user has borrowed
    borrowed_items_subq = db.session.query(
        BorrowLog.comp_id,
        BorrowLog.serial_number,
        func.sum(case(
            (BorrowLog.action == 'borrow', BorrowLog.amount),
            else_=-BorrowLog.amount
        )).label('net_amount')
    ).filter(BorrowLog.user_id == user_id_to_check).group_by(
        BorrowLog.comp_id, BorrowLog.serial_number
    ).having(func.sum(case((BorrowLog.action == 'borrow', BorrowLog.amount), else_=-BorrowLog.amount)) > 0).subquery()

    results = db.session.query(
        Component,
        borrowed_items_subq.c.serial_number,
        borrowed_items_subq.c.net_amount
    ).join(
        borrowed_items_subq, Component.id == borrowed_items_subq.c.comp_id
    ).options(selectinload(Component.inventory_items)).all()
    borrowed_items = [(comp, amount, [sn] if sn else []) for comp, sn, amount in results]


    return render_template('my_borrowed.html',
                           borrowed_items=borrowed_items,
                           component=component,
                           current_borrowed=current_borrowed,
                           serial_numbers=serial_numbers,
                           users=users,
                           selected_user_id=user_id_to_check)

@app.route('/my_borrowed')
@login_required
def my_borrowed():
    """Kullanıcının mevcut ödünç aldığı ürünleri listeler."""
    # Tek bir veritabanı sorgusu ile hem ödünç alınanları hem de iadeleri hesapla
    # Bu, Python'da döngü kurmaktan çok daha verimlidir.
    # SQLite için func.IF, PostgreSQL için CASE kullanılır. Engine'e göre uyarlanabilir.
    # Şimdilik SQLite varsayımıyla IF kullanalım.
    # Daha genel bir çözüm için CASE: func.sum(case([(BorrowLog.action == 'borrow', BorrowLog.amount)], else_=-BorrowLog.amount))
    
    borrowed_items_subq = db.session.query(
        BorrowLog.comp_id,
        BorrowLog.serial_number,
        func.sum(case(
            (BorrowLog.action == 'borrow', BorrowLog.amount),
            else_=-BorrowLog.amount
        )).label('net_amount')
    ).filter(BorrowLog.user_id == current_user.id).group_by(
        BorrowLog.comp_id, BorrowLog.serial_number
    ).having(func.sum(case((BorrowLog.action == 'borrow', BorrowLog.amount), else_=-BorrowLog.amount)) > 0).subquery()

    # Component detaylarını ve seri numaralarını almak için join yap
    results = db.session.query(
        Component,
        borrowed_items_subq.c.serial_number,
        borrowed_items_subq.c.net_amount
    ).join(
        borrowed_items_subq, Component.id == borrowed_items_subq.c.comp_id
    ).options(selectinload(Component.inventory_items)).all()

    # Şablon için veriyi hazırla
    borrowed_items = [(comp, amount, [sn] if sn else []) for comp, sn, amount in results]

    return render_template('my_borrowed.html', borrowed_items=borrowed_items)

# ==============================================================================
# KULLANICIYA ÖZEL SAYFALAR (ÖDÜNÇ ALDIKLARIM, İSTEKLER)
# ==============================================================================

@app.route('/istekler', methods=['GET', 'POST'])
@login_required
def requests():
    # Desteklenen filtreler
    status = request.args.get('status', 'all')
    req_type = request.args.get('type', 'all')
    search_query = request.args.get('q', '').strip()
    sort_by = request.args.get('sort', 'newest')
    exclude_status = request.args.get('exclude', '')  # Hariç tutulacak durum

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        req_type_form = request.form.get('req_type', 'satin_alma')
        if not name:
            flash("Ürün adı zorunlu.", "danger")
        else:
            req = Request(name=name, description=description, created_by=current_user.id, req_type=req_type_form)
            db.session.add(req)
            db.session.commit()
            flash("İstek eklendi.", "success")
        if status and status != 'all':
            return redirect(url_for('requests', status=status))
        return redirect(url_for('requests'))

    # Build base query - herkes sadece kendi isteklerini görür
    base_q = Request.query.options(selectinload(Request.messages)).filter_by(created_by=current_user.id)

    # Calculate stats before filtering (but after user filter)
    stats = {
        'total': base_q.count(),
        'beklemede': base_q.filter(Request.req_status == 'beklemede').count(),
        'kabul': base_q.filter(Request.req_status == 'kabul').count(),
        'reddedildi': base_q.filter(Request.req_status == 'reddedildi').count(),
        'tamamlandi': base_q.filter(Request.req_status == 'tamamlandi').count()
    }

    # Apply status filter
    if status and status != 'all':
        base_q = base_q.filter(Request.req_status == status)
    
    # Apply exclude filter (hariç tutma)
    if exclude_status:
        base_q = base_q.filter(Request.req_status != exclude_status)

    # Apply type filter
    if req_type and req_type != 'all':
        base_q = base_q.filter(Request.req_type == req_type)

    # Apply search filter
    if search_query:
        search_pattern = f'%{search_query}%'
        base_q = base_q.filter(
            db.or_(
                Request.name.ilike(search_pattern),
                Request.description.ilike(search_pattern)
            )
        )

    # Apply sorting
    if sort_by == 'oldest':
        base_q = base_q.order_by(Request.created_at.asc())
    elif sort_by == 'name':
        base_q = base_q.order_by(Request.name.asc())
    else:  # newest (default)
        base_q = base_q.order_by(Request.created_at.desc())

    requests_list = base_q.all()
    conversation_map = build_request_conversation_map(requests_list)

    return render_template('requests.html', 
                           requests=requests_list, 
                           conversation_map=conversation_map,
                           current_status=status,
                           current_type=req_type,
                           current_search=search_query,
                           current_sort=sort_by,
                           current_exclude=exclude_status,
                           stats=stats)


@app.route('/istek/olustur', methods=['GET', 'POST'])
@login_required
def create_request():
    """Yeni istek oluşturma sayfası."""
    status = request.args.get('status', 'all')
    components = Component.query.filter_by(is_deleted=False).order_by(Component.name).all()
    types = sorted({(c.type or 'Diğer') for c in components})
    categories = sorted({(c.category or 'Diğer') for c in components})
    existing_tags = Tag.query.order_by(Tag.name).all()

    def render_create_request_form(form_state=None):
        state = form_state or {}
        return render_template(
            'create_request.html',
            current_status=status,
            components=components,
            component_types=types,
            component_categories=categories,
            existing_tags=existing_tags,
            form_state=state
        )

    if request.method == 'POST':
        req_type = request.form.get('req_type', 'satin_alma').strip()
        description = request.form.get('description', '').strip()
        budget = request.form.get('budget', '').strip()
        project_number = request.form.get('project_number', '').strip()
        external_product_name = request.form.get('external_product_name', '').strip()
        external_description = request.form.get('external_description', '').strip()
        component_id = request.form.get('component_id', '').strip()
        serial_number = request.form.get('serial_number', '').strip()

        item_names = request.form.getlist('item_name[]')
        item_component_ids = request.form.getlist('item_component_id[]')
        item_categories = request.form.getlist('item_category[]')
        item_types = request.form.getlist('item_type[]')
        item_descriptions = request.form.getlist('item_description[]')
        item_tags = request.form.getlist('item_tags[]')
        item_quantities = request.form.getlist('item_quantity[]')
        item_links = request.form.getlist('item_link[]')
        item_prices = request.form.getlist('item_price[]')

        form_state = {
            'req_type': req_type,
            'description': description,
            'budget': budget,
            'project_number': project_number,
            'external_product_name': external_product_name,
            'external_description': external_description,
            'component_id': component_id,
            'serial_number': serial_number,
            'item_name': item_names,
            'item_component_id': item_component_ids,
            'item_category': item_categories,
            'item_type': item_types,
            'item_description': item_descriptions,
            'item_tags': item_tags,
            'item_quantity': item_quantities,
            'item_link': item_links,
            'item_price': item_prices
        }
        errors = []
        has_wet_signature_warning = False

        # Arıza/Bakım
        if req_type in ['ariza', 'bakim']:
            selected_component = None
            name = ''
            if component_id and component_id.isdigit():
                selected_component = Component.query.get(int(component_id))
                if selected_component:
                    name = selected_component.name

            if external_product_name:
                name = external_product_name
                if not external_description:
                    errors.append('Açıklama zorunlu.')
                description_to_save = external_description
            else:
                description_to_save = description
                if not description_to_save:
                    errors.append('Açıklama zorunlu.')

            if not name:
                errors.append('Ürün adı zorunlu.')

            if errors:
                for err in errors:
                    flash(err, 'danger')
                return render_create_request_form(form_state)

            req = Request(
                name=name,
                description=description_to_save,
                created_by=current_user.id,
                req_type=req_type
            )
            req.username = current_user.username
            if selected_component:
                req.component_id = selected_component.id
            if serial_number:
                req.serial_number = serial_number

            db.session.add(req)

            if req_type == 'ariza' and serial_number and selected_component:
                inventory_item = InventoryItem.query.filter_by(
                    component_id=selected_component.id,
                    serial_number=serial_number
                ).first()
                if inventory_item:
                    inventory_item.is_defective = True

            db.session.flush()
            create_request_revision(req, submitted_by=current_user.id, status_at_submit='beklemede')
            db.session.commit()
            flash('İstek eklendi.', 'success')

        # Satın Alma
        else:
            if not budget:
                errors.append('Bütçe seçimi zorunlu.')
            if budget == 'TTO' and not project_number:
                errors.append('Proje Numarası zorunlu.')
            if not description:
                errors.append('Talep Gerekçesi zorunlu.')
            if not item_names or all(not n.strip() for n in item_names):
                errors.append('En az bir ürün eklemelisiniz.')

            if errors:
                for err in errors:
                    flash(err, 'danger')
                return render_create_request_form(form_state)

            first_name = item_names[0].strip() if item_names else 'Satın Alma İsteği'
            req = Request(
                name=first_name,
                description=description,
                created_by=current_user.id,
                req_type=req_type,
                budget=budget,
                project_number=project_number if budget == 'TTO' else None
            )
            req.username = current_user.username

            total_request_price = 0.0
            for i in range(len(item_names)):
                name = item_names[i].strip() if i < len(item_names) else ''
                if not name:
                    continue

                component_id_raw = item_component_ids[i] if i < len(item_component_ids) else ''
                category = item_categories[i] if i < len(item_categories) else ''
                item_type = item_types[i] if i < len(item_types) else ''
                item_desc = item_descriptions[i] if i < len(item_descriptions) else ''
                tags = item_tags[i] if i < len(item_tags) else ''
                link = item_links[i] if i < len(item_links) else ''

                try:
                    quantity = int(item_quantities[i]) if i < len(item_quantities) and item_quantities[i] else 1
                except ValueError:
                    quantity = 1
                quantity = max(quantity, 1)

                try:
                    unit_price = float(item_prices[i]) if i < len(item_prices) and item_prices[i] else None
                except ValueError:
                    unit_price = None

                wet_signature_for_item = bool(unit_price and unit_price > WET_SIGNATURE_PRICE_THRESHOLD)
                if wet_signature_for_item:
                    has_wet_signature_warning = True

                item_total = (unit_price * quantity) if unit_price else None
                if item_total:
                    total_request_price += item_total

                request_item = RequestItem(
                    name=name,
                    component_id=int(component_id_raw) if component_id_raw and component_id_raw.isdigit() else None,
                    product_category=category if category else None,
                    product_type=item_type if item_type else None,
                    product_description=item_desc if item_desc else None,
                    tags=tags if tags else None,
                    quantity=quantity,
                    purchase_link=link if link else None,
                    unit_price=unit_price,
                    total_price=item_total,
                    requires_wet_signature=wet_signature_for_item
                )
                req.items.append(request_item)

            req.total_price = total_request_price if total_request_price > 0 else None
            req.requires_wet_signature = has_wet_signature_warning

            db.session.add(req)
            db.session.flush()
            create_request_revision(req, submitted_by=current_user.id, status_at_submit='beklemede')
            db.session.commit()
            flash('İstek eklendi.', 'success')
        
        if status and status != 'all':
            return redirect(url_for('requests', status=status))
        return redirect(url_for('requests'))
    return render_create_request_form({
        'req_type': 'satin_alma',
        'description': '',
        'budget': '',
        'project_number': '',
        'external_product_name': '',
        'external_description': '',
        'component_id': '',
        'serial_number': '',
        'item_name': [],
        'item_component_id': [],
        'item_category': [],
        'item_type': [],
        'item_description': [],
        'item_tags': [],
        'item_quantity': [],
        'item_link': [],
        'item_price': []
    })


@app.route('/istek/<int:req_id>/duzenle', methods=['GET', 'POST'])
@login_required
def edit_request(req_id):
    """Reddedilmiş isteği düzenleyip yeniden gönderme."""
    status = request.args.get('status', 'all')
    req = Request.query.get_or_404(req_id)

    if req.created_by != current_user.id:
        abort(403)
    if req.req_status != 'reddedildi':
        flash('Sadece reddedilmiş istekler düzenlenebilir.', 'warning')
        return redirect(url_for('requests', status=status))

    components = Component.query.filter_by(is_deleted=False).order_by(Component.name).all()
    types = sorted({(c.type or 'Diğer') for c in components})
    categories = sorted({(c.category or 'Diğer') for c in components})
    existing_tags = Tag.query.order_by(Tag.name).all()

    def build_edit_payload_from_request():
        payload = {
            'req_type': req.req_type,
            'description': req.description or '',
            'budget': req.budget or '',
            'project_number': req.project_number or '',
            'component_id': req.component_id,
            'serial_number': req.serial_number or '',
            'external_product_name': req.name if req.req_type in ['ariza', 'bakim'] and not req.component_id else '',
            'external_description': req.description if req.req_type in ['ariza', 'bakim'] and not req.component_id else '',
            'items': []
        }

        if req.req_type == 'satin_alma':
            existing_items = req.items.order_by(RequestItem.id.asc()).all()
            if existing_items:
                payload['items'] = [
                    {
                        'component_id': item.component_id or '',
                        'name': item.name or '',
                        'category': item.product_category or '',
                        'type': item.product_type or '',
                        'description': item.product_description or '',
                        'tags': item.tags or '',
                        'quantity': item.quantity or 1,
                        'link': item.purchase_link or '',
                        'price': item.unit_price or 0,
                        'isNew': not bool(item.component_id)
                    }
                    for item in existing_items
                ]
            elif req.name:
                payload['items'] = [{
                    'component_id': req.component_id or '',
                    'name': req.name or '',
                    'category': req.product_category or '',
                    'type': req.product_type or '',
                    'description': req.product_description or '',
                    'tags': req.tags or '',
                    'quantity': req.quantity or 1,
                    'link': req.purchase_link or '',
                    'price': req.unit_price or 0,
                    'isNew': not bool(req.component_id)
                }]
        return payload

    def build_edit_payload_from_form_state(state):
        payload = {
            'req_type': state.get('req_type', req.req_type),
            'description': state.get('description', ''),
            'budget': state.get('budget', ''),
            'project_number': state.get('project_number', ''),
            'component_id': state.get('component_id', ''),
            'serial_number': state.get('serial_number', ''),
            'external_product_name': state.get('external_product_name', ''),
            'external_description': state.get('external_description', ''),
            'items': []
        }

        if payload['req_type'] == 'satin_alma':
            names = state.get('item_name', []) or []
            component_ids = state.get('item_component_id', []) or []
            categories_state = state.get('item_category', []) or []
            types_state = state.get('item_type', []) or []
            descriptions = state.get('item_description', []) or []
            tags = state.get('item_tags', []) or []
            quantities = state.get('item_quantity', []) or []
            links = state.get('item_link', []) or []
            prices = state.get('item_price', []) or []

            for i, name in enumerate(names):
                item_name = (name or '').strip()
                if not item_name:
                    continue
                payload['items'].append({
                    'component_id': component_ids[i] if i < len(component_ids) else '',
                    'name': item_name,
                    'category': categories_state[i] if i < len(categories_state) else '',
                    'type': types_state[i] if i < len(types_state) else '',
                    'description': descriptions[i] if i < len(descriptions) else '',
                    'tags': tags[i] if i < len(tags) else '',
                    'quantity': quantities[i] if i < len(quantities) else 1,
                    'link': links[i] if i < len(links) else '',
                    'price': prices[i] if i < len(prices) else 0,
                    'isNew': not bool((component_ids[i] if i < len(component_ids) else '').strip())
                })
        return payload

    def render_edit_request_form(form_state=None, payload_override=None):
        payload = payload_override if payload_override is not None else build_edit_payload_from_request()
        return render_template(
            'create_request.html',
            current_status=status,
            components=components,
            component_types=types,
            component_categories=categories,
            existing_tags=existing_tags,
            form_state=form_state or {},
            edit_mode=True,
            edit_request=req,
            edit_payload=payload
        )

    if request.method == 'POST':
        description = request.form.get('description', '').strip()
        req_type = request.form.get('req_type', 'satin_alma')
        budget = request.form.get('budget', '').strip()
        project_number = request.form.get('project_number', '').strip()
        external_product_name = request.form.get('external_product_name', '').strip()
        external_description = request.form.get('external_description', '').strip()
        component_id = request.form.get('component_id', '').strip()
        serial_number = request.form.get('serial_number', '').strip()

        item_names = request.form.getlist('item_name[]')
        item_component_ids = request.form.getlist('item_component_id[]')
        item_categories = request.form.getlist('item_category[]')
        item_types = request.form.getlist('item_type[]')
        item_descriptions = request.form.getlist('item_description[]')
        item_tags = request.form.getlist('item_tags[]')
        item_quantities = request.form.getlist('item_quantity[]')
        item_links = request.form.getlist('item_link[]')
        item_prices = request.form.getlist('item_price[]')

        form_state = {
            'req_type': req_type,
            'description': description,
            'budget': budget,
            'project_number': project_number,
            'external_product_name': external_product_name,
            'external_description': external_description,
            'component_id': component_id,
            'serial_number': serial_number,
            'item_name': item_names,
            'item_component_id': item_component_ids,
            'item_category': item_categories,
            'item_type': item_types,
            'item_description': item_descriptions,
            'item_tags': item_tags,
            'item_quantity': item_quantities,
            'item_link': item_links,
            'item_price': item_prices
        }

        req.component_id = None
        req.serial_number = None
        req.product_category = None
        req.product_type = None
        req.product_description = None
        req.tags = None
        req.quantity = None
        req.purchase_link = None
        req.unit_price = None
        req.total_price = None
        req.budget = None
        req.project_number = None
        req.requires_wet_signature = False
        for existing_item in req.items.all():
            db.session.delete(existing_item)

        if req_type in ['ariza', 'bakim']:
            name = ''
            selected_component = None

            if component_id and component_id.isdigit():
                selected_component = Component.query.get(int(component_id))
                if selected_component:
                    name = selected_component.name

            if external_product_name:
                name = external_product_name
                if not external_description:
                    flash('Açıklama zorunlu.', 'danger')
                    return render_edit_request_form(form_state, build_edit_payload_from_form_state(form_state))
                description = external_description
            elif not description:
                flash('Açıklama zorunlu.', 'danger')
                return render_edit_request_form(form_state, build_edit_payload_from_form_state(form_state))

            if not name:
                flash('Ürün adı zorunlu.', 'danger')
                return render_edit_request_form(form_state, build_edit_payload_from_form_state(form_state))

            req.req_type = req_type
            req.name = name
            req.description = description
            if selected_component:
                req.component_id = selected_component.id

            if serial_number:
                req.serial_number = serial_number

        else:
            if not budget:
                flash('Bütçe seçimi zorunlu.', 'danger')
                return render_edit_request_form(form_state, build_edit_payload_from_form_state(form_state))
            if budget == 'TTO' and not project_number:
                flash('Proje Numarası zorunlu.', 'danger')
                return render_edit_request_form(form_state, build_edit_payload_from_form_state(form_state))

            if not description:
                flash('İstek açıklaması zorunlu.', 'danger')
                return render_edit_request_form(form_state, build_edit_payload_from_form_state(form_state))

            if not item_names or all(not n.strip() for n in item_names):
                flash('En az bir ürün eklemelisiniz.', 'danger')
                return render_edit_request_form(form_state, build_edit_payload_from_form_state(form_state))

            req.req_type = req_type
            req.name = item_names[0].strip() if item_names else 'Satın Alma İsteği'
            req.description = description
            req.budget = budget if budget else None
            req.project_number = project_number if budget == 'TTO' else None

            total_request_price = 0
            has_wet_signature_warning = False
            for i in range(len(item_names)):
                name = item_names[i].strip() if i < len(item_names) else ''
                if not name:
                    continue

                component_id = item_component_ids[i] if i < len(item_component_ids) else ''
                category = item_categories[i] if i < len(item_categories) else ''
                item_type = item_types[i] if i < len(item_types) else ''
                item_desc = item_descriptions[i] if i < len(item_descriptions) else ''
                tags = item_tags[i] if i < len(item_tags) else ''

                try:
                    quantity = int(item_quantities[i]) if i < len(item_quantities) and item_quantities[i] else 1
                except ValueError:
                    quantity = 1

                link = item_links[i] if i < len(item_links) else ''

                try:
                    unit_price = float(item_prices[i]) if i < len(item_prices) and item_prices[i] else None
                except ValueError:
                    unit_price = None

                wet_signature_for_item = bool(unit_price and unit_price > WET_SIGNATURE_PRICE_THRESHOLD)
                if wet_signature_for_item:
                    has_wet_signature_warning = True
                item_total = (unit_price * quantity) if unit_price else None
                if item_total:
                    total_request_price += item_total

                req.items.append(RequestItem(
                    name=name,
                    component_id=int(component_id) if component_id and component_id.isdigit() else None,
                    product_category=category if category else None,
                    product_type=item_type if item_type else None,
                    product_description=item_desc if item_desc else None,
                    tags=tags if tags else None,
                    quantity=quantity,
                    purchase_link=link if link else None,
                    unit_price=unit_price,
                    total_price=item_total,
                    requires_wet_signature=wet_signature_for_item
                ))

            req.total_price = total_request_price if total_request_price > 0 else None
            req.requires_wet_signature = has_wet_signature_warning

        old_status = req.req_status
        req.req_status = 'beklemede'
        append_status_event_message(req, old_status, 'beklemede')
        db.session.add(RequestMessage(
            request_id=req.id,
            author_user_id=current_user.id,
            author_username_snapshot=current_user.username,
            author_role='system',
            message_type='status_event',
            body='İstek kullanıcı tarafından düzenlenip yeniden gönderildi.'
        ))
        create_request_revision(req, submitted_by=current_user.id, status_at_submit='beklemede')
        db.session.commit()
        flash('İstek düzenlendi ve yeniden gönderildi.', 'success')
        return redirect(url_for('requests', status=status))

    return render_edit_request_form()

@app.route('/delete_request/<int:req_id>', methods=['POST'])
@login_required
def delete_request(req_id):
    # İstekler silinemez
    flash('İstekler silinemez.', 'danger')
    return redirect(url_for('requests'))


@app.route('/request/<int:req_id>/messages', methods=['POST'])
@login_required
def request_messages(req_id):
    """İstek sahibi kullanıcı için sohbet mesajı ekleme."""
    req = Request.query.get_or_404(req_id)
    if req.created_by != current_user.id:
        abort(403)

    message_body = request.form.get('message', '').strip()
    attachment = request.files.get('attachment')
    saved_attachment = None
    if attachment and attachment.filename:
        try:
            saved_attachment = save_request_message_attachment(attachment)
        except ValueError as ve:
            flash(str(ve), 'danger')
            return redirect(build_request_return_url('requests', req.id))

    validation_error = validate_request_message_content(message_body, bool(saved_attachment))
    if validation_error:
        flash(validation_error, 'danger')
        return redirect(build_request_return_url('requests', req.id))

    message = RequestMessage(
        request_id=req.id,
        author_user_id=current_user.id,
        author_username_snapshot=current_user.username,
        author_role='user',
        message_type='chat',
        body=message_body,
        attachment_path=saved_attachment['path'] if saved_attachment else None,
        attachment_name=saved_attachment['name'] if saved_attachment else None,
        attachment_mime=saved_attachment['mime'] if saved_attachment else None
    )
    db.session.add(message)
    db.session.commit()
    flash('Mesaj gönderildi.', 'success')
    return redirect(build_request_return_url('requests', req.id))


@app.route('/admin/request/<int:req_id>/messages', methods=['POST'])
@login_required
def admin_request_messages(req_id):
    """Admin için istek sohbetine mesaj ekleme."""
    if not current_user.is_admin():
        abort(403)

    req = Request.query.get_or_404(req_id)
    message_body = request.form.get('message', '').strip()
    attachment = request.files.get('attachment')
    saved_attachment = None
    if attachment and attachment.filename:
        try:
            saved_attachment = save_request_message_attachment(attachment)
        except ValueError as ve:
            flash(str(ve), 'danger')
            return redirect(build_request_return_url('admin_requests', req.id))

    validation_error = validate_request_message_content(message_body, bool(saved_attachment))
    if validation_error:
        flash(validation_error, 'danger')
        return redirect(build_request_return_url('admin_requests', req.id))

    message = RequestMessage(
        request_id=req.id,
        author_user_id=current_user.id,
        author_username_snapshot=current_user.username,
        author_role='admin',
        message_type='chat',
        body=message_body,
        attachment_path=saved_attachment['path'] if saved_attachment else None,
        attachment_name=saved_attachment['name'] if saved_attachment else None,
        attachment_mime=saved_attachment['mime'] if saved_attachment else None
    )
    db.session.add(message)
    db.session.commit()
    flash('Mesaj gönderildi.', 'success')
    return redirect(build_request_return_url('admin_requests', req.id))


@app.route('/request/<int:req_id>/messages/<int:msg_id>/edit', methods=['POST'])
@login_required
def edit_request_message(req_id, msg_id):
    """Sohbet mesajı düzenleme (yalnızca admin)."""
    if not current_user.is_admin():
        abort(403)
    req = Request.query.get_or_404(req_id)

    msg = RequestMessage.query.filter_by(id=msg_id, request_id=req.id).first_or_404()
    if msg.message_type != 'chat':
        abort(403)

    new_body = request.form.get('message', '').strip()
    validation_error = validate_request_message_content(new_body, bool(msg.attachment_path))
    if validation_error:
        flash(validation_error, 'danger')
        return redirect(build_request_return_url('requests', req.id))

    msg.body = new_body
    db.session.commit()
    flash('Mesaj güncellendi.', 'success')
    return redirect(build_request_return_url('requests', req.id))


@app.route('/request/<int:req_id>/messages/<int:msg_id>/delete', methods=['POST'])
@login_required
def delete_request_message(req_id, msg_id):
    """Sohbet mesajı silme (yalnızca admin)."""
    if not current_user.is_admin():
        abort(403)
    req = Request.query.get_or_404(req_id)

    msg = RequestMessage.query.filter_by(id=msg_id, request_id=req.id).first_or_404()
    if msg.message_type != 'chat':
        abort(403)

    if msg.attachment_path:
        try:
            attachment_abs_path = os.path.join(app.static_folder, msg.attachment_path)
            if os.path.isfile(attachment_abs_path):
                os.remove(attachment_abs_path)
        except Exception:
            logging.exception("Mesaj eklentisi silinirken hata oluştu.")

    db.session.delete(msg)
    db.session.commit()
    flash('Mesaj silindi.', 'success')
    return redirect(build_request_return_url('requests', req.id))


@app.route('/request/<int:req_id>/messages/<int:msg_id>/attachment')
@login_required
def request_message_attachment(req_id, msg_id):
    """
    İstek sohbet mesajı ekini yetkili kullanıcıya sunar.
    İstek sahibi veya admin erişebilir; diğer kullanıcılar 403 alır.
    """
    req = Request.query.get_or_404(req_id)
    if not current_user.is_admin() and req.created_by != current_user.id:
        abort(403)

    msg = RequestMessage.query.filter_by(id=msg_id, request_id=req.id).first_or_404()
    if not msg.attachment_path:
        abort(404)

    abs_path = os.path.join(app.static_folder, msg.attachment_path)
    if not os.path.isfile(abs_path):
        abort(404)

    return send_file(
        abs_path,
        download_name=secure_filename(msg.attachment_name or os.path.basename(abs_path))
    )

# ==============================================================================
# ADMİN PANELİ ROTALARI
# ==============================================================================

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    """Admin paneli ana sayfasını gösterir."""
    if not current_user.is_admin():
        abort(403)
    return render_template('admin/dashboard.html')

@app.route('/admin/users')
@login_required
def manage_users():
    """Kullanıcı yönetimi sayfasını gösterir."""
    if not current_user.is_admin():
        abort(403)
    users = User.query.all()
    return render_template('admin/manage_users.html', users=users)

@app.route('/admin/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_user(user_id):
    """Adminlerin kullanıcı bilgilerini (rol, şifre, yetkiler) düzenlemesini sağlar."""
    if not current_user.is_admin():
        abort(403)
    user = User.query.get_or_404(user_id)
    if request.method == 'POST':
        role = request.form.get('role')
        new_password = request.form.get('new_password')
        new_password2 = request.form.get('new_password2')
        
        # Yetkileri al
        can_add_product = bool(request.form.get('can_add_product'))
        can_delete_product = bool(request.form.get('can_delete_product'))

        if role in ['user', 'admin']:
            user.role = role
        
        # Yetkileri güncelle
        user.can_add_product = can_add_product
        user.can_delete_product = can_delete_product

        if new_password:
            if not new_password2 or new_password != new_password2:
                flash("Şifreler eşleşmiyor veya boş.", "danger")
                return redirect(url_for('edit_user', user_id=user.id))
            user.set_password(new_password)

        db.session.commit()
        flash("Kullanıcı güncellendi.", "success")
        return redirect(url_for('manage_users'))

    return render_template('admin/edit_user.html', user=user)

@app.route('/admin/users/delete/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    """Adminlerin kullanıcı silmesini sağlar. Log kayıtları korunur."""
    if not current_user.is_admin():
        flash("Bu işlemi yapmak için yetkiniz yok!")
        return redirect(url_for('index'))

    user = User.query.get_or_404(user_id)

    if user.id == current_user.id:
        flash("Kendi hesabınızı silemezsiniz!")
        return redirect(url_for('manage_users'))

    try:
        # Kullanıcının aktif ödünç aldığı (iade etmediği) ürünler var mı kontrol et
        active_borrows = BorrowLog.query.filter_by(user_id=user.id, action='borrow').count()
        returned = BorrowLog.query.filter_by(user_id=user.id, action='return').count()
        
        if active_borrows > returned:
            flash(f"{user.username} kullanıcısının iade etmediği ürünler var. Önce iade işlemlerini tamamlayın.", "danger")
            return redirect(url_for('manage_users'))

        username = user.username  # Kullanıcı adını sakla
        
        # Log kayıtlarını koru - username alanını doldur, user_id NULL olacak (ondelete='SET NULL')
        BorrowLog.query.filter_by(user_id=user.id).update({'username': username})
        
        # ComponentLog kayıtlarını güncelle
        from models import ComponentLog
        ComponentLog.query.filter_by(user_id=user.id).update({'user_id': None})
        
        # Request kayıtlarını güncelle
        Request.query.filter_by(created_by=user.id).update({'username': username})
        
        # Project kayıtlarını güncelle
        Project.query.filter_by(user_id=user.id).update({'username': username})

        db.session.delete(user)
        db.session.commit()
        flash(f"{username} silindi. Geçmiş kayıtlar korundu.", "success")
    except Exception as e:
        db.session.rollback()
        logging.exception(f"Kullanıcı silinirken hata: {user.username}")
        flash(f"Kullanıcı silinirken bir hata oluştu: {str(e)}", "danger")
    
    return redirect(url_for('manage_users'))

@app.route('/admin/users/toggle_role/<int:user_id>', methods=['POST'])
@login_required
def toggle_user_role(user_id):
    """Adminlerin kullanıcı rolünü (admin/user) değiştirmesini sağlar."""
    if not current_user.is_admin():
        flash("Bu işlemi yapmak için yetkiniz yok!")
        return redirect(url_for('index'))

    user = User.query.get_or_404(user_id)

    if user.id == current_user.id:
        flash("Kendi rolünüzü değiştiremezsiniz!")
        return redirect(url_for('manage_users'))

    user.role = 'admin' if user.role == 'user' else 'user'
    db.session.commit()
    flash(f"{user.username} rolü güncellendi.")
    return redirect(url_for('manage_users'))


# ------------------------------------------------------------------------------
# Admin: Requests management (list, filter, change status)
# ------------------------------------------------------------------------------

@app.route('/admin/requests')
@login_required
def admin_requests():
    """Admin istek yönetim sayfası."""
    if not current_user.is_admin():
        abort(403)

    status = request.args.get('status', 'all')
    req_type = request.args.get('type', 'all')
    search_query = request.args.get('q', '').strip()
    sort_by = request.args.get('sort', 'newest')
    user_filter = request.args.get('user', 'all')
    exclude_status = request.args.get('exclude', '')  # Hariç tutulacak durum
    
    # Kullanıcı listesi (istek oluşturmuş kullanıcılar)
    users_with_requests = db.session.query(User).join(Request, User.id == Request.created_by).distinct().order_by(User.username).all()
    
    # İstatistikler
    stats = {
        'total': Request.query.count(),
        'beklemede': Request.query.filter_by(req_status='beklemede').count(),
        'kabul': Request.query.filter_by(req_status='kabul').count(),
        'reddedildi': Request.query.filter_by(req_status='reddedildi').count(),
        'tamamlandi': Request.query.filter_by(req_status='tamamlandi').count()
    }
    
    q = Request.query.options(selectinload(Request.messages), selectinload(Request.revisions))
    if status and status != 'all':
        q = q.filter_by(req_status=status)
    
    # Exclude filter (hariç tutma)
    if exclude_status:
        q = q.filter(Request.req_status != exclude_status)
    
    if req_type and req_type != 'all':
        q = q.filter_by(req_type=req_type)
    if user_filter and user_filter != 'all':
        q = q.filter_by(created_by=int(user_filter))
    if search_query:
        q = q.filter(
            db.or_(
                Request.name.ilike(f'%{search_query}%'),
                Request.description.ilike(f'%{search_query}%'),
                Request.username.ilike(f'%{search_query}%'),
                Request.serial_number.ilike(f'%{search_query}%')
            )
        )
    
    # Sıralama
    if sort_by == 'oldest':
        q = q.order_by(Request.created_at.asc())
    elif sort_by == 'name':
        q = q.order_by(Request.name.asc())
    else:
        q = q.order_by(Request.created_at.desc())

    requests_list = q.all()
    conversation_map = build_request_conversation_map(requests_list)
    revision_diff_map = build_request_revision_diffs(requests_list)
    return render_template('admin/manage_requests.html', 
                           requests=requests_list, 
                           conversation_map=conversation_map,
                           revision_diff_map=revision_diff_map,
                           current_status=status, 
                           current_type=req_type,
                          current_search=search_query,
                          current_sort=sort_by,
                          current_user_filter=user_filter,
                          current_exclude=exclude_status,
                          users_with_requests=users_with_requests,
                          stats=stats)


@app.route('/admin/request/<int:req_id>/set_status', methods=['POST'])
@login_required
def admin_set_request_status(req_id):
    """Admin istek durumunu günceller."""
    if not current_user.is_admin():
        abort(403)

    new_status = request.form.get('status')
    admin_note = request.form.get('admin_note', '').strip()
    
    if new_status not in ('beklemede', 'reddedildi', 'kabul', 'tamamlandi'):
        flash('Geçersiz durum.', 'danger')
        return redirect(url_for('admin_requests'))

    req = Request.query.get_or_404(req_id)
    
    # Kabul edilmiş veya tamamlanmış istekler reddedilemez
    if req.req_status in ('kabul', 'tamamlandi') and new_status == 'reddedildi':
        flash('Kabul edilmiş veya tamamlanmış istekler reddedilemez.', 'warning')
        return redirect(url_for('admin_requests'))
    
    # Reddedilmiş istekler kabul edilemez
    if req.req_status == 'reddedildi' and new_status in ('kabul', 'tamamlandi'):
        flash('Reddedilmiş istekler kabul edilemez.', 'warning')
        return redirect(url_for('admin_requests'))
    
    old_status = req.req_status
    req.req_status = new_status
    append_status_event_message(req, old_status, new_status)
    if new_status == 'reddedildi':
        create_request_revision(req, submitted_by=current_user.id, status_at_submit='reddedildi')
    if admin_note:
        req.admin_note = admin_note
        append_admin_note_message(req, admin_note, current_user)
    db.session.commit()
    flash('İstek durumu güncellendi.', 'success')
    return redirect(url_for('admin_requests'))


@app.route('/admin/requests/bulk_status', methods=['POST'])
@login_required
def admin_bulk_request_status():
    """Admin toplu istek durumu günceller."""
    if not current_user.is_admin():
        abort(403)

    new_status = request.form.get('status')
    admin_note = request.form.get('admin_note', '').strip()
    request_ids_str = request.form.get('request_ids', '')
    
    if new_status not in ('beklemede', 'reddedildi', 'kabul', 'tamamlandi'):
        flash('Geçersiz durum.', 'danger')
        return redirect(url_for('admin_requests'))
    
    if not request_ids_str:
        flash('İşlem yapılacak istek seçilmedi.', 'warning')
        return redirect(url_for('admin_requests'))
    
    # Parse request IDs
    try:
        request_ids = [int(id.strip()) for id in request_ids_str.split(',') if id.strip()]
    except ValueError:
        flash('Geçersiz istek ID formatı.', 'danger')
        return redirect(url_for('admin_requests'))
    
    if not request_ids:
        flash('İşlem yapılacak istek seçilmedi.', 'warning')
        return redirect(url_for('admin_requests'))
    
    # Process each request
    updated_count = 0
    skipped_count = 0
    
    for req_id in request_ids:
        req = Request.query.get(req_id)
        if not req:
            skipped_count += 1
            continue
        
        # Apply business rules
        if new_status == 'kabul':
            # Can't accept rejected requests
            if req.req_status == 'reddedildi':
                skipped_count += 1
                continue
            # Skip already accepted/completed
            if req.req_status in ('kabul', 'tamamlandi'):
                skipped_count += 1
                continue
        elif new_status == 'reddedildi':
            # Can't reject accepted or completed requests
            if req.req_status in ('kabul', 'tamamlandi'):
                skipped_count += 1
                continue
            # Skip already rejected
            if req.req_status == 'reddedildi':
                skipped_count += 1
                continue
        elif new_status == 'tamamlandi':
            # Can only complete accepted requests
            if req.req_status != 'kabul':
                skipped_count += 1
                continue
        
        old_status = req.req_status
        req.req_status = new_status
        append_status_event_message(req, old_status, new_status)
        if new_status == 'reddedildi':
            create_request_revision(req, submitted_by=current_user.id, status_at_submit='reddedildi')
        if admin_note:
            req.admin_note = admin_note
            append_admin_note_message(req, admin_note, current_user)
        updated_count += 1
    
    db.session.commit()
    
    # Generate appropriate flash message
    if updated_count > 0 and skipped_count > 0:
        flash(f'{updated_count} istek güncellendi, {skipped_count} istek atlandı.', 'info')
    elif updated_count > 0:
        flash(f'{updated_count} istek başarıyla güncellendi.', 'success')
    else:
        flash('Hiçbir istek güncellenemedi.', 'warning')
    
    return redirect(url_for('admin_requests'))


@app.route('/admin/request/<int:req_id>/generate_pdf', methods=['GET', 'POST'])
@login_required
def generate_request_pdf(req_id):
    """Satın alma isteği için PDF form oluşturur."""
    if not current_user.is_admin():
        abort(403)
    
    req = Request.query.get_or_404(req_id)
    
    # Sadece satın alma istekleri için PDF oluşturulabilir
    if req.req_type != 'satin_alma':
        flash('PDF formu sadece satın alma istekleri için oluşturulabilir.', 'warning')
        return redirect(url_for('admin_requests'))
    
    if request.method == 'POST':
        # Admin verilerini al
        admin_data = {
            'ihtiyac_alani': request.form.get('ihtiyac_alani', '').strip(),
            'tarih': request.form.get('tarih', '').strip(),
            'talep_eden_birim': request.form.get('talep_eden_birim', '').strip()
        }
        
        try:
            from pdf_generator import generate_purchase_form
            output_path, filename = generate_purchase_form(req, admin_data)
            
            # PDF'i indir
            return send_file(
                output_path,
                mimetype='application/pdf',
                as_attachment=True,
                download_name=filename
            )
        except Exception as e:
            flash(f'PDF oluşturulurken hata oluştu: {str(e)}', 'danger')
            return redirect(url_for('admin_requests'))
    
    # GET - Form göster
    from pdf_generator import get_form_preview_data
    preview_data = get_form_preview_data(req)
    
    return render_template('admin/generate_pdf.html', req=req, preview=preview_data)


@app.route('/admin/borrow_return', methods=['GET', 'POST'])
@login_required
def admin_borrow_return():
    """Adminler için ödünç alma, iade etme ve sarf etme arayüzü."""
    if not current_user.is_admin():
        abort(403)
    components = Component.query.all()
    users = User.query.all()
    action = request.form.get('action')
    selected_user_id = request.form.get('user_id', type=int)
    selected_comp_id = request.form.get('comp_id', type=int)
    selected_component = Component.query.get(selected_comp_id) if selected_comp_id else None
    borrowed_items = []

    if action == "iade" and selected_user_id:
        # Kullanıcının iade etmediği ürünleri bul
        borrowed_logs = BorrowLog.query.filter_by(user_id=selected_user_id, action='borrow').all()
        returned_logs = BorrowLog.query.filter_by(user_id=selected_user_id, action='return').all()
        borrow_counter = Counter((log.comp_id, log.serial_number) for log in borrowed_logs)
        return_counter = Counter((log.comp_id, log.serial_number) for log in returned_logs)
        current_borrowed = borrow_counter - return_counter

        for (comp_id, serial_number), count in current_borrowed.items():
            comp = Component.query.get(comp_id)
            if count > 0:
                borrowed_items.append({
                    "id": comp.id, "name": comp.name, "type": comp.type,
                    "category": comp.category, "serial_number": serial_number, "count": count
                })

    return render_template('admin/borrow_return.html',
                           components=components, users=users, action=action,
                           selected_user_id=selected_user_id, selected_comp_id=selected_comp_id,
                           selected_component=selected_component, borrowed_items=borrowed_items)

# ==============================================================================
# UYGULAMAYI BAŞLATMA BLOĞU
# ==============================================================================

@app.template_filter('clean_text')
def clean_text(s):
    return re.sub(r"[\"'()]", "", s)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()

    # Allow runtime control via environment for local runs
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() in ('1', 'true', 'yes')
    host = os.environ.get('FLASK_HOST', '0.0.0.0')
    port = int(os.environ.get('FLASK_PORT', 5000))
    app.run(host=host, port=port, debug=debug_mode)
