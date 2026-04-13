# Envanter Yönetim Uygulaması

Bu depo, küçük bir Flask tabanlı envanter/bileşen yönetim uygulamasına aittir. Uygulama kullanıcı yönetimi, bileşen CRUD, envanter (seri numaraları), ödünç alma/iade, sarf işlemleri ve istek yönetimi içerir.

Bu README GitLab deposu için hazırlanmıştır ve proje kurulumu, çalıştırılması, ortam değişkenleri, veritabanı göçleri ve sık kullanılan yardımcı scriptlerin nasıl kullanılacağını açıklar.

## İçindekiler

- Gereksinimler
- Kurulum (geliştirme)
- Ortam Değişkenleri
- Veritabanı ve Migration'lar
- Uygulamayı Çalıştırma
  - Geliştirme
  - Production (örnek WSGI)
- Proje Yapısı
- Admin Paneli
- Dosya Yüklemeleri ve İzinler
- Yönetim Scriptleri
- Testler / Hızlı Kontroller
- Sorun Giderme
- Lisans ve Katkıda Bulunma


## Gereksinimler

- Python 3.10+ (projede ZoneInfo kullanılıyor; Python 3.9+ uyumludur)
- pip
- Bir veritabanı: SQLite (varsayılan), PostgreSQL veya MySQL (SQLALchemy URI ile)
- (Opsiyonel) virtualenv/venv tavsiye edilir

Sistem paketleri (örnek Debian/Ubuntu):

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip build-essential libpq-dev
```

## Kurulum (geliştirme)

1. Depoyu klonlayın veya sunucuya yerleştirin.
2. Sanal ortam oluşturun ve etkinleştirin:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

3. Bağımlılıkları yükleyin:

```bash
pip install -r requirements.txt
```

4. Ortam değişkenlerini ayarlayın (aşağıdaki `Ortam Değişkenleri` bölümüne bakın). Geliştirme için `.env` dosyası kullanabilirsiniz.

5. Veritabanını hazır hale getirin:

```bash
flask db upgrade
# veya uygulama içinden otomatik: python3 app.py (ilk çalıştırmada db.create_all() çalışır)
```

Not: Projede Flask-Migrate (`migrations/`) zaten mevcuttur; yeni migration eklemek için `flask db migrate -m "msg"` kullanın.

## Ortam Değişkenleri

Aşağıdaki ortam değişkenlerini üretim ve geliştirme için ayarlayın. `.env` dosyası kullanmak isterseniz repository'de örnek olarak `.env.example` oluşturabilirsiniz.

- SECRET_KEY: Uygulama gizli anahtarı (zorunlu üretimde)
- DATABASE_URL: SQLAlchemy bağlantı dizesi. Varsayılan: SQLite `sqlite:///instance/database.db`
- UPLOAD_FOLDER: (İsteğe bağlı) Yüklemelerin kaydedileceği tam yol. Varsayılan `static/uploads`.
- MAX_CONTENT_LENGTH: Maksimum yükleme boyutu byte cinsinden. Varsayılan 2MB.
- BASE_URL: Uygulama temel URL'si (örn. `https://gitlab.example.com/group/project`)
- LOGO_URL: Eğer özel logo kullanıyorsanız, tam URL
- FLASK_DEBUG: `1` veya `true` geliştirme modu için
- FLASK_HOST, FLASK_PORT: `app.py` içindeki yerel çalışma ayarları

Örnek `.env` dosyası:

```
SECRET_KEY=change_me_in_production
DATABASE_URL=sqlite:///instance/database.db
UPLOAD_FOLDER=/var/www/html/static/uploads
MAX_CONTENT_LENGTH=2097152
FLASK_DEBUG=1
FLASK_HOST=0.0.0.0
FLASK_PORT=5000
BASE_URL=http://localhost:5000
LOGO_URL=
```

## Veritabanı ve Migration'lar

Projede `migrations/` klasörü mevcut ve Flask-Migrate kullanılıyor. Yeni bir migration oluşturmak için:

```bash
export FLASK_APP=app.py
flask db migrate -m "migration message"
flask db upgrade
```

