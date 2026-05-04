# -*- coding: utf-8 -*-

# ==============================================================================
# GEREKLİ KÜTÜPHANELER VE MODÜLLER
# ==============================================================================
from sqlalchemy import event
from sqlalchemy.orm import Mapper
from sqlalchemy.types import String
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
import pytz

# ==============================================================================
# VERİTABANI VE ZAMAN DİLİMİ AYARLARI
# ==============================================================================
db = SQLAlchemy()
turkey_tz = pytz.timezone("Europe/Istanbul")

# ==============================================================================
# İLİŞKİ TABLOLARI (ASSOCIATION TABLES)
# ==============================================================================

# Component ve Tag modelleri arasında çoktan-çoğa ilişki kuran tablo.
component_tag = db.Table(
    'component_tag',
    db.Column('component_id', db.Integer, db.ForeignKey('component.id'), primary_key=True),
    db.Column('tag_id', db.Integer, db.ForeignKey('tag.id'), primary_key=True)
)

# ==============================================================================
# VERİTABANI MODELLERİ
# ==============================================================================

class User(db.Model, UserMixin):
    """
    Uygulamadaki kullanıcıları temsil eden model.
    (LDAP entegrasyonu için güncellendi)
    """
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)

    # --- DEĞİŞİKLİK 1 ---
    # Şifre alanı artık NULL olabilir (LDAP kullanıcıları için).
    password_hash = db.Column(db.String(256), nullable=True)
    # Yeni: Bu kullanıcı LDAP ile yönetiliyorsa True olur. Lokal şifre değişikliklerini engellemek için kullanılır.
    is_ldap = db.Column(db.Boolean, default=False, nullable=False)

    role = db.Column(db.String(20), default='user')  # 'admin' ya da 'user'

    can_add_product = db.Column(db.Boolean, default=False)
    can_delete_product = db.Column(db.Boolean, default=False)

    # --- DEĞİŞİKLİK 2 ---
    # Kurucu fonksiyon (init) artık 'password' parametresini zorunlu tutmuyor.
    # password=None varsayılan değeri eklendi.
    def __init__(self, username, password=None, role='user', can_add_product=False, can_delete_product=False, is_ldap=False):
        self.username = username

        # Sadece bir şifre verilirse hash'le (yerel admin oluştururken vb.)
        if password:
            self.set_password(password)

        # Eğer kullanıcı LDAP ile yönetiliyorsa, local password alanı None kalır ve is_ldap True yapılır.
        if is_ldap:
            self.is_ldap = True

        self.role = role
        self.can_add_product = can_add_product
        self.can_delete_product = can_delete_product

    def is_admin(self):
        """Kullanıcının admin olup olmadığını kontrol eder."""
        return self.role == 'admin'

    def has_add_permission(self):
        """Kullanıcının ürün ekleme izni olup olmadığını kontrol eder."""
        return self.is_admin() or self.can_add_product

    def has_delete_permission(self):
        """Kullanıcının ürün silme izni olup olmadığını kontrol eder."""
        return self.is_admin() or self.can_delete_product

    def set_password(self, password):
        """Verilen şifreyi hash'leyerek kaydeder."""
        self.password_hash = generate_password_hash(password)

    # --- DEĞİŞİKLİK 3 ---
    # Şifre kontrolü, hash'in 'None' olma ihtimaline karşı güvenli hale getirildi.
    def check_password(self, password):
        """Verilen şifrenin hash'lenmiş şifre ile eşleşip eşleşmediğini kontrol eder."""

        # Eğer kullanıcının yerel bir şifre hash'i yoksa (LDAP kullanıcısıysa),
        # şifre kontrolü her zaman False dönmelidir.
        if not self.password_hash:
            return False

        # Yerel şifresi varsa, kontrol et.
        return check_password_hash(self.password_hash, password)   

