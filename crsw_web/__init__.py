"""CRSW web deposit service: a browser route into the same storage
conventions the CLI applies (see crsw_deposit). FastAPI + boto3,
Python 3.12, runs on a centrally managed VM.

Nothing convention-shaped lives here. Keys, records, labels and noise
rules all come from crsw_deposit."""
