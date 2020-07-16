#!/bin/bash
set -x
source /opt/ivy/bash_functions.sh

###
### CONFIG ###
###
SERVICE='Mesos'
CLUSTER_NAME="mesos-$(get_environment)"
# Filled by Cloudformation
ENI_ID='{#CFN_ENI_ID}'
# Filled by Rain
ENI_IP="__ENI_IP__"
ZK_SERVER_ID="__ZK_SERVER_ID__"
HOSTS_ENTRIES="__HOSTS_ENTRIES__"
ZK_CONNECT="__ZK_CONNECT__"
ZK_PEERS="__ZK_PEERS__"


function setup_mesos_master() {
    # Configure Zookeeper
  cat <<EOF > /etc/zookeeper/conf/zoo.cfg
tickTime=2000
initLimit=10
syncLimit=5
dataDir=/var/lib/zookeeper
autopurge.purgeInterval=1
clientPort=2181
${ZK_PEERS}
EOF
  echo ${ZK_SERVER_ID} > /etc/zookeeper/conf/myid
  ln -s /etc/zookeeper/conf/myid /var/lib/zookeeper/myid
  # Update this if zk increments version!
  mkdir /var/lib/zookeeper/version-2
  chown zookeeper: /var/lib/zookeeper/version-2

  # Configure Mesos
  echo "2" > /etc/mesos-master/quorum
  echo "zk://${ZK_CONNECT}/mesos" > /etc/mesos/zk
  echo $(hostname -f) > /etc/mesos-master/hostname
  echo ${ENI_IP} > /etc/mesos-master/ip
  echo ${CLUSTER_NAME} > /etc/mesos-master/cluster
  echo WARNING > /etc/mesos-master/logging_level

  # Configure Marathon
  cat <<EOF >> /etc/default/marathon
MARATHON_MASTER="zk://${ZK_CONNECT}/mesos"
MARATHON_ZK="zk://${ZK_CONNECT}/marathon"
MARATHON_TASK_LAUNCH_TIMEOUT="600000"
MARATHON_RECONCILIATION_INITIAL_DELAY="10000"
MARATHON_HOSTNAME="$(hostname -f)"
# enabling experimental feature
MARATHON_ENABLE_FEATURES="task_killing"
EOF
#  sed -i 's/.*exec.*/env JAVA_OPTS="-Xmx4G -XX:MaxPermSize=512M"\n&/' /etc/init/marathon.conf
#  sed -i "s/.*exec.*/env LIBPROCESS_IP=\"${ENI_IP}\"\n&/" /etc/init/marathon.conf

  # Configure chronos
  echo $(hostname -f) > /etc/chronos/conf/hostname

  # hard set hosts for mesos to prevent DNS failure from exploding the world
  echo "${HOSTS_ENTRIES}" >> /etc/hosts
}

function setup_datadog() {
  # setup datadog
  cat <<EOF > /etc/datadog-agent/conf.d/mesos_master.d/conf.yaml
init_config:
  default_timeout: 5

instances:
  - url: "http://${ENI_IP}:5050"
EOF

  cat <<EOF > /etc/datadog-agent/conf.d/marathon.d/conf.yaml
init_config:
  default_timeout: 5

instances:
  - url: "http://marathon.service.$(get_ivy_tag):8080"
EOF

  cat <<EOF > /etc/datadog-agent/conf.d/zk.d/conf.yaml
init_config:

instances:
  - host: localhost
    port: 2181
EOF

  cat <<EOF >> /etc/datadog-agent/conf.d/process.d/conf.yaml
init_config:

instances:
  - name: mesos-master
    search_string: ['/usr/sbin/mesos-master']
    exact_match: false
  - name: zookeeper
    search_string: ['org.apache.zookeeper.server.quorum.QuorumPeerMain']
    exact_match: false
  - name: marathon
    search_string: ['mesosphere.marathon.Main']
    exact_match: false
EOF

  service datadog-agent restart
}

function setup_consul() {
    cat <<EOF > /etc/consul.d/master.json
{
    "performance": {
      "raft_multiplier": 1
    },
    "dns_config": {
        "allow_stale": true
    }
}
EOF

    cat <<EOF > /etc/consul.d/master-services.json
{
    "services": [
        {
            "id": "mesos-$(get_instance_id)",
            "name": "mesos",
            "tags": [
              "$(get_environment)",
              "$(get_availability_zone)",
              "master"
            ],
            "port": 5050,
            "checks": [
              {
                  "id": "mesos",
                  "name": "Mesos Process",
                  "http": "http://${ENI_IP}:5050",
                  "interval": "10s",
                  "timeout": "3s"
              }
            ]
        },
        {
            "id": "marathon-$(get_instance_id)",
            "name": "marathon",
            "tags": [
              "$(get_environment)",
              "$(get_availability_zone)"
            ],
            "port": 8080,
            "checks": [
              {
                  "id": "marathon",
                  "name": "Marathon Process",
                  "http": "http://${ENI_IP}:8080",
                  "interval": "10s",
                  "timeout": "3s"
              }
            ]
        },
        {
            "id": "zookeeper-$(get_instance_id)",
            "name": "zookeeper",
            "tags": [
              "$(get_environment)",
              "$(get_availability_zone)"
            ],
            "port": 2181,
            "checks": [
              {
                  "id": "zookeeper",
                  "name": "Zookeeper Process",
                  "args": ["nc", "-z", "-w5", "${ENI_IP}", "2181"],
                  "interval": "10s",
                  "timeout": "2s"
              }
            ]
        },
        {
            "id": "chronos-$(get_instance_id)",
            "name": "chronos",
            "tags": [
              "$(get_environment)",
              "$(get_availability_zone)"
            ],
            "port": 4400,
            "checks": [
              {
                  "id": "chronos",
                  "name": "Chronos Process",
                  "http": "http://${ENI_IP}:4400",
                  "interval": "10s",
                  "timeout": "3s"
              }
            ]
        }
    ]
}
EOF
}

# Let 'er rip!
attach_eni $(get_instance_id) ${ENI_ID}
set_hostname mesos-master-${ZK_SERVER_ID}
set_prompt_color "__PROMPT_COLOR__"
setup_mesos_master
setup_datadog
setup_consul

# Start consul (as a master) first!
bash /opt/ivy/configure_consul.sh master

# Start all the mesos-y things
systemctl enable zookeeper mesos-master marathon chronos
systemctl start zookeeper mesos-master marathon chronos
