#!/bin/bash
set -x

source /opt/ivy/bash_functions.sh

###
### CONFIG ###
###
# Filled from python side of Rain
CASSANDRA_CLUSTER='__CASSANDRA_CLUSTER__'
CASSANDRA_CLUSTER_OVERRIDE='__CASSANDRA_CLUSTER_OVERRIDE__'
CASSANDRA_SEEDS='__CASSANDRA_SEEDS__'
SERVICE='__SERVICE__'
# Filled by AWS in Cloudformation
DATA_EBS_VOLUME_ID='{#CFN_DATA_EBS_VOLUME_ID}'
ENI_ID='{#CFN_ENI_ID}'

# TODO: bake me into base!
function get_ram_mb_by_percent() {
    local PERCENT=$1
    MB=$(grep MemTotal /proc/meminfo | awk "{printf(\"%.0f\", \$2 / 1024 * ${PERCENT})}")
    echo ${MB}
}

function setup_networking() {
    ENI_IP=$(get_eni_ip ${ENI_ID})
    attach_eni $(get_instance_id) ${ENI_ID}

    NAME="cassandra-${CASSANDRA_CLUSTER}-${ENI_IP}"
    NAME=`echo ${NAME} | sed -e 's/\./-/g'`
    set_hostname ${NAME}
}

function setup_volumes() {
    local DEVICE
    local MOUNT_PATH="/mnt/data"

    if [[ ! -n "${DATA_EBS_VOLUME_ID:-''}" ]]; then
        DEVICE="/dev/sdf"
        attach_ebs $(get_instance_id) ${DATA_EBS_VOLUME_ID} ${DEVICE}
        if [ $? -ne 0 ]; then
            echo "Error attach volume, aborting"
            exit 1
        fi
    else
        DEVICE="/dev/ephemeral0"
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
    if [ ! -d ${MOUNT_PATH}/cassandra ]; then
        mkdir -p ${MOUNT_PATH}/cassandra/data
        mkdir -p ${MOUNT_PATH}/cassandra/commitlog
        chown -R cassandra: ${MOUNT_PATH}/cassandra
    fi

    local FSTAB="${DEVICE} ${MOUNT_PATH} ext4 defaults 0 0"
    sed -i '/${DEVICE}/d' /etc/fstab
    echo ${FSTAB} >> /etc/fstab
}

function update_java() {
    yum install -y java-1.8.0-openjdk
    update-alternatives --set java /usr/lib/jvm/jre-1.8.0-openjdk.x86_64/bin/java
}

function setup_cassandra() {
    MEMTABLE_SIZE_MB=$(get_ram_mb_by_percent .25 | ( CAP=8192; read CUR; [[ $CUR -gt $CAP ]] && echo $CAP || echo $CUR ))
    CASSANDRA_CLUSTER_NAME="${CASSANDRA_CLUSTER_OVERRIDE:-"${CASSANDRA_CLUSTER}"}"
    cat <<EOF >> /etc/cassandra/conf/cassandra.yaml
start_rpc: true
concurrent_compactors: 8
compaction_throughput_mb_per_sec: 200
stream_throughput_outbound_megabits_per_sec: 1000
inter_dc_stream_throughput_outbound_megabits_per_sec: 1000
cluster_name: '${CASSANDRA_CLUSTER_NAME}'
data_file_directories:
    - /mnt/data/cassandra/data
commitlog_directory: /mnt/data/cassandra/commitlog
num_tokens: 256
seed_provider:
  - class_name: org.apache.cassandra.locator.SimpleSeedProvider
    parameters:
         - seeds: "${CASSANDRA_SEEDS}"
listen_address: ${ENI_IP}
broadcast_address: ${ENI_IP}
broadcast_rpc_address: ${ENI_IP}
rpc_address: 0.0.0.0
endpoint_snitch: GossipingPropertyFileSnitch
hinted_handoff_throttle_in_kb: 10240

# Key/row cache tuning
key_cache_size_in_mb: 1024
row_cache_class_name: org.apache.cassandra.cache.OHCProvider
row_cache_size_in_mb: 1024
counter_cache_size_in_mb: 200

# perf tuning
trickle_fsync: true
memtable_offheap_space_in_mb: ${MEMTABLE_SIZE_MB}
memtable_allocation_type: offheap_objects

## Only set this to false if you are making a NEW cluster without data
#auto_bootstrap: false
EOF

    if [[ ! -z "${CASSANDRA_CLUSTER_OVERRIDE}" ]]; then
        DC="$(get_environment)-${CASSANDRA_CLUSTER}"
    else
        DC="$(get_environment)"
    fi

    cat <<EOF > /etc/cassandra/conf/cassandra-rackdc.properties
dc=${DC}
rack=$(get_availability_zone)
EOF

    # Set Xmx and Xms based on system RAM
    JVM_HEAP=$(get_ram_mb_by_percent .55 | ( CAP=30000; read CUR; [[ $CUR -gt $CAP ]] && echo $CAP || echo $CUR ))
    mv /etc/cassandra/conf/jvm.options /etc/cassandra/conf/jvm.options.bak
    # Settings for the G1 collector
    cat <<EOF >> /etc/cassandra/conf/jvm.options
-XX:+UseThreadPriorities
-XX:ThreadPriorityPolicy=42
-XX:+HeapDumpOnOutOfMemoryError
-Xss256k
-XX:StringTableSize=1000003
-XX:+AlwaysPreTouch
-XX:-UseBiasedLocking
-XX:+UseTLAB
-XX:+ResizeTLAB
-XX:+PerfDisableSharedMem
-Djava.net.preferIPv4Stack=true
-Xms${JVM_HEAP}m
-Xmx${JVM_HEAP}m
-XX:+UseG1GC
-XX:G1RSetUpdatingPauseTimePercent=5
-XX:MaxGCPauseMillis=500
-XX:+PrintFlagsFinal
-XX:+PrintGCDetails
-XX:+PrintGCDateStamps
-XX:+PrintHeapAtGC
-XX:+PrintTenuringDistribution
-XX:+PrintGCApplicationStoppedTime
-XX:+PrintPromotionFailure
-XX:+UseGCLogFileRotation
-XX:NumberOfGCLogFiles=10
-XX:GCLogFileSize=10M
EOF

    # enable remote JMX
    cat <<EOF >> /etc/default/cassandra
export LOCAL_JMX="false"
export JVM_EXTRA_OPTS="-Dcom.sun.management.jmxremote.authenticate=false"
EOF

    systemctl enable cassandra
    systemctl start cassandra

    # If dev, only backup every other day
    if [ "$(get_environment)" != "prod" ]; then
        sed -i -e 's!^.*\(root /opt/ivy/cassandrabackups.sh\)!30 7 1-31/2 * * \1!' /etc/cron.d/cassandra
    fi
}

