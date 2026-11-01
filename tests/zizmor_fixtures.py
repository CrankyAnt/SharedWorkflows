"""Reusable workflow fixtures for zizmor tests."""

WORKFLOW_HEAD = "name: fixture\non: push\npermissions: {}\njobs:\n  a:\n    runs-on: ubuntu-latest\n    permissions:\n      contents: read\n    steps:\n"

CALLER_HEAD = "name: fixture\non: pull_request\npermissions: {}\njobs:\n"

CHECKOUT_TAG = "      - uses: actions/checkout@v7\n        with:\n          persist-credentials: false\n"

CHECKOUT_SHA = "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n        with:\n          persist-credentials: false\n"

CHECKOUT_BARE_SHA = "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n        with:\n          persist-credentials: false\n"


def caller_job(job: str, reference: str) -> str:
    """One job under CALLER_HEAD that calls the reusable workflow `reference`."""
    return f"  {job}:\n    permissions:\n      contents: read\n    uses: {reference}\n"
