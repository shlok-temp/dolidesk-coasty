"""An autonomous accounts-payable exception desk built on Coasty.

The agent works a real ERP's purchase-to-pay queue from screenshots alone.
Every value it reports is checked against an independent oracle that reads the
same records over the REST API, and every change it makes is confirmed to have
actually landed.
"""

__version__ = "0.1.0"
