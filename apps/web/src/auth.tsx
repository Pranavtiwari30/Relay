import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { ApiError, api, configureClient } from "./api/client";
import type { Session, Workspace } from "./api/types";

interface AuthValue {
  /** undefined while the first /auth/me is in flight; null when signed out. */
  session: Session | null | undefined;
  workspace: Workspace | null;
  /**
   * UI hint only. The server enforces every permission; this just avoids
   * showing controls that would be rejected.
   */
  can: (permission: string) => boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  switchWorkspace: (workspaceId: string) => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const queryClient = useQueryClient();

  const apply = useCallback((next: Session | null) => {
    configureClient({ csrfToken: next?.csrf_token ?? null, workspaceId: next?.active_workspace_id ?? null });
    setSession(next);
  }, []);

  useEffect(() => {
    configureClient({
      onUnauthenticated: () => {
        apply(null);
        queryClient.clear();
      },
    });
    let cancelled = false;
    api<Session>("/api/v1/auth/me", { skipAuthHandler: true })
      .then((me) => !cancelled && apply(me))
      .catch(() => !cancelled && apply(null));
    return () => {
      cancelled = true;
    };
  }, [apply, queryClient]);

  const login = useCallback(
    async (email: string, password: string) => {
      const next = await api<Session>("/api/v1/auth/login", {
        method: "POST",
        body: { email, password },
        skipAuthHandler: true,
      });
      queryClient.clear();
      apply(next);
    },
    [apply, queryClient],
  );

  const logout = useCallback(async () => {
    try {
      await api<undefined>("/api/v1/auth/logout", { method: "POST" });
    } catch (error) {
      // Already signed out server-side is fine; anything else still ends the local session.
      if (!(error instanceof ApiError)) throw error;
    }
    apply(null);
    queryClient.clear();
  }, [apply, queryClient]);

  const switchWorkspace = useCallback(
    async (workspaceId: string) => {
      configureClient({ workspaceId });
      const next = await api<Session>("/api/v1/auth/me");
      queryClient.clear();
      apply(next);
    },
    [apply, queryClient],
  );

  const value = useMemo<AuthValue>(() => {
    const workspace = session?.workspaces.find((w) => w.id === session.active_workspace_id) ?? null;
    const permissions = new Set(session?.permissions ?? []);
    return { session, workspace, can: (p) => permissions.has(p), login, logout, switchWorkspace };
  }, [session, login, logout, switchWorkspace]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}
