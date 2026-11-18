from decimal import Decimal
from django.db import migrations


def seed_parties_and_prices(apps, schema_editor):
    Party = apps.get_model('accounting', 'Party')
    Product = apps.get_model('accounting', 'Product')
    db = schema_editor.connection.alias

    # Tiga kartu pembantu awal untuk pelanggan dan pemasok.
    for i in range(1, 4):
        Party.objects.using(db).get_or_create(
            code=f'PIU-{i:03d}',
            defaults={'name': f'Pelanggan {i}', 'kind': 'CUSTOMER',
                      'opening_balance': Decimal('0.00'), 'phone': ''})
        Party.objects.using(db).get_or_create(
            code=f'UTG-{i:03d}',
            defaults={'name': f'Pemasok {i}', 'kind': 'SUPPLIER',
                      'opening_balance': Decimal('0.00'), 'phone': ''})

    prices = {
        'PANEN-L1': Decimal('2000.00'),
        'PANEN-L2': Decimal('1750.00'),
        'PANEN-L3': Decimal('2100.00'),
        'PANEN-L4': Decimal('4000.00'),
        'PANEN-L5': Decimal('3200.00'),
    }
    for code, price in prices.items():
        Product.objects.using(db).filter(code=code).update(sale_price=price)


def reverse_seed(apps, schema_editor):
    # Tidak menghapus kartu relasi atau harga agar data transaksi pengguna aman.
    pass


class Migration(migrations.Migration):
    dependencies = [('accounting', '0002_termin_diskon_jurnal')]
    operations = [migrations.RunPython(seed_parties_and_prices, reverse_seed)]
