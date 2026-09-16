"""Sorgudaki filtre kolonlarının (predicate) sözdizim ağacından çıkarılması (Faz 31 İŞ 1b).

## Neden regex değil

Index danışmanı filtre kolonlarını regex ile arıyordu. Gerçek sorgularla ölçüldü (ILERLEME.md
Faz 31 Commit 2) ve iki tür hata çıktı:

* **Kör noktalar**: `lower(email) = $1` ve `date_trunc('day', created_at) = $1` hiç
  görünmüyordu (regex karakter sınıfında `(` yok). İki tablolu bir sorguda niteliksiz
  kolonlar `__unknown` kovasına düşüp atılıyordu.
* **YANLIŞ adaylar**: `status::text = $1` sorgusunda `text` bir KOLON sanılıyordu;
  `IN` operatörü `JOIN` ve `EXISTS` kelimelerinin içinde eşleşip `JO` ve `EX` diye
  kolonlar üretiyordu.

`$1` yer tutucuları suçlu DEĞİLDİ: aynı sorgu yer tutuculu ve literal hâliyle aynı sonucu
veriyordu. Ayrıca sqlglot `$1`'i `exp.Parameter` olarak doğru ayrıştırıyor.

## Nasıl çalışıyor

sqlglot'un kapsam (scope) çözümleyicisi her SELECT için "bu kapsamda hangi ad hangi
kaynağa bağlı" haritasını veriyor. Bir kolon:

* bir TABLOYA bağlıysa → doğrudan o tablo,
* bir CTE'ye / türetilmiş tabloya bağlıysa → o kapsamın projeksiyonunda aynı adlı sade
  kolon aranır ve içeri doğru izlenir (`WITH r AS (SELECT * FROM orders) ... WHERE r.total`
  → `orders.total`),
* dış kapsama bağlıysa (bağıntılı EXISTS: `WHERE o.customer_id = c.id`) → üst kapsamlarda
  aranır,
* nitelenmemişse ve kapsamda tek kaynak varsa → o kaynak; birden çok kaynak varsa
  ADAYLAR listelenir ve karar KATALOĞA bırakılır (danışman `pg_attribute` ile çözüyor).
  Tahmin yürütülmez.

Her predicate'in NEDEN index'e dönüştürülemediği de taşınıyor (`unusable_reason`) —
"öneri yok" sonucunun denetlenebilir olması için.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from app.services.sql_analysis import resolve_schema

# Predicate türleri. Sıralama index kolon sırasını belirliyor (danışmanda).
KIND_EQ = "eq"
KIND_IN = "in"
KIND_IS_NULL = "is_null"
KIND_JOIN = "join"
KIND_RANGE = "range"
KIND_LIKE_PREFIX = "like_prefix"
KIND_LIKE_UNANCHORED = "like_unanchored"
KIND_LIKE_UNKNOWN = "like_unknown"
KIND_SORT = "sort"
KIND_GROUP = "group"
KIND_OTHER = "other"

_RANGE_OPS = (exp.GT, exp.GTE, exp.LT, exp.LTE)


@dataclass(frozen=True)
class ColumnSource:
    """Kolonun çözüldüğü GERÇEK tablo."""

    schema: str
    table: str
    schema_assumed: bool = False


@dataclass
class ColumnPredicate:
    column: str
    kind: str
    #: where | join_on | having | order_by | group_by
    clause: str
    #: main | cte | subquery | exists — predicate'in bulunduğu kapsam türü.
    context: str
    #: Çözülebildiyse gerçek tablo; çözülemediyse None.
    source: ColumnSource | None = None
    #: Nitelenmemiş kolon ve birden çok aday tablo: karar kataloğa kalıyor.
    candidates: list[ColumnSource] = field(default_factory=list)
    #: Kolon bir ifadeyle sarılıysa ifadenin tablo nitelemesi temizlenmiş PostgreSQL metni
    #: (`lower(email)`), değilse None.
    expression: str | None = None
    #: Sorgudaki orijinal metin — kullanıcıya gösterim için.
    text: str = ""
    usable: bool = True
    unusable_reason: str | None = None

    def mark_unusable(self, reason: str) -> None:
        self.usable = False
        self.unusable_reason = reason


@dataclass
class PredicateExtraction:
    predicates: list[ColumnPredicate] = field(default_factory=list)
    #: Ayrıştırma başarısızsa sebebi; predicates boştur.
    parse_error: str | None = None


def extract_predicates(sql: str, *, default_schema: str = "public") -> PredicateExtraction:
    try:
        tree = sqlglot.parse_one(sql, dialect="postgres")
    except Exception as exc:  # noqa: BLE001 — sqlglot farklı hata tipleri fırlatıyor
        return PredicateExtraction(parse_error=str(exc)[:200])
    if tree is None:
        return PredicateExtraction(parse_error="Sorgu ayrıştırılabilir bir ifade içermiyor.")

    try:
        scopes = traverse_scope(tree)
    except Exception as exc:  # noqa: BLE001
        # Kapsam çözümleyicisi bazı sözdizimlerinde (ör. desteklenmeyen LATERAL biçimleri)
        # hata veriyor. Tahmin yürütmek yerine SEBEBİYLE boş dönülüyor.
        return PredicateExtraction(parse_error=f"Sorgunun kapsam yapısı çözümlenemedi: {str(exc)[:160]}")

    out = PredicateExtraction()
    for scope in scopes:
        _Extractor(scope, default_schema, out.predicates, sql).run()
    return out


class _Extractor:
    def __init__(
        self, scope: Scope, default_schema: str, sink: list[ColumnPredicate], sql: str
    ) -> None:
        self.sql = sql
        self.scope = scope
        self.default_schema = default_schema
        self.sink = sink
        self.context = _context_of(scope)

    def run(self) -> None:
        select = self.scope.expression
        if not isinstance(select, exp.Select):
            return
        where = select.args.get("where")
        if where is not None:
            self._condition(where.this, "where", in_or=False)
        having = select.args.get("having")
        if having is not None:
            self._condition(having.this, "having", in_or=False)
        for join in select.args.get("joins") or []:
            on = join.args.get("on")
            if on is not None:
                self._condition(on, "join_on", in_or=False)
        order = select.args.get("order")
        if order is not None:
            for ordered in order.expressions:
                self._ordering(ordered.this, KIND_SORT, "order_by")
        group = select.args.get("group")
        if group is not None:
            for item in group.expressions:
                self._ordering(item, KIND_GROUP, "group_by")

    # --- Koşul ağacı ---------------------------------------------------------------------

    def _condition(self, node: exp.Expression, clause: str, *, in_or: bool) -> None:
        node = _unwrap_paren(node)
        if isinstance(node, exp.And):
            self._condition(node.this, clause, in_or=in_or)
            self._condition(node.expression, clause, in_or=in_or)
            return
        if isinstance(node, exp.Or):
            before = len(self.sink)
            self._condition(node.this, clause, in_or=True)
            self._condition(node.expression, clause, in_or=True)
            branch = self.sink[before:]
            if _same_column_equalities(branch):
                # `status = $1 OR status = $2` bir IN'dir: tek index karşılar.
                merged = branch[0]
                merged.kind = KIND_IN
                merged.usable = True
                merged.unusable_reason = None
                merged.text = node.sql("postgres")
                del self.sink[before + 1 :]
            return

        before = len(self.sink)
        self._atom(node, clause)
        if in_or and not isinstance(node, exp.Or):
            for pred in self.sink[before:]:
                if pred.usable:
                    pred.mark_unusable(
                        "OR ile bağlı bir koşulun parçası. Tek bir index OR'un iki dalını "
                        "birden karşılamaz; her dal için ayrı index gerekir ve planlayıcı "
                        "onları BitmapOr ile birleştirir."
                    )

    def _atom(self, node: exp.Expression, clause: str) -> None:
        text = node.sql("postgres")

        if isinstance(node, exp.Not):
            inner = _unwrap_paren(node.this)
            if isinstance(inner, exp.Exists):
                return  # iç kapsam kendi başına işleniyor
            for column in _columns_in_this_scope(inner, self.scope):
                self._emit(column, KIND_OTHER, clause, text).mark_unusable(
                    "Olumsuzlanmış koşul (NOT ...). B-tree index olumsuz koşulu karşılamaz; "
                    "bu kolon üzerinden seçicilik elde edilemez."
                )
            return

        if isinstance(node, exp.Exists):
            return  # bağıntılı koşullar iç kapsamda çıkarılıyor

        if isinstance(node, (exp.EQ, exp.NullSafeEQ)):
            self._comparison(node, clause, text, KIND_EQ)
            return
        if isinstance(node, _RANGE_OPS):
            self._comparison(node, clause, text, KIND_RANGE)
            return
        if isinstance(node, exp.NEQ):
            side, _ = _column_side(node, self.scope)
            if side is not None:
                pred = self._emit_side(side, KIND_OTHER, clause, text)
                if pred:
                    pred.mark_unusable(
                        "Eşitsizlik (<> / !=) koşulu. Satırların çoğunu döndürdüğü için "
                        "index taramasıyla karşılanmaz."
                    )
            return
        if isinstance(node, exp.Between):
            self._emit_side(node.this, KIND_RANGE, clause, text)
            return
        if isinstance(node, exp.In):
            self._emit_side(node.this, KIND_IN, clause, text)
            return
        if isinstance(node, exp.Is):
            self._emit_side(node.this, KIND_IS_NULL, clause, text)
            return
        if isinstance(node, (exp.Like, exp.ILike)):
            self._like(node, clause, text)
            return

        # Tanınmayan koşul (ör. `f(a, b)` dönen boolean, `col @> $1`): içindeki kolonlar
        # raporlanıyor ama index'e dönüştürülmüyor — sessizce atmak yerine.
        for column in _columns_in_this_scope(node, self.scope):
            self._emit(column, KIND_OTHER, clause, text).mark_unusable(
                f"Tanınmayan koşul türü ({type(node).__name__}). dbace bu operatör için "
                "B-tree index önermiyor; GIN/GiST gerektirebilir, EXPLAIN ile inceleyin."
            )

    def _comparison(self, node: exp.Binary, clause: str, text: str, kind: str) -> None:
        left, right = node.this, node.expression
        left_cols = _columns_in_this_scope(left, self.scope)
        right_cols = _columns_in_this_scope(right, self.scope)

        # İki tarafta da kolon, ikisi de sade ve FARKLI kaynaklara ait → join anahtarı.
        if (
            kind == KIND_EQ
            and isinstance(_unwrap_paren(left), exp.Column)
            and isinstance(_unwrap_paren(right), exp.Column)
        ):
            a = self._emit(_unwrap_paren(left), KIND_JOIN, clause, text)
            b = self._emit(_unwrap_paren(right), KIND_JOIN, clause, text)
            if a.source is not None and a.source == b.source and not a.candidates:
                # Aynı tablonun iki kolonu karşılaştırılıyor (a.x = a.y) — join değil.
                for pred in (a, b):
                    pred.kind = KIND_OTHER
                    pred.mark_unusable(
                        "Aynı tablonun iki kolonu birbiriyle karşılaştırılıyor; index "
                        "bu koşulu karşılamaz."
                    )
            return

        if left_cols and not right_cols:
            self._emit_side(left, kind, clause, text)
        elif right_cols and not left_cols:
            self._emit_side(right, kind, clause, text)  # eşitlik ve aralık yöne göre simetrik
        elif left_cols and right_cols:
            # İki tarafta ifade içinde kolonlar (ör. a.x + 1 = b.y): join olarak
            # değerlendirmek yanlış olurdu.
            for column in left_cols + right_cols:
                self._emit(column, KIND_OTHER, clause, text).mark_unusable(
                    "Karşılaştırmanın iki tarafında da ifade içinde kolon var; index "
                    "önerilecek sade bir filtre değil."
                )

    def _like(self, node: exp.Expression, clause: str, text: str) -> None:
        pattern = _unwrap_paren(node.expression)
        is_ilike = isinstance(node, exp.ILike)
        if isinstance(pattern, exp.Literal) and pattern.is_string:
            value = pattern.this
            if value[:1] in ("%", "_"):
                kind = KIND_LIKE_UNANCHORED
            elif is_ilike:
                # ILIKE önekli olsa bile büyük/küçük harf duyarsız: B-tree karşılamaz.
                kind = KIND_LIKE_UNANCHORED
            else:
                kind = KIND_LIKE_PREFIX
        else:
            kind = KIND_LIKE_UNKNOWN
        pred = self._emit_side(node.this, kind, clause, text)
        if pred and kind == KIND_LIKE_UNKNOWN:
            pred.mark_unusable(
                "LIKE kalıbı bir parametre ($1) — normalize edilmiş sorguda gerçek kalıp "
                "bilinmiyor. Kalıp 'abc%' gibi önekliyse B-tree (text_pattern_ops), "
                "'%abc' gibi baştan jokerliyse pg_trgm GIN index gerekir. Hangisi olduğu "
                "bilinmeden index önerilmiyor."
            )

    def _ordering(self, node: exp.Expression, kind: str, clause: str) -> None:
        node = _unwrap_paren(node)
        if isinstance(node, exp.Literal):
            return  # ORDER BY 1 — konumsal; projeksiyona bakmak gerekir, önerilmiyor
        columns = _columns_in_this_scope(node, self.scope)
        if not columns:
            return
        self._emit_side(node, kind, clause, node.sql("postgres"))

    # --- Kolon → tablo ---------------------------------------------------------------------

    def _emit_side(
        self, side: exp.Expression, kind: str, clause: str, text: str
    ) -> ColumnPredicate | None:
        side = _unwrap_paren(side)
        columns = _columns_in_this_scope(side, self.scope)
        if not columns:
            return None
        if isinstance(side, exp.Column):
            return self._emit(side, kind, clause, text)
        if len(columns) > 1:
            pred = self._emit(columns[0], kind, clause, text)
            pred.mark_unusable(
                "Koşul birden çok kolonu birleştiren bir ifade üzerinde; tek kolonlu bir "
                "index bunu karşılamaz."
            )
            return pred
        pred = self._emit(columns[0], kind, clause, text)
        pred.expression = original_expression(side, self.sql)
        return pred

    def _emit(self, column: exp.Column, kind: str, clause: str, text: str) -> ColumnPredicate:
        pred = ColumnPredicate(column=column.name, kind=kind, clause=clause, context=self.context, text=text)
        source, candidates, reason = _resolve(column, self.scope, self.default_schema)
        pred.source = source
        pred.candidates = candidates
        if source is None and not candidates:
            pred.mark_unusable(reason or "Kolonun ait olduğu tablo çözülemedi.")
        self.sink.append(pred)
        return pred


def _resolve(
    column: exp.Column, scope: Scope, default_schema: str
) -> tuple[ColumnSource | None, list[ColumnSource], str | None]:
    """Kolonun GERÇEK tablosu. (kaynak, adaylar, çözülemediyse sebep)."""
    qualifier = column.table
    current: Scope | None = scope
    while current is not None:
        if qualifier:
            source = _source_named(current, qualifier)
            if source is not None:
                return _follow(source, column.name, default_schema, depth=0)
        else:
            sources = list(current.selected_sources.values())
            if len(sources) == 1:
                _, source = sources[0]
                return _follow(source, column.name, default_schema, depth=0)
            if len(sources) > 1:
                candidates: list[ColumnSource] = []
                for _, source in sources:
                    resolved, more, _ = _follow(source, column.name, default_schema, depth=0)
                    if resolved is not None:
                        candidates.append(resolved)
                    candidates.extend(more)
                if candidates:
                    return None, _dedupe(candidates), None
                return None, [], (
                    f"'{column.name}' kolonu nitelenmemiş ve sorgudaki kaynakların hiçbirine "
                    "güvenle bağlanamadı."
                )
        current = current.parent
    return None, [], (
        f"'{qualifier}.{column.name}' referansındaki '{qualifier}' adı sorguda tanımlı bir "
        "tabloya ya da takma ada karşılık gelmiyor." if qualifier else
        f"'{column.name}' kolonunun kaynağı bulunamadı."
    )


def _source_named(scope: Scope, name: str):
    lowered = name.lower()
    for alias, (_, source) in scope.selected_sources.items():
        if alias.lower() == lowered:
            return source
    return None


def _follow(source, column_name: str, default_schema: str, depth: int):
    """Kaynak bir tabloysa onu döndürür; CTE/türetilmiş tabloysa içeri doğru izler."""
    if isinstance(source, exp.Table):
        if isinstance(source.this, exp.Func):
            return None, [], "Kolon bir fonksiyon kaynağından (ör. generate_series) geliyor."
        schema, assumed = resolve_schema(source, default_schema)
        return ColumnSource(schema=schema, table=source.name, schema_assumed=assumed), [], None
    if not isinstance(source, Scope) or depth > 8:
        return None, [], "Kolonun kaynağı çözülemedi."

    inner = source.expression
    if not isinstance(inner, exp.Select):
        # UNION gibi birleşik sorgular: kolon birden çok dala dağılıyor.
        return None, [], (
            f"'{column_name}' bir UNION/set işlemi sonucundan geliyor; tek bir tabloya "
            "bağlanamadığı için index önerilmiyor."
        )
    for projection in inner.expressions:
        if isinstance(projection, exp.Star):
            inner_sources = list(source.selected_sources.values())
            if len(inner_sources) == 1:
                return _follow(inner_sources[0][1], column_name, default_schema, depth + 1)
            continue
        if isinstance(projection, exp.Column) and projection.name == "*":
            qualified = _source_named(source, projection.table) if projection.table else None
            if qualified is not None:
                return _follow(qualified, column_name, default_schema, depth + 1)
            continue
        if projection.alias_or_name.lower() != column_name.lower():
            continue
        target = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(target, exp.Column):
            resolved = _resolve(target, source, default_schema)
            return resolved
        return None, [], (
            f"'{column_name}' CTE/alt sorgu içinde bir İFADEDEN türetiliyor "
            f"({target.sql('postgres')[:80]}); filtre ifadenin sonucuna uygulandığı için "
            "tablo kolonuna index önerilemez."
        )
    return None, [], (
        f"'{column_name}' kolonu CTE/alt sorgunun çıktısında bulunamadı."
    )


# --- Yardımcılar -------------------------------------------------------------------------


def _context_of(scope: Scope) -> str:
    if scope.is_cte:
        return "cte"
    parent = scope.expression.parent
    while parent is not None and isinstance(parent, (exp.Subquery, exp.Paren)):
        parent = parent.parent
    if isinstance(parent, exp.Exists):
        return "exists"
    if scope.parent is not None:
        return "subquery"
    return "main"


def _unwrap_paren(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _columns_in_this_scope(node: exp.Expression, scope: Scope) -> list[exp.Column]:
    """Düğümdeki kolonlar — iç alt sorgulara ait olanlar HARİÇ (onlar kendi kapsamında)."""
    found: list[exp.Column] = []
    for column in node.find_all(exp.Column):
        if column.name == "*":
            continue
        enclosing = column.find_ancestor(exp.Select)
        if enclosing is not None and enclosing is not scope.expression:
            continue
        found.append(column)
    return found


def _column_side(node: exp.Binary, scope: Scope):
    if _columns_in_this_scope(node.this, scope):
        return node.this, node.expression
    if _columns_in_this_scope(node.expression, scope):
        return node.expression, node.this
    return None, None


def _same_column_equalities(preds: list[ColumnPredicate]) -> bool:
    if len(preds) < 2:
        return False
    first = preds[0]
    return all(
        p.kind in (KIND_EQ, KIND_IN)
        and p.column == first.column
        and p.source == first.source
        and p.candidates == first.candidates
        and p.expression == first.expression
        and (p.source is not None or p.candidates)
        and (p.unusable_reason is None or p.unusable_reason.startswith("OR ile"))
        for p in preds
    )


def original_expression(node: exp.Expression, sql: str) -> str:
    """İfadenin SORGUDAKİ yazımı, tablo nitelemeleri temizlenmiş.

    NEDEN sqlglot'un yeniden üretimi değil: sqlglot sabitleri normalleştiriyor —
    `date_trunc('day', ts)` → `DATE_TRUNC('DAY', ts)`. PostgreSQL ifade index'ini sorguyla
    eşleştirirken sabitleri DEĞERİYLE karşılaştırır; 'DAY' ile 'day' farklı sabitlerdir ve
    'DAY' ile kurulan index, 'day' yazan sorguda HİÇ kullanılmaz. Geçerli ama işe yaramaz
    bir DDL önermek, öneri üretmemekten kötüdür.

    Tür dönüşümleri (`x::date`) için iç ifadenin orijinal metni + tür adı kullanılıyor:
    tür adları büyük/küçük harf duyarsız, yeniden üretmek güvenli.
    """
    if isinstance(node, exp.Cast):
        inner = original_expression(node.this, sql)
        return f"({inner})::{node.to.sql('postgres').lower()}"

    span = _source_span(node, sql)
    if span is None:
        # Konum bilgisi yok: yeniden üretime düşülüyor. Danışman bu metni katalogda
        # doğruluyor (geçici tabloda CREATE INDEX), yani sözdizimi hatası sessiz kalmaz.
        copy = node.copy()
        for column in copy.find_all(exp.Column):
            column.set("table", None)
        return copy.sql("postgres")

    start, end = span
    text = sql[start:end]
    # Nitelemeleri sağdan sola sil ki önceki konumlar kaymasın.
    removals = []
    for column in node.find_all(exp.Column):
        table_id = column.args.get("table")
        meta = getattr(table_id, "meta", None) or {}
        if "start" in meta and start <= meta["start"] < end:
            dot = sql.find(".", meta["end"] + 1)
            if dot != -1:
                removals.append((meta["start"] - start, dot + 1 - start))
    for a, b in sorted(removals, reverse=True):
        text = text[:a] + text[b:]
    return text.strip()


def _source_span(node: exp.Expression, sql: str) -> tuple[int, int] | None:
    starts = [
        n.meta["start"] for n in node.walk() if isinstance(getattr(n, "meta", None), dict) and "start" in n.meta
    ]
    ends = [
        n.meta["end"] for n in node.walk() if isinstance(getattr(n, "meta", None), dict) and "end" in n.meta
    ]
    if not starts or not ends:
        return None
    start, end = min(starts), max(ends) + 1
    # Fonksiyon çağrısının kapanış parantezine kadar ilerle (tırnak içi sayılmaz).
    depth = 0
    index = start
    in_quote = False
    last_close = None
    while index < len(sql):
        char = sql[index]
        if in_quote:
            if char == "'":
                if index + 1 < len(sql) and sql[index + 1] == "'":
                    index += 2
                    continue
                in_quote = False
        elif char == "'":
            in_quote = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                last_close = index
                break
            if depth < 0:
                break
        elif depth == 0 and index >= end:
            break
        index += 1
    if last_close is not None and last_close + 1 >= end:
        end = last_close + 1
    return start, end


def _dedupe(items: list[ColumnSource]) -> list[ColumnSource]:
    seen: list[ColumnSource] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
