# Manage users and workload access

Authentik is the user directory and the UI for granting access. Heimdall's built-in
group authorizer checks the verified groups against the requested service's rule.
People and service accounts are denied until they belong to an allowed group.
There is no separate permission database or custom administration app.

## Grant a person access

1. Sign in as an administrator at `https://authentik.admin.internal/if/admin/`.
2. Under **Directory → Users**, create the account, set an initial password and keep
   **Is active** enabled. Use Authentik's password/MFA controls for enrollment.
3. Under **Directory → Groups**, open `sso-users`, then **Users → Add existing user**
   and select the account. This grants the user portal, not administrator tools.
4. For an onboarded application, add the user to its `sso-<application>` group, for
   example `sso-reports`. The service's Heimdall rule must name that group. Creating
   a group or assigning it to a user without a matching rule grants nothing.
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
| `cluster-admins` | All registered protected services, plus native Authentik superuser privileges |
| Built-in `authentik Admins` | Same Gateway access as `cluster-admins`; native Authentik admin privileges |
| New `sso-<application>` group | The services whose Heimdall rules name that group |

Reserve `cluster-admins` for actual cluster administrators. Do not use it to solve an
ordinary application's permission problem. Addresses under `.admin.internal` receive
only administrator grants; ordinary services use other names under `.internal`.

## Register a permission for a new service

Use [service onboarding](service-onboarding.md) to register the route, certificate,
callback and Heimdall rule together. For ordinary services, create an Authentik group
such as `sso-reports`. The `sso-` prefix is a naming convention; membership is checked by
exact name, including case. In the service's rule, set:

```yaml
- authorizer: service-group
  config:
    values:
      group: sso-reports
```

An empty `group: ""` means administrator-only. `cluster-admins` and the built-in
`authentik Admins` group can access every registered protected service. Unknown hosts
remain denied even for administrators. Use an empty group for `.admin.internal` hosts.

Keep the per-service rules in Git; Flux applies changes to Heimdall's rule ConfigMap.
There is no import Job, model release or second copy of user membership to maintain.
The verified Authentik claims supply membership on each request. Day-to-day account
creation, grants and removals happen in Authentik's UI. The same rule applies to people
and workload identities.

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
matching allowed group fails authorization. Spoofed identity/group headers grant nothing.
Revoking the app password blocks new tokens; issued JWTs remain valid until expiry.
The Gateway does not bypass any additional authentication required by the backend.

This is HTTP API authentication through the Gateway, not Kubernetes API authentication,
automatic projected-service-account-token federation, a service mesh identity system,
or authentication for direct PostgreSQL/LDAP/other non-HTTP connections.

References: [Authentik service accounts](https://docs.goauthentik.io/users-sources/user/account-types/service-accounts/),
[machine-to-machine flow](https://docs.goauthentik.io/add-secure-apps/providers/oauth2/machine_to_machine/),
[oauth2-proxy JWT verification](https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview/).
