# Web user-registration lifecycle — plan

> **Status (2026-07-10): BUILT.** Phase 1 (request-access, d702d1a), Phase 2
> (refresh tokens + real logout, 1c7ebcf), Phase 3 (reset links, f9975db),
> Phase 5 (rate-limit + lockout, 6ebbb31), Phase 4 (email change / deactivate /
> delete, c9e547d). Phase 6 folded into the others (all screens shipped with
> their phases). Remaining ideas only: mailer-based self-serve reset/verify,
> multi-mode join links, session-DB row purge on delete.

Target: the **web (multi-tenant) deployment**. Single mode stays admin-provisioned;
verification/reset still apply to provisioned users.

## Current system (investigated)

In-house email+password IdP, self-contained: scrypt hashing (`identity/passwords.py`),
RS256 JWT issued/validated in-process (`identity/tokens.py`, 12h TTL, **no refresh**),
JSON-file stores under `ADK_CC_IDENTITY_DIR` (`users.json`, `invites.json`,
`api_keys.json`, `audit.json`, `jwt_key.json`). `IdentityProvider` +
`UserStore/InviteStore/…` ABC seams (DB-swap-ready). `ADK_CC_TENANCY_MODE=single|multi`.
Auth middleware → `AuthPrincipal` (user_id, tenant_id) on `request.state` + a
ContextVar; `TenancyPlugin` builds the per-user workspace.

`UserRecord`: `user_id, email, password_hash, name, tenant_id, roles, status, created`
— tolerant `from_dict` (adding fields is backward-compatible).

### Exists
login, multi-mode self-signup, invites (manual link), change-password,
profile(name), PATs, roles(owner/admin/member), org admin, audit, usage.
Frontend: `AuthGate` (login + signup toggle), `AccountPage`, `OrgPage/AdminPage`,
`AcceptInvitePage`.

### Missing / partial
- **No mailer of any kind** → blocks email verification + password reset (invites are
  copy-paste today).
- No `email_verified` field / verification flow.
- No forgot/reset password (endpoint, token store, UI).
- No token refresh; logout is a client-side no-op (no server revocation).
- Email immutable (name-only profile).
- No self-service deactivate/delete; no hard delete + data cleanup.
- No rate-limiting / brute-force protection.
- No dedicated register / verify / forgot / reset pages (signup is an AuthGate toggle).

## Decisions (locked in chat)
1. **Registration = user-initiated "request access", the mirror of invites.** Today an
   admin invites → the user accepts. Add the reverse: a user **requests to register** →
   the owner/manager **approves/rejects** in-app. Same approval gate, user-initiated.
   Email-free, consistent with the existing invite flow. (Replaces self-serve email
   verification.)
2. **Hard block**: a non-approved account cannot log in. (Nearly free — `login_password`
   already rejects `status != "active"`; we add clear messaging + the approve queue.)
3. **Sessions**: add refresh tokens (short access + revocable refresh + `/auth/refresh`,
   real revoking logout).
4. **Password reset**: default **admin-mediated one-time link** (mirrors the invite `url`,
   no mailer) — flip to self-service email later if wanted (adds the `Mailer`).
5. **Account removal**: soft-deactivate (reversible) + guarded hard-delete-with-cleanup.

**Scope of approval:** applies to **single-mode / joining an existing org**. Multi-mode
"new org" signup still makes the registrant the owner (no one above to approve) — unchanged.

## Plan (phased)

### Phase 1 — "Request access" registration (headline; mirror of invites)
The existing invite flow is admin→user: `POST /orgs/invites` (admin) → shareable link →
`POST /auth/invite/{token}/accept` (user sets password) → active member. Add the mirror,
user→admin:
- `UserRecord.status` gains `"pending"` (was `active|disabled`). `from_dict` default stays
  `active` so existing records are untouched.
- `POST /auth/request-access {email, name, password, note?}` (public) → creates a
  `pending` UserRecord on the tenant (global tenant in single mode) with role `[]`. Returns
  `{status:"pending"}`, **no token** — mirrors how `accept` creates a member, but inactive
  until approved. Gated by a new `supports_access_requests` capability (on in single mode).
- Login stays blocked for `pending`/`disabled` via the existing `status != "active"` guard;
  surface a distinct **403 "awaiting approval"** (vs invalid-credentials) so the SPA shows
  the right screen.
- Admin queue (mirror the invites admin UI): `GET /orgs/requests` (or
  `GET /orgs/members?status=pending`); `POST /orgs/requests/{id}/approve` → `active` +
  assign `member` (+ audit); `POST /orgs/requests/{id}/reject` → delete/`rejected`.
- Frontend: a public **"Request access"** form (toggle in `AuthGate`, beside login) →
  "request submitted, awaiting approval" screen; admin gets a **Requests** section with a
  pending badge + approve/reject in `OrgPage` (next to Invites); login shows the
  awaiting-approval message.
- Notifications: **in-app** (admin sees the queue; requester sees the message on login).
  Email optional, only if a mailer is later built.

