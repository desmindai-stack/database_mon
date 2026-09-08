import { Link, useLocation } from "react-router-dom";

/**
 * Tanımlı hiçbir rotaya uymayan adresler (Faz 19 İŞ 1).
 *
 * Öncesinde `<Routes>` içinde catch-all yoktu: eşleşmeyen bir adres hiçbir şey render etmiyor,
 * kullanıcı kenar çubuğunun yanında BOMBOŞ bir içerik alanı görüyordu. Silinmiş bir kaydın
 * eski bağlantısı, yer imi ya da elle yazılmış bir adres bu hâle düşüyordu ve "404" diye
 * bildirilen davranış tam olarak buydu.
 */
export default function NotFoundPage() {
  const location = useLocation();
  return (
    <div className="page-state page-state-notfound" role="alert">
      <div className="page-state-icon">🧭</div>
      <h3>Sayfa bulunamadı</h3>
      <p className="page-state-detail">
        <code>{location.pathname}</code> adresinde bir sayfa yok. Bağlantı eskimiş ya da adres
        yanlış yazılmış olabilir.
      </p>
      <div className="page-state-actions">
        <Link to="/" className="btn btn-primary">
          Dashboard'a dön
        </Link>
        <Link to="/instances" className="btn">
          Veritabanı listesi
        </Link>
      </div>
    </div>
  );
}
