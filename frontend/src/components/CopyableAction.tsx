import { useState } from "react";

// Renders a command in a monospace, horizontally-scrollable box with a small copy icon pinned
// to its own top-right corner (not a flex sibling that a long command could push off-screen —
// Faz 15 İŞ 5). Shared by DashboardPage (recommendation cards) and PredictionsPage (prediction
// actions, İŞ 6) — both show a description on one line and a copy-pasteable command on another.
export default function CopyableAction({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API unavailable (permissions/non-secure context) — the command is still
      // visible and selectable by hand, so this is a silent no-op rather than an error.
    }
  };
  return (
    <div className="rec-action-box">
      <pre className="rec-action-code"><code>{command}</code></pre>
      <button
        type="button"
        className={`rec-action-copy-btn${copied ? " copied" : ""}`}
        onClick={onCopy}
        title={copied ? "Kopyalandı" : "Kopyala"}
        aria-label={copied ? "Kopyalandı" : "Kopyala"}
      >
        {copied ? (
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="20 6 9 17 4 12" />
          </svg>
        ) : (
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="9" y="9" width="13" height="13" rx="2" />
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
          </svg>
        )}
      </button>
    </div>
  );
}
