# -*- coding: utf-8 -*-

from odoo import api, fields, models, _, exceptions
from datetime import datetime, date, time, timedelta
import itertools
import datetime
from odoo.tools import DEFAULT_SERVER_DATE_FORMAT, DEFAULT_SERVER_DATETIME_FORMAT

import base64

try:
    import xlrd

    try:
        from xlrd import xlsx
    except ImportError:
        xlsx = None
except ImportError:
    xlrd = xlsx = None

PARSE_UOM = {
    'h.': ['Hours(s)', 'Hora(s)'],
    'l.': ['Liter(s)', 'Litro(s)'],
    'kg': ['kg', 'kg'],
    'm2': ['m2', 'm2'],
    'm.': ['m', 'm'],
    'pa': ['pa', 'pa'],
    'ud': ['Unit(s)', 'Unidad(es)'],
    'UD': ['Unit(s)', 'Unidad(es)'],
    'u': ['Unit(s)', 'Unidad(es)'],
    'ml': ['ml', 'ml'],
    'ML': ['ml', 'ml'],
}


class SalePrestoImporter(models.TransientModel):
    _name = "sale.presto.importer"
    _description = "Importa presupuesto de venta de PRESTO"

    file = fields.Binary('File', required=True, help="File to check and/or import, raw binary (not base64)")
    partner_id = fields.Many2one('res.partner', string='Partner', required=True, domain=[('customer', '=', True)])
    sale_id = fields.Many2one('sale.order', string='Sale order')

    @api.multi
    def _read_xls(self, options):
        """ Read file content, using xlrd lib """
        for importer in self:
            book = xlrd.open_workbook(file_contents=base64.decodestring(importer.file))
            return importer._read_xls_book(book)

    def _read_xls_book(self, book):
        sheet = book.sheet_by_index(0)
        # emulate Sheet.get_rows for pre-0.9.4
        row_list = []
        for row in itertools.imap(sheet.row, range(sheet.nrows)):
            values = []
            for cell in row:
                if cell.ctype is xlrd.XL_CELL_NUMBER:
                    is_float = cell.value % 1 != 0.0
                    values.append(
                        unicode(cell.value)
                        if is_float
                        else unicode(int(cell.value))
                    )
                elif cell.ctype is xlrd.XL_CELL_DATE:
                    is_datetime = cell.value % 1 != 0.0
                    # emulate xldate_as_datetime for pre-0.9.3
                    dt = datetime.datetime(*xlrd.xldate.xldate_as_tuple(cell.value, book.datemode))
                    values.append(
                        dt.strftime(DEFAULT_SERVER_DATETIME_FORMAT)
                        if is_datetime
                        else dt.strftime(DEFAULT_SERVER_DATE_FORMAT)
                    )
                elif cell.ctype is xlrd.XL_CELL_BOOLEAN:
                    values.append(u'True' if cell.value else u'False')
                elif cell.ctype is xlrd.XL_CELL_ERROR:
                    raise ValueError(
                        _("Error cell found while reading XLS/XLSX file: %s") %
                        xlrd.error_text_from_code.get(
                            cell.value, "unknown error code %s" % cell.value)
                    )
                else:
                    values.append(cell.value)
            if any(x for x in values if x.strip()):
                # yield values
                row_list.append(values)
        return row_list

    @api.multi
    def do(self):
        for importer in self:
            data = importer._read_xls(None)
            importer.create_sale(data)
            ctx = dict(
                self.env.context,
            )
            return {
                'name': _('Pedido de venta'),
                'type': 'ir.actions.act_window',
                'view_mode': 'form',
                'view_type': 'form',
                'res_model': 'sale.order',
                'target': 'current',
                'res_id': self.sale_id.id,
                'context': ctx,
            }
        return True

    @api.one
    def create_sale(self, book):
        sale_obj = self.env['sale.order']
        sale_line_obj = self.env['sale.order.line']
        product_obj = self.env['product.product']
        uom_obj = self.env['product.uom']

        sale_line_list = [a for a in book if a[1] == 'Partida']

        if self.sale_id:
            self.sale_id.write({
                'partner_id': self.partner_id.id,
                'payment_term_id': self.partner_id.property_payment_term_id.id,
                'payment_mode_id': self.partner_id.customer_payment_mode_id.id,
            })
        else:
            self.sale_id = sale_obj.create({
                'partner_id': self.partner_id.id,
                'payment_term_id': self.partner_id.property_payment_term_id.id,
                'payment_mode_id': self.partner_id.customer_payment_mode_id.id,
            })
        for line_data in sale_line_list:
            product_id = product_obj.search([('default_code', '=', line_data[0].strip())])
            if not product_id:
                if not line_data[2]:
                    line_data[2] = 'ud'
                if PARSE_UOM.get(line_data[2]):
                    uom_id = uom_obj.search(['|', ('name', '=', PARSE_UOM.get(line_data[2])[0]),
                                             ('name', '=', PARSE_UOM.get(line_data[2])[1])])
                    if not uom_id:
                        uom_id = uom_obj.create({'name': PARSE_UOM.get(line_data[2])[1],
                                                 'uom_type': 'reference',
                                                 'category_id': self.env['product.uom.categ'].search([])[0].id,
                                                 'rounding': 0.00100})
                else:
                    uom_id = uom_obj.search([('name', '=', line_data[2])])
                if not uom_id:
                    uom_id = uom_obj.create({'name': line_data[2],
                                             'uom_type': 'reference',
                                             'category_id': self.env['product.uom.categ'].search([])[0].id,
                                             'rounding': 0.00100})
                product_data = {
                    'name': line_data[3],
                    'default_code': line_data[0].strip(),
                    'list_price': float(line_data[11]),
                    'uom_id': uom_id.id,
                    'uom_po_id': uom_id.id
                }
                product_id = product_obj.create(product_data)

            taxes_id = []
            for tax_id in product_id.taxes_id:
                if tax_id.company_id == self.sale_id.company_id:
                    taxes_id.append(tax_id.id)
            # taxes_id = product_id.taxes_id.mapped('id')
            sale_line_obj.create({
                'order_id': self.sale_id.id,
                'product_id': product_id.id,
                'name': product_id.display_name,
                'product_uom': product_id.uom_id.id,
                'product_uom_qty': float(line_data[10]),
                'price_unit': float(line_data[11]),
                'price_subtotal': float(line_data[12]),
                'tax_id': taxes_id and [(6, 0, taxes_id)] or False,
            })

        return self.sale_id
