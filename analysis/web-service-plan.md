# Plan: adk-cc → full web service (Keycloak-backed)

## Decisions captured (from user)
- **Tenancy: selectable single-org ↔ multi-tenant** → `ADK_CC_TENANCY_MODE=single|multi`.
- **Login: email+password AND enterprise SSO (OIDC)** → provided by **Keycloak** (self-hosted IdP).
- **Identity source: self-host Keycloak.** adk-cc stays a pure JWT consumer; Keycloak issues the tokens.
- **Session transport: keep localStorage Bearer** (reuse `api/auth.ts`).
- **v1 scope:** team/org mgmt (invite, roles), usage & audit dashboards, account self-service. **No billing.**

## Keystone: adk-cc already speaks the contract — just point it at Keycloak
`JwtAuthExtractor` (auth.py:191) already validates Bearer JWTs against a JWKS URL and reads
`sub / tenant / roles / scope`. So we **do not build login, password storage, reset, MFA, or an issuer.**
We configure Keycloak to mint those exact claims, and set adk-cc's existing env to validate them:

```
ADK_CC_JWT_JWKS_URL = https://kc/realms/adk-cc/protocol/openid-connect/certs
ADK_CC_JWT_ISSUER   = https://kc/realms/adk-cc
ADK_CC_JWT_TENANT_CLAIM = tenant   ADK_CC_JWT_ROLES_CLAIM = roles   (mapped in Keycloak)
```
SPA (PKCE) → Keycloak login → access JWT in localStorage (Bearer) → adk-cc validates via JWKS →
`AuthPrincipal` → **unchanged** TenancyPlugin / authz / admin. org_id = `tenant` claim; role = `roles` claim.

## What Keycloak gives us for free (so we DON'T build it)
email+password, OIDC SSO (incl. per-org enterprise IdP), MFA/TOTP, email verification, password reset,
brute-force lockout, password policy, hosted+themeable login pages, an Account Console (profile/password/
MFA/active-sessions/self-service), and an Admin REST API for users/orgs/roles. → kills most of old Phases 0–2 and 7.

## New sub-fork (recommend now, confirm at Phase 2): how orgs map to Keycloak
- **multi mode → Keycloak Organizations** (KC 26+): native orgs with members, **invitations**, and
  **per-org IdP/SSO**. Map org → `tenant` claim. Covers most of org-mgmt + enterprise-SSO natively. ← recommended
- **single mode →** one realm, no org dimension; `tenant` = fixed global tenant; roles via realm/client roles.
- (Alternatives considered: groups-as-orgs — simpler, no native invites; realm-per-tenant — strong isolation, heavy ops. Not recommended.)

---

## Phases

### Phase 0 — Keycloak up + wired end-to-end
- `infra/keycloak/` docker-compose (dev: KC + its Postgres). Realm export committed (`realm-adk-cc.json`):
  realm `adk-cc`, a **public SPA client** (PKCE, redirect URIs), and **protocol mappers** that put
  `tenant`, `roles`, `scope` into the access token exactly as adk-cc expects.
- Set the `ADK_CC_JWT_*` env (above). Bootstrap a first admin user + `admin` role.
- **Verify:** obtain a real Keycloak token (password grant in a test) and drive it through adk-cc:
  auth → tenancy resolves org→tenant → an admin route accepts the `admin` role. Live e2e, paced.

### Phase 1 — Frontend login (replace token-paste)
- Swap `AuthGate` paste-form for **OIDC PKCE** (oidc-client-ts or keycloak-js): redirect → callback →
  store access JWT in localStorage Bearer (their choice) → silent refresh → logout = Keycloak end-session.
- Login/signup/reset/MFA are Keycloak's hosted pages (theme to brand in Phase 7). Keep `ADK_CC_ALLOW_NO_AUTH` dev path.

### Phase 2 — Org/tenancy mapping + mode switch
- Implement `ADK_CC_TENANCY_MODE`: single (fixed tenant) vs multi (Keycloak Organizations → `tenant` claim).
- Map KC org membership → `tenant`, KC roles → `roles`. Confirm the org-model sub-fork. No downstream rework
  (org_id = tenant_id already isolates workspace + scopes authz).

### Phase 3 — Org/team management UI (single pane of glass)
- adk-cc admin panel calls the **Keycloak Admin REST API** (service-account, server-side proxy in
  `service/identity_routes.py`) to: list members, invite (KC Organizations invite), change role, remove,
  and (multi) create orgs. Keeps one product UX instead of sending users to KC's admin console.
- Frontend: **Org/Team** settings page — members table, invite form, role dropdowns, pending invites.

### Phase 4 — Account self-service
- Link/embed the **Keycloak Account Console** for password/MFA/sessions (don't rebuild). Keep adk-cc
  Settings for app prefs (theme) + **API keys/PATs** for programmatic Bearer (small local table or KC
  offline tokens). Real logout via KC end-session.

### Phase 5 — Admin expansion
- Extend `/admin` (today MCP/skills/models) with **Users** + **Members/Roles** tabs (via KC Admin API)
  and, in multi mode, a **super-admin/operator** view (list orgs, suspend, usage). Reuse the admin-role gate.

### Phase 6 — Usage & audit dashboards (built in adk-cc)
- **Audit:** structured sink (DB) for existing audit events (`emit_permission_decision`, authz decisions,
  + KC login events via its Events API) + query API + **Audit log viewer** UI.
- **Usage:** per-invocation token/cost metering per user/org + dashboard endpoints + **Usage** UI.
  Surface `QuotaPlugin` quotas here.

### Phase 7 — Hardening & ops
- Prod Keycloak (Postgres-backed, TLS, realm hardening: password policy, brute-force, SMTP for verify/
  invite/reset, optional MFA-required). Theme KC login to brand. JWKS rotation is automatic (cached).
  Secure headers on adk-cc. Docs + `.env.example` (`ADK_CC_JWT_*`, `ADK_CC_TENANCY_MODE`, KC admin creds).

---

## Reuse map (don't rebuild)
- `auth.py::JwtAuthExtractor` + `ADK_CC_JWT_*` — validates KC tokens as-is (just set the env).
- `tenancy.py` — org→tenant→workspace isolation: unchanged.
- `authz/` + `plugins/authz.py` — KC roles/scopes feed the existing ABAC PDP/PEP: unchanged.
- `admin_routes.py` + admin-role gate + registry — extend with KC-Admin-API-backed routes.
- `web AuthGate/api/auth.ts/SettingsDialog` — AuthGate→OIDC; api/auth.ts Bearer flow kept.
- Keycloak: login, signup, SSO, MFA, verify, reset, lockout, Account Console, Admin API.

## Estimated effort shift (vs in-house)
Phases 0–2 drop from "build an IdP" to "deploy + configure + map." Phases 3–6 (orgs UI, account, admin,
dashboards) are the real build and are where v1 value lands.

## Known non-goals (v1)
Billing, SAML (KC supports it later if needed), SCIM, fine-grained per-resource sharing UI.

## Open items to confirm before building
1. Org-model sub-fork (recommend Keycloak Organizations for multi) — confirm at Phase 2.
2. Where to run Keycloak in dev (docker-compose under `infra/keycloak/`) — confirm before Phase 0.
