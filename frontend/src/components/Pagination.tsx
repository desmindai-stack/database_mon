import { useEffect, useMemo, useState } from "react";

/**
 * Uzun listeler için sayfalama (Faz 19 İŞ 3).
 *
 * Öncesinde her liste tek seferde render ediliyordu. Bir bankada birkaç yüz instance ya da
 * yüzlerce alarm kaydı olması normal; her satır kendi düğmeleri, rozetleri ve bağlantılarıyla
 * geldiği için bu, binlerce DOM düğümü demek ve tarayıcıyı kilitliyor.
 *
 * NOT: Bu istemci tarafı sayfalama — sunucu hâlâ tüm satırları tek yanıtta gönderiyor. Render
 * maliyetini (asıl darboğaz) çözüyor, ağ/bellek maliyetini çözmüyor. Sunucu tarafı sayfalama
 * ayrı bir iş; bkz. SORULAR.md.
 */
const PAGE_SIZES = [25, 50, 100] as const;
export const DEFAULT_PAGE_SIZE = 25;

export type PageSlice<T> = {
  items: T[];
  page: number;
  pageSize: number;
  total: number;
  setPage: (p: number) => void;
  setPageSize: (n: number) => void;
};

export function usePagination<T>(all: T[], initialSize: number = DEFAULT_PAGE_SIZE): PageSlice<T> {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(initialSize);
  const total = all.length;
  const lastPage = Math.max(1, Math.ceil(total / pageSize));

  // Liste kısaldığında (filtre, silme) boş bir sayfada asılı kalmayalım.
  useEffect(() => {
    if (page > lastPage) setPage(lastPage);
  }, [page, lastPage]);

  const items = useMemo(
    () => all.slice((page - 1) * pageSize, page * pageSize),
    [all, page, pageSize],
  );

  return { items, page, pageSize, total, setPage, setPageSize };
}

/** Sayfalama çubuğu. Tek sayfaya sığan listelerde hiç görünmez — gereksiz gürültü olmasın. */
export function Pagination({ slice, label = "kayıt" }: { slice: PageSlice<unknown>; label?: string }) {
  const { page, pageSize, total, setPage, setPageSize } = slice;
  const lastPage = Math.max(1, Math.ceil(total / pageSize));
  if (total <= PAGE_SIZES[0]) return null;

  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(page * pageSize, total);

  return (
    <div className="pagination">
      <span className="pagination-info">
        {from}–{to} / {total} {label}
      </span>
      <div className="pagination-controls">
        <button
          type="button"
          className="btn btn-xs"
          onClick={() => setPage(page - 1)}
          disabled={page <= 1}
        >
          ‹ Önceki
        </button>
        <span className="pagination-page">
          {page} / {lastPage}
        </span>
        <button
          type="button"
          className="btn btn-xs"
          onClick={() => setPage(page + 1)}
          disabled={page >= lastPage}
        >
          Sonraki ›
        </button>
        <label className="pagination-size">
          Sayfa başına
          <select
            value={pageSize}
            onChange={(e) => {
              setPageSize(Number(e.target.value));
              setPage(1);
            }}
          >
            {PAGE_SIZES.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
      </div>
    </div>
  );
}

/**
 * "Önce ilk N tanesini göster" (Faz 19 İŞ 3).
 *
 * Sayfalamanın uygun olmadığı yerler için: rapor bölümlerindeki bulgular baştan sona okunur,
 * sayfalara bölmek okumayı bozar. Ama tek bir bölümde onlarca bulgu olabiliyor ve her bulgu
 * kartı kendi öneri/adım/komut bloklarını taşıdığı için render maliyeti yüksek.
 */
export function useShowMore<T>(all: T[], initial = 10): { items: T[]; hidden: number; showAll: () => void; expanded: boolean } {
  const [expanded, setExpanded] = useState(false);
  const items = expanded ? all : all.slice(0, initial);
  return {
    items,
    hidden: Math.max(0, all.length - items.length),
    showAll: () => setExpanded(true),
    expanded,
  };
}

export function ShowMoreButton({ hidden, onClick, label = "bulgu" }: { hidden: number; onClick: () => void; label?: string }) {
  if (hidden <= 0) return null;
  return (
    <button type="button" className="btn btn-xs show-more-btn" onClick={onClick}>
      {hidden} {label} daha göster
    </button>
  );
}
