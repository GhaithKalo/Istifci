# -*- coding: utf-8 -*-

# ==============================================================================
# GEREKLİ KÜTÜPHANELERİN VE MODÜLLERİN YÜKLENMESİ
# ==============================================================================
import uuid
from flask import Flask, render_template, request, redirect, url_for, flash, abort, g
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_migrate import Migrate
from werkzeug.utils import secure_filename
from flask_wtf import CSRFProtect
from flask_wtf.csrf import generate_csrf
from sqlalchemy import func, or_, event, case
from sqlalchemy.orm import joinedload, selectinload, subqueryload
from collections import defaultdict, Counter
from zoneinfo import ZoneInfo # Python 3.9+
import os, re, logging, sys
from dotenv import load_dotenv

load_dotenv()

# Proje içi modüller
from models import db, User, Component, BorrowLog, Project, ProjectItem, Tag, Request, InventoryItem

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

# `utility_processor` fonksiyonunu context processor olarak kaydet
app.context_processor(utility_processor)

@app.template_filter('get_user_by_username')
def get_user_by_username(username):
    """
    Kullanıcı adına göre User nesnesini döndüren bir template filtresi.
    """
    return User.query.filter_by(username=username).first()

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
    query = Component.query

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


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Kullanıcı giriş sayfasını yönetir."""
    if request.method == 'POST':
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
    
    return render_template('components/list.html', 
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
                return render_template('add.html', types=types, existing_tags=existing_tags, selected_tags=selected_tags, owner_prefixes=owner_prefixes)
            
            quantity_int = int(form_data['quantity'])

            # --- 2. Seri Numaralarını İşle ---
            serial_numbers = []
            if is_fixed_asset(form_data['category']):
                if not form_data['owner_prefix']:
                    flash("Demirbaşlar için sahiplik kısaltması (prefix) zorunludur.", "danger")
                    return render_template('add.html', types=types, existing_tags=existing_tags, selected_tags=selected_tags, owner_prefixes=owner_prefixes)

                serial_numbers = [f"{form_data['owner_prefix']}-{sn.strip()}" for sn in form_data['serial_numbers_raw'].split(',') if sn.strip()]
                if len(serial_numbers) != quantity_int:
                    flash("Seri numarası sayısı ile miktar eşleşmeli.", "danger")
                    return render_template('add.html', types=types, existing_tags=existing_tags, selected_tags=selected_tags, owner_prefixes=owner_prefixes)

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

            db.session.commit()
            flash(f"{component.name} bileşeni eklendi. Kod: {component.code}", "success")
            return redirect(url_for('index'))

        except ValueError as ve:
            db.session.rollback()
            flash(str(ve), "danger")
        except Exception as e:
            db.session.rollback()
            logging.exception("Component eklenirken bir hata oluştu.")
            flash("Bileşen eklenirken beklenmedik bir hata oluştu.", "danger")

    return render_template('add.html', types=types, existing_tags=existing_tags, selected_tags=selected_tags, owner_prefixes=owner_prefixes)


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

    inventory_items = InventoryItem.query.filter_by(component_id=comp_id).all()
    for item in inventory_items:
        db.session.delete(item)

    BorrowLog.query.filter_by(comp_id=comp_id).delete()

    db.session.delete(component)
    db.session.commit()
    flash(f"{component.name} bileşeni silindi.")
    return redirect(url_for('index'))

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

    # Temel sorgu
    query = Component.query.filter(or_(fixed_asset_condition, consumable_condition))

    # Get all categories for the filter buttons, based on the low_stock query
    all_categories_query = query.with_entities(Component.category).distinct().all()
    all_categories = sorted([c[0] for c in all_categories_query if c[0]])

    # Category filtresi
    if selected_category:
        query = query.filter(func.lower(Component.category) == selected_category.lower())

    low_stock_components = query.order_by(Component.quantity).all()
    
    return render_template('low_stock.html', 
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
    if request.method == 'POST':
        name = request.form['name'].strip()
        description = request.form.get('description', '').strip()
        if not name:
            flash("Ürün adı zorunlu.", "danger")
        else:
            req = Request(name=name, description=description, created_by=current_user.id)
            db.session.add(req)
            db.session.commit()
            flash("istek eklendi.", "success")
        return redirect(url_for('requests'))
    requests = Request.query.order_by(Request.created_at.desc()).all()
    return render_template('requests.html', requests=requests)

@app.route('/delete_request/<int:req_id>', methods=['POST'])
@login_required
def delete_request(req_id):
    if not current_user.is_admin():
        abort(403)
    req = Request.query.get_or_404(req_id)
    db.session.delete(req)
    db.session.commit()
    flash('istek başarıyla silindi.', 'success')
    return redirect(url_for('requests'))

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
    """Adminlerin kullanıcı bilgilerini (rol, şifre) düzenlemesini sağlar."""
    if not current_user.is_admin():
        abort(403)
    user = User.query.get_or_404(user_id)
    if request.method == 'POST':
        role = request.form.get('role')
        new_password = request.form.get('new_password')
        new_password2 = request.form.get('new_password2')

        if role in ['user', 'admin']:
            user.role = role

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
    """Adminlerin kullanıcı silmesini sağlar."""
    if not current_user.is_admin():
        flash("Bu işlemi yapmak için yetkiniz yok!")
        return redirect(url_for('index'))

    user = User.query.get_or_404(user_id)

    if user.id == current_user.id:
        flash("Kendi hesabınızı silemezsiniz!")
        return redirect(url_for('manage_users'))

    db.session.delete(user)
    db.session.commit()
    flash(f"{user.username} silindi.")
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
