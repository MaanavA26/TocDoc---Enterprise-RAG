import { useMemo, useState } from "react";
import { AdminApiClient } from "./api/client";
import { ApiProvider } from "./api/ApiContext";
import { isConfigured, loadSettings, saveSettings, type ApiSettings } from "./config";
import SettingsBar from "./components/SettingsBar";
import { EmptyState } from "./components/StateBlocks";
import DocumentsPage from "./pages/DocumentsPage";
import IndexStatsPage from "./pages/IndexStatsPage";
import ConnectorsPage from "./pages/ConnectorsPage";
import DangerZonePage from "./pages/DangerZonePage";

type TabId = "documents" | "stats" | "connectors" | "danger";

const TABS: { id: TabId; label: string }[] = [
  { id: "documents", label: "Documents" },
  { id: "stats", label: "Index Stats" },
  { id: "connectors", label: "Connectors" },
  { id: "danger", label: "Danger Zone" },
];

const DEFAULT_BOT_TAG = "default";

export default function App() {
  const initial = useMemo(loadSettings, []);
  const [settings, setSettings] = useState<ApiSettings>(initial);
  const [botTag, setBotTag] = useState<string>(DEFAULT_BOT_TAG);
  const [tab, setTab] = useState<TabId>("documents");

  const configured = isConfigured(settings);
  const client = useMemo(
    () => (configured ? new AdminApiClient(settings) : null),
    [configured, settings],
  );

  const handleApply = (next: ApiSettings, nextTag: string) => {
    setSettings(next);
    setBotTag(nextTag || DEFAULT_BOT_TAG);
    if (isConfigured(next)) saveSettings(next);
  };

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            ▦
          </span>
          <div>
            <h1>TocDoc Admin</h1>
            <p className="brand-sub">Ingestion service management dashboard</p>
          </div>
        </div>
      </header>

      <SettingsBar settings={settings} botTag={botTag} onApply={handleApply} />

      {!configured || !client ? (
        <main className="app-main">
          <EmptyState message="Enter an API base URL and X-Admin-Token in connection settings to begin." />
        </main>
      ) : (
        <ApiProvider value={{ client, botTag }}>
          <nav className="tab-nav" aria-label="Sections">
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                className={`tab ${tab === t.id ? "tab-active" : ""} ${
                  t.id === "danger" ? "tab-danger" : ""
                }`}
                onClick={() => setTab(t.id)}
                aria-current={tab === t.id ? "page" : undefined}
              >
                {t.label}
              </button>
            ))}
          </nav>
          <main className="app-main">
            {tab === "documents" && <DocumentsPage />}
            {tab === "stats" && <IndexStatsPage />}
            {tab === "connectors" && <ConnectorsPage />}
            {tab === "danger" && <DangerZonePage />}
          </main>
        </ApiProvider>
      )}

      <footer className="app-footer">
        <span>
          Consumes the ingestion admin API at <code>/admin/*</code>. Token and
          URL are session-scoped and never persisted to disk.
        </span>
      </footer>
    </div>
  );
}
