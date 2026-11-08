import datetime
import ipaddress
import os
import shutil
from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

# Store certificates outside git tracking (certs/ is gitignored)
CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
os.makedirs(CERT_DIR, exist_ok=True)

# 1. Generate strong 4096-bit RSA Private Key
private_key = rsa.generate_private_key(
    public_exponent=65537,
    key_size=4096,
)

# 2. Subject and Issuer (Self-signed Root CA & Server Cert in one)
subject = issuer = x509.Name([
    x509.NameAttribute(NameOID.COMMON_NAME, "Enforcer-MCP-Server"),
    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Agile Comic Tracker"),
])

# 3. Subject Alternative Names (SANs) - Allows connecting by IP or hostname
alt_names = [
    x509.DNSName("enforcer"),
    x509.DNSName("enforcer.fritz.box"),
    x509.DNSName("localhost"),
    x509.IPAddress(ipaddress.IPv4Address("192.168.178.36")),
    x509.IPAddress(ipaddress.IPv4Address("192.168.178.57")),
    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
]

# 4. Build 100-Year Certificate (36,500 days)
cert = (
    x509.CertificateBuilder()
    .subject_name(subject)
    .issuer_name(issuer)
    .public_key(private_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
    .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=36500))  # 100 Years
    .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
    .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    .add_extension(
        x509.KeyUsage(
            digital_signature=True,
            content_commitment=False,
            key_encipherment=True,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=True,
            crl_sign=True,
            encipher_only=False,
            decipher_only=False,
        ),
        critical=True,
    )
    .add_extension(
        x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
        critical=False,
    )
    .sign(private_key, hashes.SHA256())
)

key_path = os.path.join(CERT_DIR, "enforcer.key")
crt_path = os.path.join(CERT_DIR, "enforcer.crt")

# 5. Save Private Key (certs/enforcer.key)
with open(key_path, "wb") as f:
    f.write(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    ))

# 6. Save Public Certificate (certs/enforcer.crt)
with open(crt_path, "wb") as f:
    f.write(cert.public_bytes(serialization.Encoding.PEM))

# Copy public certificate to D:\Share if available
if os.path.exists(r"D:\Share"):
    shutil.copy2(crt_path, r"D:\Share\enforcer.crt")

print(f"100-Year Permanent TLS Certificate Generated:\n  Cert: {crt_path}\n  Key:  {key_path}")

