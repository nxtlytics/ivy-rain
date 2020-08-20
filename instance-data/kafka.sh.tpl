#!/bin/bash
set -x
source /opt/ivy/bash_functions.sh

###
### CONFIG ###
###
CLUSTER_NAME="__CLUSTER_NAME__"
NAME="${CLUSTER_NAME}-$(get_instance_id)"
REGION=$(get_region)
set_hostname ${NAME}
set_prompt_color "__PROMPT_COLOR__"


# TODO: bake me into base!
function get_ram_mb_by_percent() {
    local PERCENT=$1
    MB=$(grep MemTotal /proc/meminfo | awk "{printf(\"%.0f\", \$2 / 1024 * ${PERCENT})}")
    echo ${MB}
}

function setup_consul() {
    # Register with Consul
    cat <<EOF > /etc/consul.d/${CLUSTER_NAME}.json
{
  "service": {
    "id": "${NAME}",
    "name": "${CLUSTER_NAME}",
    "tags": [
      "$(get_environment)",
      "$(get_availability_zone)"
    ],
    "port": 9092,
    "check":
      {
          "tcp": "localhost:9092",
          "interval": "5s",
          "timeout": "10s"
      }
  }
}
EOF
    bash /opt/ivy/configure_consul.sh

}

function configure_kafka() {
    # Add kafka properties
cat <<EOF >> /etc/kafka/server.properties
# use mesos master as ZK chroot host
zookeeper.connect=zookeeper.service.$(get_ivy_tag):2181/${CLUSTER_NAME}

# set broker rack
broker.rack=$(get_availability_zone)

# default new topic partition count
num.partitions=12

# enable topic replicas
default.replication.factor=2

# enable creating topics by publishing to (or reading from) nonexistent topics
auto.create.topics.enable=true

# 3 days default retention period for autocreated topics
log.retention.ms=259200000

# enable topic deletion
delete.topic.enable=true
EOF

    JVM_HEAP=$(get_ram_mb_by_percent .55)
    echo "KAFKA_HEAP_OPTS=\"-Xmx${JVM_HEAP}m -Xms${JVM_HEAP}m\"" >> /etc/sysconfig/kafka
}

function setup_datadog() {
    mv /etc/datadog-agent/conf.d/kafka.d/conf.yaml.example /etc/datadog-agent/conf.d/kafka.d/conf.yaml
    cat <<EOF > /etc/datadog-agent/conf.d/kafka_consumer.d/conf.yaml
init_config:

instances:
  - kafka_connect_str:
    - localhost:9092
    monitor_unlisted_consumer_groups: true
EOF

    service datadog-agent restart
}

function setup_swap() {
    echo "Setting up swap..."
    # 64MB block size x 16 = 1GB of swap - use large block size to cut down on the EBS round-trips for the SCSI commands
    dd if=/dev/zero of=/swap.img bs=64M count=16
    chmod 0600 /swap.img
    mkswap /swap.img
    swapon /swap.img
    echo "/swap.img none swap sw 0 0" >> /etc/fstab
    echo "Finished setting up swap"
}

setup_consul
#setup_swap
configure_kafka
setup_datadog

systemctl enable kafka
systemctl start kafka
