import { createContext, useContext } from "react";
import { AdminApiClient } from "./client";

interface ApiContextValue {
  client: AdminApiClient;
  /** The current default bot_tag scope entered by the operator. */
  botTag: string;
}

const ApiContext = createContext<ApiContextValue | null>(null);

export const ApiProvider = ApiContext.Provider;

export function useApi(): ApiContextValue {
  const ctx = useContext(ApiContext);
  if (!ctx) {
    throw new Error("useApi must be used within an <ApiProvider>");
  }
  return ctx;
}
