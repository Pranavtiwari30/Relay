import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import { Layout } from "./components/Layout";
import { EmptyState, Loading } from "./components/ui";
import { EventDetailPage } from "./pages/EventDetailPage";
import { EventsPage, FailuresPage } from "./pages/EventsPage";
import { IntegrationDetailPage, IntegrationsPage, NewIntegrationPage } from "./pages/IntegrationsPage";
import { AuditPage, InvestigationDetailPage, InvestigationsPage } from "./pages/InvestigationsPage";
import { LoginPage } from "./pages/LoginPage";
import { OverviewPage } from "./pages/OverviewPage";
import { SettingsPage } from "./pages/SettingsPage";

export function App() {
  const { session } = useAuth();
  if (session === undefined) return <Loading label="Loading Relay…" />;
  if (session === null) return <LoginPage />;
  if (!session.active_workspace_id) {
    return (
      <EmptyState title="You are not a member of any workspace">
        Ask a workspace administrator to add {session.user.email}.
      </EmptyState>
    );
  }
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<OverviewPage />} />
        <Route path="integrations" element={<IntegrationsPage />} />
        <Route path="integrations/new" element={<NewIntegrationPage />} />
        <Route path="integrations/:integrationId" element={<IntegrationDetailPage />} />
        <Route path="events" element={<EventsPage />} />
        <Route path="events/:eventId" element={<EventDetailPage />} />
        <Route path="failures" element={<FailuresPage />} />
        <Route path="investigations" element={<InvestigationsPage />} />
        <Route path="investigations/:investigationId" element={<InvestigationDetailPage />} />
        <Route path="audit" element={<AuditPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route
          path="*"
          element={
            <EmptyState title="Page not found">That address doesn’t match a page in Relay.</EmptyState>
          }
        />
      </Route>
      <Route path="/login" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
