import os

def fix_turkish_chars(content):
    """Bozuk Türkçe karakterleri düzeltir"""
    replacements = {
        'Ş': 'ş',
        'Ğ': 'ğ',
        'Ü': 'ü',
        'Ö': 'ö',
        'Ç': 'ç',
        'İ': 'i',
        'ı': 'ı',
        'eŞ': 'eş',
        'iŞ': 'iş',
        'boŞ': 'boş',
        'Şi': 'şi',
        'Şle': 'şle',
        'deĞi': 'deği',
        'deĞ': 'değ'
    }
    
    for old, new in replacements.items():
        content = content.replace(old, new)
    return content

def fix_file(filepath):
    """Dosyadaki bozuk Türkçe karakterleri düzeltir"""
    try:
        # Dosyayı oku
        with open(filepath, 'r', encoding='utf-8') as file:
            content = file.read()
        
        # Türkçe karakterleri düzelt
        fixed_content = fix_turkish_chars(content)
        
        # Değişiklik varsa kaydet
        if content != fixed_content:
            with open(filepath, 'w', encoding='utf-8') as file:
                file.write(fixed_content)
            print(f"✅ Dosya düzeltildi: {filepath}")
        else:
            print(f"ℹ️ Düzeltme gerekmedi: {filepath}")
            
    except Exception as e:
        print(f"❌ Hata oluştu: {str(e)}")

if __name__ == "__main__":
    # Düzeltilecek dosya yolu 
    file_to_fix = "/var/www/html/app.py"
    
    if os.path.exists(file_to_fix):
        fix_file(file_to_fix)
    else:
        print(f"❌ Dosya bulunamadı: {file_to_fix}")