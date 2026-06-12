import { useState } from "react";
import { clearSettings, type ApiSettings } from "../config";

const BOT_TAG_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;

interface Props {
  settings: ApiSettings;
  botTag: string;
  onApply: (settings: ApiSettings, botTag: string) => void;
}

/**
 * Configuration bar: API base URL, X-Admin-Token, and the default bot_tag
 * scope. Values are entered here and persisted to sessionStorage by the caller.
 * The token field is masked and never logged.
 */
export default function SettingsBar({ settings, botTag, onApply }: Props) {
  const [baseUrl, setBaseUrl] = useState(settings.baseUrl);
  const [token, setToken] = useState(settings.adminToken);
  const [tag, setTag] = useState(botTag);
  const [open, setOpen] = useState(!settings.baseUrl || !settings.adminToken);

  const tagValid = BOT_TAG_PATTERN.test(tag);
  const canApply = baseUrl.trim() !== "" && token.trim() !== "" && tagValid;

  const apply = () => {
    if (!canApply) return;
    onApply({ baseUrl: baseUrl.trim(), adminToken: token.trim() }, tag.trim());
    setOpen(false);
  };

  const disconnect = () => {
    clearSettings();
    setBaseUrl("");
    setToken("");
    onApply({ baseUrl: "", adminToken: "" }, tag.trim());
    setOpen(true);
  };

  return (
    <div className="settings-bar">
      <button
        type="button"
        className="btn btn-ghost settings-toggle"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        {open ? "Hide connection settings" : "Connection settings"}
      </button>
      {open && (
        <div className="settings-fields">
          <label>
            API base URL
            <input
              type="url"
              placeholder="http://localhost:8000"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </label>
          <label>
            X-Admin-Token
            <input
              type="password"
              placeholder="admin token"
              autoComplete="off"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </label>
          <label>
            Default bot_tag
            <input
              type="text"
              placeholder="acme-workspace"
              value={tag}
              onChange={(e) => setTag(e.target.value)}
              aria-invalid={!tagValid}
            />
          </label>
          <div className="settings-actions">
            <button
              type="button"
              className="btn btn-primary"
              onClick={apply}
              disabled={!canApply}
            >
              Apply
            </button>
            <button type="button" className="btn btn-ghost" onClick={disconnect}>
              Disconnect
            </button>
          </div>
          {!tagValid && tag !== "" && (
            <p className="field-hint-error">
              bot_tag must match {String(BOT_TAG_PATTERN)} (alphanumeric, dash,
              underscore; 1–128 chars).
            </p>
          )}
          <p className="settings-note">
            Stored in sessionStorage only (cleared when this tab closes). Never
            committed or sent anywhere except the configured API.
          </p>
        </div>
      )}
    </div>
  );
}
