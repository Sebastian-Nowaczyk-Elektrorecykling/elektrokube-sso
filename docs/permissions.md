# Manage users and workload access

Authentik is the user directory and the UI for granting access. OpenFGA holds the
service-to-group grants. People and service accounts are denied until their verified
groups have a grant for the requested service. There is no custom administration app.

## Grant a person access

1. Sign in as an administrator at `https://authentik.admin.internal/if/admin/`.
2. Under **Directory → Users**, create the account, set an initial password and keep
   **Is active** enabled. Use Authentik's password/MFA controls for enrollment.
3. Under **Directory → Groups**, open `sso-users`, then **Users → Add existing user**
   and select the account. This grants the user portal, not administrator tools.
4. For an onboarded application, add the user to its `sso-<application>` group, for
   example `sso-reports`. Its OpenFGA grant must already exist. Creating a group or
   assigning it to a user without a service grant grants nothing.
5. To remove access, remove the membership or deactivate the account. Existing cookies
   and JWTs can retain the previous claims for up to five minutes. Sign out and sign
   back in at the application's `/oauth2/sign_out` to pick up grants immediately.

The UI may also offer membership editing from the user detail page. Membership changes
are database state; no Git edit or Flux reconciliation is needed for each user.
The blueprint manages group definitions and the provider, not group membership.

| Authentik group | Shipped access |
| --- | --- |
| No matching group | No protected service |
| `sso-users` | User portal at `auth.internal` |
| `cluster-admins` | All three shipped endpoints, plus native Authentik superuser privileges |
| Built-in `authentik Admins` | Same Gateway access as `cluster-admins`; native Authentik admin privileges |
| New `sso-<application>` group | Nothing until a service grant is registered in OpenFGA |

Reserve `cluster-admins` for actual cluster administrators. Do not use it to solve an
ordinary application's permission problem. Addresses under `.admin.internal` receive
only administrator grants; ordinary services use other names under `.internal`.

## Register a permission for a new service

Use [service onboarding](service-onboarding.md) to register the route, certificate,
callback, Heimdall rule and OpenFGA grant together. For ordinary services, create a group
with a name matching `sso-[a-z0-9][a-z0-9._-]*`. The built-in `sso-users` follows the same
rule. Heimdall automatically converts only verified groups in this naming convention
into OpenFGA contextual membership tuples; a new group does not require a mechanism edit.
The two administrator group names have a separate explicit mapping to `cluster-admins`.

A grant such as the following lets members of `sso-reports` access that service:

```yaml
- user: group:sso-reports#member
  relation: viewer
  object: service:reports.team.internal
```

Keep these **service grants** and their tests in Git and follow the versioned OpenFGA
policy release procedure in the [README](../README.md#openfga-policy-lifecycle).
Do not copy user/group memberships into OpenFGA: verified Authentik claims supply them
on every check. A claim naming a nonexistent or ungranted group cannot create access.
OpenFGA's admin endpoint is an API, not a permission-management web UI; day-to-day
user access is managed in Authentik.

Gateway access is an outer gate. Applications still enforce their own roles and session
checks. For example, granting entry to the Authentik admin hostname alone does not
create a native Authentik administrator.

## Workload credentials

1. In Authentik's **Directory → Users**, use **New User → Service Account**. Give each
   workload a separate name, such as `svc-reports-reader`.
2. Give it only the application groups it needs. Leave new service accounts without
   `cluster-admins` and `authentik Admins` membership. Ordinary workload access does not
   require `sso-users`. Authentik service accounts cannot use its browser portals;
   use them for an onboarded application's API.
3. Generate an expiring **app password** for that account (the service-account dialog
   can create one; tokens can also be managed under **Directory → Tokens and App
   passwords**). An Authentik API token and an app password have different purposes.
   Store the app password in the workload's secret store, never in a Git plaintext file.
4. Trust the internal CA and ensure the workload resolves `auth.internal` and its target
   hostname to the Gateway. The LAN DNS service does not replace Kubernetes cluster DNS;
   in-cluster callers need an appropriate DNS configuration or explicit host aliases.
5. Request a token with the service-account username and app password. For example,
   with the password mounted at `/run/secrets/authentik-app-password`:

```sh
curl --fail --cacert elektrokube-sso-ca.crt \
  https://auth.internal/application/o/token/ \
  --data-urlencode grant_type=client_credentials \
  --data-urlencode client_id=elektrokube \
  --data-urlencode username=svc-reports-reader \
  --data-urlencode password@/run/secrets/authentik-app-password \
  --data-urlencode scope=profile
```

Use the returned `access_token` as a bearer token when calling an onboarded HTTPS
endpoint. The example hostname below is not deployed by this repository:

```sh
curl --fail --cacert elektrokube-sso-ca.crt \
  -H 'Authorization: Bearer <access_token>' \
  https://reports.team.internal/api/example
```

Request a new token before expiry; the configured access-token lifetime is five minutes.
Do not use a human password or share oauth2-proxy's OAuth client secret. Authentik's
native client-credentials flow accepts the service account's app password with no
custom policy or property mapping. Its standard `profile` mapping provides group claims.

oauth2-proxy verifies tokens for this provider and audience. Invalid, expired,
wrong-audience or wrong-issuer tokens fail authentication. A valid token with no
matching service grant fails authorization. Spoofed identity/group headers grant nothing.
Revoking the app password blocks new tokens; issued JWTs remain valid until expiry.
The Gateway does not bypass any additional authentication required by the backend.

This is HTTP API authentication through the Gateway, not Kubernetes API authentication,
automatic projected-service-account-token federation, a service mesh identity system,
or authentication for direct PostgreSQL/LDAP/other non-HTTP connections.

References: [Authentik service accounts](https://docs.goauthentik.io/users-sources/user/account-types/service-accounts/),
[machine-to-machine flow](https://docs.goauthentik.io/add-secure-apps/providers/oauth2/machine_to_machine/),
[oauth2-proxy JWT verification](https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview/).
