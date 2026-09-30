# Register another service

DNS answers every name below `.internal`, at any depth. The shared `sso/lan-gateway`
has hostname-free HTTP/HTTPS listeners. Neither needs editing for a new hostname.
An actual application still needs an explicit route, a valid certificate and a permission
rule. Resolving a name never publishes an application or authorizes a user.

This procedure uses the hypothetical ordinary-user endpoint `reports.team.internal`,
backed by `Service/reports` in namespace `reports` on port 80. **No reports service or
route is deployed by this repository.** Add registrations only for services deliberately
introduced by the corresponding service repository. Do not publish all existing Services.

## Declarative registration

Commit these changes to the relevant Flux sources. All registration resources are plain
upstream configuration; no generator, custom controller or bootstrap script is required.

| Item | Change |
| --- | --- |
| Workload | Deploy the new application's workload and ClusterIP Service in its own Flux stage. Make its route stage depend on the workload and the SSO stages. |
| TLS certificate | Add `reports.team.internal` to `Certificate/sso-gateway.spec.dnsNames` in `infrastructure/certificates/certificates.yaml`. Keep the existing SANs. cert-manager renews `sso-gateway-tls`; the Gateway reference stays unchanged. |
| HTTPS route | Copy a protected route from `infrastructure/routes/routes.yaml` into the new service's Flux bundle. Keep it in namespace `sso`, with `parentRefs: [{name: lan-gateway, sectionName: https}]`, and set the exact hostname. Rename the route. |
| Backend | Set its application `backendRefs` to `name: reports`, `namespace: reports`, `port: 80`, and add the ReferenceGrant below in namespace `reports`. |
| Login handling | Retain the separate `/oauth2/` rule targeting `oauth2-proxy:4180` in namespace `sso`. Update every fixed `X-Forwarded-Host` value to the new hostname and keep `X-Forwarded-Proto: https` plus the header-removal filters. |
| External authorization | Retain the `ExternalAuth` GRPC filter targeting `heimdall-authz:4456` on **every application backend rule**, including API paths. Keep `cookie`, `authorization` and `accept` in `grpc.allowedHeaders`. Only the `/oauth2/` authentication-service rule is exempt. |
| HTTP redirect | Add an exact-host HTTPRoute for the new endpoint attached to `lan-gateway`'s `http` listener with an HTTPS `RequestRedirect`, or add the hostname to `sso-http-redirect` in `infrastructure/gateway/login-routes.yaml`. |
| OIDC callback | Add the strict URL `https://reports.team.internal/oauth2/callback` to the existing provider's `redirect_uris` in `infrastructure/authentik/blueprint.yaml`. Add `--whitelist-domain=reports.team.internal` to `infrastructure/oauth2-proxy/workload.yaml`. |
| Heimdall rule | Copy the `authentik-users` rule in `infrastructure/heimdall/rules.yaml`; assign a unique ID, change `match.hosts` to the exact hostname and the `service-group` authorizer's `values.group` to `sso-reports`. Keep both `/` and `/**`, scheme `https`, the `session` authenticator, the `noop` finalizer and both `on_error` handlers. These send anonymous browsers to login and authenticated, unauthorized browsers to the shared 403 page. No new error-page route is needed on the application's hostname. |
| Permission | Create `sso-reports` in the Authentik blueprint or UI, then assign users or service accounts through Authentik's UI. That group matches the service's Heimdall rule directly; there is no separate policy store to update. |

For admin-only endpoints under `.admin.internal`, set `values.group: ""` to admit only
`cluster-admins` and built-in `authentik Admins`. Ordinary services use other names under
`.internal`. Administrators can also access every registered ordinary service.

`allowedRoutes.namespaces.from: Same` deliberately keeps route publication under the
`sso` namespace's administrative control. It does not prevent using applications in
other namespaces. The following grant belongs with the new backend's configuration:

```yaml
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ReferenceGrant
metadata:
  name: reports-from-lan-gateway
  namespace: reports
spec:
  from:
  - group: gateway.networking.k8s.io
    kind: HTTPRoute
    namespace: sso
  to:
  - group: ""
    kind: Service
    name: reports
```

The ordinary-user authorization step in the new Heimdall rule is:

```yaml
- authorizer: service-group
  config:
    values:
      group: sso-reports
```

Group names are matched exactly against oauth2-proxy's verified claims; `sso-` is a
convention, not a wildcard grant. No edit to the shared authorizer is needed for a new
service. Creating an account, resolving the name, adding a route, or receiving a valid
token alone grants no access. Apply rule changes through Flux and manage group membership
in Authentik. No database import or policy-version coordination is needed.

Test the new group, an unrelated group, an administrator and an account with no groups.
For an admin hostname, ordinary users must remain denied.

## Names, paths and TLS

Exact certificate SANs support `reports.internal`, `reports.team.internal` and deeper
names equally. A TLS certificate for `*.internal` only covers one label and cannot
cover all possible depths. This repository uses explicit SANs, not a misleading
infinite-depth TLS wildcard. Endpoint aliases each need their own registration.
Several paths on one hostname need no additional DNS or SAN entries, but every protected
application rule still needs ExternalAuth. `/oauth2/` is reserved for authentication.

The shared certificate is a simple central inventory of registered names. At larger
scale, separate certificates/listeners can be registered explicitly through Gateway API;
do not assume cert-manager automatically discovers HTTPRoute hosts. The bootstrap script
checks the shipped routes; future service bundles must check their own readiness.

After Flux reconciles, check the Certificate is Ready, the Gateway is Programmed and the
new HTTPRoute has current `Accepted=True` and `ResolvedRefs=True` conditions. Test the
actual HTTPS hostname with an allowed user, a denied user, no credentials, and (for APIs)
a workload bearer token. Never work around a missing SAN by disabling TLS verification.
Backend NetworkPolicies should admit the Gateway ingress identity and required internal
callers; a direct ClusterIP/Pod-IP call does not traverse this Gateway authorization.
