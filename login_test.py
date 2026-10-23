import os
import pyotp
from dotenv import load_dotenv
from neo_api_client import NeoAPI
from common.utils import get_resource_path

load_dotenv(get_resource_path(".env"))

CONSUMER_KEY = os.getenv("CONSUMER_KEY")
MOBILE = os.getenv("MOBILE")
UCC = os.getenv("UCC")
MPIN = os.getenv("MPIN")
TOTP_SECRET = os.getenv("TOTP_SECRET")

client = NeoAPI(
    consumer_key=CONSUMER_KEY,
    environment="prod"
)

totp = pyotp.TOTP(TOTP_SECRET).now()

login_resp = client.totp_login(
    mobile_number=MOBILE,
    ucc=UCC,
    totp=totp
)
print("totp_login response:")
print(login_resp)

validate_resp = client.totp_validate(mpin=MPIN)
print("\ntotp_validate response:")
print(validate_resp)

print("\nclient config after login / validate:")
print(f"base_url: {client.configuration.base_url}")
print(f"edit_token: {client.configuration.edit_token}")
print(f"edit_sid: {client.configuration.edit_sid}")
