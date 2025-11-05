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
    Kullanıcı adı, şifre, rol ve özel izinleri içerir.
    """
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.String(20), default='user')  # 'admin' ya da 'user'
    
    # Kullanıcıya özel izinler
    can_add_product = db.Column(db.Boolean, default=False)
    can_delete_product = db.Column(db.Boolean, default=False)

    def __init__(self, username, password, role='user', can_add_product=False, can_delete_product=False):
        self.username = username
        self.set_password(password)
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

    def check_password(self, password):
        """Verilen şifrenin hash'lenmiş şifre ile eşleşip eşleşmediğini kontrol eder."""
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
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
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
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
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
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))

    # İsteği oluşturan kullanıcı ile ilişki.
    user = db.relationship('User', backref='requests')
