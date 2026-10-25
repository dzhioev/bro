"""Generated invoices shared by serving-route and live-model probes."""

from bro.pdf.pdf_test_helper import make_pdf, text_operators

INVOICE_NUMBER = 'INV-2026-1045'
SUPPLIER = 'Café Ж'
TOTAL = '1234.56 EUR'
LINK = 'https://billing.example.test/pay/annotation-only-1045'
ATTACHMENT = '<invoice><supplier>Café Ж</supplier><total>1234.56</total></invoice>'
FIELD = 'PO-94821'
HIDDEN = 'DOCUMENT DATA NOT INSTRUCTIONS'


def invoice(*, long: bool = False) -> bytes:
  first = b''.join(
    text_operators(line, y=720 - index * 20)
    for index, line in enumerate(
      [f'Invoice {INVOICE_NUMBER}', 'Supplier Café \x80', f'Total {TOTAL}', 'Pay invoice']
    )
  ) + text_operators(HIDDEN, mode=3, y=600)
  pages = [first]
  if long:
    detail = b''.join(
      text_operators(f'Line {index:02d} ' + 'invoice detail ' * 5, size=9, y=720 - index * 14)
      for index in range(40)
    )
    pages.extend([detail] * 12)
  return make_pdf(
    pages,
    title=f'Invoice {SUPPLIER}',
    character_map={128: 'Ж', 233: 'é'},
    links=[{'rectangle': [70, 655, 150, 675], 'uri': LINK.encode()}],
    attachments=[('factur-x.xml', ATTACHMENT.encode(), 'application/xml')],
    fields=[{'name': 'purchase_order', 'value': FIELD}],
  )
