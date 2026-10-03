from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    orderinbox_appliance_url = fields.Char(
        string="OrderInbox appliance URL",
        config_parameter="orderinbox.appliance_url",
        help="Base URL of the OrderInbox AI appliance, e.g. http://appliance.lan:8501",
    )
    orderinbox_api_token = fields.Char(
        string="OrderInbox API token",
        config_parameter="orderinbox.api_token",
        help="Shared secret. Must match ORDERINBOX_API_TOKEN on the appliance.",
    )
    orderinbox_match_threshold = fields.Float(
        string="Auto-ready match threshold (0-100)",
        default=80,
        config_parameter="orderinbox.match_threshold",
    )
    orderinbox_price_tolerance = fields.Float(
        string="Price deviation tolerance (%)",
        default=15,
        config_parameter="orderinbox.price_tolerance",
    )
    orderinbox_currencies = fields.Char(
        string="Accepted currencies",
        default="USD,CAD,EUR",
        config_parameter="orderinbox.currencies",
    )
