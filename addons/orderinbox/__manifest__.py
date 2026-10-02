{
    "name": "OrderInbox AI — PO Drafts",
    "version": "18.0.1.0.0",
    "category": "Sales",
    "summary": "Review emailed customer purchase orders: matches, validation, one-click draft sales orders.",
    "author": "OrderInbox AI",
    "website": "https://github.com/orderinbox/orderinbox",
    "license": "LGPL-3",
    "depends": ["sale", "mail"],
    "external_dependencies": {},
    "data": [
        "security/orderinbox_security.xml",
        "security/ir.model.access.csv",
        "views/orderinbox_views.xml",
        "views/menuitems.xml",
        "views/res_config_settings_views.xml",
        "data/orderinbox_data.xml",
    ],
    "assets": {},
    "application": True,
    "installable": True,
    "support": "support@orderinbox.ai",
    "description": """
OrderInbox AI — review queue for emailed customer purchase orders
=================================================================

This module is the in-Odoo face of the OrderInbox AI appliance.

The appliance (a Docker container that runs on your network) watches your
orders inbox and reads customer purchase orders in any format — PDF,
Excel, CSV, or plain email. It matches the customer and every SKU against
your live Odoo catalog, validates quantities against pack sizes, minimums,
prices and duplicates, and pushes the results here.

What you get in Odoo
--------------------

* A kanban review queue: READY FOR APPROVAL, EXCEPTIONS, SENT, REJECTED
* Every order shows its matched customer, matched SKUs with confidence
  scores, catalog vs. quoted prices, and each validation issue with a
  suggested fix
* One click **Approve** creates a *draft* sales order in Odoo (state:
  draft) — nothing is confirmed until your team posts it, the normal way
* One click **Reject** with a reason
* A cron that keeps this queue in sync with the appliance automatically

How it connects
---------------

Set the appliance URL and API token in Settings → OrderInbox. The module
then polls the appliance for new orders and, on approval, asks the
appliance to create the draft order in your Odoo (or, if no appliance is
configured, builds the draft directly from the lines in Odoo).

Private by design: documents and inference stay on your own infrastructure.
"""
}
