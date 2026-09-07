import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import ErrorBoundary from "./components/ErrorBoundary";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      {/* En dıştaki hata sınırı: rota içindeki sınırın kapsamadığı yerler — oturum sağlayıcı,
          kenar çubuğu, gezinme ağacı — patlarsa da kullanıcı beyaz ekran yerine bir mesaj ve
          "Tekrar dene" görsün (Faz 19 İŞ 1). */}
      <ErrorBoundary>
        <App />
      </ErrorBoundary>
    </BrowserRouter>
  </StrictMode>,
);