function setup_datadog() {
    # this is dumb and i'm sorry, please forgive me
    cat <<EOF > /etc/datadog-agent/conf.d/cassandra.d/conf.yaml
instances:
  - host: localhost
    port: 7199
    max_returned_metrics: 30000
    cassandra_aliasing: true

init_config:
  conf:
    - include:
        domain: org.apache.cassandra.metrics
        type: ClientRequest
        attribute:
          - Count
          - OneMinuteRate
    - include:
        domain: org.apache.cassandra.metrics
        type: ClientRequest
        scope:
          - Read
          - Write
        name:
          - TotalLatency
    - include:
        domain: org.apache.cassandra.metrics
        type: Storage
        name:
          - Load
          - Exceptions
    - include:
        domain: org.apache.cassandra.metrics
        type:
          - Cache
          - CQL
          - Streaming
          - Connection
          - DroppedMessage
          - Messaging
          - HintsService
        attribute:
          - Value
          - Count
          - Mean
          - 99thPercentile
          - OneMinuteRate
          - Max
    - include:
        domain: org.apache.cassandra.metrics
        type:
          - ColumnFamily
          - Table
          - IndexColumnFamily
          - IndexTable
          - Keyspace
        bean_regex:
          - .*keyspace=.*
        attribute:
          - Value
          - Count
          - Mean
          - OneMinuteRate
          - 99thPercentile
          - Max
      exclude:
        keyspace:
          - OpsCenter
          - system
          - system_auth
          - system_distributed
          - system_schema
          - system_traces
    - include:
        domain: org.apache.cassandra.metrics
        type: Cache
        name:
          - Capacity
          - Size
        attribute:
          - Value
    - include:
        domain: org.apache.cassandra.metrics
        type: Cache
        name:
          - Hits
          - Requests
        attribute:
          - Count
    - include:
        domain: org.apache.cassandra.metrics
        type: ThreadPools
        path: request
        name:
          - ActiveTasks
          - CompletedTasks
          - PendingTasks
          - CurrentlyBlockedTasks
    - include:
        domain: org.apache.cassandra.db
        attribute:
          - UpdateInterval
EOF
    service datadog-agent restart
}

function setup_consul() {
    # Register with Consul
    cat <<EOF > /etc/consul.d/${SERVICE}.json
{
  "service": {
    "id": "${NAME}",
    "name": "${SERVICE}",
    "tags": [
      "$(get_availability_zone)"
    ],
    "port": 9042,
    "check": {
      "args": ["nc", "-z", "-w5", "127.0.0.1", "9042"],
      "interval": "5s"
    }
  }
}
EOF

    bash /opt/ivy/configure_consul.sh
}

setup_networking
set_prompt_color "__PROMPT_COLOR__"
setup_volumes
update_java
setup_cassandra
setup_datadog
setup_consul
