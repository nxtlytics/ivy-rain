#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

THIS_SCRIPT=$(basename $0)
PADDING=$(printf %-${#THIS_SCRIPT}s " ")

usage () {
    echo "Usage:"
    echo "${THIS_SCRIPT} -u <REQUIRED: administrator username> -p <REQUIRED: administrator password>"
    echo "${PADDING} -h <REQUIRED: psql hostname, port is optional (host[:port])>"
    echo "${PADDING} -d <REQUIRED: New database name> -e <Optional, enable postgis>"
    echo
    echo 'Creates postgresql database, admin, read, app users'
    echo "Note: this uses 1Password's op command line."
    echo '      Please run: `eval $(op signin <1Password account name>)`'
    exit 1
}

# Ensure dependencies are present
if [[ ! -x $(command -v psql) || ! -x $(command -v op) || ! -x $(command -v jq) ]]; then
    echo "[-] Dependencies unmet.  Please verify that the following are installed and in the PATH: psql, jq, op (1Password cli)" >&2
    exit 1
fi

while getopts ":eu:p:d:h:" opt; do
  case ${opt} in
    e)
      POSTGIS='yes' ;;
    u)
      USERNAME="${OPTARG}" ;;
    p)
      PASSWORD="${OPTARG}" ;;
    d)
      DATABASE_NAME="$(echo "${OPTARG}" | tr '[:upper:]' '[:lower:]')" ;;
    h)
      if grep -q ':' <<< "${OPTARG}"; then
        HOSTNAME="$(echo "${OPTARG}" | cut -d ':' -f1)"
        PORT="$(echo "${OPTARG}" | cut -d ':' -f2)"
      else
        HOSTNAME="${OPTARG}"
        PORT='5432'
      fi;;
    \?)
      usage ;;
    :)
      usage ;;
  esac
done

if [[ -z ${USERNAME:-""} || -z ${PASSWORD:-""} || -z ${DATABASE_NAME:-""} || -z ${HOSTNAME:-""} || -z ${PORT:-""} ]]; then
  usage
fi

if [[ "${POSTGIS}" == 'yes' ]]; then
  POSTGIS='create extension postgis;'
else
  POSTGIS='select current_user;'
fi

TMP_DIR=$(mktemp -d 2>/dev/null || mktemp -d -t 'mytmpdir')

function cleanup () {
  rm -rf "${TMP_DIR}/"
}

# Make sure cleanup runs on exit
trap cleanup EXIT

OP_VAULT='Temporary'
OP_ITEMS="${TMP_DIR}/items_in_vault.json"
op list items --vault "${OP_VAULT}" > "${OP_ITEMS}"

function create_item_if_not_exists () {
  local TITLE="${1}"
  local PERMISSION="$(echo "${TITLE}" | cut -d ' ' -f3 | tr '[:upper:]' '[:lower:]')"
  local USERNAME="${DATABASE_NAME}_${PERMISSION}"
  local QUERY=".[] | select(.overview.title | test(\"^${TITLE}$\"))"
  if jq -e "${QUERY}" "${OP_ITEMS}" &> /dev/null; then
    echo "Item ${TITLE} already exists no need to create it"
  else
    op create item login $(op get template login | op encode) "username=${USERNAME}" --generate-password --vault "${OP_VAULT}" --title "${TITLE}" --url "${HOSTNAME}"
  fi
}

for item in "RDS ${DATABASE_NAME} Admin" "RDS ${DATABASE_NAME} App" "RDS ${DATABASE_NAME} Read"; do
  create_item_if_not_exists "${item}"
done

function get_password_from_op () {
  local TITLE="${1}"
  local GET_PASSWORD_QUERY='.details.fields[] | select(.name | test("^password$")) | .value'
  op get item "${TITLE}" --vault "${OP_VAULT}" | jq -r "${GET_PASSWORD_QUERY}"
}


ADMIN_PASS="$(get_password_from_op "RDS ${DATABASE_NAME} Admin")"
APP_PASS="$(get_password_from_op "RDS ${DATABASE_NAME} App")"
READ_PASS="$(get_password_from_op "RDS ${DATABASE_NAME} Read")"

SQL_FILE="${TMP_DIR}/createdb.sql"

cat << EOF > "${SQL_FILE}"
CREATE DATABASE ${DATABASE_NAME};
\c ${DATABASE_NAME}
begin;
create user ${DATABASE_NAME}_admin with password '${ADMIN_PASS}';
create user ${DATABASE_NAME}_app with password '${APP_PASS}';
create user ${DATABASE_NAME}_read with password '${READ_PASS}';
GRANT ALL PRIVILEGES ON DATABASE ${DATABASE_NAME} to ${DATABASE_NAME}_admin;
grant all on all tables in schema public to ${DATABASE_NAME}_app;
grant select on all tables in schema public to ${DATABASE_NAME}_read;
${POSTGIS}
commit;
EOF

echo "I will now run the sql commands below:"
printf '\n```\n'
cat "${SQL_FILE}"
printf '```\n\n'

read -p "Are you sure you want to create Database '${DATABASE_NAME}' and its users? " -n 1 -r USER_REPLY
echo
if [[ ${USER_REPLY} =~ ^[Yy]$ ]]; then
  psql "postgres://${USERNAME}:${PASSWORD}@${HOSTNAME}:${PORT}/postgres" \
      --echo-all \
      -f "${SQL_FILE}" \
      --set AUTOCOMMIT=off \
      --set ON_ERROR_STOP=on
else
  echo "Did not execute psql"
fi

echo "I'm done"
