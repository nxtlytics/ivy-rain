#!/bin/bash
set -x
source /opt/ivy/bash_functions.sh

###
### CONFIG ###
###
SERVICE='__SERVICE__'
NAME="${SERVICE}-$(get_instance_id)"
SERVER_ID='__SERVER_ID__'
MONGODB='__MONGODB__'
PROMPT_COLOR="__PROMPT_COLOR__"
# Filled by Cloudformation
ENI_ID='{#CFN_ENI_ID}'
EBS_ID='{#CFN_EBS_ID}'

function setup_nat() {
  sysctl -w net.ipv4.ip_forward=1
  sed -i -e 's/net.ipv4.ip_forward = 0/net.ipv4.ip_forward = 1/' /etc/sysctl.conf
}

function do_setup_volume() {
    local DEVICE="/dev/sdf"
    local MOUNT_PATH="/mnt/data"

    attach_ebs $(get_instance_id) ${EBS_ID} ${DEVICE}
    if [ $? -ne 0 ]; then
        echo "Error attach volume, aborting"
        exit 1
    fi

    # check if volume needs formatting (DANGEROUS!)
    # we'll use 'file' to check if the device lacks the ext4 magic
    if ! file -sL ${DEVICE} | grep -q "ext4"; then
        echo "Device needs formatting..."
        mkfs.ext4 ${DEVICE}
        if [ $? -ne 0 ]; then
            echo "Error formatting volume, aborting"
            exit 1
        fi
    fi

    # Mount to ${MOUNT_PATH}
    if [ ! -d ${MOUNT_PATH} ]; then
        mkdir -p ${MOUNT_PATH}
    fi
    mount ${DEVICE} ${MOUNT_PATH}
    if [ $? -ne 0 ]; then
        echo "Error mounting volume, aborting"
        exit 1
    fi

    # check if storage folders exist in mounted volume
    if [ ! -d ${MOUNT_PATH}/mongo ]; then
        mkdir -p ${MOUNT_PATH}/mongo
        chown -R mongod: ${MOUNT_PATH}/mongo
    fi

    local FSTAB="${DEVICE} ${MOUNT_PATH} ext4 defaults 0 0"
    sed -i '/${DEVICE}/d' /etc/fstab
    echo ${FSTAB} >> /etc/fstab
}

function setup_volume() {
    if [ ! -z ${EBS_ID} ]; then
        echo "Setting up data volume ${EBS_ID} ..."
        do_setup_volume
        echo "Configuring mongodb..."
        rm -rf /var/lib/mongo
        ln -s /mnt/data/mongo /var/lib/mongo
        echo "Finished"
    fi
}

function install_pritunl() {
    cat <<EOF > /etc/yum.repos.d/mongodb-org-4.0.repo
[mongodb-org-4.0]
name=MongoDB Repository
baseurl=https://repo.mongodb.org/yum/redhat/7/mongodb-org/4.0/x86_64/
gpgcheck=1
enabled=1
gpgkey=https://www.mongodb.org/static/pgp/server-4.0.asc
EOF

    cat <<EOF > /etc/yum.repos.d/pritunl.repo
[pritunl]
name=Pritunl Repository
baseurl=https://repo.pritunl.com/stable/yum/centos/7/
gpgcheck=1
enabled=1
EOF

    amazon-linux-extras install -y epel
    gpg --keyserver hkp://keyserver.ubuntu.com --recv-keys 7568D9BB55FF9E5287D586017AE645C0CF8E292A
    gpg --armor --export 7568D9BB55FF9E5287D586017AE645C0CF8E292A > key.tmp; sudo rpm --import key.tmp; rm -f key.tmp
    yum -y install pritunl-1.29.2276.91 mongodb-org
}

function configure_pritunl() {
    if [ ! -z ${MONGODB} ]; then
        pritunl set-mongodb "${MONGODB}"
    else
        sed -i -e "s/.*bindIp: 127.0.0.1.*/  bindIp: 0.0.0.0/" /etc/mongod.conf
    fi
    echo "${SERVER_ID}" > /var/lib/pritunl/pritunl.uuid
}

attach_eni $(get_instance_id) ${ENI_ID}
set_hostname ${NAME}
setup_nat

# Install Pritunl and configure the data volume (UIDs and GIDs not created until install, so setup data volume after)
install_pritunl
setup_volume

# Configure Pritunl and start it
configure_pritunl
if [ ! -z ${MONGODB} ]; then
    systemctl enable mongod
    systemctl start mongod
fi

systemctl start pritunl
systemctl enable pritunl
