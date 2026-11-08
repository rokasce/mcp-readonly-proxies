# Deploying the Gmail read-only proxy (Google Cloud Run)

Nothing in this repo deploys itself or creates cloud resources. This is the runbook for
the people who will. Each step is tagged with who must do it:

| Tag | Who |
|---|---|
| **[GCP owner]** | Owner (or equivalent IAM) of a Google Cloud project **that belongs to your Google Workspace organization**. Needed for the Internal OAuth consent screen |
| **[GWA]** | Google Workspace admin with the Service Settings privilege (API controls) |
| **[operator]** | Whoever runs the deploy commands, with deploy rights in that project |
| **[nexos Owner]**, **[Claude Owner]** | Org owners in those products |

Why Cloud Run in an org-owned project: an OAuth app whose audience is **Internal** (only
your Workspace users) does not need Google's restricted-scope verification for
`gmail.readonly`, and Internal is only available to projects inside your Workspace org.

Placeholders used below: `PROJECT`, `REGION` (e.g. `europe-west1`), `example.com` (your
Workspace domain), `gmail-ro.example.com` (the proxy's public host).

## 1. Project and APIs [GCP owner]

```sh
gcloud config set project PROJECT
gcloud services enable run.googleapis.com secretmanager.googleapis.com \
  sqladmin.googleapis.com artifactregistry.googleapis.com gmail.googleapis.com
```

The Gmail API must be enabled in the same project as the OAuth client.

## 2. Create the Google OAuth client

Decide the public URL first, because the redirect URI must match it exactly: either a
custom domain (`https://gmail-ro.example.com`, mapped to Cloud Run in step 6) or the
service's `https://...run.app` URL (deploy once, then come back and add it).

In Google Cloud console > **Google Auth Platform** (formerly "OAuth consent screen"):

1. **Branding** [GCP owner]: app name (e.g. "Gmail read-only connector"), user support
   email, developer contact.
2. **Audience** [GCP owner]: user type **Internal**. Only accounts in your Workspace
   org can then authorize the app.
3. **Data Access** [GCP owner]: add exactly these scopes and nothing else:
   `openid`, `https://www.googleapis.com/auth/userinfo.email`,
   `https://www.googleapis.com/auth/gmail.readonly`.
4. **Clients** > Create client [GCP owner]: application type **Web application**,
   name "gmail-ro proxy", **Authorized redirect URI**
   `https://gmail-ro.example.com/auth/callback`. No JavaScript origins are needed.
5. Copy the client ID and secret. The secret goes straight into Secret Manager (step 4);
   do not paste it into chat, tickets or the repo.

Then restrict the client at the Workspace level [GWA], as described in the findings
runbook for Workspace admins:

6. Admin console > Security > Access and data control > **API controls** > Manage
   Third-Party App Access > Configure new app > search by the **client ID** > set it to
   **Specific Google data** with only `gmail.readonly` plus the Google sign-in scopes
   (or **Trusted**). Scope it to the OUs that should use the proxy.
7. In the same place, set Anthropic's "Claude for Gmail" and nexos.ai's Gmail app to
   **Blocked** if the goal is that Gmail is reachable only read-only (coordinate first:
   blocking revokes existing tokens for those apps).

The proxy enforces the read-only scope set on its own as well: it refuses and revokes any
grant that holds more than `openid email gmail.readonly`.

## 3. Database [operator]

Cloud Run instances are ephemeral, so production state goes to PostgreSQL (Cloud SQL).
SQLite is for local runs only.

```sh
gcloud sql instances create gmail-ro-db --database-version=POSTGRES_16 \
  --region=REGION --edition=ENTERPRISE --tier=db-f1-micro
gcloud sql databases create proxy --instance=gmail-ro-db
gcloud sql users create proxy --instance=gmail-ro-db --password='GENERATED_PASSWORD'
```

The connection URL over Cloud Run's Cloud SQL socket (URL-encode special characters in
the password):

```
postgresql+psycopg://proxy:GENERATED_PASSWORD@/proxy?host=/cloudsql/PROJECT:REGION:gmail-ro-db
```

Tables are created on first start. The store holds encrypted refresh tokens and hashed
keys only; back it up like any other credential store.

## 4. Secrets in Secret Manager [operator]

Generate secrets locally (`uv run ...` from a checkout, or `docker run --rm IMAGE
mcp-ro-proxy ...`) and pipe them straight in:

```sh
uv run mcp-ro-proxy generate-key --kid k1 | tr -d '\n' | \
  gcloud secrets create gmail-ro-token-keys --data-file=-
uv run mcp-ro-proxy generate-session-secret | tr -d '\n' | \
  gcloud secrets create gmail-ro-session-secret --data-file=-
printf '%s' 'GOOGLE_CLIENT_SECRET_VALUE' | gcloud secrets create gmail-ro-google-client-secret --data-file=-
printf '%s' 'postgresql+psycopg://proxy:...@/proxy?host=/cloudsql/PROJECT:REGION:gmail-ro-db' | \
  gcloud secrets create gmail-ro-database-url --data-file=-
```

A dedicated runtime service account that can read only these secrets and connect to
Cloud SQL:

