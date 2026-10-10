import { useState, type FormEvent } from "react";
import { ApiError } from "../api/client";
import { useAuth } from "../auth";
import { Field, Notice } from "../components/ui";

function describe(error: unknown): string {
  if (!(error instanceof ApiError)) return "Sign-in failed. Please try again.";
  if (error.code === "invalid_credentials") return "That email and password don’t match an account.";
  if (error.code === "rate_limited") {
    const wait = error.retryAfterSeconds
      ? ` Try again in about ${Math.ceil(error.retryAfterSeconds / 60)} minute(s).`
      : "";
    return `Too many sign-in attempts.${wait}`;
  }
  return error.message;
}

export function LoginPage() {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
    } catch (cause) {
      setError(describe(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <section className="card">
        <div className="card-header">
          <h1>Sign in to Relay</h1>
        </div>
        <form className="card-body stack" onSubmit={(e) => void submit(e)} noValidate>
          {error && <Notice tone="critical">{error}</Notice>}
          <Field label="Email">
            {(props) => (
              <input
                {...props}
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            )}
          </Field>
          <Field label="Password">
            {(props) => (
              <input
                {...props}
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            )}
          </Field>
          <button className="btn btn-primary" type="submit" disabled={busy || !email || !password}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
          <p className="field-hint">
            Local demo accounts are created by <code>make seed</code>; their passwords are the{" "}
            <code>RELAY_SEED_*_PASSWORD</code> values in your <code>.env</code>.
          </p>
        </form>
      </section>
    </div>
  );
}
