from odoo import models, fields, api


class MrpInventory(models.Model):
    _inherit = 'mrp.inventory'

    routing_detail = fields.Char('Routing Detail', compute='_compute_routing_detail', store=True)

    @api.depends('product_id')
    def _compute_routing_detail(self):
        for record in self:
            if record.product_id:
                operations = self.env['mrp.bom'].search([('product_id', '=', record.product_id.id)]).\
                    mapped('routing_id.operation_ids')
                if operations:
                    work_center_codes = [code for code in operations.mapped('workcenter_id.code') if code]
                    record.routing_detail = ", ".join(work_center_codes)
