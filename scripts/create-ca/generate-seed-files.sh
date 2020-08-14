#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

SYSENV_SHORT_NAME="${1}"
IVY_TAG="${2:-ivy}"
BASE_DIRECTORY="${3:-.}"
SYSENV_DIRECTORY="${BASE_DIRECTORY}/${SYSENV_SHORT_NAME}"
export AWS_PROFILE="${4:-default}"
AWS_REGION="$(aws configure get region --profile=${AWS_PROFILE})"
export AWS_DEFAULT_REGION="${5:-${AWS_REGION}}"

# Default to 10 years
VALID_IN_YEARS="${6:-10}"
DAYS_IN_YEAR='365'
HOURS_IN_DAY='24'
HOURS_IN_YEAR='8760'

let "VALID_IN_HOURS = ${VALID_IN_YEARS} * ${HOURS_IN_YEAR}"


CA_DIRECTORY="${SYSENV_DIRECTORY}/CA"
mkdir -p "${CA_DIRECTORY}"
cd "${CA_DIRECTORY}"

cat > ca-config.json <<EOF
{
  "signing": {
    "default": {
      "expiry": "${VALID_IN_HOURS}h"
    },
    "profiles": {
      "${SYSENV_SHORT_NAME}": {
        "usages": ["signing", "key encipherment", "server auth", "client auth"],
        "expiry": "${VALID_IN_HOURS}h"
      }
    }
  }
}
EOF

COUNTRY="${7:-US}"
CITY="${8:-Austin}"
STATE="${9:-Texas}"

cat > ca-csr.json <<EOF
{
  "CN": "${SYSENV_SHORT_NAME}",
  "key": {
    "algo": "rsa",
    "size": 2048
  },
  "names": [
    {
      "C": "${COUNTRY}",
      "L": "${CITY}",
      "O": "${SYSENV_SHORT_NAME}",
      "OU": "CA",
      "ST": "${STATE}"
    }
  ]
}
EOF

cfssl gencert -initca ca-csr.json | cfssljson -bare ca

CA_KEY_FILE="${CA_DIRECTORY}/ca-key.pem"
CA_CERTIFICATE_FILE="${CA_DIRECTORY}/ca.pem"

aws ssm put-parameter \
  --name "/main/${SYSENV_SHORT_NAME}/CA_key" \
  --type SecureString \
  --value "$(cat ${CA_KEY_FILE})"


aws ssm put-parameter \
  --name "/main/${SYSENV_SHORT_NAME}/CA_certificate" \
  --type SecureString \
  --value "$(cat ${CA_CERTIFICATE_FILE})"

# going back to default dir
cd -

CLUSTER_NAME="${1}"

# Creating the certificate Authorities (CAs)
# mounts new pki backends to cluster-unique paths and generates a 10 year root certificate for each pki backend
# What is the value of COMPONENT?
#vault mount -path ${CLUSTER_NAME}/pki/${COMPONENT} pki
#vault mount-tune -max-lease-ttl=87600h ${CLUSTER_NAME}/pki/etcd
vault write ${CLUSTER_NAME}/pki/${COMPONENT}/root/generate/internal common_name=${CLUSTER_NAME}/pki/${COMPONENT} ttl=87600h

# In Kubernetes, it is possible to use the Common Name (CN) field of client certificates as their user name.
# We leveraged this by creating different roles for each set of CN certificate requests
# The role below, under the cluster's etcd CA, can create a 30 day cert for any CN.
vault write ${CLUSTER_NAME}/pki/etcd/roles/member allow_any_name=true max_ttl="720h"

# The role below, under the Kubernetes CA, can only create a certificate with the CN of "kubelet".
# We can create roles that are limited to individual CNs, such as "kube-proxy" or "kube-scheduler",
# for each component that we want to communicate with the kube-apiserver.
vault write ${CLUSTER_NAME}/pki/k8s/roles/kubelet allowed_domains="kubelet" allow_bare_domains=true \
  allow_subdomains=false max_ttl="720h"

