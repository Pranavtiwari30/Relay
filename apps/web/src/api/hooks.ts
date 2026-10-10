import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useAuth } from "../auth";
import { api, newIdempotencyKey } from "./client";
import type {
  AiStatus,
  Attempt,
  AuditEntry,
  Connection,
  Dashboard,
  DashboardWindow,
  DemoReceived,
  DemoSetting,
  EventDetail,
  EventSummary,
  Integration,
  IntegrationConfig,
  IntegrationDetail,
  IntegrationVersion,
  Investigation,
  Member,
  Page,
  PreviewResult,
  Role,
  SimulatorOutcome,
  ValidationResult,
} from "./types";

const V1 = "/api/v1";

/** Every cache key is scoped to the workspace, so switching workspace can never show stale data. */
function useScope(): string {
  return useAuth().session?.active_workspace_id ?? "none";
}

// --- dashboard --------------------------------------------------------------

export function useDashboard(window: DashboardWindow) {
  const scope = useScope();
  return useQuery({
    queryKey: [scope, "dashboard", window],
    queryFn: ({ signal }) => api<Dashboard>(`${V1}/dashboard/summary`, { query: { window }, signal }),
    refetchInterval: 10_000,
  });
}

// --- events -----------------------------------------------------------------

export interface EventFilters {
  status: string[];
  integration_id: string;
  q: string;
}

export function useEvents(filters: EventFilters) {
  const scope = useScope();
  return useInfiniteQuery({
    queryKey: [scope, "events", filters],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      api<Page<EventSummary>>(`${V1}/events`, {
        query: {
          status: filters.status,
          integration_id: filters.integration_id,
          q: filters.q,
          cursor: pageParam,
          limit: 25,
        },
        signal,
      }),
    getNextPageParam: (last) => last.next_cursor,
    refetchInterval: 10_000,
  });
}

const SETTLED = new Set(["DELIVERED", "FILTERED", "VALIDATION_FAILED", "DEAD_LETTERED", "HELD"]);

export function useEvent(eventId: string) {
  const scope = useScope();
  return useQuery({
    queryKey: [scope, "event", eventId],
    queryFn: ({ signal }) => api<EventDetail>(`${V1}/events/${eventId}`, { signal }),
    // Poll only while the event is still moving.
    refetchInterval: (query) => (query.state.data && SETTLED.has(query.state.data.status) ? false : 2000),
  });
}

export function useAttempts(eventId: string, eventUpdatedAt: string, live: boolean) {
  const scope = useScope();
  return useQuery({
    // Keyed by the event's last change: when its status moves (for example to
    // DELIVERED) the attempts are fetched again, so the final attempt is never
    // missed by polling that stopped a moment too early.
    queryKey: [scope, "event", eventId, "attempts", eventUpdatedAt],
    queryFn: ({ signal }) => api<Attempt[]>(`${V1}/events/${eventId}/attempts`, { signal }),
    placeholderData: keepPreviousData,
    refetchInterval: live ? 2000 : false,
  });
}

export function useReplay(eventId: string) {
  const queryClient = useQueryClient();
  const scope = useScope();
  return useMutation({
    // The key is created by the caller once per confirmation dialog, so a
    // double-click or a retried request cannot replay the event twice.
    mutationFn: (input: { reason: string; version_mode: "original" | "current"; idempotencyKey: string }) =>
      api<{ created: boolean }>(`${V1}/events/${eventId}/replay`, {
        method: "POST",
        body: { reason: input.reason, version_mode: input.version_mode },
        headers: { "Idempotency-Key": input.idempotencyKey },
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [scope] }),
  });
}

// --- investigations ---------------------------------------------------------

export function useInvestigations(eventId?: string) {
  const scope = useScope();
  return useInfiniteQuery({
    queryKey: [scope, "investigations", eventId ?? "all"],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      api<Page<Investigation>>(`${V1}/investigations`, {
        query: { event_id: eventId, cursor: pageParam, limit: 25 },
        signal,
      }),
    getNextPageParam: (last) => last.next_cursor,
  });
}

