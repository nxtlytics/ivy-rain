#!/bin/bash
set -x
source /opt/ivy/bash_functions.sh

###
### CONFIG ###
###
SERVICE='__SERVICE__'
NAME="${SERVICE}-$(get_instance_id)"
PROMPT_COLOR="__PROMPT_COLOR__"
DEFAULT_DOMAIN="__DEFAULT_DOMAIN__"
TOP_DOMAIN="__TOP_DOMAIN__"
#REPOSITORIES=(__REPOSITORIES__)
# Filled by Cloudformation
ENI_ID='{#CFN_ENI_ID}'
EBS_ID='{#CFN_EBS_ID}'

function setup_volume() {
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
    if [ ! -d ${MOUNT_PATH}/nexus ]; then
        mkdir -p ${MOUNT_PATH}/nexus
        chown -R nexus: ${MOUNT_PATH}/nexus
    fi

    local FSTAB="${DEVICE} ${MOUNT_PATH} ext4 defaults 0 0"
    sed -i '/${DEVICE}/d' /etc/fstab
    echo ${FSTAB} >> /etc/fstab
}

function setup_nginx() {
    # Install and configure nginx default site as a reverse proxy for nexus
    amazon-linux-extras install -y nginx1 epel
    yum install -y python-virtualenv

    if [ ! -d /mnt/data/nexus/nginx ]; then
        # Nexus nginx config does not exist, make it!
        mkdir -p /mnt/data/nexus/nginx/conf.d
        mkdir -p /mnt/data/nexus/nginx/default.d
        mkdir -p /mnt/data/nexus/letsencrypt/live/${DEFAULT_DOMAIN}

#        DOMAINS=''
#        for d in ${REPOSITORIES[@]}; do
#            DOMAINS="${DOMAINS} ${d}.${DEFAULT_DOMAIN} ${d}.${TOP_DOMAIN}"
#        done

        cat <<'EOF' > /mnt/data/nexus/nginx/default.d/redirect.conf
server {
    listen 80 default_server;
    server_name _;
    return 301 https://$host$request_uri;
}
EOF

        cat <<EOF > /mnt/data/nexus/nginx/conf.d/nexus.conf
# Main Nexus UI proxy
server {
    listen       443 ssl http2 default_server;
    listen       [::]:443 ssl http2 default_server;
    server_name  nexus.${DEFAULT_DOMAIN} nexus.${TOP_DOMAIN};

    ssl_certificate /mnt/data/certbot/config/live/nexus.${TOP_DOMAIN}/fullchain.pem;
    ssl_certificate_key /mnt/data/certbot/config/live/nexus.${TOP_DOMAIN}/privkey.pem;
    ssl_trusted_certificate /mnt/data/certbot/config/live/nexus.${TOP_DOMAIN}/chain.pem;
    ssl_session_cache shared:SSL:1m;
    ssl_session_timeout  10m;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    client_max_body_size 1g;

    location / {
        proxy_buffers 8 24k;
        proxy_buffer_size 2k;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header Host      \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-Proto "https";
        proxy_pass http://localhost:8081;
    }
}

# Per-repo proxies
server {
    listen       443 ssl http2;
    listen       [::]:443 ssl http2;
    server_name  ~^(?<sub>[^.]+)\..*$;

    ssl_certificate /mnt/data/certbot/config/live/nexus.${TOP_DOMAIN}/fullchain.pem;
    ssl_certificate_key /mnt/data/certbot/config/live/nexus.${TOP_DOMAIN}/privkey.pem;
    ssl_trusted_certificate /mnt/data/certbot/config/live/nexus.${TOP_DOMAIN}/chain.pem;
    ssl_session_cache shared:SSL:1m;
    ssl_session_timeout  10m;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    client_max_body_size 1g;

    location / {
        proxy_buffers 8 24k;
        proxy_buffer_size 2k;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header Host      \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-Proto "https";
        proxy_pass http://localhost:8081/repository/\$sub/\$request_uri;
    }
}
EOF
    fi

    # symlink nginx configs
    ln -s /mnt/data/nexus/nginx/default.d/redirect.conf /etc/nginx/default.d/redirect.conf
    ln -s /mnt/data/nexus/nginx/conf.d/nexus.conf /etc/nginx/conf.d/nexus.conf
}

function setup_stunnel() {
    yum install -y stunnel
    if [ -d /mnt/data/nexus/stunnel/ ]; then
        # configs for stunnel aren't stored in git - must be bootstrapped (TODO: secret stores)
        cp /mnt/data/nexus/stunnel/stunnel-gsuite.service /etc/systemd/system
        systemctl enable stunnel-gsuite
        systemctl start stunnel-gsuite
    fi
}


function install_nexus() {
    local NEXUS_VERSION="nexus-3.18.1-01"
    # Add nexus user - using static GID here so that the IDs stored on the volume mount are consistent across OS updates
    groupadd -g 742 -r nexus
    adduser -d /var/lib/nexus -m -N -r -u 742 -g nexus nexus
    # Make install directory
    mkdir /opt/nexus

    # Download, extract, and symlink
    download_file=${NEXUS_VERSION}-unix.tar.gz
    wget -O /tmp/${download_file} http://download.sonatype.com/nexus/3/${download_file}
    tar -C /opt/nexus/ -xvf /tmp/${download_file}
    ln -s /opt/nexus/${NEXUS_VERSION} /opt/nexus/latest

    # nexus not compatible with jdk11
    #amazon-linux-extras install -y java-openjdk11
    yum install -y java-1.8.0-openjdk-headless
}

function configure_nexus() {
    # TODO: ...
    # Raise FD limit
    # Setup logrotate

    # Configure nexus storage location
    mkdir /var/log/nexus
    chown nexus: /var/log/nexus
    if [ ! -d /mnt/data/nexus/sonatype-work ]; then
        # Only move the default storage if one doesn't exist on the EBS volume (initial bootstrapping)
        mv /opt/nexus/sonatype-work /mnt/data/nexus/
        chown -R nexus: /mnt/data/nexus/sonatype-work
        # Setup log symlink if it doesn't exist
        rm -rf /mnt/data/nexus/sonatype-work/nexus3/log
        ln -s /var/log/nexus /mnt/data/nexus/sonatype-work/nexus3/log
    fi
    # Nuke the installer-provided data folder if it still exists
    rm -rf /opt/nexus/sonatype-work
    # Symlink to the workdir on the EBS volume
    ln -s /mnt/data/nexus/sonatype-work /opt/nexus/sonatype-work
    # Create systemd unit
    cat <<EOF > /etc/systemd/system/nexus.service
[Unit]
Description=Nexus Repository Manager
After=network.target

[Service]
ExecStart=/opt/nexus/latest/bin/nexus run

User=nexus
Group=nexus

SuccessExitStatus=0 143
RestartSec=15
Restart=on-failure

LimitNOFILE=102642

[Install]
WantedBy=multi-user.target
EOF
}

attach_eni $(get_instance_id) ${ENI_ID}
set_hostname ${NAME}
setup_volume

setup_nginx
install_nexus
configure_nexus

# start 'em up!
systemctl enable nginx
systemctl start nginx
systemctl enable nexus
systemctl start nexus
