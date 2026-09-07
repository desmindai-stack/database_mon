import { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError, errorMessage } from "../api";

/**
 * Sayfa geneli durum ekranları (Faz 19 İŞ 1 + İŞ 3).
 *
 * Aynı üç durum — yükleniyor / hata / kayıt yok — her sayfada farklı görünüyordu: kimi yerde
 * `<div className="error">{ham JSON}</div>`, kimi yerde hiçbir şey. Burası tek görünüm.
 */

export function PageLoading({ label = "Yükleniyor…" }: { label?: string }) {
  return (
    <div className="page-state" role="status" aria-live="polite">
      <div className="page-state-spinner" aria-hidden="true" />
      <p className="page-state-detail">{label}</p>
    </div>
  );
}

/**
 * İskelet yükleme — içeriğin YERİNİ KORUR (Faz 22 İŞ 2).
 *
 * `PageLoading` ortalanmış küçük bir kutu; veri gelince sayfa boyu birden değişiyor ve
 * içerik sıçrıyordu. İskelet, gelecek içeriğin kabaca yüksekliğini şimdiden ayırıyor:
 * başlık satırı + belirtilen sayıda satır.
 *
 * `rows` gelecek listenin uzunluğuna yakın seçilmeli — abartmak da sıçrama yaratır.
 */
export function PageSkeleton({
  rows = 5,
  withHeader = true,
  label = "Yükleniyor…",
}: {
  rows?: number;
  withHeader?: boolean;
  label?: string;
}) {
  return (
    <div className="skeleton" role="status" aria-live="polite" aria-label={label}>
      {withHeader && <div className="skeleton-line skeleton-header" />}
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton-line" />
      ))}
      <span className="sr-only">{label}</span>
    </div>
  );
}

/** Tablo gövdesinde iskelet satırlar — `TableState`'in yükleniyor hâlinin yerini tutar. */
export function TableSkeleton({ colSpan, rows = 5 }: { colSpan: number; rows?: number }) {
  return (
    <>
      {Array.from({ length: rows }, (_, i) => (
        <tr key={i} className="skeleton-row">
          <td colSpan={colSpan}>
            <div className="skeleton-line" />
          </td>
        </tr>
      ))}
    </>
  );
}

/**
 * Bir API çağrısı başarısız olduğunda gösterilir. `onRetry` verilirse "Tekrar dene" düğmesi
 * çıkar — kullanıcının sayfayı komple yenilemeye mecbur kalmaması için.
 */
export function PageError({
  error,
  onRetry,
  children,
}: {
  error: unknown;
  onRetry?: () => void;
  children?: ReactNode;
}) {
  const status = error instanceof ApiError ? error.status : null;
  // Ağ kopması ile sunucu hatası kullanıcı için farklı eylemler demek.
  const title =
    status === 0
      ? "Sunucuya ulaşılamadı"
      : status === 403
        ? "Bu içeriği görme yetkiniz yok"
        : status !== null && status >= 500
          ? "Sunucu hatası"
          : "Veri alınamadı";
  return (
    <div className="page-state page-state-error" role="alert">
      <div className="page-state-icon">⚠️</div>
      <h3>{title}</h3>
      <p className="page-state-detail">{errorMessage(error)}</p>
      {children}
      {onRetry && (
        <div className="page-state-actions">
          <button type="button" className="btn btn-primary" onClick={onRetry}>
            Tekrar dene
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * İstenen kayıt yok (silinmiş instance, eski rapor, elle yazılmış hatalı URL).
 *
 * Eskiden bu durumda ya ham hata metni ya da sonsuz "Yükleniyor…" görünüyordu ve geri dönüş
 * yolu yoktu — kullanıcı 404 sanıyordu. Artık ne aradığı, neden bulunamamış olabileceği ve
 * nereye gidebileceği yazıyor.
 */
export function NotFoundState({
  title = "Kayıt bulunamadı",
  detail,
  backTo,
  backLabel,
}: {
  title?: string;
  detail?: string;
  backTo: string;
  backLabel: string;
}) {
  return (
    <div className="page-state page-state-notfound" role="alert">
      <div className="page-state-icon">🔎</div>
      <h3>{title}</h3>
      <p className="page-state-detail">
        {detail ?? "Bu kayıt silinmiş olabilir ya da adres yanlış yazılmış olabilir."}
      </p>
      <div className="page-state-actions">
        <Link to={backTo} className="btn btn-primary">
          {backLabel}
        </Link>
      </div>
    </div>
  );
}

/**
 * Boş liste. "Kayıt yok" demek yetmiyor — NEDEN boş ve NE yapılmalı da yazılmalı (İŞ 3).
 */
export function EmptyState({
  title,
  detail,
  action,
}: {
  title: string;
  detail?: string;
  action?: ReactNode;
}) {
  return (
    <div className="page-state page-state-empty">
      <div className="page-state-icon" aria-hidden="true">
        📭
      </div>
      <h3>{title}</h3>
      {detail && <p className="page-state-detail">{detail}</p>}
      {action && <div className="page-state-actions">{action}</div>}
    </div>
  );
}

/**
 * Tablo gövdesindeki tek satırlık durum (Faz 19 İŞ 2/İŞ 3).
 *
 * Liste sayfalarının hepsi boş tabloya `<td className="empty">Kayıt yok</td>` basıyordu — ve
 * bunu YÜKLEME BAŞARISIZ OLDUĞUNDA DA basıyordu. Yani API düştüğünde kullanıcı "kayıt yok"
 * okuyup gerçekten kayıt olmadığına inanıyordu. Üç durum artık ayrı: yükleniyor, hata
 * (tekrar dene), gerçekten boş.
 */
export function TableState({
  colSpan,
  loading = false,
  error = null,
  onRetry,
  title,
  detail,
  action,
}: {
  colSpan: number;
  loading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  title: string;
  detail?: string;
  action?: ReactNode;
}) {
  if (error) {
    return (
      <tr>
        <td colSpan={colSpan} className="table-state">
          <span className="table-state-error">⚠️ Liste yüklenemedi — {errorMessage(error)}</span>
          {onRetry && (
            <button type="button" className="btn btn-xs" onClick={onRetry}>
              Tekrar dene
            </button>
          )}
        </td>
      </tr>
    );
  }
  if (loading) {
    // Tek satırlık bir spinner yerine iskelet satırlar: tablo yüksekliği veri gelince
    // birden değişmiyor, sayfa sıçramıyor (Faz 22 İŞ 2).
    return <TableSkeleton colSpan={colSpan} />;
  }
  return (
    <tr>
      <td colSpan={colSpan} className="table-state">
        <strong>{title}</strong>
        {detail && <span className="table-state-detail">{detail}</span>}
        {action}
      </td>
    </tr>
  );
}
