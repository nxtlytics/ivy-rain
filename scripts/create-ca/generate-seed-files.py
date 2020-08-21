#!/usr/bin/env python3
import argparse
import boto3
import datetime
import logging

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes, serialization
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKeyWithSerialization, RSAPrivateKey
from cryptography.x509.oid import NameOID
from pathlib import Path
from typing import Optional, List

log = logging.getLogger(__name__)

def create_certificate_authority(
        cn: str,
        ca_dir: Path,
        today: datetime,
        expiration: datetime.timedelta
) -> (Path, Path):
    ca_key = ca_dir / 'ca-key.pem'
    ca_crt = ca_dir / 'ca.pem'
    one_day = datetime.timedelta(days=1)
    yesterday = today - one_day
    valid_until = today + expiration
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    public_key = private_key.public_key()
    builder = x509.CertificateBuilder()
    builder = builder.subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ]))
    builder = builder.issuer_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ]))
    builder = builder.not_valid_before(yesterday)
    builder = builder.not_valid_after(valid_until)
    builder = builder.serial_number(x509.random_serial_number())
    builder = builder.public_key(public_key)
    builder = builder.add_extension(
        x509.BasicConstraints(ca=True, path_length=None),
        critical=True
    )
    ca_key, ca_crt = create_key_cert(
        ca_key, ca_crt, builder, private_key, private_key
    )
    return ca_key, ca_crt


def create_service_key_cert(
        cn: str,
        service_dir: Path,
        today: datetime,
        expiration: datetime.timedelta,
        sign_private_key_path: Path
) -> (Path, Path):
    service_key = service_dir / cn / '-key.pem'
    service_crt = service_dir / cn / '.pem'
    sign_private_key = serialization.load_pem_private_key(
        sign_private_key_path,
        password=None,
        backend=default_backend()
    )
    one_day = datetime.timedelta(days=1)
    yesterday = today - one_day
    valid_until = today + expiration
    service_private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    service_public_key = service_private_key.public_key()
    builder = x509.CertificateBuilder()
    builder = builder.subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, cn)
    ]))
    builder = builder.not_valid_before(yesterday)
    builder = builder.not_valid_after(valid_until)
    builder = builder.public_key(service_public_key)
    service_key, service_crt = create_key_cert(
        service_key, service_crt, builder, service_private_key, sign_private_key
    )
    return service_key, service_crt


def create_key_cert(
        key_file: Path,
        crt_file: Path,
        builder: x509.CertificateBuilder,
        private_key: rsa.RSAPrivateKey,
        sign_private_key: rsa.RSAPrivateKey
) -> (Path, Path):
    certificate = builder.sign(
        private_key=sign_private_key, algorithm=hashes.SHA256(),
        backend=default_backend()
    )
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption())
    public_bytes = certificate.public_bytes(
        encoding=serialization.Encoding.PEM)
    with open(key_file, "wb") as fout:
        fout.write(private_bytes + public_bytes)
    with open(crt_file, "wb") as fout:
        fout.write(public_bytes)
    log.info("Wrote files %s and %s", key_file, crt_file)
    return key_file, crt_file


def regions_with_ssm(
        ssm_client: boto3.session.Session.client
) -> [str]:
    response = ssm_client.get_parameters_by_path(
        Path='/aws/service/global-infrastructure/services/ssm/regions'
    )['Parameters']
    ssm_regions = [x['Value'] for x in response]
    ec2_client = boto3.client('ec2')
    aws_partition = ec2_client.meta.partition
    log.debug("Just got all regions where ssm is available in AWS Partition: %s", aws_partition)
    log.debug("Regions are: %s", ssm_regions)
    return ssm_regions


def is_parameter_in_a_region(
        parameter_name: str
) -> bool:
    ssm_client = boto3.client('ssm')
    regions = regions_with_ssm(ssm_client)
    exists: List[bool] = []
    for region in regions:
        _ssm_client = boto3.client('ssm', region_name=region)
        try:
            _ = _ssm_client.get_parameter(
                Name=parameter_name,
                WithDecryption=True
            )
            log.info("Parameter %s is present in ssm at region %s", parameter_name, region)
            exists.append(True)
        except Exception as e:
            log.debug("Parameter %s is not present in ssm at region %s, error was: %s", parameter_name, region, e)
            exists.append(False)
    if any(exists):
        return True
    else:
        return False


