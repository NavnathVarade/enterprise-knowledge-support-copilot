import { Navigate, Route, Routes } from "react-router-dom";
import { Layout, RequireAuth, RequireRole } from "./components/Layout";
import { AuditPage } from "./pages/AuditPage";
import { DocumentsPage } from "./pages/DocumentsPage";
import { LoginPage } from "./pages/LoginPage";
import { QueryPage } from "./pages/QueryPage";
import { SignupPage } from "./pages/SignupPage";
import { SystemPage } from "./pages/SystemPage";
import { UsagePage } from "./pages/UsagePage";

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/signup" element={<SignupPage />} />
      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route index element={<QueryPage />} />
          <Route path="documents" element={<DocumentsPage />} />
          <Route path="system" element={<SystemPage />} />
          <Route element={<RequireRole roles={["admin"]} />}>
            <Route path="usage" element={<UsagePage />} />
          </Route>
          <Route element={<RequireRole roles={["admin", "security_auditor"]} />}>
            <Route path="audit" element={<AuditPage />} />
          </Route>
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