export function useInvestigation(id: string) {
  const scope = useScope();
  return useQuery({
    queryKey: [scope, "investigation", id],
    queryFn: ({ signal }) => api<Investigation>(`${V1}/investigations/${id}`, { signal }),
  });
}

export function useRunInvestigation(eventId: string) {
  const queryClient = useQueryClient();
  const scope = useScope();
  return useMutation({
    mutationFn: () => api<Investigation>(`${V1}/events/${eventId}/investigations`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [scope, "investigations"] }),
  });
}

export function useDecision(investigationId: string) {
  const queryClient = useQueryClient();
  const scope = useScope();
  return useMutation({
    mutationFn: (input: { recommendationId: string; decision: "approved" | "rejected"; note: string }) =>
      api<Investigation>(
        `${V1}/investigations/${investigationId}/recommendations/${input.recommendationId}/decision`,
        {
          method: "POST",
          body: { decision: input.decision, note: input.note },
        },
      ),
    onSuccess: (data) => {
      queryClient.setQueryData([scope, "investigation", investigationId], data);
      void queryClient.invalidateQueries({ queryKey: [scope, "investigations"] });
    },
  });
}

export function useAiStatus() {
  const scope = useScope();
  return useQuery({ queryKey: [scope, "ai-status"], queryFn: () => api<AiStatus>(`${V1}/ai/status`) });
}

// --- integrations -----------------------------------------------------------

export function useIntegrations() {
  const scope = useScope();
  return useQuery({
    queryKey: [scope, "integrations"],
    queryFn: ({ signal }) => api<Integration[]>(`${V1}/integrations`, { signal }),
  });
}

export function useIntegration(id: string) {
  const scope = useScope();
  return useQuery({
    queryKey: [scope, "integration", id],
    queryFn: ({ signal }) => api<IntegrationDetail>(`${V1}/integrations/${id}`, { signal }),
  });
}

export function useIntegrationVersions(id: string) {
  const scope = useScope();
  return useQuery({
    queryKey: [scope, "integration", id, "versions"],
    queryFn: ({ signal }) => api<IntegrationVersion[]>(`${V1}/integrations/${id}/versions`, { signal }),
  });
}

export interface IntegrationInput {
  name: string;
  description: string;
  source_connection_id: string;
  destination_connection_id: string;
  config: IntegrationConfig;
  change_note: string;
}

export function useSaveIntegration(id?: string) {
  const queryClient = useQueryClient();
  const scope = useScope();
  return useMutation({
    mutationFn: (input: IntegrationInput) =>
      api<IntegrationDetail>(id ? `${V1}/integrations/${id}` : `${V1}/integrations`, {
        method: id ? "PATCH" : "POST",
        body: input,
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [scope] }),
  });
}

export function useIntegrationAction(id: string) {
  const queryClient = useQueryClient();
  const scope = useScope();
  return useMutation({
    mutationFn: (action: "activate" | "pause" | "resume" | "disable") =>
      api<{ integration: Integration; held_events_affected: number }>(`${V1}/integrations/${id}/${action}`, {
        method: "POST",
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [scope] }),
  });
}

export function useValidateIntegration(id: string) {
  return useMutation({
    mutationFn: (config: IntegrationConfig) =>
      api<ValidationResult>(`${V1}/integrations/${id}/validate`, { method: "POST", body: { config } }),
  });
}

export function usePreview() {
  return useMutation({
    mutationFn: (input: { config: IntegrationConfig; payload: unknown }) =>
      api<PreviewResult>(`${V1}/integrations/preview`, { method: "POST", body: input }),
  });
}

// --- connections ------------------------------------------------------------

export function useConnections() {
  const scope = useScope();
  return useQuery({
    queryKey: [scope, "connections"],
    queryFn: ({ signal }) => api<Connection[]>(`${V1}/connections`, { signal }),
  });
}

export function useAdapters() {
  const scope = useScope();
  return useQuery({
    queryKey: [scope, "adapters"],
    queryFn: () => api<{ sources: string[]; destinations: string[] }>(`${V1}/adapters`),
    staleTime: Infinity,
  });
}

