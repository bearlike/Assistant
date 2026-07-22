# Identity Providers

Pick a connection mode, pick your provider, and go to its walkthrough. Read [Authentication & Access](authentication.md) first for the concepts and the security preconditions; this page is the chooser.

Each provider has one home, its own guide, which carries the full configuration. This page carries only what no single provider page can: the comparison.

All examples use placeholder hostnames. Substitute your own:

| Placeholder | What it is |
|---|---|
| `https://mewbo.example.com` | The origin the API and console are served from |
| `https://console.example.com` | The console origin, when it is served separately |
| `https://idp.example.com` | Your identity provider |

> [!IMPORTANT] The redirect URI is derived, not configured
> Mewbo builds the OIDC redirect URI as `<origin>/api/auth/callback`, where `<origin>` comes from the request: `X-Forwarded-Proto` and `X-Forwarded-Host` when a reverse proxy sets them, otherwise the request's own scheme and host. Register that exact URL with your provider. If the value your provider rejects looks like `http://` when you expected `https://`, your proxy is not forwarding `X-Forwarded-Proto`.

The SAML equivalents are `<origin>/api/auth/saml/acs` for the assertion consumer service and `<origin>/api/auth/saml/metadata` for the service-provider metadata document.

---

## Step 1: pick a connection mode

Several providers can be connected more than one way, so this decision comes before the provider choice.

| Mode | Choose it when | What Mewbo trusts | Cost |
|---|---|---|---|
| **OIDC** | Almost always. It is the right default. | A signature it verifies itself | One login button of its own |
| **Trusted header** | A forward-auth gateway already fronts your other internal services and you want Mewbo to inherit that session | The peer's network address | Mewbo must be unreachable except through that proxy |
| **SAML** | Your provider speaks SAML and not OIDC, which is common with older enterprise identity providers | A signed assertion | More setup, and the replay caveat below |
| **LDAP** | You want a username and password form against a directory, with no browser redirect | The directory and its TLS certificate | Passwords transit Mewbo, so certificate verification is critical |

