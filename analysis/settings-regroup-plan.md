# Plan — Topic-centric Settings (dissolve admin into tabs)

Status: in progress · Date: 2026-06-26

## Decisions (user)
- Container: **roomier tabbed modal** (gear opens a wide modal, left sidebar of
  tabs, Esc/backdrop closes).
- Admin page: **dissolve into topic tabs, keep a thin "Advanced" tab** for the
  rarely-used global admin controls.

## IA — gear → `SettingsModal` with sidebar tabs

Each tab = a personal section + an `[admin]` org section where it applies.

| Tab | Everyone | + admin | Reuse |
|---|---|---|---|
| Account | profile, password, API keys, theme | — | AccountPage sections + theme |
| Secrets | your grouped secrets | org credentials | SecretsSection + new admin-creds section |
| MCP | Your MCP servers | Org MCP servers | UserMcpSection + `McpAdminTab` |
| Skills | Your skills | Org skills | UserSkillsSection + `SkillsAdminTab` |
| Usage | (your usage) | org usage + audit | `UsageAdminTab` + `AuditAdminTab` |
| Team | your org/role | members, roles, invites | extracted from OrgPage |
| Advanced | — | model endpoints, (wiki) | `ModelAdminTab` |

Role gate: `maybeAdmin()` (existing). Admin-only tabs (Usage/Team/Advanced) and
the `[admin]` sub-sections render only for admins.

## Phases (each committed)

### Phase 1 — modal shell + Account/Secrets/MCP/Skills
- New `components/SettingsModal.tsx`: wide modal, left sidebar, active-tab state,
  Esc/backdrop, role-gated tab list. Footer: Sign out.
- Export the personal sections from `AccountPage` (Profile/Password/ApiKeys/
  Secrets/UserMcp/UserSkills) so the modal and the page share them.
- MCP/Skills tabs append the admin `*AdminTab` when admin. Secrets tab appends a
  small admin org-credentials section (admin.ts listCredentialKeys/put/delete).
- Wire the gear (ChatPage) to open `SettingsModal` instead of the old dialog.

### Phase 2 — Usage / Team / Advanced
- Usage tab: `UsageAdminTab` + `AuditAdminTab` (admin). Non-admin: a "your usage"
  view if cheap, else hidden.
- Team tab: extract OrgPage's members/invites into a `TeamSection`, reuse in the
  modal AND keep OrgPage rendering it.
- Advanced tab: `ModelAdminTab` (+ wiki settings if ADK_CC_WIKI).

### Phase 3 — cleanup + verify
- Replace the old `SettingsDialog` (theme + links) with the modal everywhere;
  keep `/account`, `/admin`, `/org` routes as deep-links (no UI breakage, e2es
  green). Move the secrets-missing badge onto the Secrets tab in the sidebar.
- Verify with Playwright on the ACTIVE :8000 server (admin alice + a non-admin):
  tabs present per role; personal + org sections; add/remove still work.

## Notes
- Keep existing routes/pages working (deep links + existing e2es); the modal is
  the new primary surface. No backend changes — pure frontend re-composition.
- The gear's missing-secrets badge moves to the Secrets tab row in the sidebar.
