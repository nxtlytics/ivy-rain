#!/bin/bash
set -x
source /opt/ivy/bash_functions.sh

set_ivy_tag '__IVY_TAG__'

###
### TEMP and should be in ami-bakery ##
###

function trust_sysenv_ca() {
    local DISTRO="$(grep '^NAME=' /etc/os-release | cut -d '"' -f2)"
    local SSM_CA_CERTIFICATE="/$(get_ivy_tag)/$(get_environment)/CA/ca.pem"
    local REGION="${1:-$(get_region)}"
    case "${DISTRO}" in
      Amazon Linux)
      local CA_TRUST_DIR='/etc/pki/ca-trust/source/anchors/'
      local UPDATE_CA_COMMAND='update-ca-trust extract'
      ;;
      Ubuntu)
      local CA_TRUST_DIR='/usr/local/share/ca-certificates/'
      local UPDATE_CA_COMMAND='update-ca-certificates'
      ;;
      *)
      echo "Only Amazon Linux and Ubuntu are supported at the moment" >&2
      return 1
      ;;
    esac
    local CA_CRT="${CA_TRUST_DIR}/ivy.pem"
    get_ssm_param "${SSM_CA_CERTIFICATE}" "${REGION}" > "${CA_CRT}"
    sudo ${UPDATE_CA_COMMAND}
}

###
### CONFIG ###
###
SERVICE='Vault'
AWS_REGION="$(get_region)"
SLEEP=20
SPLAY=$(shuf -i 1-10 -n 1)
INSTANCE_ID="$(get_instance_id)"
SSM_PREFIX="/$(get_ivy_tag)/$(get_environment)"
SSM_CA_KEY="${SSM_PREFIX}/CA/ca-key.pem"
# Filled by Cloudformation
ENI_ID='{#CFN_ENI_ID}'
KMS_KEY='{#VaultKMSUnseal}'
VAULT_CLIENT_ROLE_NAME='{#VAULT_CLIENT_ROLE_NAME}'
VAULT_CLIENT_ROLE='{#VAULT_CLIENT_ROLE}'
# Filled by Rain
ENI_IP="__ENI_IP__"
SERVER_ID="__SERVER_ID__"
HOSTS_ENTRIES="__HOSTS_ENTRIES__"
SSM_CA_REGION="__CA_REGION__"
VAULT_SECRET="__VAULT_SECRET__"
VAULT_NUMBER_OF_KEYS=5
VAULT_NUMBER_OF_KEYS_FOR_UNSEAL=3
NODE_NAME="vault-master-${SERVER_ID}.node.$(get_environment).$(get_ivy_tag)"

function setup_vault_systemctl() {
  systemctl daemon-reload
  systemctl enable vault
  systemctl start vault
}

function generate_keys_certs() {
  local DISTRO="$(grep '^NAME=' /etc/os-release | cut -d '"' -f2)"
  case "${DISTRO}" in
    Amazon Linux)
    local CA_KEY_DIR='/etc/pki/CA/private/'
    local UPDATE_CA_COMMAND='update-ca-trust extract'
    ;;
    Ubuntu)
    local CA_TRUST_DIR='/etc/ssl/private/'
    local UPDATE_CA_COMMAND='update-ca-certificates'
    ;;
    *)
    echo "Only Amazon Linux and Ubuntu are supported at the moment" >&2
    return 1
    ;;
  esac
  local CA_KEY="${CA_KEY_DIR}/ca.pem"
  get_ssm_param "${SSM_CA_KEY}" '--with-decryption' "${SSM_CA_REGION}" > "${CA_KEY}"
  cd "${CA_KEY_DIR}"
  openssl genrsa -out "${NODE_NAME}.key" 2048
  openssl req -new -key "${NODE_NAME}.key" -out "${NODE_NAME}.csr"
}

