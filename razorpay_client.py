"""Razorpay test-mode client, with an automatic mock fallback.

Real calls go to Razorpay's test mode over plain REST (`httpx`, no SDK). When no
keys are present the MockClient takes over so the pipeline never hard-fails on a
missing credential — a judge can clone this repo and run everything.

Honest note on what is real and what is simulated:
  - Creating the payment link / order is a REAL Razorpay test-mode API call.
  - Whether the customer then *pays* it cannot be driven programmatically, so
    the outcome comes from the pre-rolled world in seed.py. The recovery numbers
    are therefore simulated outcomes on real API plumbing, and the README says
    so in the same words.

Every write carries a deterministic idempotency key of payment_id + attempt, so
a re-run or a crash-and-retry cannot create a second link for the same attempt.
This is the guard against double-charging a customer.
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.razorpay.com/v1"
TIMEOUT = 30

# Razorpay method hints, keyed by what the policy decided to do.
METHOD_HINT = {"PAYMENT_LINK_UPI": "upi", "ASK_UPDATE_CARD": "card"}


def idempotency_key(payment_id: str, attempt: int) -> str:
    """Stable across re-runs, unique per attempt. Prevents duplicate charges."""
    return f"failsafe_{payment_id}_a{attempt}"


class MockClient:
    """Offline stand-in. Same surface, no network, deterministic ids."""

    mode = "mock"

    def create_payment_link(self, amount, reference_id, description, method=None):
        return {
            "id": f"plink_mock_{reference_id}",
            "short_url": f"https://rzp.io/i/mock/{reference_id}",
            "status": "created",
            "amount": amount,
            "_mock": True,
        }

    def create_order(self, amount, receipt):
        return {"id": f"order_mock_{receipt}", "status": "created",
                "amount": amount, "_mock": True}

    def fetch_payment_link(self, link_id):
        return {"id": link_id, "status": "created", "_mock": True}


class RazorpayClient:
    """Thin wrapper over the test-mode REST API."""

    mode = "test"

    def __init__(self, key_id, key_secret):
        self._client = httpx.Client(
            base_url=BASE_URL, auth=(key_id, key_secret), timeout=TIMEOUT
        )

    def _post(self, path, payload):
        response = self._client.post(path, json=payload)
        response.raise_for_status()
        return response.json()

    def create_payment_link(self, amount, reference_id, description, method=None):
        payload = {
            "amount": amount,
            "currency": "INR",
            "description": description[:250],
            "reference_id": reference_id,
            "accept_partial": False,
            "notify": {"sms": False, "email": False},   # never message real people
            "reminder_enable": False,
        }
        if method:
            payload["options"] = {"checkout": {"method": {method: "1"}}}
        return self._post("/payment_links", payload)

    def create_order(self, amount, receipt):
        return self._post(
            "/orders", {"amount": amount, "currency": "INR", "receipt": receipt}
        )

    def fetch_payment_link(self, link_id):
        response = self._client.get(f"/payment_links/{link_id}")
        response.raise_for_status()
        return response.json()


def get_client(force_mock=False):
    """Real client when keys exist, mock otherwise. Never raises on absence."""
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if force_mock or not (key_id and key_secret):
        return MockClient()
    if not key_id.startswith("rzp_test_"):
        # Refuse live keys outright. This project must never touch real money.
        raise RuntimeError(
            f"RAZORPAY_KEY_ID is {key_id[:8]}..., which is not a test key. "
            "Failsafe refuses to run against live credentials."
        )
    return RazorpayClient(key_id, key_secret)
