"""Bir kullanıcının şifresini elle sıfırlar — admin hesabı kilitlenirse (şifre unutuldu,
ADMIN_PASSWORD .env'e geç eklendi vb.) çıkış yolu.

Kullanım (backend/ dizininden):
    python scripts/reset_admin_password.py <kullanici_adi> <yeni_sifre> [--activate]

--activate: kullanıcı pasifse (is_active=False) aynı zamanda aktifleştirir.

Şifre sıfırlanınca must_change_password otomatik olarak False'a çekilir — bu
script'i çalıştırabilen kişinin zaten veritabanına/sunucuya doğrudan erişimi
var, web arayüzünde ayrıca bir "ilk girişte şifre değiştir" adımına zorlamanın
güvenlik faydası yok (bkz. SORULAR.md).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import User  # noqa: E402
from app.services.security import hash_password  # noqa: E402


async def reset_password(username: str, new_password: str, activate: bool) -> None:
    await init_db()
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if user is None:
            existing_usernames = (await session.execute(select(User.username))).scalars().all()
            print(f"Kullanıcı bulunamadı: {username!r}")
            if existing_usernames:
                print(f"Kayıtlı kullanıcılar: {', '.join(sorted(existing_usernames))}")
            else:
                print(
                    "users tablosu boş — uygulamayı bir kere başlatıp ilk admin bootstrap'ının "
                    "çalışmasını bekleyin (ADMIN_USERNAME/ADMIN_PASSWORD .env'de tanımlıysa o "
                    "kimlik bilgileriyle otomatik oluşturulur)."
                )
            raise SystemExit(1)

        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        if activate:
            user.is_active = True
        await session.commit()

        status = "aktif" if user.is_active else "PASİF — aktifleştirmek için --activate ekleyin"
        print(f"'{username}' şifresi güncellendi. Durum: {status}.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("username", help="Şifresi sıfırlanacak kullanıcı adı")
    parser.add_argument("new_password", help="Yeni şifre")
    parser.add_argument(
        "--activate", action="store_true", help="Kullanıcı pasifse (is_active=False) aynı zamanda aktifleştir"
    )
    args = parser.parse_args()

    if len(args.new_password) < 8:
        print("Uyarı: şifre 8 karakterden kısa (web arayüzündeki minimum uzunluk) — yine de ayarlanacak.")

    asyncio.run(reset_password(args.username, args.new_password, args.activate))


if __name__ == "__main__":
    main()
