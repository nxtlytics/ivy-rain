#!/usr/bin/env python3
import argparse
import boto3
import configparser
import datetime
import json

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes, serialization
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKeyWithSerialization
from cryptography.x509.oid import NameOID
from pathlib import Path

def create_certificate_authority(
        cn: str,
        ca_dir: Path,
        today: datetime.date,
        expiration: int
) -> (Path, Path):
    ca_key = ca_dir + 'ca-key.pem'
    ca_crt = ca_dir + 'ca.pem'
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
        critical=True)
    ca_key, ca_crt = create_key_cert(ca_key, ca_crt, builder, private_key, private_key)
    return ca_key, ca_crt

def create_service_key_cert(
        cn: str,
        service_dir: Path,
        today: datetime.date,
        expiration: int,
        sign_private_key: rsa.RSAPrivateKeyWithSerialization
) -> (Path, Path):
    service_key = service_dir + cn + '-key.pem'
    service_crt = service_dir + cn + '.pem'
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
    service_key, service_crt = create_key_cert(service_key, service_crt, builder, service_private_key, sign_private_key)
    return service_key, service_crt

def create_key_cert(
        key_file: Path,
        crt_file: Path,
        builder: x509.CertificateBuilder,
        private_key: rsa.RSAPrivateKeyWithSerialization,
        sign_private_key: rsa.RSAPrivateKeyWithSerialization
) -> (Path, Path):
    certificate = builder.sign(
        private_key=sign_private_key, algorithm=hashes.SHA256(),
        backend=default_backend()
    )
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncrption())
    public_bytes = certificate.public_bytes(
        encoding=serialization.Encoding.PEM)
    with open(key_file, "wb") as fout:
        fout.write(private_bytes + public_bytes)
    with open(crt_file, "wb") as fout:
        fout.write(public_bytes)
    return key_file, crt_file

def regions_with_ssm(
        ssm_client: boto3.session.Session.client
) -> [str]:
    response = ssm_client.get_parameters_by_path(
        Path='/aws/service/global-infrastructure/services/ssm/regions'
    )['Parameters']
    ssm_regions = [x['Value'] for x in response if x['Value'] is str]
    return ssm_regions

def create_update_ssm_parameter(
        ssm_client: boto3.session.Session.client,
        parameter_name: str,
        parameter_value: str,
        parameter_type: Optional[str] = 'SecureString',
        key_id: Optional[str] = None
) -> (str, str):
    response = ssm_client.put_parameter(
        Name=parameter_name,
        Value=parameter_value,
        Type=parameter_type,
        KeyId=key_id,
        Tier='Intelligent-Tiering'
    )
    version = response['Version']
    tier = response['Tier']
    return version, tier

def is_parameter_in_a_region(
        ssm_client: boto3.session.Session.client
) -> None:

def main(
        expiration: int,
        sysenv: str,
        ivy_tag: str,
        aws_profile: str,
        aws_region: str,
        base_directory: Optional[Path] = None
) -> None:
    today = datetime.date.today()
    expiration = datetime.timedelta(years=expiration)
    ca_cn = sysenv
    if base_directory == None:
        base_directory = Path.cwd()
    ca_dir = base_directory / 'CA'
    ca_key, ca_crt = create_certificate_authority(ca_cn, ca_dir, today, expiration)
    print("entrypoint of this")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Setup Ivy seed files (Right now only Certificate Authority, privat key and public key)")
    parser.add_argument(
        "-s",
        "--sysenv",
        type=str,
        help="SysEnv Short Name",
    )
    parser.add_argument(
        "-t",
        "--ivy-tag",
        type=str,
        default="ivy",
        help="Ivy tag also known as namespace",
    )
    parser.add_argument(
        "-b",
        "--base-directory",
        type=str,
        help="Base directory where to store seed files",
    )
    parser.add_argument(
        "-p",
        "--profile",
        type=str,
        help="AWS Profile to use",
    )
    parser.add_argument(
        "-r",
        "--region",
        type=str,
        help="AWS Region to use",
    )
    parser.add_argument(
        "-e",
        "--expiration",
        type=int,
        default=10,
        help="Validity of Certificate Authority (CA) keys, if files already exist this will be ignored",
    )
    args = parser.parse_args()