"""Aturan jurnal umum otomatis.

Admin hanya mengisi keterangan + satu nominal. Modul ini menentukan akun debet/kredit
dari kata kunci pada keterangan, menghitung diskon/termin, dan menghasilkan baris
jurnal yang selalu seimbang. Dipakai bersama oleh halaman simpan dan pratinjau live,
sehingga yang terlihat di layar persis sama dengan yang diposting.
"""
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from .models import Account, JournalEntry, Party, ZERO
from .services import money

# Akun retur dan potongan (kontra). Dibuat oleh `seed_agriculture`.
SALES_DISCOUNT = '4130'
# Dalam metode perpetual, retur pembelian barang dagang mengurangi persediaan.

TERM_SUGGESTIONS = ['n/30', 'n/60', '2/10,n/30', '1/10,n/30', '2/10,n/60']
_TERM_RE = re.compile(r'^(?:(?P<pct>\d+(?:[.,]\d+)?)/(?P<days>\d+)\s*,\s*)?n/(?P<net>\d+)$', re.I)

BANK_WORDS = ('bank', 'transfer', 'rekening', 'qris', 'giro', 'mobile banking')
CASH_WORDS = ('tunai', 'cash', 'kas', 'kontan', 'uang tunai')
CREDIT_WORDS = (' kredit', 'utang', 'piutang', 'tempo')


@dataclass
class Term:
    text: str
    discount_pct: Decimal | None
    discount_days: int | None
    net_days: int


@dataclass
class JournalPlan:
    type: str
    lines: list = field(default_factory=list)  # {'account','party','debit','credit'}
    discount: Decimal = ZERO
    term: Term | None = None
    due_date: date | None = None

    @property
    def total_debit(self):
        return sum((x['debit'] for x in self.lines), ZERO)

    @property
    def total_credit(self):
        return sum((x['credit'] for x in self.lines), ZERO)


def parse_term(text):
    """'2/10,n/30' -> diskon 2% jika lunas ≤10 hari, jatuh tempo 30 hari. 'n/30' tanpa diskon."""
    text = (text or '').strip()
    if not text:
        return None
    m = _TERM_RE.match(text.replace(' ', ''))
    if not m:
        raise ValidationError('Format termin tidak dikenali. Contoh: n/30 atau 2/10,n/30.')
    pct = Decimal(m['pct'].replace(',', '.')) if m['pct'] else None
    if pct is not None and not (ZERO < pct < 100):
        raise ValidationError('Persen diskon termin harus di antara 0 dan 100.')
    days = int(m['days']) if m['days'] else None
    net = int(m['net'])
    if net <= 0 or (days is not None and days >= net):
        raise ValidationError('Masa diskon harus lebih pendek daripada jatuh tempo (contoh 2/10,n/30).')
    clean = f'{m["pct"]}/{days},n/{net}' if pct is not None else f'n/{net}'
    return Term(clean, pct, days, net)


def next_reference(entry_date):
    """Nomor bukti berikutnya yang pasti belum dipakai (aman setelah jurnal dihapus)."""
    prefix = f'JU-{entry_date:%Y%m%d}-'
    used = set(JournalEntry.objects.filter(date=entry_date, reference__startswith=prefix)
               .values_list('reference', flat=True))
    n = 1
    while f'{prefix}{n:03d}' in used:
        n += 1
    return f'{prefix}{n:03d}'


def _account(code):
    account = Account.objects.filter(code=code, is_active=True, is_header=False).first()
    if not account:
        raise ValidationError(
            f'Akun {code} belum tersedia atau tidak aktif. Tambahkan di Daftar Akun '
            f'atau jalankan: python manage.py seed_agriculture')
    return account


