# -*- coding: utf-8 -*-
# See __openerp__.py file for full copyright and licensing details.

import time
from datetime import datetime, timedelta
from openerp import models
from dateutil.relativedelta import relativedelta
from openerp.tools import DEFAULT_SERVER_DATETIME_FORMAT, \
    DEFAULT_SERVER_DATE_FORMAT, float_compare, float_round
from psycopg2 import OperationalError
import openerp
import logging
_logger = logging.getLogger(__name__)


class stock_warehouse_orderpoint(models.Model):
    _inherit = 'stock.warehouse.orderpoint'

    # override stock/stock
    def subtract_procurements_from_orderpoints(self, cr, uid, orderpoint_ids, context=None):
        '''This function returns quantity of product that needs to be deducted from the orderpoint computed quantity because there's already a procurement created with aim to fulfill it.
        We are also considering procurement-only demand for the product and subtracting it from the returned quantity. This method could return a negative quantity, assuming that there is more demand than supply.
        '''

        # only consider procurements within current plan step (context['to_date'])
        cr.execute("""select op.id, p.id, p.product_uom, p.product_qty, pt.uom_id, sm.product_qty, p.origin from procurement_order as p left join stock_move as sm ON sm.procurement_id = p.id,
                                    stock_warehouse_orderpoint op, product_product pp, product_template pt
                                WHERE p.orderpoint_id = op.id AND p.state not in ('done', 'cancel') AND (sm.state IS NULL OR sm.state not in ('draft'))
                                AND pp.id = p.product_id AND pp.product_tmpl_id = pt.id
                                AND op.id IN %s
                                AND p.date_planned <= %s
                                ORDER BY op.id, p.id
                   """, (tuple(orderpoint_ids), context.get('to_date', datetime.max),))
        results = cr.fetchall()
        current_proc = False
        current_op = False
        uom_obj = self.pool.get("product.uom")
        op_qty = 0
        res = dict.fromkeys(orderpoint_ids, 0.0)
        for move_result in results:
            op = move_result[0]
            if current_op != op:
                if current_op:
                    res[current_op] = op_qty
                current_op = op
                op_qty = 0
            proc = move_result[1]
            if proc != current_proc:
                if 'OUT/' in move_result[6]:
                    # subtract outbound procurements for production
                    op_qty -= uom_obj._compute_qty(cr, uid, move_result[2], move_result[3], move_result[4], round=False)
                else:
                    op_qty += uom_obj._compute_qty(cr, uid, move_result[2], move_result[3], move_result[4], round=False)
                current_proc = proc
            if move_result[5]: #If a move is associated (is move qty)
                op_qty -= move_result[5]
        if current_op:
            res[current_op] = op_qty
        return res


class mrp_bom(models.Model):
    _inherit = 'mrp.bom'

    # override mrp/mrp
    def _bom_find(self, cr, uid, product_tmpl_id=None, product_id=None, properties=None, context=None):
        """ Finds BoM for particular product and product uom.
        @param product_tmpl_id: Selected product.
        @param product_uom: Unit of measure of a product.
        @param properties: List of related properties.
        @return: False or BoM id.
        """
        if not context:
            context = {}
        if properties is None:
            properties = []
        if product_id:
            if not product_tmpl_id:
                product_tmpl_id = self.pool['product.product'].browse(cr, uid, product_id, context=context).product_tmpl_id.id
            domain = [
                '|',
                    ('product_id', '=', product_id),
                    '&',
                        ('product_id', '=', False),
                        ('product_tmpl_id', '=', product_tmpl_id)
            ]
        elif product_tmpl_id:
            domain = [('product_id', '=', False), ('product_tmpl_id', '=', product_tmpl_id)]
        else:
            # neither product nor template, makes no sense to search
            return False
        if context.get('company_id'):
            domain = domain + [('company_id', '=', context['company_id'])]
        domain = domain + [ '|', ('date_start', '=', False), ('date_start', '<=', (context and context.get('bom_effectivity_date') or time.strftime(DEFAULT_SERVER_DATE_FORMAT))),
                            '|', ('date_stop', '=', False), ('date_stop', '>=', (context and context.get('bom_effectivity_date') or time.strftime(DEFAULT_SERVER_DATE_FORMAT)))]
        # order to prioritize bom with product_id over the one without
        ids = self.search(cr, uid, domain, order='sequence, product_id', context=context)
        # Search a BoM which has all properties specified, or if you can not find one, you could
        # pass a BoM without any properties with the smallest sequence
        bom_empty_prop = False
        for bom in self.pool.get('mrp.bom').browse(cr, uid, ids, context=context):
            if not set(map(int, bom.property_ids or [])) - set(properties or []):
                if not properties or bom.property_ids:
                    return bom.id
                elif not bom_empty_prop:
                    bom_empty_prop = bom.id
        return bom_empty_prop

    # override mrp/mrp
    def _skip_bom_line(self, cr, uid, line, product, context=None):
        """ Control if a BoM line should be produce, can be inherited for add
        custom control.
        @param line: BoM line.
        @param product: Selected product produced.
        @return: True or False
        """
        # date_start and date_stop really should be a fields.Datetime and not a fields.Date, see odoo #3961
        if line.date_start and line.date_start >= (context and context.get('bom_effectivity_date') or time.strftime(DEFAULT_SERVER_DATE_FORMAT)) or \
            line.date_stop and line.date_stop < (context and context.get('bom_effectivity_date') or time.strftime(DEFAULT_SERVER_DATE_FORMAT)):
                return True
        # all bom_line_id variant values must be in the product
        if line.attribute_value_ids:
            if not product or (set(map(int,line.attribute_value_ids or [])) - set(map(int,product.attribute_value_ids))):
                return True
        return False


