from datetime import date
from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from accounting.models import Account, Product
from accounting.services import create_opening_stock, money

# (kode, nama, satuan, jumlah, HPP awal per satuan, harga jual standar per satuan)
# Kebijakan harga: laba kotor target Rp1.000/kg, sehingga HPP = harga jual - Rp1.000.
PRODUK = [
    ('Panen 1', 'Panen Lahan 1', 'kg', Decimal('300'), Decimal('1000'), Decimal('2000')),
    ('Panen 2', 'Panen Lahan 2', 'kg', Decimal('1000'), Decimal('750'), Decimal('1750')),
    ('Panen 3', 'Panen Lahan 3', 'kg', Decimal('1225'), Decimal('1100'), Decimal('2100')),
    ('Panen 4', 'Panen Lahan 4', 'kg', Decimal('275'), Decimal('3000'), Decimal('4000')),
    ('Panen 5', 'Panen Lahan 5', 'kg', Decimal('115'), Decimal('2200'), Decimal('3200')),
]


class Command(BaseCommand):
    help = ('Tambah lima persediaan hasil panen lahan beserta stok awal FIFO. '
            'Aman dijalankan ulang: produk yang sudah ada dilewati.')

    def add_arguments(self, parser):
        parser.add_argument('--tanggal', help='Tanggal saldo awal (YYYY-MM-DD), default hari ini.')

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            opening_date = date.fromisoformat(options['tanggal']) if options['tanggal'] else timezone.localdate()
        except ValueError:
            raise CommandError('Format --tanggal harus YYYY-MM-DD.')
        try:
            inventory = Account.objects.get(code='1150')
            equity = Account.objects.get(code='3100')
        except Account.DoesNotExist:
            raise CommandError('Akun 1150 dan 3100 belum ada. Jalankan dulu: python manage.py seed_agriculture')
        added = Decimal('0')
        for code, name, unit, qty, cost, price in PRODUK:
            if Product.objects.filter(code=code).exists():
                self.stdout.write(f'Lewati {code}: sudah ada.')
                continue
            product = Product.objects.create(code=code, name=name, unit=unit, sale_price=price)
            create_opening_stock(product, qty, cost, opening_date)
            value = money(qty * cost)
            added += value
            self.stdout.write(f'+ {code} {name}: {qty:,.0f} {unit} × Rp{cost:,.0f} = Rp{value:,.0f}')
        if added:
            inventory.opening_balance += added
            equity.opening_balance += added
            inventory.save(update_fields=['opening_balance'])
            equity.save(update_fields=['opening_balance'])
        self.stdout.write(self.style.SUCCESS(
            f'Selesai. Persediaan awal bertambah Rp{added:,.0f} (akun 1150 didebet, 3100 Modal Pemilik dikredit).'))