function setup_vault_leader() {
  # Do leader stuff
  echo 'I was elected leader doing leader stuff'
  sleep ${SLEEP}
  echo 'done'

  setup_vault_systemctl

  until curl -fs -o /dev/null localhost:8200/v1/sys/init; do
    echo 'Waiting for Vault to start...'
    sleep 1
  done

  init=$(curl -fs localhost:8200/v1/sys/init | jq -r .initialized)

  if [ "${init}" == "false" ]; then
    echo 'Initializing Vault'
    install -d -m 0755 -o vault -g vault /etc/vault
    SECRET_VALUE=$(vault operator init -recovery-shares=${VAULT_NUMBER_OF_KEYS} -recovery-threshold=${VAULT_NUMBER_OF_KEYS_FOR_UNSEAL})
    echo 'storing vault init values in secrets manager'
    aws secretsmanager put-secret-value --region ${AWS_REGION} --secret-id ${VAULT_SECRET} --secret-string "${SECRET_VALUE}"
  else
    echo "Vault is already initialized"
  fi

  sealed=$(curl -fs localhost:8200/v1/sys/seal-status | jq -r .sealed)

  VAULT_SECRET_VALUE=$(get_secret ${VAULT_SECRET})

  root_token=$(echo ${VAULT_SECRET_VALUE} | awk '{ if (match($0,/Initial Root Token: (.*)/,m)) print m[1] }' | cut -d " " -f 1)
  # Handle a variable number of unseal keys
  for UNSEAL_KEY_INDEX in {1..${VAULT_NUMBER_OF_KEYS_FOR_UNSEAL}}; do
    unseal_key+=($(echo ${VAULT_SECRET_VALUE} | awk '{ if (match($0,/Recovery Key '${UNSEAL_KEY_INDEX}': (.*)/,m)) print m[1] }'| cut -d " " -f 1))
  done

  # Should Auto unseal using KMS but this is for demonstration for manual unseal
  if [ "$sealed" == "true" ]; then
    echo "Unsealing Vault"
    # Handle variable number of unseal keys
    for UNSEAL_KEY_INDEX in {1..${VAULT_NUMBER_OF_KEYS_FOR_UNSEAL}}; do
      vault operator unseal $unseal_key[${UNSEAL_KEY_INDEX}]
    done
  else
    echo "Vault is already unsealed"
  fi

  sleep ${SLEEP}

  # Login to Vault
  vault login token=$root_token 2>&1 > /dev/null  # Hide this output from the console

  # Enable Vault audit logs
  vault audit enable file file_path=/var/log/vault/vault-audit.log

  # Enable AWS Auth
  vault auth enable aws

  # Enable pki secrets engine
  vault secrets enable pki

  # pki secrets engine to issue certificates with a maximum time-to-live (TTL) of 87600 hours
  vault secrets tune -max-lease-ttl=87600h pki

  # Create client-role-iam role
  vault write auth/aws/role/${VAULT_CLIENT_ROLE_NAME} auth_type=iam \
    bound_iam_principal_arn=${VAULT_CLIENT_ROLE} \
    policies=vaultclient \
    ttl=24h

  # Take a consul snapshot
  consul snapshot save postinstall-consul.snapshot
}

function setup_vault_member() {
  while true; do
    echo "Sleeping ${SLEEP} seconds to allow leader to bootstrap: "
    sleep ${SLEEP}
    echo 'done'

    echo -n 'Checking the cluster members to see if I am allowed to bootstrap: '
    # Check if my instance id exists in the list
    echo "${HOSTS_ENTRIES}"
    HOSTS_IPS=( $(awk '{ print $1 }' <<< "${HOSTS_ENTRIES}") )
    echo "${HOSTS_ENTRIES}" | grep "${ENI_IP}"
    I_CAN_BOOTSTRAP=$?  # Check exit status of grep command
    if [ $I_CAN_BOOTSTRAP -eq 0 ]; then
      UNHEALTHY_COUNT=0
      # Check each node in the cluster is okay (Except myself)
      for i in "${HOSTS_IPS[@]}"; do
        if [ ${i} != ${ENI_IP} ]; then  # Don't check ourselves since we have not joined; then
          status=$(curl -s "http://${i}:8200/v1/sys/init" | jq -r .initialized)
          # increment counter if a node is initialized
          if [ "$status" != true ]; then
            ((++UNHEALTHY_COUNT))
          fi
        fi
      done

      #if [ $UNHEALTHY_COUNT -eq 0 ]; then
        echo "I am a cluster member now and all nodes healthy start vault"
        break
      #fi
    fi
    echo "I am NOT a cluster member or other nodes unhealthy trying again..."
  done

  setup_vault_systemctl

  # Don't signal until we report that we have started
  until curl -fs -o /dev/null localhost:8200/v1/sys/init; do
    echo "Waiting for Vault to start..."
    sleep 2
  done

  # Don't signal until we are unsealed
  while true; do
    sealed=$(curl -fs localhost:8200/v1/sys/seal-status | jq -r .sealed)
    echo -n "Making sure vault is unsealed..."
    if [ $sealed != "false" ]; then
      echo " sealed sleep 2"
      sleep 2
    else
      echo " unsealed signal success"
      break
    fi
  done
}

