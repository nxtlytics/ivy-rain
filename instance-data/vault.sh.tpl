#!/bin/bash
set -x
source /opt/ivy/bash_functions.sh

set_ivy_tag '__IVY_TAG__'

###
### CONFIG ###
###
SERVICE='Vault'
CLUSTER_NAME="vault-$(get_environment)"
AWS_REGION="$(get_region)"
SLEEP=20
SPLAY=$(shuf -i 1-10 -n 1)
INSTANCE_ID="$(get_instance_id)"
# Filled by Cloudformation
ENI_ID='{#CFN_ENI_ID}'
KMS_KEY='{#VaultKMSUnseal}'
VAULT_CLIENT_ROLE_NAME='{#VAULT_CLIENT_ROLE_NAME}'
VAULT_CLIENT_ROLE='{#VAULT_CLIENT_ROLE}'
# Filled by Rain
ENI_IP="__ENI_IP__"
SERVER_ID="__SERVER_ID__"
HOSTS_ENTRIES="__HOSTS_ENTRIES__"
VAULT_SECRET="__VAULT_SECRET__"
VAULT_NUMBER_OF_KEYS=5
VAULT_NUMBER_OF_KEYS_FOR_UNSEAL=3

function setup_vault_systemctl() {
  systemctl daemon-reload
  systemctl enable vault
  systemctl start vault
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

  # Create client-role-iam role
  vault write auth/aws/role/${VAULT_CLIENT_ROLE_NAME} auth_type=iam \
    bound_iam_principal_arn=${VAULT_CLIENT_ROLE} \
    policies=vaultclient \
    ttl=24h

  # Take a consul snapshot
  consul snapshot save postinstall-consul.snapshot

  # Bailout
  exit 0
}

function setup_vault_member() {
  while true; do
    echo "Sleeping ${SLEEP} seconds to allow leader to bootstrap: "
    sleep ${SLEEP}
    echo 'done'

    echo -n 'Checking the cluster members to see if I am allowed to bootstrap: '
    # Check if my instance id exists in the list
    echo "${HOSTS_ENTRIES}"
    echo "${HOSTS_ENTRIES}" | grep "${ENI_IP}"
    HOSTS_IPS=( $(awk '{ print $1 }' <<< "${HOSTS_ENTRIES}") )
    I_CAN_BOOTSTRAP=$?  # Check exit status of grep command
    if [ $I_CAN_BOOTSTRAP -eq 0 ]; then
      # TODO: We may need to rather interrogate Vault for this bit to make sure the Lambda publishes only nodes added to vault(and ourselves) 2 nodes coming up at the same time race condition
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

  # Vault has started signal success to Cloudformation
  exit 0
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

function setup_datadog() {
  # setup datadog
  cat <<EOF > /etc/datadog-agent/conf.d/vault.d/conf.yaml
init_config:

instances:
  - api_url: http://vault.service.$(get_ivy_tag):8200/v1
EOF

  service datadog-agent restart
}

function setup_consul() {
    cat <<EOF > /etc/consul.d/master.json
{
    "performance": {
      "raft_multiplier": 1
    },
    "dns_config": {
        "allow_stale": true
    }
}
EOF

}

# Let 'er rip!
attach_eni $(get_instance_id) ${ENI_ID}
set_hostname vault-${SERVER_ID}
set_prompt_color "__PROMPT_COLOR__"
setup_datadog
setup_consul

# Start all the vault
setup_vault