class procurement_order(models.Model):
    _inherit = "procurement.order"

    def _get_bucket_size(self, cr, uid, context=None):
        # weekly: 7
        # daily: 1
        return 1 # 1 or preset

    def _get_bucket_delay(self, cr, uid, context=None):
        # subtract relativedelta of days=time_bucket
        # this would need to be handled by a user configurable setting in mfg
        #   if time_bucket=weekly, then we could provide a day of the week for
        #   the user to choose when product should be available and in addition
        #   to the time_bucket, subtract the relative # of days (MON thru SUN)
        #     e.g. MON = -6, - relativedelta(days=6)
        time_bucket = self._get_bucket_size(cr, uid, context=context)
        bucket_day = time_bucket == 7 and 1 or 0 # 1 or preset
        bucket_delay = time_bucket - bucket_day
        return bucket_delay

    def _get_procurement_date_start(self, cr, uid, orderpoint, to_date, context=None):
        days = 0.0
        # make addition of lead_days an optional setting
        days += orderpoint.lead_days or 0.0
        product = orderpoint.product_id
        for route in product.route_ids:
            if route.pull_ids:
                for rule in route.pull_ids:
                    if rule.action == 'buy':
                        days += product._select_seller(product).delay or 0.0
                        days += product.product_tmpl_id.company_id.po_lead
                    if rule.action == 'manufacture':
                        days += product.produce_delay or 0.0
                        days += product.product_tmpl_id.company_id.manufacturing_lead
        date_start = datetime.combine(datetime.strptime(to_date, DEFAULT_SERVER_DATE_FORMAT) - relativedelta(days=days), datetime.min.time())
        return date_start.strftime(DEFAULT_SERVER_DATETIME_FORMAT)

    # stock/procurement
    def _prepare_orderpoint_procurement(self, cr, uid, orderpoint, product_qty, context=None):
        res = super(procurement_order, self)._prepare_orderpoint_procurement(cr, uid, orderpoint, product_qty, context=context)
        res['date_planned'] = context['bucket_date']
        return res

    # Method to override in mrp_procurement_only module
    def _process_procurement(self, cr, uid, ids, context=None):
        self.run(cr, uid, ids)

    # Method to override in mrp_procurement_only module
    def _plan_orderpoint_procurement(self, cr, uid, op, qty_rounded, context=None):
        procurement_obj = self.pool.get('procurement.order')
        proc_id = procurement_obj.create(cr, uid,
                                        self._prepare_orderpoint_procurement(cr, uid, op, qty_rounded, context=context),
                                        context=context)
        return proc_id

    # override stock/procurement
    def _procure_orderpoint_confirm(self, cr, uid, use_new_cursor=False, company_id = False, context=None):
        '''
        Create procurement based on Orderpoint

        :param bool use_new_cursor: if set, use a dedicated cursor and auto-commit after processing each procurement.
            This is appropriate for batch jobs only.
        '''
        if context is None:
            context = {}
        self._update_llc(cr, uid, use_new_cursor=use_new_cursor, context=context)
        if use_new_cursor:
            cr = openerp.registry(cr.dbname).cursor()
        orderpoint_obj = self.pool.get('stock.warehouse.orderpoint')
        procurement_obj = self.pool.get('procurement.order')
        product_obj = self.pool.get('product.product')

        #TODO:
        # set up time_bucket as mfg cfg setting
        # also need option to choose day of week for procurement
        #   scheduled date when using weekly bucket
        # consider adjusting bucket datetime objects relative to user timezone
        time_bucket = self._get_bucket_size(cr, uid, context=context)
        # get bucket datetime objects
        utc_dt = datetime.combine(datetime.utcnow().date(), datetime.min.time())
        first_bucket_dt = time_bucket == 7 and utc_dt - timedelta(days=utc_dt.weekday()) or utc_dt
        last_bucket_dt = utc_dt
        last_procurement = procurement_obj.search(cr, uid, [('state', '=', 'running')], order="date_planned DESC", limit=1)
        if last_procurement:
            last_procurement_id = last_procurement[0]
            last_bucket_dt = datetime.strptime(procurement_obj.browse(cr, uid, last_procurement_id)['date_planned'], DEFAULT_SERVER_DATETIME_FORMAT)
            last_bucket_dt = time_bucket == 7 and last_bucket_dt + timedelta(days=time_bucket - last_bucket_dt.isoweekday()) or last_bucket_dt
            last_bucket_dt = (last_bucket_dt < first_bucket_dt) and first_bucket_dt or last_bucket_dt
        # get delta from first to last bucket datetime objects
        planning_horizon = (last_bucket_dt - first_bucket_dt).days + time_bucket

        dom = company_id and [('company_id', '=', company_id)] or []
        orderpoint_ids = orderpoint_obj.search(cr, uid, dom, order="location_id, llc")
        prev_ids = []
        tot_procs = []
        while orderpoint_ids:
            ids = orderpoint_ids[:1000]
            del orderpoint_ids[:1000]
            product_dict = {}
            ops_dict = {}
            ops = orderpoint_obj.browse(cr, uid, ids, context=context)

            #Calculate groups that can be executed together
            for op in ops:
                key = (op.location_id.id, op.llc)
                if not product_dict.get(key):
                    product_dict[key] = [op.product_id]
                    ops_dict[key] = [op]
                else:
                    product_dict[key] += [op.product_id]
                    ops_dict[key] += [op]

            for key in sorted(product_dict.keys()):
                plan_days = 0
                while plan_days <= planning_horizon:
                    to_date = first_bucket_dt.date() + relativedelta(days=plan_days)
                    _logger.info("to_date: %s", to_date)

                    ctx = context and context.copy() or {}
                    ctx.update({'location': ops_dict[key][0].location_id.id})
                    ctx.update({'bucket_date': (to_date - relativedelta(days=self._get_bucket_delay(cr, uid, context=context))).strftime(DEFAULT_SERVER_DATE_FORMAT)})
                    ctx.update({'to_date': to_date.strftime(DEFAULT_SERVER_DATE_FORMAT)})
                    ctx.update({'procurement_autorun_defer': True})
                    prod_qty = product_obj._product_available(cr, uid, [x.id for x in product_dict[key]],
                                                            context=ctx)
                    subtract_qty = orderpoint_obj.subtract_procurements_from_orderpoints(cr, uid, [x.id for x in ops_dict[key]], context=ctx)
                    for op in ops_dict[key]:
                        try:
                            prods = prod_qty[op.product_id.id]['virtual_available']
                            _logger.info("op: %s", op)
                            _logger.info("prods: %s", prods)
                            if prods is None:
                                continue
                            if float_compare(prods, op.product_min_qty, precision_rounding=op.product_uom.rounding) <= 0:
                                qty = max(op.product_min_qty, op.product_max_qty) - prods

                                # maintain qty_multiple by subtracting procurements first
                                qty -= subtract_qty[op.id]
                                _logger.info("subtract_qty: %s", subtract_qty[op.id])
                                _logger.info("qty: %s", qty)

                                reste = op.qty_multiple > 0 and qty % op.qty_multiple or 0.0
                                if float_compare(reste, 0.0, precision_rounding=op.product_uom.rounding) > 0:
                                    qty += op.qty_multiple - reste

                                if float_compare(qty, 0.0, precision_rounding=op.product_uom.rounding) < 0:
                                    continue

                                qty_rounded = float_round(qty, precision_rounding=op.product_uom.rounding)
                                if qty_rounded > 0:
                                    ctx.update({'bom_effectivity_date': self._get_procurement_date_start(cr, uid, op, ctx['bucket_date'], context=ctx)})
                                    proc_id = self._plan_orderpoint_procurement(cr, uid, op, qty_rounded, context=ctx)
                                    tot_procs.extend(proc_id) if isinstance(proc_id, list) else tot_procs.append(proc_id)
                                if use_new_cursor:
                                    cr.commit()
                        except OperationalError:
                            if use_new_cursor:
                                orderpoint_ids.append(op.id)
                                cr.rollback()
                                continue
                            else:
                                raise
                    plan_days = plan_days + time_bucket
            try:
                tot_procs.reverse()
                self._process_procurement(cr, uid, tot_procs, context=context)
                tot_procs = []
                if use_new_cursor:
                    cr.commit()
            except OperationalError:
                if use_new_cursor:
                    cr.rollback()
                    continue
                else:
                    raise

            if use_new_cursor:
                cr.commit()
            if prev_ids == ids:
                break
            else:
                prev_ids = ids

        if use_new_cursor:
            cr.commit()
            cr.close()
        return {}