function setup_vault() {
  # hard set hosts for vault to prevent DNS failure from exploding the world
  echo "${HOSTS_ENTRIES}" >> /etc/hosts

  cat << EOF > /etc/vault.d/vault.hcl
storage "consul" {
  address = "127.0.0.1:8500"
  path    = "vault/"
}

listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_disable     = true
}

seal "awskms" {
  region     = "${AWS_REGION}"
  kms_key_id = "${KMS_KEY}"
}

api_addr = "http://${ENI_IP}:8200"
cluster_addr = "http://${ENI_IP}:8201"
ui = true
EOF

  cat << EOF > /etc/environment
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_SKIP_VERIFY=true
EOF

  chown vault: /etc/vault.d/vault.hcl

  . /etc/environment

  # Start consul (as a master) first!
  bash /opt/ivy/configure_consul.sh master

  # So each node doesn't start same time spread out the starts
  echo "Sleeping for a splay time: ${SPLAY}"
  sleep ${SPLAY}

  # Check for leader
  while true; do
    echo -n "Sleeping for ${SLEEP} seconds to allow for election:"
    sleep ${SLEEP}
    echo "done"

    echo -n 'Checking if leader election has happened:'
    LEADER_ELECTED=$(consul operator raft list-peers 2>&1 | grep leader)
    echo "${LEADER_ELECTED}"
    if consul operator raft list-peers 2>&1 | grep leader &> /dev/null; then
      echo "Leader has been elected continue bootstrapping"
      break
    fi
    echo "No leader elected trying again..."
  done

  echo -n 'Am I the elected leader:'
  LEADER="$(consul info | grep 'leader =' | awk '{ print $3 }')"
  echo "${LEADER}"

  # If I am the leader do the leader bootstrap stuff
  if [ "${LEADER}" = "true" ]; then
    # Note: function below exits this process
    setup_vault_leader
  fi

  # Only Vault cluster members are here so
  sleep ${SLEEP}
  echo "Checking if I am able to bootstrap further: "
  setup_vault_member
}

function setup_kubernetes_master() {

}

function setup_datadog() {
  # setup datadog
  cat <<EOF > /etc/datadog-agent/conf.d/vault.d/conf.yaml
init_config:

instances:
  - api_url: http://vault.service.$(get_ivy_tag):8200/v1
EOF

  service datadog-agent restart
}

function generate_consul_tokens() {
  echo 'Disabling bash tracing mode to avoid logging token values'
  set +x
  declare -A CONSUL_TOKENS
  for token in 'CONSUL_ADMIN_TOKEN' 'CONSUL_AGENT_TOKEN' 'CONSUL_ENCRYPT_KEY' 'CONSUL_MASTER_TOKEN' 'CONSUL_REGISTRATOR_TOKEN' 'CONSUL_VAULT_TOKEN'; do
    "${CONSUL_TOKENS[${token}]}"="$(uuidgen | tr '[:upper:]' '[:lower:]')"
    aws secretsmanager put-secret-value --region ${AWS_REGION} --secret-id "${token}" --secret-string "${CONSUL_TOKENS[${token}]}" 
  done
  set -x
}

