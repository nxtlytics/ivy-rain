#!/bin/bash
set -x
source /opt/ivy/bash_functions.sh

function setup_bind() {
    # install bind
    yum install -y bind

    # nuke standard bind config and replace with ours
    cat <<EOF > /etc/named.conf
options {
	listen-on port 53 { 0.0.0.0; };
	listen-on-v6 port 53 { ::1; };
	directory 	"/var/named";
	dump-file 	"/var/named/data/cache_dump.db";
    statistics-file "/var/named/data/named_stats.txt";
    memstatistics-file "/var/named/data/named_mem_stats.txt";
	allow-query     { localhost; };
	// No recursion!
	recursion no;

	dnssec-enable yes;
	dnssec-validation yes;

	/* Path to ISC DLV key */
	bindkeys-file "/etc/named.iscdlv.key";

	managed-keys-directory "/var/named/dynamic";

    // hehe
	version "9.11.1.2.0_HP-UX_B.11.31_IA_PA";
};

logging {
        channel default_debug {
                file "data/named.run";
                severity dynamic;
        };
};

// This machine is a Route53 DNS forwarder to expose VPCDNS to public DNS servers
include "/etc/named/route53-forwarder.zone";
EOF

    echo ${BIND_ZONEFILE} > /etc/named/route53-forwarder.zone
}


###
### CONFIG ###
###
SERVICE='__SERVICE__'
NAME="${SERVICE}-$(get_instance_id)"
PROMPT_COLOR="__PROMPT_COLOR__"
BIND_ZONEFILE='__BIND_ZONEFILE__'
# Filled by Cloudformation
ENI_ID='{#CFN_ENI_ID}'

attach_eni $(get_instance_id) ${ENI_ID}
set_hostname ${NAME}

setup_bind
systemctl enable bind
systemctl start bind