**Prefer OIDC over trusted-header when you have the choice.** Trusting a signature holds regardless of network topology; trusting a network address holds only as long as nothing else can reach the server. The trusted-header mode is faster to drop into an existing gateway setup, and that convenience is its whole argument. Read [the trusted-header precondition](authentication.md#trusted-header-mode) before committing to it.

**On SAML**, note that replay protection is per process, so a multi-worker deployment does not share it. See [Known limits](authentication.md#known-limits).

---

## Step 2: pick your provider

| Provider | Modes | The one thing that catches people out |
|---|---|---|
| [Keycloak](integrations-keycloak.md) | OIDC | Realm roles live at `realm_access.roles`, nested rather than top-level. Groups need an explicit mapper before they appear at all. |
| [Authentik](integrations-authentik.md) | OIDC, trusted header | In proxy mode the groups header is pipe-delimited, so `group_delimiter` must be set to a pipe. |
| [Authelia](integrations-authelia.md) | OIDC, trusted header | As an OpenID provider it emits groups only when the `groups` scope is requested, which the default scope list omits. |
| [Microsoft Entra ID](integrations-entra-id.md) | OIDC | Groups arrive as UUIDs rather than names, and past a tenant-dependent size the claim is replaced by a pointer instead. Prefer app roles. |
| [Google](integrations-google.md) | OIDC | No groups claim exists under any scope, so everyone lands on `default_role`. |
| [AWS](integrations-aws.md) | OIDC, SAML | Cognito namespaces its claim as `cognito:groups`; IAM Identity Center integrates over SAML instead. |
| [LDAP & Active Directory](integrations-ldap.md) | LDAP | Leave `tls_verify` on. The password check is a bind, so an unverified connection hands credentials to whoever answers. |

Any other SAML 2.0 provider uses the [generic SAML setup](#any-other-saml-provider) below.

### Why claim paths are the recurring theme

Four of the seven quirks above are the same underlying thing: providers disagree about where group membership lives in a token. Mewbo reads claim paths as dotted strings and walks nesting, so `realm_access.roles` reaches into a nested object and `cognito:groups` is read literally because paths split on `.` and nothing else.

The failure mode is quiet, and worth recognising: **a wrong claim path yields no groups rather than an error**, so every user lands on `default_role` and nothing in the log says why. If a whole organisation shows up as `viewer`, suspect the claim path before anything else.

One related trap that is not a claim path: `group_delimiter` applies to trusted-header mode only. An OIDC provider that packs several groups into one delimited string is not split apart.

---

## Any other SAML provider

The seven guides above cover the providers people most often connect. Any other SAML 2.0 identity provider is configured directly, and this is its only home:

```json
{
  "name": "saml-idp",
  "kind": "saml",
  "issuer": "https://idp.example.com/metadata",
  "sp_entity_id": "https://mewbo.example.com/api/auth/saml/metadata",
  "idp_metadata_url": "https://idp.example.com/metadata",
  "subject_attribute": "NameID",
  "email_attribute": "email",
  "name_attribute": "displayName",
  "groups_attribute": "groups"
}
```

Supply exactly one metadata source, either `idp_metadata_url` or `idp_metadata_xml`, never both and never neither. That is validated at startup. Register these two URLs with your provider:

| Purpose | URL |
|---|---|
| Assertion consumer service | `https://mewbo.example.com/api/auth/saml/acs` |
| Service-provider metadata | `https://mewbo.example.com/api/auth/saml/metadata` |

The metadata endpoint is served without authentication by design, since publishing it to identity-provider administrators is its entire purpose.

---

## Running more than one at once

`authenticators` is an ordered list and every entry is live simultaneously:

```json
"authenticators": [
  { "name": "keycloak", "kind": "oidc", "...": "..." },
  { "name": "directory", "kind": "ldap", "...": "..." },
  { "name": "local-keys", "kind": "api_key" }
]
```

The console discovers what to offer on the login screen from the server, so adding an authenticator changes the login options with no frontend change. When several OIDC authenticators are configured, `GET /api/auth/login?authenticator=<name>` selects one by its `name`; with no parameter the first enabled OIDC authenticator is used.

Set `"enabled": false` on any entry to keep its configuration in place while taking it out of service.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Server refuses to boot naming a missing extra | The authenticator kind's optional driver is not installed. See [Optional dependencies](#optional-dependencies). |
| Server refuses to boot asking for `session.secret` | Any non-`api_key` authenticator requires a session signing secret. |
| Server refuses to boot about a wildcard CORS origin | A browser-login authenticator cannot work with `CORS_ORIGIN=*`. Pin it to the console's exact origin. |
| Provider rejects the redirect URI | The derived origin does not match what you registered. Check that your proxy forwards `X-Forwarded-Proto` and `X-Forwarded-Host`. |
| Everyone lands on `default_role` | The groups claim is absent or the claim path is wrong. A wrong path yields no groups rather than an error. |
| A user's hand-assigned role disappeared | Roles are re-derived from group mappings at every login. Change the group, not the user. |
| Browser returns with `?auth_error=groups_overage` | Entra replaced the groups claim with a pointer. Switch to app roles. |
| Browser returns with `?auth_error=account_disabled` | The user record exists but is disabled. |
| Browser returns with `?auth_error=login_failed` | The callback was rejected. The server log carries the reason. |
| Trusted-header identity ignored, warning in the log | The request's direct peer address is not inside `trusted_proxies`. |

## Optional dependencies

Provider integrations are heavy, so each sits behind an extra. A configured authenticator whose driver is missing is a **boot failure by design**, naming the kind, the extra and the missing module, rather than a login method that mysteriously never works.

| Extra | Install | Needed for |
|---|---|---|
| `oidc` | `pip install mewbo-iam[oidc]` | OIDC relying-party flow, JWT and JWKS verification |
| `ldap` | `pip install mewbo-iam[ldap]` | LDAP and Active Directory bind and group search |
| `saml` | `pip install mewbo-iam[saml]` | SAML 2.0 service-provider support |

`api_key` and `trusted_header` need no extra.