function bootstrap_consul_acl() {
    until curl 'http://localhost:8500/v1/status/leader' -s --fail; do
        sleep `shuf -i 2-15 -n 1`
    done

    if [ $(curl --retry 10 --silent --fail "http://localhost:8500/v1/acl/info/${CONSUL_MASTER_TOKEN}?token=${CONSUL_ADMIN_TOKEN}") == "[]" ]; then
        # MesosMaster token doesn't exist, add it.
        curl "http://localhost:8500/v1/acl/create?token=${CONSUL_ADMIN_TOKEN}" -X PUT --data '{"ID":"'${CONSUL_MASTER_TOKEN}'","Name":"MesosMaster Token","Type":"client","Rules":"# Mesos Master token\n\n# Write to all KV\nkey \"\" {\n  policy = \"write\"\n}\n\n# No access to vault (protected) secrets\nkey \"vault/\" {\n    policy = \"deny\"\n}\n\n# Write access to reply to exec commands\nkey \"_rexec/\" {\n    policy = \"write\"\n}\n\n# Register any service (semi insecure, but necessary)\nservice \"\" {\n    policy = \"write\"\n}\n\n# Allow vault service registration\nservice \"vault\" {\n    policy = \"write\"\n}\n\n# Broadcast any event\nevent \"\" {\n    policy = \"write\"\n}\n\n# Read exec commands, but not launch them\nevent \"_rexec\" {\n    policy = \"read\"\n}"}'
    fi

    if [ $(curl --retry 10 --silent --fail "http://localhost:8500/v1/acl/info/${CONSUL_VAULT_TOKEN}?token=${CONSUL_ADMIN_TOKEN}") == "[]" ]; then
        # Vault token doesn't exist, add it.
        curl "http://localhost:8500/v1/acl/create?token=${CONSUL_ADMIN_TOKEN}" -X PUT --data '{"ID":"'${CONSUL_VAULT_TOKEN}'","Name":"Vault Token","Type":"client","Rules":"# Token used by Vault itself to store secure data\n\n# Read/write access to vault (protected) data\nkey \"vault/\" {\n     policy = \"write\"\n}"}'
    fi

    if [ $(curl --retry 10 --silent --fail "http://localhost:8500/v1/acl/info/${CONSUL_AGENT_TOKEN}?token=${CONSUL_ADMIN_TOKEN}") == "[]" ]; then
        # Agent token doesn't exist, add it.
        curl "http://localhost:8500/v1/acl/create?token=${CONSUL_ADMIN_TOKEN}" -X PUT --data '{"ID":"'${CONSUL_AGENT_TOKEN}'","Name":"Agent Token","Type":"client","Rules":"# Generic agent token, used by all consul agents (mesos agents, etc)\n\n# Write to all KV\nkey \"\" {\n  policy = \"write\"\n}\n\n# No access to vault (protected) secrets\nkey \"vault/\" {\n    policy = \"deny\"\n}\n\n# Write access to reply to exec commands\nkey \"_rexec/\" {\n    policy = \"write\"\n}\n\n# Register any service (semi insecure, but necessary)\nservice \"\" {\n    policy = \"write\"\n}\n\n# Read only Vault service\nservice \"vault\" {\n    policy = \"read\"\n}\n\n# Broadcast any event\nevent \"\" {\n    policy = \"write\"\n}\n\n# Read exec commands, but not launch them\nevent \"_rexec\" {\n    policy = \"read\"\n}"}'
    fi

    if [ $(curl --retry 10 --silent --fail "http://localhost:8500/v1/acl/info/${CONSUL_REGISTRATOR_TOKEN}?token=${CONSUL_ADMIN_TOKEN}") == "[]" ]; then
        curl "http://localhost:8500/v1/acl/create?token=${CONSUL_ADMIN_TOKEN}" -X PUT --data '{"ID":"'${CONSUL_REGISTRATOR_TOKEN}'","Name":"Registrator Token","Type":"client","Rules":"# ACL token for Registrator on Mesos Agents\n\n# Allow any service registration, except for secured services\nservice \"\" {\n   policy = \"write\"\n}\n\n# Registrator cannot register vault\nservice \"vault\" {\n  policy = \"read\"\n}\n\n# Registrator can only read KV\nkey \"\" {\n  policy = \"read\"\n}\n\n# Deny access to vault from registrator.\n# If registrator tries to read vault, we have a problem.\nkey \"vault/\" {\n  policy = \"deny\"\n}\n\n# Deny read access to rexec replies, might include sensitive information\nkey \"_rexec/\" {\n    policy = \"deny\"\n}\n\n# Deny access to fire the rexec event too\nevent \"_rexec\" {\n    policy = \"deny\"\n}"}'
    fi
}

function setup_consul() {
  CONSUL_MASTER_TOKEN_VALUE=$(get_secret ${CONSUL_MASTER_TOKEN})
  CONSUL_AGENT_TOKEN_VALUE=$(get_secret ${CONSUL_AGENT_TOKEN})
  cat <<EOF > /etc/consul.d/master.json
{
    "performance": {
      "raft_multiplier": 1
    },
    "dns_config": {
        "allow_stale": true
    },
    "tokens": {
        "master": "${CONSUL_MASTER_TOKEN_VALUE}",
        "agent": "${CONSUL_AGENT_TOKEN_VALUE}",
    }
}
EOF

}

# Let 'er rip!
attach_eni $(get_instance_id) ${ENI_ID}
set_hostname "vault-master-${SERVER_ID}"
set_prompt_color "__PROMPT_COLOR__"
trust_sysenv_ca "${SSM_CA_REGION}"
setup_datadog
generate_consul_tokens
setup_consul
setup_vault
setup_kubernetes_master

