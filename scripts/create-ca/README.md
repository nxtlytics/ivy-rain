# Creates CA and stores it in SSM

## Requirements for bash version

- cfssl/cfssljson
- awscli
- AWS permissions:
  - SSM read/write

## Overview

Here you will find scripts to setup the necessary secrets/certificates for standing up an Ivy environment

## How to run it

```shell
$ AWS_PROFILE=sandbox ./generate-seed-files.sh -s sandbox -t ivy -e 10
Parameter /ivy/sandbox/CA/ca-key.pem does not exist in any region where ssm is available
I will create directories ./ivy/sandbox/CA, CA key and certificate and push them to ssm
2020/08/25 12:18:39 [INFO] generating a new CA key and certificate from CSR
2020/08/25 12:18:39 [INFO] generate received request
2020/08/25 12:18:39 [INFO] received CSR
2020/08/25 12:18:39 [INFO] generating key: rsa-2048
2020/08/25 12:18:40 [INFO] encoded CSR
2020/08/25 12:18:40 [INFO] signed certificate with serial number 8039204297172209663615822211768421487651832388
/Users/ricardo/src/infrastructure-ivy-rain/scripts/create-ca
CA_KEY_FILE is at ./ivy/sandbox/CA/ca-key.pem and CA_CERTIFICATE_FILE is at ./ivy/sandbox/CA/ca.pem
{
    "Version": 1,
    "Tier": "Standard"
}
{
    "Version": 1,
    "Tier": "Standard"
}
```

## Related links

- [Digital Ocean's Vault and Kubernetes](https://www.digitalocean.com/blog/vault-and-kubernetes)
- [Vault's Build Your Own Certificate Authority (CA)](https://learn.hashicorp.com/vault/secrets-management/sm-pki-engine)
- [Be your own certificate authority](https://opensource.com/article/19/4/certificate-authority)
