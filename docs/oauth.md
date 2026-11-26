# Login OAuth2 / OpenID Connect per la GUI

La GUI può autenticare gli amministratori con un provider OAuth2 / OpenID
Connect (Authentik, Keycloak, Google, Microsoft Entra ID, GitHub, ...) invece
che, o in aggiunta a, `ADMIN_TOKEN`.

## Variabili

| Variabile | Descrizione |
|---|---|
| `OAUTH_CLIENT_ID` | Client ID registrato sul provider. |
| `OAUTH_CLIENT_SECRET` | Client secret (è un segreto: Secret Kubernetes o `.env`, mai in chiaro nei values). |
| `OAUTH_SERVER_METADATA_URL` | URL di discovery OpenID (`https://<idp>/.well-known/openid-configuration`). Consigliato: da qui arrivano endpoint e chiavi (JWKS). |
| `OAUTH_AUTHORIZE_URL`, `OAUTH_TOKEN_URL`, `OAUTH_USERINFO_URL` | Endpoint espliciti, per provider senza discovery (es. GitHub). |
| `OAUTH_JWKS_URI` | Chiavi per verificare l'id_token quando si usano endpoint espliciti con un provider OpenID. |
| `OAUTH_SCOPE` | Scope richiesti (default `openid profile email`). |
| `OAUTH_REDIRECT_URI` | URL pubblico di callback, es. `https://admin.example.com/auth`. Impostalo sempre dietro un reverse proxy. |
| `OAUTH_ADMIN_GROUPS` | Gruppi del provider (separati da virgola) i cui membri sono amministratori. Gli altri utenti sono amministratori solo se registrati con ruolo `admin` nella GUI. |
| `SECRET_KEY` | Chiave dei cookie di sessione, almeno 32 caratteri casuali. |
| `SESSION_COOKIE_SECURE` | `true` quando la GUI è in HTTPS. |

## Registrare il client

1. Sul provider crea un'applicazione OAuth2/OIDC di tipo "web" (confidential).
2. Come redirect/callback URL usa `https://<dominio-della-gui>/auth` (in
   locale `http://127.0.0.1:8081/auth`). Deve coincidere esattamente con
   `OAUTH_REDIRECT_URI`.
3. Copia client ID e secret.
4. Per gli amministratori basati sui gruppi, fai includere al provider il claim
   dei gruppi (es. scope o mapper `groups`) e imposta `OAUTH_ADMIN_GROUPS`.

## Esempio: provider OpenID (Authentik, Keycloak, ...)

```bash
export OAUTH_CLIENT_ID="<client-id>"
export OAUTH_CLIENT_SECRET="<client-secret>"
export OAUTH_SERVER_METADATA_URL="https://auth.example.com/application/o/smr/.well-known/openid-configuration"
export OAUTH_REDIRECT_URI="https://admin.example.com/auth"
export OAUTH_ADMIN_GROUPS="authentik Admins"
export SECRET_KEY="$(openssl rand -hex 32)"
export SESSION_COOKIE_SECURE=true
```

## Esempio: GitHub (solo OAuth2)

1. GitHub → Settings → Developer settings → OAuth Apps → New OAuth App,
   callback `https://admin.example.com/auth`.
2. Variabili:

```bash
export OAUTH_CLIENT_ID="<client-id>"
export OAUTH_CLIENT_SECRET="<client-secret>"
export OAUTH_AUTHORIZE_URL="https://github.com/login/oauth/authorize"
export OAUTH_TOKEN_URL="https://github.com/login/oauth/access_token"
export OAUTH_USERINFO_URL="https://api.github.com/user"
export OAUTH_SCOPE="read:user user:email"
export OAUTH_REDIRECT_URI="https://admin.example.com/auth"
export SECRET_KEY="$(openssl rand -hex 32)"
```

GitHub non ha gruppi OIDC: gli amministratori si registrano nella GUI.

## Con Helm

Metti i valori nel Secret della GUI, creato dal chart da `gui.secrets.*` o
esistente (`gui.existingSecret`):

```yaml
gui:
  secrets:
    oauthClientId: <client-id>
    oauthClientSecret: <client-secret>   # meglio con --set-string o un Secret esistente
    oauthServerMetadataUrl: https://auth.example.com/application/o/smr/.well-known/openid-configuration
    oauthRedirectUri: https://admin.example.com/auth
    oauthAdminGroups: authentik Admins
  env:
    SESSION_COOKIE_SECURE: true
```

Con OAuth configurato il chart non genera `ADMIN_TOKEN`; se serve anche il
login con token (script, emergenze) imposta `gui.secrets.adminToken`.

## Dietro un reverse proxy

Il reverse proxy termina TLS e inoltra in HTTP alla GUI. Perché la GUI conosca
schema e host pubblici:

- imposta `OAUTH_REDIRECT_URI` con l'URL pubblico;
- avvia uvicorn con `--proxy-headers --forwarded-allow-ips=<indirizzi del
  proxy>` (il chart lo fa già, vedi `gui.forwardedAllowIps`; in Docker usa la
  variabile `FORWARDED_ALLOW_IPS` di uvicorn, che `docker-compose.yml` prende
  dal file `.env`: vedi [Deploy](deploy.md#docker-compose));
- imposta `SESSION_COOKIE_SECURE=true`.

Senza questi accorgimenti il provider riceve un redirect URI `http://...` e
rifiuta il login con "redirect_uri mismatch".