def create_update_ssm_parameter(
        ssm_client: boto3.session.Session.client,
        parameter_name: str,
        parameter_value: str,
        parameter_type: Optional[str] = 'SecureString',
        key_id: Optional[str] = None
) -> (str, str):
    params = {
        'Name': parameter_name,
        'Value': parameter_value,
        'Type':  parameter_type,
        'Tier': 'Intelligent-Tiering'
    }
    if key_id != None:
        params['KeyId'] = key_id
    response = ssm_client.put_parameter(**params)
    version = response['Version']
    tier = response['Tier']
    return version, tier


def main(
        expiration: int,
        sysenv: str,
        ivy_tag: str,
        base_directory: Optional[Path] = None
) -> None:
    today = datetime.datetime.today()
    years_to_days = expiration * 365
    years_delta = datetime.timedelta(days=years_to_days)
    ca_cn = sysenv
    if base_directory == None:
        base_directory = Path.cwd()
    ca_dir = base_directory / ivy_tag / sysenv / 'CA'
    ca_key = ca_dir / 'ca-key.pem'
    ca_key_name_in_ssm = '/' + ivy_tag + '/' + sysenv + '/' + 'CA' + '/' + 'ca-key.pem'
    ca_crt_name_in_ssm = '/' + ivy_tag + '/' + sysenv + '/' + 'CA' + '/' + 'ca.pem'
    ca_key_exists = is_parameter_in_a_region(ca_key_name_in_ssm)
    log.info("Does %s exist in ssm? %s", ca_key_name_in_ssm, ca_key_exists)
    if ca_key_exists:
        log.info("%s already exists in ssm", ca_key_name_in_ssm)
    else:
        log.info("%s does not exist in ssm", ca_key_name_in_ssm)
        if ca_dir.exists():
            log.info("%s already exists so I will not create it", ca_dir)
        else:
            ca_dir.mkdir(
                mode=0o744,
                parents=True
            )
            if ca_key.exists():
                log.info("%s already exists so I will not create it", ca_key)
            else:
                ca_key, ca_crt = create_certificate_authority(ca_cn, ca_dir, today, years_delta)
                _ssm_client = boto3.client('ssm')
                with open(ca_key) as fout:
                    _version, _tier = create_update_ssm_parameter(
                        _ssm_client, ca_key_name_in_ssm, fout.read(), 'SecureString'
                    )
                    log.info(
                        f"Certificate Authority private key for sysenv: {sysenv} has been pushed to AWS' ssm with name: {ca_key_name_in_ssm}, version: {_version} and tier: {_tier}"
                    )
                with open(ca_crt) as fout:
                    _version, _tier = create_update_ssm_parameter(
                        _ssm_client, ca_crt_name_in_ssm, fout.read(), 'String'
                    )
                    log.info(
                        f"Certificate Authority certificate for sysenv: {sysenv} has been pushed to AWS' ssm with name: {ca_crt_name_in_ssm}, version: {_version} and tier: {_tier}"
                    )


if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s %(levelname)s (%(threadName)s) [%(name)s] %(message)s")
    _LOG_LEVEL_STRINGS = {
        'CRITICAL': logging.CRITICAL,
        'ERROR': logging.ERROR,
        'WARNING': logging.WARNING,
        'INFO': logging.INFO,
        'DEBUG': logging.DEBUG
    }
    parser = argparse.ArgumentParser(
        description="Setup Ivy seed files (Right now only Certificate Authority, privat key and public key)")
    parser.add_argument(
        "-s",
        "--sysenv",
        type=str,
        required=True,
        help="SysEnv Short Name"
    )
    parser.add_argument(
        "-t",
        "--ivy-tag",
        type=str,
        default="ivy",
        help="Ivy tag also known as namespace"
    )
    parser.add_argument(
        "-b",
        "--base-directory",
        type=str,
        help="Base directory where to store seed files"
    )
    parser.add_argument(
        "-e",
        "--expiration",
        type=int,
        default=10,
        help="Validity of Certificate Authority (CA) keys in years, if files already exist this will be ignored"
    )
    parser.add_argument(
        "-l",
        "--log-level",
        type=str,
        default='INFO',
        choices=_LOG_LEVEL_STRINGS.keys(),
        help="Set the logging output level"
    )
    args = parser.parse_args()
    log.setLevel(_LOG_LEVEL_STRINGS[args.log_level])
    main(args.expiration, args.sysenv, args.ivy_tag, args.base_directory)
