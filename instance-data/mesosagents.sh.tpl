#!/bin/bash
set -x
source /opt/ivy/bash_functions.sh

###
### CONFIG ###
###
PLACEMENT="__PLACEMENT__"
ZK_CONNECT="__ZK_CONNECT__"
CLUSTER_NAME="mesos-$(get_environment)"
AVAILABILITY_ZONE=$(get_availability_zone)
ENVIRONMENT=$(get_environment)
INSTANCE_ID=$(get_instance_id)
IP=$(get_ip_from_interface eth0)

function setup_docker_overlay() {
    local DEVICE="/dev/xvdb"
    local MOUNT_PATH="/mnt/docker"

    service docker stop
    sleep 2

    mkfs.xfs ${DEVICE}
    mkdir -p ${MOUNT_PATH}
    mount ${DEVICE} ${MOUNT_PATH}

    rm -rf /var/lib/docker
    ln -s ${MOUNT_PATH} /var/lib/docker

    # TODO: can probably remove this once it's baked into the AMI(?)
    echo 'DOCKER_STORAGE_OPTIONS="--storage-driver overlay2"' > /etc/sysconfig/docker-storage

    local FSTAB="${DEVICE} ${MOUNT_PATH} xfs defaults 0 0"
    sed -i '/${DEVICE}/d' /etc/fstab
    echo ${FSTAB} >> /etc/fstab

    service docker start
}

function setup_docker_config() {
    # grab docker auth from consul
    # use --fail to prevent writing an output when the file does not exist and eat the status code to prevent
    # failure of the entire cloud-init script
    mkdir /root/.docker
    curl --fail -o /root/.docker/config.json http://localhost:8500/v1/kv/config/infrastructure/docker_config?raw || true
}

function setup_mesos_agent() {
    cat <<'EOF' > /etc/mesos-slave-modules.json
{
  "libraries": [
    {
      "file": "/usr/lib/mesos/modules/liblogrotate_container_logger-1.9.0.so",
      "modules": [
        {
          "name": "org_apache_mesos_LogrotateContainerLogger",
          "parameters": [
            {
              "key": "max_stdout_size",
              "value": "300MB"
            },
            {
              "key": "max_stderr_size",
              "value": "300MB"
            },
            {
              "key": "logrotate_stdout_options",
              "value": "rotate 10\ncompress\ndelaycompress\ncopytruncate"
            },
            {
              "key": "logrotate_stderr_options",
              "value": "rotate 10\ncompress\ndelaycompress\ncopytruncate"
            }
          ]
        }
      ]
    }
  ]
}
EOF

    # TODO: automatically discover mesos masters - use consul or AWS tags?
    echo "zk://${ZK_CONNECT}/mesos" > /etc/mesos/zk
    echo ${IP} > /etc/mesos-slave/ip
    echo $(hostname -f) > /etc/mesos-slave/hostname
    echo 9mins > /etc/mesos-slave/executor_registration_timeout
    echo 30secs > /etc/mesos-slave/docker_stop_timeout
    echo 35secs > /etc/mesos-slave/executor_shutdown_grace_period
    echo 1hrs > /etc/mesos-slave/docker_remove_delay
    echo docker,mesos > /etc/mesos-slave/containerizers
    echo cgroups/cpu,cgroups/mem > /etc/mesos-slave/isolation
    echo WARNING > /etc/mesos-slave/logging_level
    echo /etc/mesos-slave-modules.json > /etc/mesos-slave/modules
    echo org_apache_mesos_LogrotateContainerLogger > /etc/mesos-slave/container_logger
    echo '/mnt/docker/work_dir' > /etc/mesos-slave/work_dir

    if [[ -f /root/.docker/config.json ]]; then
        # custom docker options (auth/etc)
        echo 'file:///root/.docker/config.json' > /etc/mesos-slave/docker_config
    fi

    # Set Mesos Slave attributes
    if [[ ! -d "/etc/mesos-slave/attributes" ]]; then
        mkdir -p /etc/mesos-slave/attributes
    fi

    # TODO: Add attributes from Cloudformation here per ASG (or dynamically generate)
    echo "${AVAILABILITY_ZONE}" > /etc/mesos-slave/attributes/availability_zone
    echo "general" > /etc/mesos-slave/attributes/cluster_group
    echo "${PLACEMENT}" > /etc/mesos-slave/attributes/placement
}

