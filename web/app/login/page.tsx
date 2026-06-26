import { login, signup } from "./actions";
import { OAuthButtons } from "./oauth-buttons";

type SearchParams = Promise<{ error?: string; message?: string }>;

export default async function LoginPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const { error, message } = await searchParams;

  return (
    <main className="container">
      <h1>promptdict</h1>
      <p className="muted">Sign in, or create an account with email and password.</p>

      {error ? (
        <p className="alert" role="alert">
          {error}
        </p>
      ) : null}
      {message ? (
        <p className="notice" role="status">
          {message}
        </p>
      ) : null}

      <OAuthButtons />

      <div className="divider" role="separator">
        <span>or</span>
      </div>

      <form className="form">
        <label htmlFor="email">Email</label>
        <input id="email" name="email" type="email" autoComplete="email" required />

        <label htmlFor="password">Password</label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          minLength={6}
          required
        />

        <div className="actions">
          <button formAction={login} type="submit">
            Log in
          </button>
          <button formAction={signup} type="submit" className="secondary">
            Sign up
          </button>
        </div>
      </form>
    </main>
  );
}
