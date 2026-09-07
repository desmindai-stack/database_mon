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
