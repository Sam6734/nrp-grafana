#!/usr/bin/env python3
"""
One-time import of Grafana dashboard JSON files into Grafana via HTTP API.

Dashboards land directly in Grafana's SQLite database (backed by the PVC),
NOT as provisioned/sidecar-managed files. This means:
  - Authentik admins (GrafanaAdmin) can freely edit any dashboard.
  - Changes persist across pod restarts (PVC).
  - No ConfigMap sync will ever overwrite user edits.

Usage:
  # Port-forward Grafana to localhost first:
  kubectl -n monitoring port-forward svc/grafana-dev 3000:80

  # Then run (replace password with yours):
  python3 grafana-helm/dashboards/import.py \
      --url http://localhost:3000 \
      --user admin \
      --password T3pu77P5Rlu3u2

  # Dry-run (prints what would be imported, no changes):
  python3 grafana-helm/dashboards/import.py --url http://localhost:3000 \
      --user admin --password T3pu77P5Rlu3u2 --dry-run

  # Overwrite dashboards that already exist:
  python3 grafana-helm/dashboards/import.py --url http://localhost:3000 \
      --user admin --password T3pu77P5Rlu3u2 --overwrite
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


# ── Folder mapping ─────────────────────────────────────────────────────────────
# Keys are JSON file basenames (without .json). Values are Grafana folder names.
# Dashboards not listed here go into "General" (folder uid: "general").
FOLDER_MAP: dict[str, str] = {
    # Ceph
    "Ceph-Cluster":                     "Ceph",
    "Ceph-MDS-caps":                    "Ceph",
    "Ceph-OSDs":                        "Ceph",
    "Ceph-Pools":                       "Ceph",
    "Ceph-RBD":                         "Ceph",
    "Ceph-S3":                          "Ceph",
    "Ceph-Recovery":                    "Ceph",
    "Ceph-Capacity":                    "Ceph",
    "Volumes":                          "Ceph",
    # Hardware
    "ipmi":                             "Hardware",
    "Node-Disk-Usage":                  "Hardware",
    "Node-Exporter":                    "Hardware",
    "Node-Exporter-Full":               "Hardware",
    "Smartctl-drive-temp":              "Hardware",
    "Node-Resources":                   "Hardware",
    # Storage
    "Seaweedfs":                        "Storage",
    "Linstor":                          "Storage",
    # Apps
    "Thanos-Compactor":                 "Apps",
    "yunikorn":                         "Apps",
    "Authentik":                        "Apps",
    "Matrix":                           "Apps",
    "Synapse":                          "Apps",
    "kubevirt":                         "Apps",
    "KubeVirt-Control-Plane":           "Apps",
    "EnvoyLLM":                         "Apps",
    "VLLM":                             "Apps",
    "OSG-Shoveler":                     "Apps",
    "Falco-Talon":                      "Apps",
    "Spegel":                           "Apps",
    # GPU
    "GPU-Cluster":                      "GPU",
    "GPU-Cluster-A100":                 "GPU",
    "GPU-Cluster-Old":                  "GPU",
    "GPU-Namespace":                    "GPU",
    "GPU-Namespace-Requests":           "GPU",
    "GPUs-usage":                       "GPU",
    "K8SNvidiaGPU-Cluster":             "GPU",
    "K8SNvidiaGPU-Node":                "GPU",
    "GPU-cooling":                      "GPU",
    "qaic-user":                        "GPU",
    "qaic-admin":                       "GPU",
    # TIDE
    "TIDE-GPU-CPU-Utilization-Metrics": "TIDE",
    # General (explicit — also catches anything not listed above)
    "Wasted-Resources":                 "General",
    "CoreDNS":                          "General",
    "CPU-namespaces":                   "General",
    "HAproxy":                          "General",
    "HAProxy-ControlPlane":             "General",
    "Utilization":                      "General",
    "requests":                         "General",
    "Cluster-usage":                    "General",
    "Cluster-usage-NRP":                "General",
    "workload-total":                   "General",
    "pod-total":                        "General",
    "namespace-by-pod":                 "General",
    "namespace-by-workload":            "General",
    "node-map":                         "General",
    "ETCD-Latency":                     "General",
    "Ephemeral-Storage-Nodes":          "General",
    "Ephemeral-Storage-Namespaces":     "General",
    "NRP-Accounting":                   "General",
    "Network":                          "General",
}


class GrafanaClient:
    def __init__(self, url: str, user: str, password: str) -> None:
        self.base = url.rstrip("/")
        import base64
        creds = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Basic {creds}",
        }

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers=self.headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            msg = exc.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {exc.code} {method} {path}: {msg}") from exc

    def health(self) -> bool:
        try:
            self._req("GET", "/api/health")
            return True
        except Exception:
            return False

    def get_folders(self) -> dict[str, str]:
        """Return {title: uid} for all existing folders."""
        folders = self._req("GET", "/api/folders?limit=100")
        return {f["title"]: f["uid"] for f in folders}

    def create_folder(self, title: str) -> str:
        """Create a folder and return its uid."""
        result = self._req("POST", "/api/folders", {"title": title})
        return result["uid"]

    def import_dashboard(self, dashboard: dict, folder_uid: str, overwrite: bool) -> dict:
        """Import a dashboard JSON into Grafana. Returns the API response."""
        # Strip id so Grafana treats it as a new/update rather than a conflict.
        dashboard = dict(dashboard)
        dashboard.pop("id", None)
        payload = {
            "dashboard": dashboard,
            "folderUid": folder_uid,
            "overwrite": overwrite,
            "message": "Imported by import.py",
        }
        return self._req("POST", "/api/dashboards/db", payload)


def ensure_folder(
    client: GrafanaClient,
    name: str,
    folder_cache: dict[str, str],
    dry_run: bool,
) -> str:
    """Return folder uid, creating the folder if needed."""
    if name == "General":
        return ""  # Empty string = root/General in Grafana API
    if name in folder_cache:
        return folder_cache[name]
    if dry_run:
        print(f"    [dry-run] would create folder: {name}")
        folder_cache[name] = f"dry-run-{name}"
        return folder_cache[name]
    uid = client.create_folder(name)
    folder_cache[name] = uid
    print(f"  Created folder: {name} (uid={uid})")
    return uid


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Grafana dashboards via API")
    parser.add_argument("--url", default="http://localhost:3000", help="Grafana base URL")
    parser.add_argument("--user", default="admin", help="Grafana admin username")
    parser.add_argument("--password", required=True, help="Grafana admin password")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite dashboards that already exist")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without making any changes")
    parser.add_argument("--delay", type=float, default=0.1,
                        help="Seconds to wait between imports (default: 0.1)")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.normpath(os.path.join(script_dir, "..", ".."))
    dashboards_dir = os.path.join(repo_root, "monitoring", "local", "grafana", "dashboards")

    if not os.path.isdir(dashboards_dir):
        print(f"ERROR: dashboards directory not found:\n  {dashboards_dir}", file=sys.stderr)
        sys.exit(1)

    client = GrafanaClient(args.url, args.user, args.password)

    if args.dry_run:
        print("DRY RUN — no changes will be made\n")
    else:
        print(f"Checking Grafana at {args.url} ...", end=" ")
        if not client.health():
            print("FAILED\nGrafana is not reachable. Is port-forward running?", file=sys.stderr)
            sys.exit(1)
        print("OK")

    # Pre-load existing folders so we don't create duplicates.
    folder_cache: dict[str, str] = {}
    if not args.dry_run:
        folder_cache = client.get_folders()
        print(f"Existing folders: {list(folder_cache.keys()) or '(none)'}\n")

    json_files = sorted(f for f in os.listdir(dashboards_dir) if f.endswith(".json"))
    print(f"Dashboards to import: {len(json_files)}\n")
    print(f"  {'File':<45} {'Folder':<12} Status")
    print("  " + "-" * 75)

    ok = err = skip = 0
    for filename in json_files:
        basename = filename[:-5]
        folder_name = FOLDER_MAP.get(basename, "General")
        src = os.path.join(dashboards_dir, filename)

        try:
            with open(src) as fh:
                dashboard = json.load(fh)
        except json.JSONDecodeError as exc:
            print(f"  {filename:<45} {'':12} ERROR (invalid JSON: {exc})")
            err += 1
            continue

        if args.dry_run:
            print(f"  {filename:<45} {folder_name:<12} [dry-run] would import")
            ok += 1
            continue

        try:
            folder_uid = ensure_folder(client, folder_name, folder_cache, dry_run=False)
            result = client.import_dashboard(dashboard, folder_uid, args.overwrite)
            status = result.get("status", "?")
            slug = result.get("slug", "")
            print(f"  {filename:<45} {folder_name:<12} {status} — {slug}")
            ok += 1
        except RuntimeError as exc:
            msg = str(exc)
            if "already exists" in msg and not args.overwrite:
                print(f"  {filename:<45} {folder_name:<12} skipped (exists — use --overwrite)")
                skip += 1
            else:
                print(f"  {filename:<45} {folder_name:<12} ERROR: {msg}")
                err += 1

        if args.delay:
            time.sleep(args.delay)

    print(f"\nDone — imported: {ok}  skipped: {skip}  errors: {err}")
    if err:
        sys.exit(1)


if __name__ == "__main__":
    main()
