"""Penomoran bukti otomatis per jenis dokumen."""
import re
from .models import JournalEntry

CASH_CODES = {"1110", "1120", "1130"}

def voucher_prefix(lines, *, kind="GENERAL", movement_kind=None, settlement_code=None):
    """Tentukan prefix dari substansi akun/transaksi, bukan urutan input pengguna."""
    if kind == JournalEntry.Kind.ADJUSTMENT:
        return "JP"
    if movement_kind == "IN":
        return "FP" if settlement_code == "2110" else "BKK"
    if movement_kind == "OUT":
        return "FJ" if settlement_code == "1140" else "BKM"
    debit_codes = {x["account"].code for x in lines if x.get("debit", 0) > 0}
    credit_codes = {x["account"].code for x in lines if x.get("credit", 0) > 0}
    if debit_codes & CASH_CODES:
        return "BKM"
    if credit_codes & CASH_CODES:
        return "BKK"
    if "1140" in debit_codes and (credit_codes & {"4110", "4120"}):
        return "FJ"
    if "2110" in credit_codes and (debit_codes & {"1150", "1160", "1210", "1220", "1230", "1240", "1250"}):
        return "FP"
    return "JU"


def next_voucher_reference(prefix):
    """Nomor urut berkelanjutan per prefix: BKM 01, BKM 02, dan seterusnya."""
    pattern = re.compile(rf"^{re.escape(prefix)} (\d+)$")
    used = JournalEntry.objects.filter(reference__startswith=f"{prefix} ").values_list("reference", flat=True)
    highest = 0
    for value in used:
        match = pattern.match(value)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{prefix.upper()} {highest + 1:02d}"
