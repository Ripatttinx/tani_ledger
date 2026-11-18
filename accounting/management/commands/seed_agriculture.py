from datetime import date
from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from accounting.models import Account, BusinessProfile, Product, JournalEntry
from accounting.services import create_opening_stock, create_stock_movement

ACCOUNT_ROWS = [
    ('1110','Kas di Bank','ASSET','D'), ('1120','Kas Tunai','ASSET','D'),
    ('1130','Kas Kecil','ASSET','D'), ('1135','Deposito Jangka Pendek','ASSET','D'), ('1140','Piutang Usaha','ASSET','D'),
    ('1141','Cadangan Kerugian Piutang','ASSET','C'), ('1150','Persediaan Barang dan Hasil Tani','ASSET','D'),
    ('1160','Perlengkapan','ASSET','D'), ('1165','Bahan Habis Pakai','ASSET','D'),
    ('1170','Sewa Dibayar di Muka','ASSET','D'), ('1180','Asuransi Dibayar di Muka','ASSET','D'),
    ('1190','Iklan Dibayar di Muka','ASSET','D'), ('1195','Pajak Dibayar di Muka','ASSET','D'),
    ('1210','Tanah Pertanian','ASSET','D'), ('1220','Bangunan / Gudang','ASSET','D'),
    ('1221','Akumulasi Penyusutan Bangunan','ASSET','C'),
    ('1230','Mesin dan Peralatan Pertanian','ASSET','D'), ('1231','Akumulasi Penyusutan Mesin dan Peralatan','ASSET','C'),
    ('1240','Kendaraan Operasional','ASSET','D'), ('1241','Akumulasi Penyusutan Kendaraan','ASSET','C'),
    ('1250','Komputer dan Inventaris','ASSET','D'), ('1251','Akumulasi Penyusutan Inventaris','ASSET','C'),
    ('1260','Aset Tetap dalam Pembangunan','ASSET','D'), ('1270','Aset Biologis - Tanaman Produktif','ASSET','D'),
    ('1280','Aset Hak Guna','ASSET','D'), ('1281','Akumulasi Penyusutan Aset Hak Guna','ASSET','C'),
    ('2110','Utang Usaha','LIABILITY','C'), ('2120','Utang Wesel / Pinjaman','LIABILITY','C'),
    ('2130','Utang Pajak','LIABILITY','C'), ('2140','Utang Gaji dan Upah','LIABILITY','C'),
    ('2150','Beban yang Masih Harus Dibayar','LIABILITY','C'), ('2160','Uang Muka Pelanggan','LIABILITY','C'),
    ('2170','Pendapatan Diterima di Muka','LIABILITY','C'), ('2210','Utang Bank Jangka Panjang','LIABILITY','C'),
    ('2220','Liabilitas Sewa Jangka Panjang','LIABILITY','C'),
    ('3100','Modal Pemilik','EQUITY','C'), ('3110','Saldo Laba','EQUITY','C'),
    ('3120','Prive Pemilik','EQUITY','D'), ('3130','Tambahan Modal Disetor','EQUITY','C'),
    ('4110','Penjualan Hasil Tani','REVENUE','C'), ('4120','Pendapatan Angkut / Jasa','REVENUE','C'),
    ('4150','Pendapatan Penjualan Produk Lain','REVENUE','C'), ('4160','Pendapatan Sewa','REVENUE','C'),
    ('4130','Potongan Penjualan','REVENUE','D'), ('4140','Retur Penjualan','REVENUE','D'),
    ('5110','Harga Pokok Penjualan','COGS','D'), ('5120','Potongan Pembelian','COGS','C'),
    ('5130','Retur Pembelian','COGS','C'), ('5140','Beban Angkut Pembelian','COGS','D'),
    ('6100','Beban Penjualan','EXPENSE','D'), ('6110','Beban Iklan dan Promosi','EXPENSE','D'), ('6120','Beban Telepon, Air, dan Listrik','EXPENSE','D'),
    ('6130','Beban Sewa','EXPENSE','D'), ('6140','Beban Perlengkapan','EXPENSE','D'),
    ('6150','Beban Pemeliharaan dan Perbaikan','EXPENSE','D'), ('6160','Beban Kerugian Piutang','EXPENSE','D'),
    ('6170','Beban Penyusutan','EXPENSE','D'), ('6180','Beban Asuransi','EXPENSE','D'),
    ('6190','Beban Gaji dan Upah','EXPENSE','D'), ('6200','Beban Kebersihan','EXPENSE','D'),
    ('6210','Beban Pupuk dan Pestisida','EXPENSE','D'), ('6220','Beban Transportasi','EXPENSE','D'),
    ('6230','Beban Pajak dan Retribusi','EXPENSE','D'), ('6240','Beban Bahan Baku dan Produksi','EXPENSE','D'),
    ('6250','Beban Administrasi','EXPENSE','D'), ('6260','Beban Perjalanan Dinas','EXPENSE','D'),
    ('6270','Beban Keamanan','EXPENSE','D'), ('6280','Beban Pelatihan','EXPENSE','D'),
    ('6290','Beban Donasi dan Sumbangan','EXPENSE','D'), ('6300','Beban Pajak Penghasilan','EXPENSE','D'),
    ('7110','Pendapatan Denda / Lain-lain','OTHER_INCOME','C'),
    ('8110','Pendapatan Bunga','OTHER_INCOME','C'), ('8120','Beban Bunga','OTHER_EXPENSE','D'),
    ('8130','Beban Administrasi Bank','OTHER_EXPENSE','D'), ('8140','Beban Lain-lain','OTHER_EXPENSE','D'),
    ('8150','Laba / Rugi Pelepasan Aset Tetap','OTHER_EXPENSE','D'),
    ('8160','Kerugian Selisih Kurs','OTHER_EXPENSE','D'), ('8170','Kerugian Penurunan Nilai Aset','OTHER_EXPENSE','D'),
    # Aset lancar tambahan
    ('1142','Piutang Nonusaha','ASSET','D'), ('1143','Piutang Karyawan','ASSET','D'),
    ('1155','Persediaan Bahan Baku','ASSET','D'), ('1156','Persediaan Barang Dalam Proses','ASSET','D'),
    ('1157','Persediaan Barang Jadi','ASSET','D'), ('1161','Perlengkapan Pertanian','ASSET','D'),
    ('1162','Perlengkapan Kantor','ASSET','D'), ('1196','Pajak Masukan','ASSET','D'),
    ('1197','Uang Muka Pembelian','ASSET','D'), ('1198','Uang Muka Pajak Penghasilan','ASSET','D'),
    ('1200','Investasi Jangka Pendek','ASSET','D'), ('1290','Aset Takberwujud','ASSET','D'),
    ('1291','Akumulasi Amortisasi Aset Takberwujud','ASSET','C'),
    ('1295','Aset Pajak Tangguhan','ASSET','D'),
    # Liabilitas dan pajak
    ('2111','Utang Nonusaha','LIABILITY','C'), ('2112','Utang Pembelian Aset Tetap','LIABILITY','C'),
    ('2131','Utang PPN Keluaran','LIABILITY','C'), ('2132','Utang PPh','LIABILITY','C'),
    ('2180','Uang Muka dari Pelanggan','LIABILITY','C'), ('2190','Provisi / Kewajiban Diestimasi','LIABILITY','C'),
    ('2230','Liabilitas Pajak Tangguhan','LIABILITY','C'),
    # Ekuitas
    ('3140','Saldo Laba Ditahan','EQUITY','C'), ('3150','Penghasilan Komprehensif Lain','EQUITY','C'),
    # Pendapatan
    ('4170','Pendapatan Hibah Operasional','OTHER_INCOME','C'),
    ('4180','Keuntungan Selisih Kurs','OTHER_INCOME','C'),
    # Beban terperinci
    ('6310','Beban Panen','EXPENSE','D'), ('6320','Beban Pengolahan Hasil','EXPENSE','D'),
    ('6330','Beban Distribusi dan Pengiriman','EXPENSE','D'), ('6340','Beban Komisi','EXPENSE','D'),
    ('6350','Beban Perizinan','EXPENSE','D'), ('6360','Beban Konsumsi','EXPENSE','D'),
    ('6370','Beban Penyusutan Tanaman Produktif','EXPENSE','D'),
    ('6380','Beban Amortisasi','EXPENSE','D'), ('6390','Beban Penurunan Nilai','EXPENSE','D'),
    ('6400','Beban Tanggung Jawab Sosial','EXPENSE','D'),
    ('8180','Beban Denda dan Sanksi','OTHER_EXPENSE','D'),
]


