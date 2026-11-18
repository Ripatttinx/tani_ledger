from django.db import migrations


def rename_harvest_product_codes(apps, schema_editor):
    Product = apps.get_model('accounting', 'Product')
    db = schema_editor.connection.alias
    for number in range(1, 6):
        Product.objects.using(db).filter(code=f'PANEN-L{number}').update(
            code=f'PANEN-{number}'
        )


def reverse_rename_harvest_product_codes(apps, schema_editor):
    Product = apps.get_model('accounting', 'Product')
    db = schema_editor.connection.alias
    for number in range(1, 6):
        Product.objects.using(db).filter(code=f'PANEN-{number}').update(
            code=f'PANEN-L{number}'
        )


class Migration(migrations.Migration):
    dependencies = [('accounting', '0004_hpp_margin_seribu')]
    operations = [
        migrations.RunPython(
            rename_harvest_product_codes,
            reverse_rename_harvest_product_codes,
        )
    ]