function setup_consul() {
    cat <<EOF > /etc/consul.d/mesos-agent.json
{
    "services": [
        {
            "id": "mesos-agent-${INSTANCE_ID}",
            "name": "mesos",
            "tags": [
              "${ENVIRONMENT}",
              "${AVAILABILITY_ZONE}",
              "agent"
            ],
            "port": 5050,
            "checks": [
              {
                  "id": "mesos-slave",
                  "name": "Mesos Slave Process",
                  "http": "http://${IP}:5051/metrics/snapshot",
                  "interval": "10s",
                  "timeout": "1s"
              }
            ]
        },
        {
            "id": "haproxy-${INSTANCE_ID}",
            "name": "haproxy",
            "tags": [
              "${ENVIRONMENT}",
              "${AVAILABILITY_ZONE}",
              "slave"
            ],
            "port": 80,
            "checks": [
              {
                  "id": "haproxy",
                  "name": "haproxy",
                  "http": "http://${IP}/ping",
                  "interval": "5s",
                  "timeout": "5s"
              }
            ]
        }
    ]
}
EOF
}

function setup_datadog() {
  # 99th percentile, enable if needed
  #sed -i -e "s/# histogram_percentiles:.*/histogram_percentiles: 0.95, 0.99/" /etc/datadog-agent/datadog.yaml

  # Datadog
  cat <<EOF > /etc/datadog-agent/conf.d/mesos_slave.d/conf.yaml
init_config:
  default_timeout: 5

instances:
  - url: "http://${IP}:5051"
EOF

  cat <<EOF > /etc/datadog-agent/conf.d/haproxy.d/conf.yaml
init_config:

instances:
  - url: http://localhost:9090/admin?stats
    username: admin
    password: admin
    collect_aggregates_only: True
EOF

  cat <<EOF > /etc/datadog-agent/conf.d/docker.d/conf.yaml
init_config:

instances:
  - ## Daemon and system configuration
    url: "unix://var/run/docker.sock"
    collect_events: false
    collect_container_size: false
    tags: ["$(get_ivy_tag):environment:${ENVIRONMENT}"]
    performance_tags: ["image_name", "image_tag"]
    container_tags: ["image_name", "image_tag"]
EOF

  cat <<EOF >> /etc/datadog-agent/conf.d/process.d/conf.yaml
init_config:

instances:
  - name: consul-template
    search_string: ['consul-template']
    exact_match: false
  - name: mesos-slave
    search_string: ['/usr/sbin/mesos-slave']
    exact_match: false
  - name: haproxy
    search_string: ['/usr/local/sbin/haproxy -Ws -f /etc/haproxy/haproxy.cfg']
    exact_match: false
    thresholds:
      critical: [1, 20]
EOF

  service datadog-agent restart
}

# Don't let your dreams be dreams! DO IT!
set_hostname mesos-agent-${INSTANCE_ID}
set_prompt_color "__PROMPT_COLOR__"

# Start consul before enabling any services that require it
setup_consul
bash /opt/ivy/configure_consul.sh

# Enable services
setup_docker_overlay
setup_docker_config
setup_mesos_agent
setup_datadog

# Enable statically configured services
systemctl enable haproxy mesos-slave consul-template
systemctl start haproxy mesos-slave consul-template

# Start dynamically configured services
bash /opt/ivy/setup_registrator.sh
bash /opt/ivy/ec2metaproxy.sh