class Command(BaseCommand):
    help = 'Isi bagan akun usaha pertanian, dengan demo transaksi FIFO opsional.'

    def add_arguments(self, parser):
        parser.add_argument('--demo', action='store_true', help='Tambah data uji contoh yang saldonya seimbang')

    @transaction.atomic
    def handle(self, *args, **options):
        BusinessProfile.current()
        for code, name, cat, normal in ACCOUNT_ROWS:
            account, created = Account.objects.get_or_create(code=code, defaults={
                'name':name, 'category':cat, 'normal_balance':normal})
            if not created:
                changed = []
                for field, value in (('name', name), ('category', cat), ('normal_balance', normal)):
                    if getattr(account, field) != value:
                        setattr(account, field, value)
                        changed.append(field)
                if changed:
                    account.save(update_fields=changed)
        self.stdout.write(self.style.SUCCESS(f'Bagan akun pertanian siap ({len(ACCOUNT_ROWS)} akun standar). Saldo awal yang sudah ada tidak diubah.'))
        if not options['demo']:
            self.stdout.write('Tambahkan --demo untuk contoh stok, pembelian, dan penjualan.')
            return
        if JournalEntry.objects.exists() or Product.objects.exists() or any(
                Account.objects.get(code=code).opening_balance != 0 for code in ('1120','1150','3100')):
            raise CommandError('Mode demo hanya aman dijalankan pada database awal tanpa produk, jurnal dan saldo awal.')
        cash = Account.objects.get(code='1120')
        inventory = Account.objects.get(code='1150')
        equity = Account.objects.get(code='3100')
        cash.opening_balance = Decimal('20000000.00')
        inventory.opening_balance = Decimal('4000000.00')
        equity.opening_balance = Decimal('24000000.00')
        for a in (cash, inventory, equity): a.save(update_fields=['opening_balance'])
        rice = Product.objects.create(code='PADI-01', name='Beras Organik', unit='kg', sale_price=Decimal('30000'))
        corn = Product.objects.create(code='JAG-01', name='Jagung Pipil', unit='kg', sale_price=Decimal('50000'))
        today = timezone.localdate()
        create_opening_stock(rice, Decimal('100'), Decimal('20000'), today)
        create_opening_stock(corn, Decimal('50'), Decimal('40000'), today)
        create_stock_movement(product=rice, date_value=today, reference='BELI-DEMO-01',
            kind='IN', quantity=Decimal('30'), unit_price=Decimal('22000'),
            settlement_account=cash, description='Pembelian beras organik', user=None)
        create_stock_movement(product=rice, date_value=today, reference='JUAL-DEMO-01',
            kind='OUT', quantity=Decimal('110'), unit_price=Decimal('30000'),
            settlement_account=cash, description='Penjualan beras organik', user=None)
        self.stdout.write(self.style.SUCCESS('Demo terisi. FIFO penjualan: 100×20.000 + 10×22.000 = 2.220.000.'))
