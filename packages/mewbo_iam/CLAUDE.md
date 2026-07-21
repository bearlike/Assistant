> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo IAM — Identity-Kernel Guidance

Scope: `packages/mewbo_iam/src/mewbo_iam/` — the identity kernel: principals and
authenticators, the closed permission catalog + roles, teams, resource
ownership/grants, group→role/team mappings, durable user records, and the auth
audit trail. Root `CLAUDE.md` covers monorepo layering; this file captures the
decisions specific to this library.

**Layering (see root CLAUDE.md → "Monorepo layering"):** a *capability library*.
It imports DOWN into `mewbo-core` (only the store drivers, for the config/logging
seam) + `pydantic`, and NOTHING else — never `mewbo-tools`, never `mewbo-graph`,
never an app. Apps consume it; it consumes no app. Provider integrations (OIDC
discovery, LDAP bind, SAML) are heavy + optional: they sit behind the
`oidc`/`ldap`/`saml` extras plus in-code import-guards, so a bare install carries
only the models and the JSON/Mongo stores.

## Laws that are easy to get wrong

- **Models never import I/O.** A model's behavior lives on the model, but the
  clock, HTTP headers, and IdP claims arrive as method ARGUMENTS — never fetched
  inside. `authenticator.resolve(claims)`, `decider.decide(principal, …)`,
  `principal.effective_permissions(role_records)`, `principal.avatar_url(policy)`,
  `UserRecord(created_at=…)`. The network legs (discovery, bind, signature
  checks) live in `drivers/` — same package, opposite side of the line. A model
  module must never import one; the drivers import the models, not the reverse.

- **`scopes` is three-state, and the states are NOT interchangeable.**
  `Principal.scopes`: `None` = unrestricted-legacy (impose no narrowing), `()` =
  explicitly none (a scopeless service key can do nothing scope-gated), a
  non-empty tuple = exactly those. Collapsing `None`↔`()` is the fail-open bug —
  a scopeless key silently becoming unrestricted. Guard it.

- **Variant families are discriminated unions — no `if kind ==` dispatch.**
  `AuthenticatorSpec` and `AuthAuditEvent` mirror `mewbo_core.triggers.spec`:
  each member owns its validators + strategy methods; there is ONE parse seam per
  family (`parse_authenticator`, `parse_audit_event` — `TypeAdapter(...).validate_python`
  aliases). Add a variant by adding a class, never by extending a service switch.

- **A driver inherits its library's INSECURE default unless it overrides it.**
  `ldap3`'s `Tls` validates nothing (`ssl.CERT_NONE`), so `ldaps://` and StartTLS
  encrypt a bind without ever authenticating the peer — the connection looks
  secure while handing the user's password to anyone on the path. Every driver
  therefore constructs its transport EXPLICITLY, in ONE helper that all of its
  connection sites share (`drivers/ldap.py`: the search bind and the
  password-verify rebind go through the same builder, so neither can quietly
  skip the certificate). Never let a client library's default stand where a
  credential crosses it.

- **The permission catalog is CLOSED.** `PermissionCatalog` ids are
  `Final[Literal[...]]` constants; `ALL` is derived from them (single source of
  truth); `RoleRecord` validates its permissions ⊆ `ALL` at definition. A new
  verb is a new constant here, deliberately — never a free string at a call site.

## Where things live

- **Models** (pure): `principal.py`, `authenticators.py`, `permissions.py`,
  `roles.py`, `teams.py`, `access.py`, `mappings.py`, `users.py`, `audit.py`,
  `settings.py` (the parsed `api.auth` block), `scim.py` (the SCIM 2.0 resource
  subset — the ONE place `extra="ignore"` is correct, because the wire format
  itself defines unknown fields as optional metadata; the file says why).
- **Stores** (I/O): `stores/` — an ABC + a JSON driver + a `create_*_store`
  factory per store, mirroring `key_store`/`triggers.store`. `pymongo` is imported
  ONLY in `stores/mongo.py`, lazily when `storage.driver == "mongodb"`, so a json
  install never needs a reachable MongoDB. The json-or-mongo choice is read in ONE
  place — `_JsonCollectionStore.resolve_driver`, which each factory delegates to;
  a store declares `_FILENAME` + `_MONGO_DRIVER` and inherits both its default
  path and that selection. Built-in roles are data constants (`BUILTIN_ROLES`)
  seeded by the role store, which refuses to update or delete them.
- **Drivers** (network + crypto): `drivers/` — the other half of the pure
  authenticators, behind the `oidc`/`ldap`/`saml` extras. `drivers/__init__.py`
  resolves every export lazily through a PEP 562 `__getattr__` keyed on one
  `_EXPORTS` table, so each name probes only ITS OWN extra: an LDAP-only
  deployment never needs the OIDC dependencies.