Eğer SQLite kullanıyorsanız, `PRAGMA` ve WAL modu gibi ayarlar `app.py` içinde otomatik olarak uygulanır.

## Uygulamayı Çalıştırma

Geliştirme ortamı için:

```bash
# Sanal ortam aktifken
export FLASK_APP=app.py
export FLASK_ENV=development
flask run --host=0.0.0.0 --port=5000
```

Ya da doğrudan:

```bash
python3 app.py
```

Production (örnek WSGI + systemd)

- `app.wsgi` dosyası mevcut. Aşağıdaki `systemd` servis örneğiyle birlikte `gunicorn` veya `mod_wsgi` tercih edilebilir.

Örnek `systemd` (gunicorn) hizmet dosyası (`/etc/systemd/system/inventory.service`):

```
[Unit]
Description=Inventory Flask App
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/html
Environment="PATH=/var/www/html/.venv/bin"
Environment="FLASK_ENV=production"
Environment="DATABASE_URL=sqlite:////var/www/html/instance/database.db"
ExecStart=/var/www/html/.venv/bin/gunicorn -w 3 -b 0.0.0.0:8000 app:app

[Install]
WantedBy=multi-user.target
```

Sonra:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now inventory.service
```

Alternatif: Apache + mod_wsgi ile `app.wsgi` dosyasını kullanabilirsiniz.

## Proje Yapısı

```
/var/www/html/
├── app.py                 # Ana Flask uygulaması
├── models.py              # SQLAlchemy modelleri
├── app.wsgi               # WSGI entry point
├── requirements.txt       # Python bağımlılıkları
├── instance/              # Veritabanı dosyaları
├── migrations/            # Flask-Migrate dosyaları
├── scripts/               # Yönetim scriptleri
├── static/
│   ├── css/              # CSS dosyaları
│   ├── js/               # JavaScript dosyaları
│   ├── fonts/            # Font dosyaları
│   ├── uploads/          # Yüklenen dosyalar
│   └── vendor/           # Bootstrap ve diğer kütüphaneler
└── templates/
    ├── base.html         # Ana şablon
    ├── index.html        # Ana sayfa
    ├── login.html        # Giriş sayfası
    ├── requests.html     # İsteklerim sayfası
    ├── create_request.html
    ├── my_borrowed.html
    ├── admin/            # Admin paneli şablonları
    │   ├── dashboard.html
    │   ├── manage_users.html
    │   ├── add_user.html
    │   ├── edit_user.html
    │   ├── manage_requests.html
    │   ├── generate_pdf.html
    │   ├── borrow_return.html
    │   ├── component_list.html
    │   └── low_stock.html
    └── partials/         # Yeniden kullanılabilir parçalar
