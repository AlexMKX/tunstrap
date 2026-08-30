# Provider environment precedence for kubeconfig paths (issue #15)

- Date measured: 2026-08-07
- Probe runtime: OpenTofu v1.12.5; kind v0.30.0; `hashicorp/kubernetes`
  v2.38.0; `hashicorp/helm` v2.17.0.
- Scope: provider environment resolution relevant to tunstrap's kube channel.

## Measured result

For both providers, plain `KUBECONFIG` alone did not configure the provider.
`KUBE_CONFIG_PATH` configured a single kubeconfig, and `KUBE_CONFIG_PATHS`
configured a list. When both provider-specific variables were present,
`KUBE_CONFIG_PATH` won over `KUBE_CONFIG_PATHS`.

The Kubernetes probe read `data.kubernetes_namespace.system`; with only
`KUBECONFIG` it tried `127.0.0.1:80` and failed, while either provider-specific
variable read `kube-system` successfully. The Helm probe similarly failed with
only `KUBECONFIG`; it applied successfully with either provider-specific
variable. With an invalid `KUBE_CONFIG_PATH` and a valid
`KUBE_CONFIG_PATHS`, Helm failed trying the invalid single path.

## Decision supported by this result

`render_kube_env` exports `KUBECONFIG` plus exactly one provider-specific
variable: `KUBE_CONFIG_PATH` for one materialized file, or
`KUBE_CONFIG_PATHS` for two or more. It must not export both provider-specific
variables for multiple files, because the single-path variable would hide the
list.

The path-list separator was measured as colon on Linux. This is an observation
from the stated runtime, not a cross-platform claim.

## Source-level corroboration

The measured behavior is consistent with the Kubernetes provider's documented
configuration precedence and Helm's nested Kubernetes configuration, but this
spec does not independently verify a source-level resolution order. The tagged
source trees are [Kubernetes v2.38.0](https://github.com/hashicorp/terraform-provider-kubernetes/tree/v2.38.0)
and [Helm v2.17.0](https://github.com/hashicorp/terraform-provider-helm/tree/v2.17.0).

## Related measurement: resource attributes (Q3)

With `hashicorp/kubernetes` v2.38.0, changing a file-derived
`kubernetes_config_map_v1.data["value"]` between planning and applying a saved
plan produced `Provider produced inconsistent final plan`. This result applies
to a resource attribute, not a provider configuration block.
