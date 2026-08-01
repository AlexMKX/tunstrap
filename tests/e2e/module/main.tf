# The exact chain this tier exists to prove:
#   tunstrap run --output-var TF_VAR_tunstrap
#     -> var.tunstrap (JSON string)
#     -> try(jsondecode(...))
#     -> connections.node.kube_targets.k3s.path
#     -> provider config_path
#
# The inert branch is deliberately identical in shape to the one the consumer
# recipe tells operators to write, so this module doubles as a regression test
# for that recipe.

variable "tunstrap" {
  type    = string
  default = ""
}

locals {
  # try() is load-bearing: jsondecode("") is an error, so a bare jsondecode
  # would make `tofu plan` fail whenever the infrastructure is not applied yet.
  tunnel   = try(jsondecode(var.tunstrap), { connections = {} })
  kubepath = try(local.tunnel.connections.node.kube_targets.k3s.path, "")

  # Both providers must be configured *equivalently* - the whole point of the
  # helm block is that it reaches the same cluster the kubernetes provider
  # does. Defined once here and referenced twice below, so a one-sided edit is
  # not expressible: there is no second copy of the expression to change.
  inert               = local.kubepath == ""
  kube_config_path    = local.inert ? null : local.kubepath
  kube_host           = local.inert ? "https://127.0.0.1:0" : null
  kube_ca_certificate = local.inert ? "" : null
  kube_client_cert    = local.inert ? "" : null
  kube_client_key     = local.inert ? "" : null
}

provider "kubernetes" {
  config_path            = local.kube_config_path
  host                   = local.kube_host
  cluster_ca_certificate = local.kube_ca_certificate
  client_certificate     = local.kube_client_cert
  client_key             = local.kube_client_key
}

provider "helm" {
  kubernetes {
    config_path            = local.kube_config_path
    host                   = local.kube_host
    cluster_ca_certificate = local.kube_ca_certificate
    client_certificate     = local.kube_client_cert
    client_key             = local.kube_client_key
  }
}

resource "kubernetes_namespace" "probe" {
  metadata {
    name = "tunstrap-e2e"
  }
}

resource "helm_release" "probe" {
  name      = "probe"
  chart     = "${path.module}/charts/probe"
  namespace = kubernetes_namespace.probe.metadata[0].name
}

# Read straight out of terraform.tfstate by the chain-integrity assertion and
# compared against connections.node.kube_targets.k3s.path in the envelope, so a
# hard-coded or fallback path cannot pass.
output "kubepath_used" {
  value = local.kubepath
}
