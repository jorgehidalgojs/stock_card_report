# Copyright 2019 Ecosoft Co., Ltd. (http://ecosoft.co.th)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
import threading
from itertools import groupby
from types import SimpleNamespace

from odoo import api, fields, models

# Cache por hilo — aislado por request, sin contaminación entre usuarios
_thread_cache = threading.local()

_logger = logging.getLogger(__name__)

# Avisa en log si se superan este número de líneas en una sola consulta
_LINES_WARNING_THRESHOLD = 50_000


class StockCardView(models.TransientModel):
    """Mantenido para compatibilidad con seguridad y módulos externos."""

    _name = "stock.card.view"
    _description = "Stock Card View"
    _order = "date"

    date = fields.Datetime()
    product_id = fields.Many2one(comodel_name="product.product")
    product_qty = fields.Float()
    product_uom_qty = fields.Float()
    product_uom = fields.Many2one(comodel_name="uom.uom")
    reference = fields.Char()
    location_id = fields.Many2one(comodel_name="stock.location")
    location_dest_id = fields.Many2one(comodel_name="stock.location")
    is_initial = fields.Boolean()
    product_in = fields.Float()
    product_out = fields.Float()
    picking_id = fields.Many2one(comodel_name="stock.picking")
    partner_id = fields.Many2one(comodel_name="res.partner", string="Partner")
    picking_type_id = fields.Many2one(
        comodel_name="stock.picking.type", string="Operation Type"
    )
    picking_origin = fields.Char(string="Source Document")
    price_unit = fields.Float(string="Unit Price", digits="Product Price")
    price_total = fields.Float(string="Total Price", digits="Account")


class StockCardReport(models.TransientModel):
    _name = "report.stock.card.report"
    _description = "Stock Card Report"

    date_from = fields.Date()
    date_to = fields.Date()
    product_ids = fields.Many2many(comodel_name="product.product")
    location_id = fields.Many2one(comodel_name="stock.location")

    # ------------------------------------------------------------------ #
    # Capa de datos optimizada: sin registros ORM intermedios             #
    # ------------------------------------------------------------------ #

    def _get_stock_data(self):
        """
        Ejecuta la consulta SQL una sola vez y devuelve los datos pre-agrupados
        por producto como un dict puro:

            {product_id: {'initial': float, 'lines': [dict, ...]}}

        El resultado se cachea en el atributo de instancia _stock_data_cache
        para que llamadas repetidas en el mismo request (template + XLSX)
        no vuelvan a la base de datos.
        """
        cache_key = (self.env.cr.dbname, self.id)
        cache = getattr(_thread_cache, "stock_data", {}).get(cache_key)
        if cache is not None:
            return cache

        date_from = str(self.date_from or "0001-01-01")
        date_to = str(self.date_to or fields.Date.context_today(self))

        locations = self.env["stock.location"].search(
            [("id", "child_of", [self.location_id.id])]
        )
        loc_ids = tuple(locations.ids) if locations else (0,)
        product_ids = tuple(self.product_ids.ids) if self.product_ids else (0,)

        # Una sola query con todos los JOINs necesarios para evitar N+1 en Python.
        # ORDER BY product_id permite usar itertools.groupby sin índice extra.
        self._cr.execute(
            """
            SELECT
                move.date,
                move.product_id,
                move.product_qty,
                move.reference,
                move.location_id,
                move.location_dest_id,
                move.picking_id,
                COALESCE(pick.origin, '')            AS picking_origin,
                pt.name                              AS picking_type_name,
                rp.name                              AS partner_name,
                COALESCE(move.price_unit, 0.0)       AS price_unit,
                COALESCE(move.price_unit, 0.0) * move.product_qty AS price_total,
                CASE WHEN move.location_dest_id IN %s
                    THEN move.product_qty ELSE 0.0 END AS product_in,
                CASE WHEN move.location_id IN %s
                    THEN move.product_qty ELSE 0.0 END AS product_out,
                CASE WHEN move.date::date < %s::date
                    THEN True ELSE False END           AS is_initial
            FROM stock_move move
            LEFT JOIN stock_picking      pick ON pick.id = move.picking_id
            LEFT JOIN stock_picking_type pt
                ON pt.id = COALESCE(pick.picking_type_id, move.picking_type_id)
            LEFT JOIN res_partner        rp  ON rp.id  = pick.partner_id
            WHERE
                (move.location_id IN %s OR move.location_dest_id IN %s)
                AND move.state = 'done'
                AND move.product_id IN %s
                AND move.date::date <= %s::date
            ORDER BY move.product_id, move.date, move.reference
            """,
            (
                loc_ids, loc_ids, date_from,
                loc_ids, loc_ids, product_ids, date_to,
            ),
        )
        rows = self._cr.dictfetchall()

        if len(rows) > _LINES_WARNING_THRESHOLD:
            _logger.warning(
                "Stock Card Report: %d líneas cargadas. "
                "Considere reducir el rango de fechas o la selección de productos.",
                len(rows),
            )

        # Agrupación O(N) — un solo recorrido de la lista completa
        grouped = {}
        for pid, product_rows in groupby(rows, key=lambda r: r["product_id"]):
            product_rows = list(product_rows)
            initial_rows = [r for r in product_rows if r["is_initial"]]
            move_rows = [r for r in product_rows if not r["is_initial"]]

            # Pre-computa display_name para evitar name_get ORM por línea
            for r in move_rows:
                name = r["reference"] or ""
                if r.get("picking_origin"):
                    name = "{} ({})".format(name, r["picking_origin"])
                r["display_name"] = name

            grouped[pid] = {
                "initial": sum(
                    r["product_in"] - r["product_out"] for r in initial_rows
                ),
                # SimpleNamespace permite acceso por atributo (product_line.date)
                # en expresiones QWeb t-value y t-if, donde los dicts fallan.
                "lines": [SimpleNamespace(**r) for r in move_rows],
            }

        if not hasattr(_thread_cache, "stock_data"):
            _thread_cache.stock_data = {}
        _thread_cache.stock_data[cache_key] = grouped
        return grouped

    def _get_product_initial(self, product):
        return self._get_stock_data().get(product.id, {}).get("initial", 0.0)

    def _get_product_lines(self, product):
        return self._get_stock_data().get(product.id, {}).get("lines", [])

    def _get_product_totals(self, product):
        """Retorna {'total_in': float, 'total_out': float, 'final_balance': float}"""
        lines = self._get_product_lines(product)
        initial = self._get_product_initial(product)
        total_in = sum(l.product_in for l in lines)
        total_out = sum(l.product_out for l in lines)
        return {
            "total_in": total_in,
            "total_out": total_out,
            "final_balance": initial + total_in - total_out,
        }

    # ------------------------------------------------------------------ #
    # Métodos de impresión                                                #
    # ------------------------------------------------------------------ #

    def print_report(self, report_type="qweb"):
        self.ensure_one()
        action = (
            report_type == "xlsx"
            and self.env.ref("stock_card_report.action_stock_card_report_xlsx")
            or self.env.ref("stock_card_report.action_stock_card_report_pdf")
        )
        return action.report_action(self, config=False)

    def _get_html(self):
        result = {}
        rcontext = {}
        report = self.browse(self._context.get("active_id"))
        if report:
            rcontext["o"] = report
            result["html"] = self.env.ref(
                "stock_card_report.report_stock_card_report_html"
            )._render(rcontext)
        return result

    @api.model
    def get_html(self, given_context=None):
        return self.with_context(**(given_context or {}))._get_html()
