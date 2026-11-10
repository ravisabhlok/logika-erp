"""Bank bulk-upload file (ICICI 'PAB_VENDOR' layout) for a payment batch.

Same layout the Access tool produced (Sheet1, 19 columns, one payment per row, header in row 1):
PYMT_PROD_TYPE_CODE, PYMT_MODE, DEBIT_ACC_NO, BNF_NAME, BENE_ACC_NO, BENE_IFSC, AMOUNT, DEBIT_NARR, CREDIT_NARR,
MOBILE_NUM, EMAIL_ID, REMARK, PYMT_DATE (dd-mm-yyyy text), REF_NO, ADDL_INFO1..5.

Payment mode is decided from the beneficiary's IFSC and the amount, never typed:
  ICICI beneficiary (IFSC starts with ICIC)  -> FT   (fund transfer inside the bank)
  any other bank, below 2,00,000             -> NEFT
  any other bank, 2,00,000 and above         -> RTGS (RTGS's own minimum is 2 lakh)
"""
import io
import re
import unicodedata
from decimal import Decimal

import openpyxl

HEADER = ['PYMT_PROD_TYPE_CODE', 'PYMT_MODE', 'DEBIT_ACC_NO', 'BNF_NAME', 'BENE_ACC_NO', 'BENE_IFSC', 'AMOUNT',
          'DEBIT_NARR', 'CREDIT_NARR', 'MOBILE_NUM', 'EMAIL_ID', 'REMARK', 'PYMT_DATE', 'REF_NO',
          'ADDL_INFO1', 'ADDL_INFO2', 'ADDL_INFO3', 'ADDL_INFO4', 'ADDL_INFO5']
RTGS_MIN = Decimal('200000')


def mode_for(ifsc, amount):
    if str(ifsc or '').strip().upper().startswith('ICIC'):
        return 'FT'
    return 'RTGS' if Decimal(str(amount or 0)) >= RTGS_MIN else 'NEFT'


def bank_text(value, max_len=None):
    """Text for the bank file's BNF_NAME / REMARK columns: the bank rejects anything but letters, digits and spaces
    (no '.', '-', '/', '&' ...). Accents are folded to plain letters, '&' becomes AND, '-' and '/' (e.g. in
    BILL/25-26/0001 or PO-00012) become spaces, every other special character is dropped, spaces are tidied.
    The stored payment is untouched - only the file is cleaned."""
    text = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode('ascii')
    text = text.replace('&', ' AND ')
    text = re.sub(r'[-/_]', ' ', text)
    text = re.sub(r'[^A-Za-z0-9 ]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_len].strip() if max_len else text


def _amount(v):
    d = Decimal(str(v))
    return int(d) if d == d.to_integral_value() else float(d)


def build_bank_file(debit_acc, lines):
    """lines: objects with beneficiary_name, account_no, ifsc, amount, remark and an effective pay date
    (`pay_date` already resolved to the batch date by the caller). Returns .xlsx bytes.
    Name and remark are cleaned to letters, digits and spaces by bank_text()."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Sheet1'
    ws.append(HEADER)
    for ln in lines:
        ws.append([
            'PAB_VENDOR', mode_for(ln.ifsc, ln.amount), str(debit_acc), bank_text(ln.beneficiary_name),
            str(ln.account_no).strip(), ln.ifsc.strip().upper(), _amount(ln.amount), None, None, None, None,
            bank_text(ln.remark, 100), ln.pay_date.strftime('%d-%m-%Y'), None, None, None, None, None, None])
    # Account numbers must stay text so Excel never drops a leading zero (07641000051005) or shows 1.2E+13.
    for row in ws.iter_rows(min_row=2, min_col=3, max_col=5):
        for cell in (row[0], row[2]):
            cell.number_format = '@'
    for col, w in zip('ABCDEFGHIJKLMNOPQRS', (22, 11, 16, 28, 20, 14, 12, 12, 12, 13, 12, 18, 12, 10, 12, 12, 12, 12, 12)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
