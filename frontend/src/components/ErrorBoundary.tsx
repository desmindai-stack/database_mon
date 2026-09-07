import { Component, ErrorInfo, ReactNode } from "react";

/**
 * Render sırasında fırlayan bir hatayı yakalar (Faz 19 İŞ 1).
 *
 * Öncesinde uygulamada hiç hata sınırı yoktu: bir bileşen render sırasında patlarsa React
 * tüm ağacı söküyor ve kullanıcı bomboş beyaz bir ekran görüyordu — "sayfa patlıyor"
 * şikâyetinin görünen hâli buydu. Artık hata mesajı ve "Tekrar dene" düğmesi gösteriliyor;
 * kenar çubuğu ve gezinme ayakta kaldığı için kullanıcı sayfada kilitli kalmıyor.
 *
 * NOT: Hata sınırı yalnızca RENDER sırasındaki hataları yakalar; olay işleyicilerindeki ve
 * `await` sonrası fırlayan hataları yakalamaz. Onlar için sayfalar `catch` + `PageError`
 * kullanıyor (bkz. components/PageState.tsx).
 */
type Props = {
  children: ReactNode;
  /** Değiştiğinde sınır kendini sıfırlar — rota değişiminde eski hata ekranda kalmasın. */
  resetKey?: string;
};

type State = { error: Error | null };

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Konsola bırakıyoruz: kullanıcı ekranda sade bir mesaj görsün, DBA/destek tarayıcı
    // konsolundan bileşen yığınına ulaşabilsin.
    console.error("Sayfa render hatası:", error, info.componentStack);
  }

  componentDidUpdate(prev: Props): void {
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  private retry = () => this.setState({ error: null });

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="page-state page-state-error" role="alert">
        <div className="page-state-icon">⚠️</div>
        <h3>Bu sayfa görüntülenirken bir hata oluştu</h3>
        <p className="page-state-detail">{error.message || "Beklenmeyen bir hata."}</p>
        <p className="page-state-hint">
          Sorun sürerse tarayıcı konsolundaki ayrıntıyla birlikte bildirin.
        </p>
        <div className="page-state-actions">
          <button type="button" className="btn btn-primary" onClick={this.retry}>
            Tekrar dene
          </button>
          <button type="button" className="btn" onClick={() => window.location.reload()}>
            Sayfayı yenile
          </button>
        </div>
      </div>
    );
  }
}
