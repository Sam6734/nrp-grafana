# nrp-grafana

GitOps-managed Grafana deployment for NRP Nautilus (`monitoring` namespace).

Managed by Argo CD. Secrets are encrypted with [Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) and safe to commit.

## Structure

```
helm/values.yaml                       # Grafana Helm chart values
manifests/
  ingress.yaml                         # HAProxy Ingress
  authentik-sealedsecret.yaml          # Authentik OAuth2 credentials
  grafana-admin-sealedsecret.yaml      # Grafana admin credentials
  es-credentials-sealedsecret.yaml     # Elasticsearch basic auth
argocd/
  grafana-app.yaml                     # Argo CD App (Helm multi-source)
  grafana-manifests-app.yaml           # Argo CD App (manifests + dashboards)
dashboards/
  migrate.py                           # Converts JSON dashboards → ConfigMaps
  configmaps/                          # Commit generated ConfigMaps here
```

## Deployment order

```bash
# 1. Register the GitHub repo in Argo CD (if private)
argocd repo add https://github.com/sam6734/nrp-grafana.git

# 2. Apply the Argo CD applications
kubectl -n argocd apply -f argocd/grafana-manifests-app.yaml
kubectl -n argocd apply -f argocd/grafana-app.yaml

# 3. Sync manually and review diff
argocd app sync grafana-dev-config
argocd app sync grafana-dev

# 4. Watch rollout
kubectl -n monitoring rollout status deployment/grafana-dev
```

## Re-sealing a secret

```bash
kubectl create secret generic authentik \
  --namespace monitoring \
  --from-literal=client_id=<id> \
  --from-literal=client_secret=<secret> \
  --dry-run=client -o yaml \
| kubeseal --cert sealed-secret.pub --scope strict --format yaml \
> manifests/authentik-sealedsecret.yaml
```

## Chart version

Grafana chart `8.5.2` → Grafana `11.4.0`. To upgrade, bump `targetRevision` in
`argocd/grafana-app.yaml` and `image.tag` in `helm/values.yaml`, then sync.