export interface ConnectionInput {
  name: string;
  direction: "source" | "destination";
  adapter_type: string;
  config: Record<string, unknown>;
  secret_ref: string | null;
  is_active?: boolean;
}

export function useSaveConnection(id?: string) {
  const queryClient = useQueryClient();
  const scope = useScope();
  return useMutation({
    mutationFn: (input: ConnectionInput) => {
      if (!id) return api<Connection>(`${V1}/connections`, { method: "POST", body: input });
      const { name, config, secret_ref, is_active } = input;
      return api<Connection>(`${V1}/connections/${id}`, {
        method: "PATCH",
        body: { name, config, secret_ref, is_active },
      });
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [scope] }),
  });
}

export function useDeleteConnection() {
  const queryClient = useQueryClient();
  const scope = useScope();
  return useMutation({
    mutationFn: (id: string) => api<undefined>(`${V1}/connections/${id}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [scope, "connections"] }),
  });
}

// --- members / audit --------------------------------------------------------

export function useMembers() {
  const scope = useScope();
  return useQuery({ queryKey: [scope, "members"], queryFn: () => api<Member[]>(`${V1}/members`) });
}

export function useMemberMutations() {
  const queryClient = useQueryClient();
  const scope = useScope();
  const done = () => queryClient.invalidateQueries({ queryKey: [scope, "members"] });
  return {
    add: useMutation({
      mutationFn: (input: { email: string; display_name: string; role: Role; password: string | null }) =>
        api<Member>(`${V1}/members`, { method: "POST", body: input }),
      onSuccess: done,
    }),
    changeRole: useMutation({
      mutationFn: (input: { userId: string; role: Role }) =>
        api<Member>(`${V1}/members/${input.userId}`, { method: "PATCH", body: { role: input.role } }),
      onSuccess: done,
    }),
    remove: useMutation({
      mutationFn: (userId: string) => api<undefined>(`${V1}/members/${userId}`, { method: "DELETE" }),
      onSuccess: done,
    }),
  };
}

export function useAuditLog(action: string) {
  const scope = useScope();
  return useInfiniteQuery({
    queryKey: [scope, "audit", action],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      api<Page<AuditEntry>>(`${V1}/audit-logs`, { query: { action, cursor: pageParam, limit: 50 }, signal }),
    getNextPageParam: (last) => last.next_cursor,
  });
}

// --- demo tooling -----------------------------------------------------------

export function useDemoSetting(enabled: boolean) {
  const scope = useScope();
  return useQuery({
    queryKey: [scope, "demo", "setting"],
    queryFn: () => api<DemoSetting>(`${V1}/demo/destination`),
    enabled,
  });
}

export function useDemoReceived(enabled: boolean) {
  const scope = useScope();
  return useQuery({
    queryKey: [scope, "demo", "received"],
    queryFn: () => api<DemoReceived[]>(`${V1}/demo/destination/received`),
    enabled,
    refetchInterval: 5000,
  });
}

export function useSetDemoMode() {
  const queryClient = useQueryClient();
  const scope = useScope();
  return useMutation({
    mutationFn: (input: { mode: string; fail_first_n: number; delay_seconds: number }) =>
      api<DemoSetting>(`${V1}/demo/destination`, { method: "PUT", body: input }),
    onSuccess: (data) => queryClient.setQueryData([scope, "demo", "setting"], data),
  });
}

export function useScenarios(enabled: boolean) {
  const scope = useScope();
  return useQuery({
    queryKey: [scope, "demo", "scenarios"],
    queryFn: () => api<{ scenarios: Record<string, string> }>(`${V1}/simulator/scenarios`),
    enabled,
    staleTime: Infinity,
  });
}

export function useSimulate() {
  const queryClient = useQueryClient();
  const scope = useScope();
  return useMutation({
    mutationFn: (input: { connection_id: string; scenario: string; count: number }) =>
      api<SimulatorOutcome[]>(`${V1}/simulator/send`, { method: "POST", body: input }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [scope] }),
  });
}

export { newIdempotencyKey };
