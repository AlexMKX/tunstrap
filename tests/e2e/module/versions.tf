terraform {
  required_version = ">= 1.6.0"

  required_providers {
    # Pinned, not floated. `>= 2.30.0` resolves hashicorp/kubernetes v3.2.1,
    # which emits `Deprecated; use kubernetes_namespace_v1` for the resource
    # below - a provider major bump must not be able to turn this tier red for
    # a reason unrelated to tunnelling.
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
    # helm 3.x replaces the nested `kubernetes { }` block with a `kubernetes = {}`
    # attribute, which would make main.tf a syntax error.
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.17"
    }
  }
}
