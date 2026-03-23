#!/usr/bin/env python3
"""
Migrate Grafana dashboards from JSON files to Kubernetes ConfigMaps.

Reads dashboard JSON files from:
  monitoring/local/grafana/dashboards/

Generates one ConfigMap YAML per dashboard in:
  grafana-helm/dashboards/configmaps/

Each ConfigMap gets:
  labels:
    grafana_dashboard: "1"          ← picked up by Grafana sidecar
  annotations:
    grafana_folder: "<FolderName>"  ← places dashboard in correct folder

UIDs inside the dashboard JSON are preserved exactly as-is, which ensures
any existing panel links and datasource references remain valid.

Usage:
  cd /path/to/monitoring   # repo root
  python3 grafana-helm/dashboards/migrate.py

  # Apply to cluster:
  kubectl -n monitoring apply -f grafana-helm/dashboards/configmaps/

  # Or commit and let Argo CD sync (grafana-dev-dashboards app):
  git add grafana-helm/dashboards/configmaps/
  git commit -m "Add migrated Grafana dashboard ConfigMaps"
"""

import os
import json
import re
import sys


# ── Folder mapping ────────────────────────────────────────────────────────────
# Keys are JSON file basenames (without .json extension).
# Values are Grafana folder names that the sidecar will create/use.
# Dashboards not listed here land in the "General" folder.
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
    "qaic-user":                        "Apps",
    "qaic-admin":                       "Apps",
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
    # TIDE
    "TIDE-GPU-CPU-Utilization-Metrics": "TIDE",
    # General
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


def slugify(name: str, max_len: int = 52) -> str:
    """
    Convert a dashboard filename into a valid Kubernetes name segment.
    Kubernetes names: lowercase alphanumeric + hyphens, max 63 chars.
    We prefix 'grafana-dashboard-' (19 chars), leaving 44 chars for slug.
    """
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_len]


def make_configmap_name(basename: str) -> str:
    return f"grafana-dashboard-{slugify(basename)}"


def make_data_key(basename: str) -> str:
    """ConfigMap data key: lowercase, dots allowed, .json suffix."""
    key = basename.lower()
    key = re.sub(r"[^a-z0-9._-]", "-", key)
    return f"{key}.json"


def compact_json(raw: str) -> str:
    """Validate JSON and return compacted (single-line) form."""
    return json.dumps(json.loads(raw), separators=(",", ":"))


def generate_configmap(basename: str, json_content: str, folder: str, namespace: str) -> str:
    """
    Return a Kubernetes ConfigMap YAML string.

    The dashboard JSON is stored as a single-line compact string to keep
    ConfigMap YAML diff noise minimal in git.
    """
    cm_name = make_configmap_name(basename)
    data_key = make_data_key(basename)

    try:
        compact = compact_json(json_content)
    except json.JSONDecodeError as exc:
        print(f"  WARNING: {basename}.json is not valid JSON — storing raw ({exc})")
        compact = json_content.replace("\n", "\\n")

    return f"""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {cm_name}
  namespace: {namespace}
  labels:
    grafana_dashboard: "1"
  annotations:
    grafana_folder: "{folder}"
data:
  {data_key}: '{compact}'
"""


def main() -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.normpath(os.path.join(script_dir, "..", ".."))

    dashboards_dir = os.path.join(repo_root, "monitoring", "local", "grafana", "dashboards")
    output_dir = os.path.join(script_dir, "configmaps")
    namespace = "monitoring"

    if not os.path.isdir(dashboards_dir):
        print(f"ERROR: dashboards directory not found:\n  {dashboards_dir}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)

    json_files = sorted(f for f in os.listdir(dashboards_dir) if f.endswith(".json"))
    if not json_files:
        print("No JSON files found.", file=sys.stderr)
        sys.exit(1)

    print(f"Source : {dashboards_dir}")
    print(f"Output : {output_dir}")
    print(f"Namespace: {namespace}")
    print(f"Dashboards found: {len(json_files)}\n")
    print(f"{'File':<45} {'Folder':<12} Output")
    print("-" * 90)

    count = 0
    for filename in json_files:
        basename = filename[:-5]
        src = os.path.join(dashboards_dir, filename)
        folder = FOLDER_MAP.get(basename, "General")
        cm_name = make_configmap_name(basename)
        dst = os.path.join(output_dir, f"{cm_name}.yaml")

        with open(src) as fh:
            raw = fh.read()

        yaml_text = generate_configmap(basename, raw, folder, namespace)

        with open(dst, "w") as fh:
            fh.write(yaml_text)

        print(f"  {filename:<43} {folder:<12} {os.path.basename(dst)}")
        count += 1

    print(f"\nGenerated {count} ConfigMaps → {output_dir}")
    print("\nNext steps:")
    print("  1. Review a few files in configmaps/ to confirm correctness.")
    print("  2. kubectl -n monitoring apply -f grafana-helm/dashboards/configmaps/")
    print("     (or commit + let Argo CD sync grafana-dev-dashboards app)")
    print("  3. Grafana sidecar will hot-reload dashboards within ~30 s.")


if __name__ == "__main__":
    main()
