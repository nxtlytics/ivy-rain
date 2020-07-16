#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

THIS_SCRIPT=$(basename $0)
PADDING=$(printf %-${#THIS_SCRIPT}s " ")

usage () {
    echo "Usage:"
    echo "${THIS_SCRIPT} -p <REQUIRED: AWS profile name>"
    echo
    echo "Suspends scaling processes for all autoscaling groups in an"
    echo "aws region"
    exit 1
}

# Ensure dependencies are present
if [[ ! -x $(which aws) ]] || [[ ! -x $(which jq) ]]; then
    echo "[-] Dependencies unmet.  Please verify that the following are installed and in the PATH:  aws, jq" >&2
    exit 1
fi

while getopts ":p:" opt; do
  case ${opt} in
    p)
      export AWS_PROFILE=${OPTARG} ;;
    \?)
      usage ;;
    :)
      usage ;;
  esac
done

if [[ -z ${AWS_PROFILE:-""} ]] ; then
  usage
fi

REGION=$(aws configure get region)

echo "Getting all autoscaling groups for region: ${REGION}"
AUTOSCALING_GROUPS=( $(aws autoscaling describe-auto-scaling-groups --query="AutoScalingGroups[*].{AutoScalingGroupName: AutoScalingGroupName}" --output text) )

for asg in "${AUTOSCALING_GROUPS[@]}"; do
  aws autoscaling suspend-processes --auto-scaling-group-name "${asg}" --scaling-processes 'ReplaceUnhealthy'
  echo "ReplaceUnhealthy has been suspended for ASG: ${asg}"
done