**Tenancy:** the pending `UserRecord` is born with a `tenant_id` — a request always targets
one tenant. Single mode: implicitly the global tenant (no org field on the form). Multi
mode: "request access" = "ask to join an existing org" — target named via an org-slug field
or (cleaner, invite-symmetric) a shareable per-org **join link** `/join/<org-slug>`; new-org
creation stays the separate owner-signup path. The admin queue is tenant-scoped for free:
`/orgs/*` filters by the admin's own JWT tenant (`_require_admin`), so owners only see and
approve their org's requests. Tenancy machinery never runs pre-approval — a pending user
has no token, and the workspace `<root>/<tenant>/<user>/` is created lazily at first
session; rejecting a request deletes a JSON record, nothing on disk.

### Phase 2 — Session hardening (decision #3)
- `RefreshTokenStore` (JsonFile): `{token_hash, user_id, expires, revoked, rotated_from}`.
- Login/signup(approved) → short access (~30m) + long refresh (~30d).
- `POST /auth/refresh {refresh}` → new access + **rotate** refresh (reuse-detection:
  a presented-but-already-rotated token revokes the chain).
- `POST /auth/logout` → **revoke** the refresh token (real logout, replaces the no-op).
- Frontend: store both; `apiFetch` silently refreshes on access-expiry/401 then retries;
  logout calls the endpoint.

### Phase 3 — Password reset (decision #4 — pending)
- Shared `VerificationTokenStore` (JsonFile): single-use, expiring `{token_hash, user_id,
  purpose, new_email?, expires, used}`; random 32-byte urlsafe token; hash-at-rest;
  constant-time compare.
- **If admin-mediated:** `POST /orgs/members/{id}/reset-password` → one-time link the admin
  delivers out-of-band (exactly like the existing invite `url`). No mailer needed.
- **If self-service email:** build `identity/mailer.py` (`Mailer` ABC + `SmtpMailer` env-
  config + `ConsoleMailer` dev fallback that logs the link) + `mailer_from_env()`;
  `POST /auth/password/forgot {email}` → always 200 (no existence leak) → email the link.
- Both share `POST /auth/password/reset {token, new_password}` → verify → set password
  (min-8) → invalidate token → revoke refresh tokens → audit.
- Frontend: `/reset-password` (token → new password); `/forgot-password` only if email path.

### Phase 4 — Profile: email change + account removal (decision #5)
- Email change: `POST /auth/email/change {new_email, password}`. With approval model +
  no email, simplest is admin-mediated or immediate swap with re-approval; if the mailer
  exists, confirm-via-link to the new address. (Finalize alongside the reset decision.)
- Deactivate: `POST /auth/account/deactivate` → `status="disabled"` (reversible; blocks
  login) + revoke refresh tokens.
- Hard delete: `DELETE /auth/account {password}` → remove record, revoke tokens/PATs,
  delete workspace + sessions + per-user secrets/MCP/skills; owner/last-admin must transfer
  first.
- Frontend: deactivate + guarded "Delete account" flow in `AccountPage`.

### Phase 5 — Security hardening
- Rate-limiting (per-IP + per-account sliding window; JSON/in-memory, Redis-swappable) on
  `/auth/login`, `/auth/signup`, and any reset endpoint. Temporary lockout after N failed
  logins.
- Generic auth errors (don't reveal which of email/password is wrong) — but keep the
  distinct "awaiting approval" signal for pending accounts.
- Audit every auth event (signup, approve/reject, login, reset, email-change, delete).
- Optional: password-strength meter, HaveIBeenPwned k-anonymity breach check.

### Phase 6 — Frontend consolidation
- Routes/pages: signup "awaiting approval" screen; admin approve/reject queue; login
  awaiting-approval message; `/reset-password` (+ `/forgot-password` if email);
  deactivate/delete in `AccountPage`.

## Cross-cutting
- **Config**: refresh/access TTLs, `ADK_CC_PUBLIC_BASE_URL` (link building), rate-limit
  params; SMTP_* only if the self-service email path is chosen.
- **Security**: passwords/tokens never logged; tokens single-use + expiring + hashed +
  constant-time compare; reset never leaks account existence; audit trail.
- **Persistence**: new JSON stores (`refresh_tokens.json`, `reset_tokens.json`) per the
  `JsonFile*Store` pattern; `UserRecord.status` add is backward-compat; ABC seams for a
  real DB at scale.
- **Testing**: real e2e (signup→pending→admin approve→login→refresh→reset→deactivate),
  scripted-LLM where the runner is involved; the reset e2e uses the admin link or the
  `ConsoleMailer`-captured link and skips gracefully without SMTP.

## Suggested build order
Phase 1 (approval) → 2 (refresh/logout) → 3 (reset) → 5 (rate-limit) → 4 (profile/delete)
→ 6 (frontend polish), committing per phase. Phase 1 is the headline and mostly reuses the
existing status guard + Org admin UI, so it lands fast.
