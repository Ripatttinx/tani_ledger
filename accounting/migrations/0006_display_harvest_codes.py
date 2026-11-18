from django.db import migrations


def rename_harvest_codes(apps, schema_editor):
    Product = apps.get_model('accounting', 'Product')
    db = schema_editor.connection.alias

    # Jika kode Panen 5 sudah dipakai produk tambahan, geser ke nomor berikutnya.
    Product.objects.using(db).filter(code='Panen 5').update(code='TMP-PANEN-6')

    for number in range(1, 6):
        Product.objects.using(db).filter(code__in=[
            f'PANEN-L{number}', f'PANEN-{number}'
        ]).update(code=f'Panen {number}')

    Product.objects.using(db).filter(code='TMP-PANEN-6').update(code='Panen 6')


def reverse_harvest_codes(apps, schema_editor):
    Product = apps.get_model('accounting', 'Product')
    db = schema_editor.connection.alias
    for number in range(1, 6):
        Product.objects.using(db).filter(code=f'Panen {number}').update(
            code=f'PANEN-{number}'
        )
    Product.objects.using(db).filter(code='Panen 6').update(code='Panen 5')


class Migration(migrations.Migration):
    dependencies = [('accounting', '0005_rename_harvest_product_codes')]
    operations = [migrations.RunPython(rename_harvest_codes, reverse_harvest_codes)]