```

## Admin Paneli

Admin paneli tutarlı bir tasarım temasıyla oluşturulmuştur:

### Tema Özellikleri
- **Renk Paleti**: Indigo gradient (#1a237e → #3949ab)
- **Sayfa Başlığı**: Gradient arka plan, beyaz metin, geri butonu
- **Kartlar**: Beyaz arka plan, 16px border-radius, hafif gölge
- **Tablolar**: Gradient başlık, zebra satırları
- **Butonlar**: 25px border-radius, yeşil (kaydet), gri (iptal), kırmızı (sil)

### Admin Sayfaları
| Sayfa | URL | Açıklama |
|-------|-----|----------|
| Dashboard | `/admin` | Admin ana menüsü |
| Kullanıcı Yönetimi | `/admin/manage_users` | Kullanıcı listesi ve yönetimi |
| Kullanıcı Ekle | `/admin/add_user` | Yeni kullanıcı oluşturma |
| Kullanıcı Düzenle | `/admin/edit_user/<id>` | Kullanıcı bilgilerini güncelleme |
| İstek Yönetimi | `/admin/requests` | Tüm istekleri yönetme (kullanıcı filtreleme) |
| PDF Oluştur | `/admin/generate_pdf/<id>` | İstek için PDF talep formu |
| Ödünç Ver/İade Al | `/admin/borrow_return` | Ödünç işlemleri |
| Bileşen Listesi | `/components` | Tüm bileşenleri listeleme |
| Azalan Stoklar | `/azalan_stok` | Düşük stoklu ürünler |

### Kullanıcı Rolleri ve Yetkiler
- **Admin**: Tam yetki (kullanıcı yönetimi, istek onaylama, PDF oluşturma)
- **User**: Temel yetkiler + opsiyonel ürün ekleme/silme yetkisi

## Dosya Yüklemeleri ve İzinler

Uygulama `static/uploads` dizinine dosya yükler. Sunucu ortamında bu dizinin yazılabilir olduğundan emin olun:

```bash
sudo mkdir -p /var/www/html/static/uploads
sudo chown -R www-data:www-data /var/www/html/static/uploads
sudo chmod -R 750 /var/www/html/static/uploads
```

Ayrıca `instance/database.db` (SQLite) veya veritabanı socket/credentials için gerekli izinleri kontrol edin.

## Yönetim Scriptleri ve Yardımcı Araçlar

`/scripts` klasöründe bazı yardımcı scriptler bulunmaktadır:

- `create_admin.py`: İlk admin kullanıcı oluşturma (kullanım: `python3 scripts/create_admin.py`)
- `fix_chars.py`: Karakter temizleme/fix scripti
- `test_locations.py`: Lokasyon testi scripti

Kendi ihtiyaçlarınıza göre scriptleri düzenleyebilir veya yeni CLI komutları ekleyebilirsiniz.

## Testler / Hızlı Kontroller

Projede otomatik test framework'ü yoksa, manuel smoke-test yapın:

- Uygulamayı başlatın
- `/login` sayfasına gidin
- Admin olarak oturum açıp kullanıcı, bileşen ekleme/düzenleme/silme işlemlerini deneyin

Otomasyon eklemek isterseniz, `pytest` ve `Flask-Testing` ile küçük bir test suit'i oluşturmayı öneririm.

## Sorun Giderme

- "SECRET_KEY is not set" uyarısı: Üretim ortamında `SECRET_KEY` ayarlayın.
- Dosya yükleme hataları: `UPLOAD_FOLDER` izinlerini kontrol edin.
- Veritabanı kilitlenmeleri (SQLite): `PRAGMA journal_mode = WAL` ve `busy_timeout` ayarları `app.py` içindedir.
- Migration hataları: Mevcut `migrations/` ile eşleşmeyen modeller için dikkatli olun; migration oluştururken `flask db migrate` kullanın.

Loglar: Uygulamayı `systemd` veya `gunicorn` ile çalıştırıyorsanız, `journalctl -u inventory.service -f` komutuyla logları takip edin.

## Güvenlik Notları

- Üretim ortamında `DEBUG` kapalı olmalıdır.
- `SECRET_KEY` güçlü ve gizli olmalı.
- Dosya yüklemelerinde MIME/uzantı doğrulaması eklemek faydalıdır.

## CI / GitLab

Temel `.gitlab-ci.yml` eklemek için öneri: Python bağımlılıklarını kur, lint/test çalıştır, ve (opsiyonel) Docker image veya deployment job'u tetikle.

Basit örnek:

```yaml
image: python:3.11

stages:
  - test

before_script:
  - python -m venv venv
  - source venv/bin/activate
  - pip install -r requirements.txt

lint:
  stage: test
  script:
    - pip install flake8
    - flake8 .

# test job ekleyin, eğer test suite varsa
```

## Katkıda Bulunma


Katkıda bulunmak isterseniz, fork -> branch -> merge request akışı izlenmelidir.

## Son Güncellemeler (Aralık 2024)

- ✅ Admin paneli tasarımı tutarlı hale getirildi (indigo tema)
- ✅ İstek yönetimi sayfasına kullanıcı filtreleme eklendi
- ✅ "İstekler" → "İsteklerim" olarak değiştirildi (kullanıcılar sadece kendi isteklerini görür)
- ✅ İstek silme özelliği devre dışı bırakıldı
- ✅ Hover efektleri ve animasyonlar kaldırıldı
- ✅ Bileşen ekleme formunda hata durumunda form verileri korunuyor
- ✅ `low_stock.html` ve `component_list.html` admin klasörüne taşındı
- ✅ Tüm admin sayfaları ortak tasarım temasıyla güncellendi

---