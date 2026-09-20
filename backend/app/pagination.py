"""Liste uçlarının ortak sayfalaması (Faz 31 Commit 9, egress).

Canlıda Supabase egress kotası aşıldığında liste uçları sınırsızdı: satır sayısı veriyle birlikte büyüyordu.
Her liste ucu `limit` (üst sınırlı) ve `offset` alıyor ve bunları SQL'e (LIMIT/OFFSET) uyguluyor;
`tests/test_meta_query_guards.py` OpenAPI'deki her liste ucunu denetliyor.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Query

#: Bir sayfada en fazla kalem.
MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 100


@dataclass(frozen=True)
class Page:
    limit: int
    offset: int

    def apply(self, statement):
        """SQL'e uygular — sayfalama Python'da değil veritabanında."""
        return statement.limit(self.limit).offset(self.offset)

    def slice(self, items: list) -> list:
        """Hesaplanmış (SQL satırı olmayan) listeler için."""
        return items[self.offset:self.offset + self.limit]


def page_params(
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Sayfadaki en fazla kalem."),
    offset: int = Query(default=0, ge=0, description="Atlanacak kalem sayısı."),
) -> Page:
    return Page(limit=limit, offset=offset)
