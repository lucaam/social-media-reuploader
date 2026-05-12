# OAuth setup (Generic OAuth2)

This project supports optional OAuth2 login for the admin GUI. The GUI accepts a generic OAuth2/OpenID Connect provider.

Environment variables used by the GUI:

- `OAUTH_CLIENT_ID` — OAuth client id
- `OAUTH_CLIENT_SECRET` — OAuth client secret
- `OAUTH_AUTHORIZE_URL` — Authorization endpoint (e.g. `https://github.com/login/oauth/authorize` for GitHub)
- `OAUTH_TOKEN_URL` — Token endpoint (e.g. `https://github.com/login/oauth/access_token`)
- `OAUTH_USERINFO_URL` — Optional userinfo endpoint (used to fetch user profile)
- `OAUTH_SCOPE` — Scope to request (default: `openid profile email`)
- `SECRET_KEY` — Session cookie secret (set to a strong random value)
- `ADMIN_TOKEN` — Optional bearer token for admin automation (takes precedence)

Example: GitHub OAuth

1. Register a new OAuth App in your GitHub organization or user settings.
2. Set the Callback URL to: `https://<your-domain>/auth` (or `http://localhost:8081/auth` for local testing).
3. Set the following environment variables for the GUI deployment:

```bash
export OAUTH_CLIENT_ID="<client-id>"
export OAUTH_CLIENT_SECRET="<client-secret>"
export OAUTH_AUTHORIZE_URL="https://github.com/login/oauth/authorize"
export OAUTH_TOKEN_URL="https://github.com/login/oauth/access_token"
export OAUTH_USERINFO_URL="https://api.github.com/user"
export SECRET_KEY="$(openssl rand -hex 32)"
```

Notes
- The GUI will fall back to `ADMIN_TOKEN` bearer authentication if present. This is useful for CI scripts or local debugging.
- For production deployments, use HTTPS and a strong `SECRET_KEY`.