## The team store owns both sides of membership

`TeamStoreBase` persists `TeamRecord`s AND the `TeamMembershipRecord` edges
pointing at them — a second file (`iam_team_members.json`, a `_JsonMemberFile`
beside the team file) or a second collection (`iam_team_members`). Three
consequences worth not re-litigating:

- **Edges are NOT fields on `TeamRecord`.** SCIM does a full-document `update`
  on every group rename; a members field would let a rename silently clear the
  roster.
- **`delete` cascades, and it is concrete on the base** (over `_remove_team`)
  precisely so no driver can forget it. Same reason `create`/`update` are
  templates over `_insert_team`/`_replace_team`: uniqueness on id/slug/
  `external_id` is enforced identically for both drivers instead of only by
  mongodb's unique indexes. The indexes stay as the backstop for the
  read-then-write race the check cannot close.
- **`_ordered` is the only place edges are sorted**, mirroring the audit-trail
  rule below — drivers return matches in backend order, the base imposes
  `(team_id, user_id)`. Never push that sort down; that is how two drivers drift.

**Deleting a user does not cascade here.** The user store must not import the
team store, so an edge can outlive its user. That is deliberate and inert:
`list_members` returns edges and every caller resolves `user_id` against the
user store, skipping what no longer resolves. The product disables users rather
than deleting them, so it does not arise in normal operation.

**Two membership shapes, one projection seam.** `TeamMembershipRecord` is the
durable edge (it names the user); `TeamMembership` is what a `Principal`
carries (it does not — the principal already IS that user). `list_for_user` is
the ONE place one becomes the other, and `Principal.with_team_memberships` is
the ONE way they get attached. Never assemble that tuple inline at a call site.

## `TeamMembership.team_id` is an ID, never a slug

Memberships reach a principal from two sources with different keys, and
conflating them is silent, not loud:

- **store-backed** — durable, `team:<uuid>`-keyed, written by SCIM and the
  admin surface. The only source that can express `team_admin`.
- **config-mapped** — ephemeral, recomputed from IdP groups on every login.
  `GroupTeamMapping.resolve` returns **slugs**, and it cannot express a role.

`AccessDecider` matches `team_id` against `OwnershipStamp.team_id` and team
grantee ids, both durable ids. A slug written into `team_id` type-checks,
stores, and then matches nothing — every resource a team owns goes invisible to
its own members. So the mapping resolves through
`TeamStoreBase.memberships_for_slugs`, which returns memberships carrying real
ids.

**An unmatched slug is skipped, never auto-created.** Auto-creation would make
login a write path: a config typo would mint a durable resource-owning team,
and two concurrent first logins would race into the uniqueness check and fail
the login itself. Teams are created deliberately (SCIM, admin surface); a
mapping miss degrades the principal and logs, exactly as an unknown role name
is inert in `Principal.effective_permissions`.

**Store-backed wins a conflict**, and the reason is privilege loss rather than
taste: mapping only ever yields `team_member`, so letting it win would demote a
stored `team_admin` on every login. The reverse gains nothing, because mapping
can never confer a role storage lacks. `Principal.with_team_memberships(durable,
mapped=…)` applies that precedence in one place so no caller re-derives it.

**SCIM's `replace` goes through `replace_members`,** not a hand-rolled diff: the
obvious "drop all, re-add" demotes every `team_admin` in the team, because a
SCIM member list carries no roles. `replace_members` preserves a retained
member's existing role and adds only genuinely new members.

**`external_id` is last-write-wins when present, and is never cleared.** A push
that omits `externalId` keeps the stored link (otherwise one client omitting it
strands the team behind the slug fallback); a push that sends a different one
re-points it. First-write-wins was rejected: `external_id` is uniquely indexed,
so a stale value would permanently block the real group from claiming it with
no recourse short of DB surgery.

**Loguru, not `%s`.** `get_logger` returns a bound loguru logger, which formats
with `{}`. A `logging.warning("... %s", x)` silently drops `x` and prints the
literal `%s` — the diagnostic loses the one value it existed to show.

## Ordering the audit trail

`AuthAuditStoreBase.list` is a concrete template and the ONLY place audit events
are ordered. `ts` persists as an ISO-8601 STRING, so a backend-side sort on it is
lexicographic — `...Z` and `+00:00` spellings of one instant order by their text.
Drivers implement `_stored_records` (exact-value filters only); the base parses,
sorts by instant, then applies `since` and `limit`. Never push the sort down.