class Tag(db.Model):
    """
    Bileşenleri (Component) gruplamak için kullanılan etiketleri temsil eder.
    Örn: 'Arduino', 'Sensör', 'Motor'
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)

    # Bir etiketin hangi bileşenlere sahip olduğunu gösteren ilişki.
    components = db.relationship(
        'Component',
        secondary=component_tag,
        back_populates='tags'
    )

class Component(db.Model):
    """
    Sistemdeki her bir malzeme, ürün veya bileşeni temsil eden ana model.
    Demirbaş, sarf malzemesi veya gereç olabilir.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(20), nullable=False)  # 'demirbas', 'sarf', 'gerec'
    type = db.Column(db.String(100), nullable=True)
    location = db.Column(db.String(150), nullable=True)
    description = db.Column(db.Text, nullable=True)
    quantity = db.Column(db.Integer, nullable=False)
    image_url = db.Column(db.String(300), nullable=True, default=None)
    part_number = db.Column(db.String(100), nullable=True)
    code = db.Column(db.String(150), unique=True)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)

    # Bir bileşenin hangi etiketlere sahip olduğunu gösteren ilişki.
    tags = db.relationship(
        'Tag',
        secondary=component_tag,
        back_populates='components'
    )

class InventoryItem(db.Model):
    """
    Demirbaş veya gereç gibi seri numarası ile takip edilen her bir fiziksel ürünü temsil eder.
    Bir 'Component' birden çok 'InventoryItem'a sahip olabilir.
    """
    id = db.Column(db.Integer, primary_key=True)
    component_id = db.Column(db.Integer, db.ForeignKey('component.id'), nullable=False, index=True)
    serial_number = db.Column(db.String(100), unique=True, nullable=False)
    is_defective = db.Column(db.Boolean, default=False, nullable=False) # Arızalı olup olmadığını belirten alan
    assigned_to = db.Column(db.String(100), nullable=True)  # Ürünün zimmetlendiği kullanıcının adı.

    # Bu envanter öğesinin hangi bileşene ait olduğunu belirten ilişki.
    component = db.relationship('Component', backref='inventory_items')

class ComponentLog(db.Model):
    """
    Bileşenler üzerinde yapılan tüm stok hareketlerini (artırma, azaltma vb.) kaydeden log modeli.
    Bu model şu an aktif olarak kullanılmıyor olabilir, BorrowLog daha spesifiktir.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    comp_id = db.Column(db.Integer, db.ForeignKey("component.id"), index=True)
    action = db.Column(db.String(50))  # örn: 'borrow', 'return', 'increase', 'decrease'
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(turkey_tz))
    amount = db.Column(db.Integer, default=1)

    # Log kaydını oluşturan kullanıcı ve ilgili bileşen ile ilişkiler.
    user = db.relationship("User", backref="logs")
    component = db.relationship("Component")

class BorrowLog(db.Model):
    """
    Ödünç alma (borrow), iade etme (return) ve sarf etme (consume) gibi
    kullanıcı-bileşen etkileşimlerini kaydeden model.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True, index=True)
    username = db.Column(db.String(64), nullable=True)  # Kullanıcı silinse bile adı korunur
    comp_id = db.Column(db.Integer, db.ForeignKey('component.id'), nullable=False, index=True)
    action = db.Column(db.String(10), nullable=False)  # "borrow", "return", veya "consume"
    amount = db.Column(db.Integer, nullable=False)
    location = db.Column(db.String(255), nullable=True)  # Ürünün kullanılacağı yer veya ödünç alınan yer
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(turkey_tz), nullable=False)
    serial_number = db.Column(db.String(100), nullable=True)
    notes = db.Column(db.Text, nullable=True)  # İşlemle ilgili notlar

    # Log kaydını oluşturan kullanıcı ve ilgili bileşen ile ilişkiler.
    user = db.relationship('User', backref=db.backref('borrow_logs', lazy=True))
    component = db.relationship('Component', backref=db.backref('borrow_logs', lazy=True))


