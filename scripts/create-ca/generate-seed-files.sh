#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

THIS_SCRIPT=$(basename $0)
PADDING=$(printf %-${#THIS_SCRIPT}s " ")

function usage () {
  echo "Usage:"
  echo "${THIS_SCRIPT} -s <REQUIRED: SysEnv Short Name> -t <Ivy tag also known as namespace>"
  echo "${PADDING} -b <Base directory where to store seed files>"
  echo "${PADDING} -e <Validity of Certificate Authority (CA) keys in years, if files already exist this will be ignored>"
  echo
  echo 'Setup Ivy seed files (Right now only Certificate Authority, privat key and public key)'
  exit 1
}

function is_parameter_in_ssm() {
  local PARAMETER="${1}"
  local REGIONS_IN_SSM=( $(aws ssm get-parameters-by-path --path '/aws/service/global-infrastructure/services/ssm/regions' --query 'Parameters[*].Value' --output='text') )
  for region in "${REGIONS_IN_SSM[@]}"; do
    if aws --region="${region}" ssm get-parameter --name "${PARAMETER}" &> /dev/null; then
        echo "Parameter ${PARAMETER} already exists in region ${region}" >&2
        return 0
    fi
  done
  echo "Parameter ${PARAMETER} does not exist in any region where ssm is available" >&2
  return 1
}

function generate_certificate_authority() {
  local CA_DIRECTORY="${1}"
  local VALID_IN_HOURS="${2}"
  local SYSENV_SHORT_NAME="${3}"
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

  cat > ca-csr.json <<EOF
{
  "CN": "${SYSENV_SHORT_NAME}",
  "key": {
    "algo": "rsa",
    "size": 2048
  },
  "names": [
    {
      "O": "${SYSENV_SHORT_NAME}",
      "OU": "CA"
    }
  ]
}
EOF

  cfssl gencert -initca ca-csr.json | cfssljson -bare ca
  cd -

  CA_KEY_FILE="${CA_DIRECTORY}/ca-key.pem"
  CA_CERTIFICATE_FILE="${CA_DIRECTORY}/ca.pem"
  echo "CA_KEY_FILE is at ${CA_KEY_FILE} and CA_CERTIFICATE_FILE is at ${CA_CERTIFICATE_FILE}"
}

# Ensure dependencies are present
if [[ ! -x $(command -v cfssl) || ! -x $(command -v cfssljson) || ! -x $(command -v aws) ]]; then
  echo "[-] Dependencies unmet.  Please verify that the following are installed and in the PATH: cfssl, cfssljson, awscli" >&2
  exit 1
fi

while getopts ":s:t:b:e:" opt; do
  case ${opt} in
    s)
      SYSENV_SHORT_NAME="${OPTARG}" ;;
    t)
      IVY_TAG="${OPTARG}" ;;
    b)
      BASE_DIRECTORY="${OPTARG}" ;;
    e)
      VALID_IN_YEARS="${OPTARG}" ;;
    \?)
      usage ;;
    :)
      usage ;;
  esac
done

if [[ -z ${SYSENV_SHORT_NAME:-""} ]]; then
  usage
fi

IVY_TAG="${IVY_TAG:-ivy}"
BASE_DIRECTORY="${BASE_DIRECTORY:-.}"
SSM_PREFIX="${IVY_TAG}/${SYSENV_SHORT_NAME}"
SYSENV_DIRECTORY="${BASE_DIRECTORY}/${SSM_PREFIX}"

# Default to 10 years
VALID_IN_YEARS="${VALID_IN_YEARS:-10}"
DAYS_IN_YEAR='365'
HOURS_IN_DAY='24'
HOURS_IN_YEAR='8760'

let "VALID_IN_HOURS = ${VALID_IN_YEARS} * ${HOURS_IN_YEAR}"

CA_DIRECTORY="${SYSENV_DIRECTORY}/CA"
CA_KEY_SSM="/${SSM_PREFIX}/CA/ca-key.pem"
CA_CERTIFICATE_SSM="/${SSM_PREFIX}/CA/ca.pem"
echo "I will check if ${CA_KEY_SSM} is in ssm already or not"
if is_parameter_in_ssm "${CA_KEY_SSM}"; then
  echo 'Nothing to do here'
else
  echo "I will create directories ${CA_DIRECTORY}, CA key and certificate and push them to ssm"
  mkdir -p "${CA_DIRECTORY}"
  generate_certificate_authority "${CA_DIRECTORY}" "${VALID_IN_HOURS}" "${SYSENV_SHORT_NAME}"

  aws ssm put-parameter \
    --name "${CA_KEY_SSM}" \
    --type SecureString \
    --value "$(cat ${CA_KEY_FILE})"


  aws ssm put-parameter \
    --name "${CA_CERTIFICATE_SSM}" \
    --type String \
    --value "$(cat ${CA_CERTIFICATE_FILE})"
fi
