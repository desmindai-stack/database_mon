-- Faz 31 Commit 9 (madde 1, sır yönetimi): şifre değişince ESKİ oturumlar düşsün.
--
-- Ölçüldü: jetonlar durumsuz (stateless) JWT. Şifre değiştirildiğinde ya da yönetici şifreyi sıfırladığında
-- ESKİ şifreyle alınmış access jetonu süresi dolana kadar (varsayılan 60 dk), refresh jetonu 7 gün boyunca
-- geçerli kalıyordu — yani "şifreyi değiştirdim" demek "oturumları kapattım" demek DEĞİLDİ. Canlıda eski bir
-- ADMIN_PASSWORD kalmıştı ve değiştirildi; o anda üretilmiş bir jeton hâlâ çalışıyordu.
--
-- Artık her jetonun `iat` (üretim anı) değeri kullanıcının `password_changed_at` değerinden ÖNCEYSE jeton
-- reddediliyor (services/auth_deps.py). Kolon NULL kalabilir: hiç şifre değiştirmemiş kullanıcı için kontrol
-- yok (davranış eskisi gibi).

ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;