class Project(db.Model):
    """
    Kullanıcıların belirli malzemeleri bir araya getirerek oluşturduğu projeleri
    temsil eden model.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    location = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(20), default="Bekliyor")  # "Bekliyor", "Onaylandı", "Tamamlandı"
    approved = db.Column(db.Boolean, default=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True)
    username = db.Column(db.String(64), nullable=True)  # Kullanıcı silinse bile adı korunur
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(turkey_tz))

    # Projeyi oluşturan kullanıcı ve projeye ait malzemeler ile ilişkiler.
    user = db.relationship('User', backref='projects')
    items = db.relationship('ProjectItem', backref='project', cascade="all, delete-orphan")

class ProjectItem(db.Model):
    """
    Bir projenin hangi bileşenlerden kaç adet içerdiğini belirten ara model.
    """
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    comp_id = db.Column(db.Integer, db.ForeignKey('component.id'), nullable=False)
    amount = db.Column(db.Integer, nullable=False)

    # Proje malzemesinin hangi bileşen olduğunu belirten ilişki.
    component = db.relationship('Component')



class Request(db.Model):
    """
    Kullanıcıların sistemde bulunmayan ve talep ettikleri malzemeleri
    kaydetmek için kullanılan model.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    # İstek türü: 'satin_alma', 'ariza', 'bakim'
    req_type = db.Column(db.String(30), nullable=False, default='satin_alma')
    # İstek durumu: 'beklemede', 'reddedildi', 'kabul', 'tamamlandi'
    req_status = db.Column(db.String(30), nullable=False, default='beklemede')
    description = db.Column(db.Text)  # İstek açıklaması
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True)
    username = db.Column(db.String(64), nullable=True)  # Kullanıcı silinse bile adı korunur
    
    # Mevcut ürün seçildiyse component_id
    component_id = db.Column(db.Integer, db.ForeignKey('component.id'), nullable=True)
    # Arıza/bakım istekleri için seri numarası
    serial_number = db.Column(db.String(100), nullable=True)

    # Satın alma istekleri için ek alanlar
    purchase_type = db.Column(db.String(50), nullable=True)  # Talep türü: 'Nalbur', 'Yazılım', 'Elektronik', 'Kırtasiye', 'Mobilya'
    product_category = db.Column(db.String(50), nullable=True)  # Ürün kategorisi (demirbas, gerec, sarf)
    product_type = db.Column(db.String(100), nullable=True)  # Ürün türü
    product_description = db.Column(db.Text, nullable=True)  # Ürün açıklaması
    tags = db.Column(db.String(255), nullable=True)  # Etiketler (virgülle ayrılmış)
    quantity = db.Column(db.Integer, nullable=True, default=1)  # Adet
    purchase_link = db.Column(db.String(500), nullable=True)  # Satın alma linki
    unit_price = db.Column(db.Float, nullable=True)  # Birim fiyatı (KDV'siz)
    total_price = db.Column(db.Float, nullable=True)  # Toplam fiyat (KDV'siz) = Adet * Birim Fiyatı
    budget = db.Column(db.String(50), nullable=True)  # Bütçe: 'TTO', 'Merkez', 'SSB'
    tto_subtype = db.Column(db.String(50), nullable=True)  # TTO alt türü: 'BAP', 'Tübitak', 'Tuseb', 'USI'
    project_number = db.Column(db.String(120), nullable=True)  # TTO bütçeli talepler için proje numarası
    requires_wet_signature = db.Column(db.Boolean, default=False, nullable=False)  # Bilgilendirici ıslak imza uyarısı

    # Admin notu (kabul/red sırasında eklenen not)
    admin_note = db.Column(db.Text, nullable=True)
    
    # Tek ürünlü istekler için envantere eklenme durumu
    added_to_inventory = db.Column(db.Boolean, default=False, nullable=True)
    added_component_id = db.Column(db.Integer, db.ForeignKey('component.id'), nullable=True)

    # İsteği oluşturan kullanıcı ile ilişki.
    user = db.relationship('User', backref='requests')
    # Seçilen bileşen ile ilişki (eski tek ürün için - geriye uyumluluk)
    component = db.relationship('Component', foreign_keys=[component_id], backref='requests')
    # Eklenen bileşen ile ilişki
    added_component = db.relationship('Component', foreign_keys=[added_component_id])
    # Birden fazla ürün için ilişki
    items = db.relationship('RequestItem', backref='request', cascade='all, delete-orphan', lazy='dynamic')
    # İstek bazlı sohbet/zaman çizelgesi mesajları
    messages = db.relationship(
        'RequestMessage',
        backref='request',
        cascade='all, delete-orphan',
        lazy='select',
        order_by='RequestMessage.created_at.asc(), RequestMessage.id.asc()'
    )
    revisions = db.relationship(
        'RequestRevision',
        backref='request',
        cascade='all, delete-orphan',
        lazy='select',
        order_by='RequestRevision.revision_no.asc()'
    )
    
    @property
    def total_items_price(self):
        """Tüm ürün kalemlerinin toplam fiyatını hesaplar."""
        total = 0
        for item in self.items:
            if item.total_price:
                total += item.total_price
        return total if total > 0 else self.total_price
    
    @property
    def items_count(self):
        """İstekteki ürün kalemi sayısını döndürür."""
        return self.items.count()


