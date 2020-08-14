# Creates CA and stores it in SSM

*Note:* Still undecided on doing bash or python for this

## Requirements for bash version

- cfssl/cfssljson
- awscli
- AWS permissions:
  - SSM read/write

## Requirements for python version

- cryptography==3.0
- boto3

## Overview

Here you will find scripts to setup the necessary secrets/certificates for standing up an Ivy environment

## Related links

- [Digital Ocean's Vault and Kubernetes](https://www.digitalocean.com/blog/vault-and-kubernetes)
- [Vault's Build Your Own Certificate Authority (CA)](https://learn.hashicorp.com/vault/secrets-management/sm-pki-engine)
- 