```sh
gcloud iam service-accounts create gmail-ro-proxy
SA=gmail-ro-proxy@PROJECT.iam.gserviceaccount.com
for s in gmail-ro-token-keys gmail-ro-session-secret gmail-ro-google-client-secret gmail-ro-database-url; do
  gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:$SA" \
    --role=roles/secretmanager.secretAccessor
done
gcloud projects add-iam-policy-binding PROJECT --member="serviceAccount:$SA" \
  --role=roles/cloudsql.client
```

Key rotation: add a new version of `gmail-ro-token-keys` containing `k2:NEW,k1:OLD`
(new key first), redeploy, and keep `k1` until every user has signed in again.

## 5. Build and push the image [operator]

```sh
gcloud artifacts repositories create mcp-proxies --repository-format=docker --location=REGION
gcloud auth configure-docker REGION-docker.pkg.dev
IMAGE=REGION-docker.pkg.dev/PROJECT/mcp-proxies/mcp-ro-proxy:$(git rev-parse --short HEAD)
docker build -f deploy/Dockerfile -t "$IMAGE" .
docker push "$IMAGE"
```

The image runs as a non-root user and serves `config/gmail.yaml` by default
(`PROXY_CONFIG` selects another baked-in config).

## 6. Deploy [operator]

```sh
gcloud run deploy gmail-ro \
  --image="$IMAGE" --region=REGION \
  --service-account="$SA" \
  --add-cloudsql-instances=PROJECT:REGION:gmail-ro-db \
  --set-env-vars=PUBLIC_BASE_URL=https://gmail-ro.example.com,GOOGLE_CLIENT_ID=CLIENT_ID.apps.googleusercontent.com,GOOGLE_WORKSPACE_DOMAIN=example.com \
  --set-secrets=GOOGLE_CLIENT_SECRET=gmail-ro-google-client-secret:latest,TOKEN_ENCRYPTION_KEYS=gmail-ro-token-keys:latest,SESSION_SECRET=gmail-ro-session-secret:latest,DATABASE_URL=gmail-ro-database-url:latest \
  --allow-unauthenticated \
  --min-instances=0 --max-instances=3 --cpu=1 --memory=512Mi
```

- **Public reachability is required.** nexos.ai calls the MCP endpoint from the internet,
  and users' browsers reach the sign-in page. The proxy authenticates every MCP request
  itself (personal keys) and every web action (Google sign-in, CSRF tokens), so Cloud Run
  IAM must not block anonymous HTTP. If an org policy (Domain Restricted Sharing) forbids
  `allUsers`, use `--no-invoker-iam-check` instead of `--allow-unauthenticated`, or ask
  for a policy exception for this service.
- Optional: restrict ingress to nexos.ai's egress IPs (nexos provides them on request)
  plus your users' networks, with a load balancer and Cloud Armor.
- Custom domain: map `gmail-ro.example.com` to the service (Cloud Run domain mappings or
  a global external load balancer). `PUBLIC_BASE_URL` must be exactly the origin users
  and nexos use; requests for any other `Host` are refused with `421`.
- The MCP endpoint is stateless (no server-side MCP sessions), so any number of
  instances can serve it.

Smoke test:

```sh
curl -s https://gmail-ro.example.com/healthz          # ok
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://gmail-ro.example.com/mcp   # 401
```

Then open `https://gmail-ro.example.com/`, sign in with a pilot account, and check:

- Google's consent screen lists only "View your email messages and settings" (plus basic
  profile/email).
- Google account > Security > Third-party connections shows the app with read access
  only.
- `mcp-ro-proxy` logs a `sign_in` line whose `scopes` are exactly `openid`,
  `userinfo.email`, `gmail.readonly`.

## 7. Logs and alerts [operator]

Audit lines are JSON on stdout, so Cloud Logging indexes them as `jsonPayload`. Useful
queries (Logs Explorer):

```
resource.type="cloud_run_revision" jsonPayload.event="tool_hidden"
resource.type="cloud_run_revision" jsonPayload.event="tool_call" jsonPayload.decision="deny"
resource.type="cloud_run_revision" jsonPayload.event="sign_in" jsonPayload.decision="deny"
```

Create log-based alerts on the first two: a hidden tool means an upstream definition
changed and needs review, and denied calls mean a client tried something outside the
allowlist. See [configuration.md](configuration.md#audit) for every field.

## 8. Wire up clients

- **nexos.ai**: [nexos-setup.md](nexos-setup.md).
- **Claude Code** (CLI) can use it today with a personal key:
  `claude mcp add --transport http gmail-ro https://gmail-ro.example.com/mcp --header "Authorization: Bearer mrp_..."`.
- **Claude web, desktop and mobile** are not wired to this proxy yet. Claude custom
  connectors sign in with OAuth, so the proxy would have to act as an MCP OAuth
  authorization server; that is a documented follow-up
  ([configuration.md](configuration.md#client_auth)). Until then, use the built-in Gmail
  connector with its write tools **Blocked** by a Claude Owner (enforced by Anthropic on
  Team and Enterprise), per the Claude org runbook.