# Because we configure our kube-apiserver in a high availability configuration, separate from the kube-controller-manager,
# we also generated a shared secret for those components to use with the `--service-account-private-key-file`
# flag and write it to the generic secrets backend:
openssl genrsa 4096 > token-key
vault write secret/${CLUSTER_NAME}/k8s/token key=@token-key
rm token-key

# Policies for etcd members and kubernetes masters
cat <<EOT | vault policy-write ${CLUSTER_NAME}/pki/etcd/member -
path "${CLUSTER_NAME}/pki/etcd/issue/member" {
  capabilities = ["create", "update"]
}
EOT

cat <<EOT | vault policy-write ${CLUSTER_NAME}/pki/k8s/kube-apiserver -

path "${CLUSTER_NAME}/pki/k8s/issue/kube-apiserver" {
  capabilities = ["create", "update"]
}

path "secret/${CLUSTER_NAME}/k8s/token" {
  capabilities = ["read"]
}
EOT

`may need to add kubelet policies`


# Getting certificates

`Create iam roles here`

# We may want to create 1 role for kubernetes masters and another for kubernetes workers
vault write auth/aws/role/k8s-${CLUSTER_NAME} auth_type=iam \
  bound_iam_principal_arn=${VAULT_CLIENT_ROLE} \
  policies="${CLUSTER_NAME}/pki/etcd/member,${CLUSTER_NAME}/pki/k8s/kube-apiserver..." \
  ttl=720h

#vault write auth/token/roles/k8s-${CLUSTER_NAME} period="720h" orphan=true \
#  allowed_policies="${CLUSTER_NAME}/pki/etcd/member,${CLUSTER_NAME}/pki/k8s/kube-apiserver..."
#vault token-create -policy="${CLUSTER_NAME}/pki/etcd/member" -role="k8s-${CLUSTER_NAME}"

# Consul template should be in AMI

cat <<EOF > /path/to/consul-template/configs
{

  "template": {
    "source": "/opt/consul-template/templates/cert.template",
    "destination": "/opt/certs/etcd.serial",
    "command": "systemctl reload etcd"
  },

  "vault": {
    "address": "VAULT_ADDRESS",
    "token": "VAULT_TOKEN",
    "renew": true
  }

}
EOF

## certdump.go https://gist.github.com/tam7t/1b45125ae4de13b3fc6fd0455954c08e

cat <<EOF > /path/to/consul-template/plugins/certdump/configs
{{ with secret "${CLUSTER_NAME}/pki/data/issue/member" "common_name=${FQDN}"}}

{{ .Data.serial_number }}

{{ .Data.certificate | plugin "certdump" "/opt/certs/etcd-cert.pem" "etcd"}}

{{ .Data.private_key | plugin "certdump" "/opt/certs/etcd-key.pem" "etcd"}}

{{ .Data.issuing_ca | plugin "certdump" "/opt/certs/etcd-ca.pem" "etcd"}}

{{ end }}
EOF

# etcd systemd unit should use the flags below:

## --peer-cert-file=/opt/certs/etcd-cert.pem
## --peer-key-file=/opt/certs/etcd-key.pem
## --peer-trusted-ca-file=/opt/certs/etcd-ca.pem
## --peer-client-cert-auth
## --cert-file=/opt/certs/etcd-cert.pem
## --key-file=/opt/certs/etcd-key.pem
## --trusted-ca-file=/opt/certs/etcd-ca.pem
## --client-cert-auth

# The kube-apiserver has one certificate template for communicating with etcd and one for the Kubernetes components,
# and the process is configured with the appropriate flags:

## --etcd-certfile=/opt/certs/etcd-cert.pem
## --etcd-keyfile=/opt/certs/etcd-key.pem
## --etcd-cafile=/opt/certs/etcd-ca.pem
## --tls-cert-file=/opt/certs/apiserver-cert.pem
## --tls-private-key-file=/opt/certs/apiserver-key.pem
## --client-ca-file=/opt/certs/apiserver-ca.pem