def _party_for(kind, description):
    parties = list(Party.objects.filter(kind=kind))
    label = 'pelanggan' if kind == Party.Kind.CUSTOMER else 'pemasok'
    normalized = description.casefold()
    matched = [p for p in parties if p.name.casefold() in normalized or p.code.casefold() in normalized]
    if len(matched) == 1:
        return matched[0]
    if len(parties) == 1:
        return parties[0]
    if parties:
        raise ValidationError(f'Keterangan harus memuat nama/kode {label} agar relasi akun dapat ditentukan otomatis.')
    raise ValidationError(f'Daftar {label} belum tersedia. Tambahkan dahulu di Kode Pembantu (/parties/).')


def _has(text, words):
    return any(w in text for w in words)


def resolve_general_journal(description):
    """Klasifikasi transaksi berbasis frasa dan COA; master produk dipakai untuk mutasi FIFO bila kuantitas juga diisi.

    Keterangan bebas tetap memerlukan konteks yang cukup (jenis transaksi dan cara
    penyelesaian). Sistem sengaja menolak uraian ambigu daripada membuat jurnal salah.
    """
    raw = (description or '').casefold()
    text = ' ' + ' '.join(re.sub(r'[^a-z0-9]+', ' ', raw).split()) + ' '
    def has(*terms):
        return any(t in text for t in terms)

    def settlement(purchase=False):
        if has(' kredit ', ' tempo ', ' utang ', ' hutang '):
            return '2110' if purchase else '1140'
        if has(' transfer ', ' bank ', ' rekening ', ' debit ', ' debet ', ' qris ', ' giro ', ' mobile banking '):
            return '1110'
        if has(' tunai ', ' cash ', ' kas ', ' kontan ', ' uang tunai '):
            return '1120'
        raise ValidationError(
            'Cara pembayaran belum jelas. Cantumkan tunai, transfer/bank, atau kredit/tempo.')

    def cash_account():
        if has(' transfer ', ' bank ', ' rekening ', ' qris ', ' giro ', ' mobile banking '):
            return '1110'
        if has(' tunai ', ' cash ', ' kas ', ' kontan ', ' uang tunai '):
            return '1120'
        raise ValidationError('Cantumkan penerimaan/pembayaran melalui tunai atau transfer/bank.')

    buying = has(' beli ', ' pembelian ', ' membeli ', ' pengadaan ')
    selling = has(' jual ', ' penjualan ', ' menjual ', ' pendapatan penjualan ')
    receipt = has(' penerimaan piutang ', ' terima piutang ', ' pelunasan piutang ',
                  ' pembayaran dari pelanggan ', ' pelunasan pelanggan ')
    payment = has(' pembayaran utang ', ' bayar utang ', ' pelunasan utang ',
                  ' membayar pemasok ', ' pelunasan pemasok ')
    sale_return = has(' retur penjualan ', ' retur jual ', ' pengembalian penjualan ')
    purchase_return = has(' retur pembelian ', ' retur beli ', ' pengembalian pembelian ')

    # Pembayaran kewajiban yang sudah diakru harus mengurangi akun utangnya,
    # bukan membebankan biaya untuk kedua kalinya.
    if has(' pembayaran utang pajak ', ' bayar utang pajak ', ' pelunasan utang pajak '):
        return 'other', '2130', cash_account()
    if has(' pembayaran utang gaji ', ' bayar utang gaji ', ' pelunasan utang gaji '):
        return 'other', '2140', cash_account()
    if has(' pembayaran pinjaman bank ', ' angsuran pinjaman bank ', ' pelunasan pinjaman bank '):
        return 'other', '2120', cash_account()

    # Retur barang memerlukan referensi bukti asal, kuantitas, kondisi barang,
    # dan penyesuaian lapisan FIFO. Jangan membuat jurnal parsial otomatis.
    if sale_return or purchase_return:
        raise ValidationError(
            'Retur penjualan/pembelian persediaan memerlukan produk, kuantitas, '
            'bukti transaksi asal, dan pembaruan lapisan FIFO. Gunakan prosedur retur '
            'terverifikasi; jangan catat sebagai jurnal umum biasa.'
        )
    if receipt:
        return 'receipt', cash_account(), '1140'
    if payment:
        return 'payment', '2110', cash_account()

    # Bibit/benih untuk dipakai sendiri adalah biaya budidaya; jika untuk dijual,
    # kata kunci persediaan di bawah akan mengarah ke akun persediaan.
    if buying and has(' bibit untuk tanam ', ' benih untuk tanam ', ' bibit ditanam ', ' benih ditanam '):
        return 'purchase', '6210', settlement(True)

    # Persediaan dagang/hasil tani versus bahan habis pakai.
    inventory_words = has(' persediaan ', ' stok ', ' barang dagang ', ' hasil tani ',
        ' hasil panen ', ' bibit ', ' benih ', ' pupuk untuk dijual ', ' komoditas ',
        ' gabah ', ' beras ', ' jagung ', ' sayuran ', ' buah ')
    if buying and inventory_words:
        return 'purchase', '1150', settlement(True)
    if selling and inventory_words:
        return 'sale', settlement(False), '4110'

    # Pembelian aset lancar nonpersediaan yang manfaatnya lintas periode.
    if buying and has(' sewa dibayar di muka ', ' bayar sewa di muka '):
        return 'purchase', '1170', settlement(True)
    if buying and has(' asuransi dibayar di muka ', ' premi asuransi di muka '):
        return 'purchase', '1180', settlement(True)
    if buying and has(' iklan dibayar di muka ', ' promosi dibayar di muka '):
        return 'purchase', '1190', settlement(True)
    if buying and has(' uang muka pembelian ', ' dp pembelian ', ' down payment pembelian '):
        return 'purchase', '1197', settlement(True)

    # Pembelian aset tetap (bukan beban langsung).
    if buying and has(' tanah ', ' lahan ', ' sawah ', ' gedung ', ' bangunan ',
                      ' gudang ', ' traktor ', ' mesin ', ' kendaraan ', ' kendaraan operasional ',
                      ' komputer ', ' peralatan ', ' inventaris '):
        if has(' tanah ', ' lahan ', ' sawah '):
            debit = '1210'
        elif has(' gedung ', ' bangunan ', ' gudang '):
            debit = '1220'
        elif has(' kendaraan ', ' traktor '):
            debit = '1240'
        elif has(' mesin '):
            debit = '1230'
        else:
            debit = '1250'
        return 'purchase', debit, settlement(True)

    # Aset biologis dan aset takberwujud tidak dibebankan langsung.
    if buying and has(' tanaman produktif ', ' aset biologis ', ' ternak produktif '):
        return 'purchase', '1270', settlement(True)
    if buying and has(' software ', ' perangkat lunak ', ' lisensi ', ' paten ', ' hak cipta '):
        return 'purchase', '1290', settlement(True)

    # Pembelian perlengkapan/bahan produksi dan beban operasional.
    if buying and has(' perlengkapan ', ' alat tulis ', ' atk ', ' kemasan ', ' karung '):
        return 'purchase', '1160', settlement(True)
    if buying and has(' pupuk ', ' pestisida ', ' herbisida ', ' insektisida '):
        return 'purchase', '6210', settlement(True)
    if buying and has(' pakan ', ' bahan baku ', ' bahan produksi '):
        return 'purchase', '6240', settlement(True)

    # Pengeluaran operasional dipetakan ke akun beban yang paling spesifik.
    expense_map = [
        ((' listrik ', ' air ', ' pln ', ' pdam ', ' telepon ', ' internet ', ' pulsa ', ' komunikasi '), '6120'),
        ((' sewa ',), '6130'),
        ((' perlengkapan ', ' alat tulis kantor ', ' atk ', ' karung ', ' kemasan '), '6140'),
        ((' pemeliharaan ', ' perawatan ', ' perbaikan ', ' servis '), '6150'),
        ((' piutang tak tertagih ', ' kerugian piutang '), '6160'),
        ((' penyusutan ', ' depresiasi '), '6170'),
        ((' asuransi ',), '6180'),
        ((' upah ', ' gaji ', ' tenaga kerja ', ' honor ', ' honorarium '), '6190'),
        ((' kebersihan ', ' cleaning '), '6200'),
        ((' pupuk ', ' pestisida ', ' herbisida ', ' insektisida '), '6210'),
        ((' transport ', ' transportasi ', ' ongkos angkut ', ' biaya angkut ', ' bensin ', ' solar '), '6220'),
        ((' pajak ', ' retribusi '), '6230'),
        ((' bahan baku ', ' bahan produksi ', ' pakan '), '6240'),
        ((' administrasi ', ' materai '), '6250'),
        ((' iklan ', ' promosi ', ' pemasaran '), '6110'),
        ((' bunga ', ' biaya pinjaman '), '8120'),
        ((' panen ', ' pemetikan ', ' pemungutan hasil '), '6310'),
        ((' pengolahan hasil ', ' penggilingan ', ' sortasi ', ' pengeringan '), '6320'),
        ((' pengiriman ', ' ekspedisi ', ' kurir '), '6330'),
        ((' komisi ', ' fee penjualan '), '6340'),
        ((' izin usaha ', ' perizinan ', ' sertifikasi '), '6350'),
        ((' konsumsi ', ' makan minum ', ' jamuan '), '6360'),
        ((' amortisasi ',), '6380'),
        ((' denda ', ' sanksi administrasi '), '8180'),
        ((' tanggung jawab sosial ', ' csr ', ' sumbangan sosial '), '6400'),
    ]
    expense_context = has(' bayar ', ' pembayaran ', ' biaya ', ' beban ', ' tagihan ',
                          ' pembelian ', ' pengeluaran ', ' pelunasan ')
    if expense_context:
        for terms, code in expense_map:
            if any(term in text for term in terms):
                return 'other', code, settlement(True)

    # Modal, pinjaman, prive, dan pendapatan selain penjualan barang.
    if has(' setoran modal ', ' modal disetor ', ' tambahan modal ', ' investasi pemilik '):
        return 'other', cash_account(), '3100'
    if has(' prive ', ' penarikan pemilik ', ' pengambilan pribadi '):
        return 'other', '3120', cash_account()
    if has(' penerimaan pinjaman ', ' pinjaman bank ', ' pencairan kredit bank '):
        return 'other', cash_account(), '2120'
    if has(' angsuran pokok pinjaman ', ' pembayaran pokok pinjaman '):
        return 'other', '2120', cash_account()
    if has(' pendapatan jasa ', ' penerimaan jasa ', ' jasa pengolahan ', ' penjualan jasa ', ' jasa angkut '):
        return 'other', cash_account(), '4120'
    if has(' pendapatan sewa ', ' penerimaan sewa '):
        return 'other', cash_account(), '4160'
    if has(' pendapatan bunga ', ' penerimaan bunga '):
        return 'other', cash_account(), '8110'
    if has(' pendapatan lain ', ' penerimaan lain ', ' keuntungan penjualan aset ', ' pendapatan hibah '):
        return 'other', cash_account(), '7110'

    # Pembelian/penjualan yang belum bisa dibedakan tidak dipaksakan.
    raise ValidationError(
        'Transaksi belum dapat diklasifikasikan dengan aman. Lengkapi keterangan dengan '
        'jenis transaksi, objek (akun/produk), dan cara pembayaran. Untuk transaksi khusus, '
        'gunakan jurnal manual/penyesuaian agar akun tidak salah.')


