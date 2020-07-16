#!/bin/bash
set -x
source /opt/ivy/bash_functions.sh

###
### CONFIG ###
###
SERVICE="__SERVICE__"
VPN_NAME="__VPN_NAME__"
NAME="${SERVICE}-$(get_instance_id)"
VPC_ID="__VPC_ID__"
REGION=$(get_region)
LOCAL_SUBNETS="__LOCAL_SUBNETS__"
REMOTE_IP="__REMOTE_IP__"
REMOTE_SUBNETS="__REMOTE_SUBNETS__"
SECRET="__SECRET__"
IKE="__IKE__"
IKE_LIFETIME="__IKE_LIFETIME__"
KEYLIFE="__KEYLIFE__"
ESP="__ESP__"

# Filled by Cloudformation
ENI_ID='{#CFN_ENI_ID}'
LOCAL_PUBLIC_IP='{#CFN_EIP_ADDR}'


function setup_vpn() {
  sysctl -w net.ipv4.ip_forward=1
  sed -i -e 's/net.ipv4.ip_forward = 0/net.ipv4.ip_forward = 1/' /etc/sysctl.conf

  amazon-linux-extras install -y epel
  yum -y install strongswan iptables-services

  echo "include /etc/strongswan/ipsec.d/*.conf" >> /etc/strongswan/ipsec.conf

  cat <<EOF > "/etc/strongswan/ipsec.d/${VPN_NAME}.conf"
conn ${VPN_NAME}
    #type=tunnel
    auto=start
    left=%defaultroute
    leftnexthop=%defaultroute
    leftid=${LOCAL_PUBLIC_IP}
    leftsubnet=${LOCAL_SUBNETS}
    right=${REMOTE_IP}
    rightsubnet=${REMOTE_SUBNETS}
    rightid=${REMOTE_IP}
    keyexchange=ike
    ikelifetime=${IKE_LIFETIME}
    ike=${IKE}
    authby=secret
    keylife=${KEYLIFE}
    esp=${ESP}
EOF

  cat <<EOF >> "/etc/strongswan/ipsec.secrets"
${REMOTE_IP} : PSK "${SECRET}"
EOF
}

function setup_iptables() {
    iptables -I FORWARD -s ${LOCAL_SUBNETS} -d ${REMOTE_SUBNETS} -j ACCEPT
    iptables -I FORWARD -s ${REMOTE_SUBNETS} -d ${LOCAL_SUBNETS} -j ACCEPT
    # Begin custom iptables rules
    __IPTABLES_RULES__
    # End custom iptables rules

    # commandment number 11: thou shalt persist thine rules
    service iptables save
    systemctl enable iptables
}

attach_eni $(get_instance_id) ${ENI_ID}
set_hostname ${NAME}
set_prompt_color "__PROMPT_COLOR__"
setup_vpn
setup_iptables

# Configure to start on reboot.
systemctl enable strongswan
systemctl start strongswan