class RequestItem(db.Model):
    """
    Bir satın alma isteğindeki her bir ürün kalemini temsil eder.
    Bir Request birden fazla RequestItem'a sahip olabilir.
    """
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey('request.id'), nullable=False, index=True)
    
    # Ürün bilgileri - mevcut ürün seçildiyse
    component_id = db.Column(db.Integer, db.ForeignKey('component.id'), nullable=True)
    
    # Yeni ürün için bilgiler
    name = db.Column(db.String(120), nullable=False)
    product_category = db.Column(db.String(50), nullable=True)  # demirbas, gerec, sarf
    product_type = db.Column(db.String(100), nullable=True)
    product_description = db.Column(db.Text, nullable=True)
    brand = db.Column(db.String(120), nullable=True)  # Ürün Markası (opsiyonel)
    model_name = db.Column(db.String(120), nullable=True)  # Ürün Modeli (opsiyonel)
    tags = db.Column(db.String(255), nullable=True)
    
    # Satın alma detayları
    quantity = db.Column(db.Integer, nullable=False, default=1)
    purchase_link = db.Column(db.String(500), nullable=True)
    unit_price = db.Column(db.Float, nullable=True)
    total_price = db.Column(db.Float, nullable=True)
    requires_wet_signature = db.Column(db.Boolean, default=False, nullable=False)
    
    # Envantere eklenme durumu
    added_to_inventory = db.Column(db.Boolean, default=False, nullable=False)
    added_component_id = db.Column(db.Integer, db.ForeignKey('component.id'), nullable=True)  # Eklenen bileşenin ID'si
    
    # İlişkiler
    component = db.relationship('Component', foreign_keys=[component_id], backref='request_items')
    added_component = db.relationship('Component', foreign_keys=[added_component_id])


class RequestMessage(db.Model):
    """
    Bir isteğe ait sohbet mesajlarını ve durum geçiş olaylarını saklar.
    """
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey('request.id', ondelete='CASCADE'), nullable=False, index=True)

    author_user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True)
    author_username_snapshot = db.Column(db.String(64), nullable=True)
    author_role = db.Column(db.String(20), nullable=False, default='user')  # 'admin', 'user', 'system'

    message_type = db.Column(db.String(20), nullable=False, default='chat')  # 'chat', 'status_event', 'admin_note'
    body = db.Column(db.Text, nullable=True)
    attachment_path = db.Column(db.String(500), nullable=True)
    attachment_name = db.Column(db.String(255), nullable=True)
    attachment_mime = db.Column(db.String(120), nullable=True)

    status_from = db.Column(db.String(30), nullable=True)
    status_to = db.Column(db.String(30), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    user = db.relationship('User', backref=db.backref('request_messages', lazy=True))

    __table_args__ = (
        db.Index('ix_request_message_request_id_created_at', 'request_id', 'created_at'),
    )


class RequestRevision(db.Model):
    """
    İsteklerin revision geçmişini ve karşılaştırma için snapshot verisini tutar.
    """
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey('request.id', ondelete='CASCADE'), nullable=False, index=True)
    revision_no = db.Column(db.Integer, nullable=False)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    submitted_by = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True)
    status_at_submit = db.Column(db.String(30), nullable=False, default='beklemede')
    snapshot = db.Column(db.JSON, nullable=False)

    user = db.relationship('User', backref=db.backref('request_revisions', lazy=True))

    __table_args__ = (
        db.UniqueConstraint('request_id', 'revision_no', name='uq_request_revision_request_revision_no'),
        db.Index('ix_request_revision_request_id_submitted_at', 'request_id', 'submitted_at'),
    )