def build_general_journal(description, amount, entry_date, *, term_text='', discount_percent=None,
                          discount_rp=None, use_term_discount=False):
    """Susun baris jurnal. `amount` boleh nol (untuk pratinjau sebelum nominal diisi)."""
    amount = money(amount or 0)
    kind, debit_code, credit_code = resolve_general_journal(description)
    debit_account, credit_account = _account(debit_code), _account(credit_code)
    debit_party = credit_party = None
    if debit_code == '1140':
        debit_party = _party_for(Party.Kind.CUSTOMER, description)
    if credit_code == '1140':
        credit_party = _party_for(Party.Kind.CUSTOMER, description)
    if debit_code == '2110':
        debit_party = _party_for(Party.Kind.SUPPLIER, description)
    if credit_code == '2110':
        credit_party = _party_for(Party.Kind.SUPPLIER, description)

    term = parse_term(term_text)
    pct = Decimal(discount_percent) if discount_percent else None
    rp = money(discount_rp) if discount_rp else None
    wants_discount = bool((pct and pct > 0) or (rp and rp > 0) or use_term_discount)
    credit_settlement = kind in ('sale', 'purchase') and (debit_code == '1140' or credit_code == '2110')

    if pct and rp:
        raise ValidationError('Isi diskon dalam persen ATAU rupiah, tidak keduanya.')
    if pct is not None and not (ZERO <= pct < 100):
        raise ValidationError('Diskon persen harus di antara 0 dan kurang dari 100.')
    if wants_discount and kind not in ('sale', 'purchase', 'receipt', 'payment'):
        raise ValidationError('Diskon hanya berlaku untuk penjualan, pembelian, penerimaan piutang, dan pembayaran utang.')
    if wants_discount and kind == 'payment':
        raise ValidationError(
            'Diskon pelunasan utang persediaan perlu mengubah biaya lapisan FIFO. '
            'Fitur jurnal umum belum memproses penyesuaian lapisan tersebut; jangan diposting otomatis.'
        )
    if term:
        if kind in ('sale', 'purchase') and not credit_settlement:
            raise ValidationError('Termin hanya berlaku untuk transaksi kredit. Tambahkan kata "kredit" pada keterangan.')
        if kind not in ('sale', 'purchase', 'receipt', 'payment'):
            raise ValidationError('Termin hanya berlaku untuk penjualan/pembelian kredit, penerimaan piutang, atau pembayaran utang.')
    if use_term_discount:
        if not term or term.discount_pct is None:
            raise ValidationError('Pilih termin yang memuat diskon (contoh 2/10,n/30) untuk memakai potongan termin.')
        if kind in ('sale', 'purchase'):
            raise ValidationError('Potongan termin diambil saat pelunasan: gunakan "penerimaan piutang" atau "pembayaran utang".')
        if pct or rp:
            raise ValidationError('Potongan termin tidak dapat digabung dengan diskon manual.')
        pct = term.discount_pct

    discount = ZERO
    if wants_discount:
        discount = rp if rp else money(amount * pct / 100)
        if amount > 0 and not (ZERO < discount < amount):
            raise ValidationError('Diskon harus lebih besar dari nol dan lebih kecil daripada nominal transaksi.')

    def line(account, party=None, debit=ZERO, credit=ZERO):
        return {'account': account, 'party': party, 'debit': debit, 'credit': credit}

    if wants_discount and kind in ('sale', 'receipt'):
        # Kas/piutang berkurang sebesar diskon; potongan penjualan dicatat tersendiri (metode bruto).
        lines = [line(debit_account, debit_party, debit=amount - discount),
                 line(_account(SALES_DISCOUNT), debit=discount),
                 line(credit_account, credit_party, credit=amount)]
    elif wants_discount and kind == 'purchase':
        # Metode perpetual: diskon yang diketahui pada saat pembelian langsung
        # mengurangi biaya perolehan aset/persediaan. Tidak memakai akun
        # kontra-pembelian periodik (5120).
        net_amount = amount - discount
        lines = [line(debit_account, debit_party, debit=net_amount),
                 line(credit_account, credit_party, credit=net_amount)]
    else:
        lines = [line(debit_account, debit_party, debit=amount),
                 line(credit_account, credit_party, credit=amount)]

    due = entry_date + timedelta(days=term.net_days) if term and credit_settlement and entry_date else None
    return JournalPlan(type=kind, lines=lines, discount=discount, term=term, due_date=due)
