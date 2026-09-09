"""Runs before any test module is collected/imported — guarantees DATABASE_URL points at a
dedicated test SQLite file (not the real dev data/dbace.db) before app.config.Settings() gets
instantiated by whichever test module happens to import app.database/app.config first."""

from __future__ import annotations

import os
from pathlib import Path

_TEST_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "dbace_pytest.db"
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TEST_DB_PATH}")

# Test veritabanı her oturumda SIFIRDAN oluşturuluyor.
#
# NEDEN: dosya kalıcı ve testler onu hiç temizlemiyordu; her koşu bir öncekinin üstüne
# yazıyor ve dosya sınırsız büyüyordu. Bu, yavaş bir suite'ten daha kötü bir sorun üretiyor:
# "global" kapsamlı bir rapor üreten testler birikmiş veriyi de tarıyor ve zamana bağlı
# testler bir gün eşiği aşıp KIRILIYOR — hem de kodda hiçbir şey değişmemişken. Böyle bir
# başarısızlık, gerçek bir gerilemeyle karıştırılabilir.
#
# Ölçüldü: 1.16 GB'a şişmiş bir dosyayla suite 682 saniye ve bir test kırık; sıfırdan
# başlayınca 75 saniye ve tamamı yeşil.
#
# Silme conftest'in en üstünde, uygulama import edilmeden yapılıyor — motor dosyayı açtıktan
# sonra silmek Windows'ta kilitli dosya hatası verirdi.
try:
    _TEST_DB_PATH.unlink()
except FileNotFoundError:
    pass
except OSError:
    # Dosya kilitliyse (paralel koşu, açık bir DB istemcisi) testi düşürmek yerine devam
    # ediliyor: eski davranış (birikmiş veri) yavaş ama çalışır.
    pass
