from templates import vpc, vpn, security_groups, rds, elasticache, cassandra, kafka, pritunl, nexus, mesos_masters, mesos_agents, vault

TEMPLATES = {
    'VPC': vpc.VPCTemplate,
    'VPN': vpn.VPNTemplate,
    'SecurityGroups': security_groups.SecurityGroupTemplate,
    'RDS': rds.RDSTemplate,
    'ElastiCache': elasticache.ElastiCacheTemplate,
    'Cassandra': cassandra.CassandraTemplate,
    'Kafka': kafka.KafkaTemplate,
    'Pritunl': pritunl.PritunlTemplate,
    'Nexus': nexus.NexusTemplate,
    'MesosMasters': mesos_masters.MesosMastersTemplate,
    'MesosAgents': mesos_agents.MesosAgentsTemplate,
    'Vault': vault.VaultTemplate
}
