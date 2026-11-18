from datetime import date
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from accounting.models import Account, BusinessProfile, JournalEntry, JournalLine, Party, Product
from accounting.services import (create_opening_stock, create_stock_movement,
                                  ledger, period_totals, report_balances, stock_card)

D = Decimal


class AccountingFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='tester', password='strong-test-pass-123')
        self.client.force_login(self.user)
        self.profile = BusinessProfile.current()
        self.cash = Account.objects.create(code='1120', name='Kas', category='ASSET',
            normal_balance='D', opening_balance=D('20000000'))
        self.stock = Account.objects.create(code='1150', name='Persediaan', category='ASSET',
            normal_balance='D', opening_balance=D('4000000'))
        self.equity = Account.objects.create(code='3100', name='Modal', category='EQUITY',
            normal_balance='C', opening_balance=D('24000000'))
        self.sales = Account.objects.create(code='4110', name='Penjualan', category='REVENUE', normal_balance='C')
        self.cogs = Account.objects.create(code='5110', name='HPP', category='COGS', normal_balance='D')
        self.receivable = Account.objects.create(code='1140', name='Piutang', category='ASSET', normal_balance='D')
        self.payable = Account.objects.create(code='2110', name='Utang', category='LIABILITY', normal_balance='C')
        self.product = Product.objects.create(code='PADI', name='Beras', unit='kg', sale_price=D('30000'))
        create_opening_stock(self.product, D('100'), D('20000'), date(2026, 10, 1))

    def test_trial_balance_opening(self):
        rows = report_balances(date(2026, 10, 1))
        self.assertEqual(sum(x['debit'] for x in rows), D('24000000'))
        self.assertEqual(sum(x['credit'] for x in rows), D('24000000'))
        self.assertEqual(period_totals()['difference'], 0)

    def test_fifo_cost_and_automatic_balanced_journals(self):
        create_stock_movement(product=self.product, date_value=date(2026, 10, 2),
            reference='BELI-01', kind='IN', quantity=D('30'), unit_price=D('22000'),
            settlement_account=self.cash, description='Pembelian')
        movement = create_stock_movement(product=self.product, date_value=date(2026, 10, 3),
            reference='JUAL-01', kind='OUT', quantity=D('110'), unit_price=D('30000'),
            settlement_account=self.cash, description='Penjualan')
        self.assertEqual(movement.cost_amount, D('2220000'))
        self.assertEqual(movement.sales_amount, D('3300000'))
        self.assertEqual(movement.allocations.count(), 2)
        # Penjualan perpetual langsung memunculkan HPP di jurnal umum:
        # Debit HPP dan Kredit Persediaan pada bukti penjualan yang sama.
        self.assertTrue(JournalLine.objects.filter(
            entry=movement.entry, account=self.cogs, debit=D('2220000'), credit=D('0')
        ).exists())
        self.assertTrue(JournalLine.objects.filter(
            entry=movement.entry, account=self.stock, debit=D('0'), credit=D('2220000')
        ).exists())
        card = stock_card(self.product)
        self.assertEqual(card['qty'], D('20'))
        self.assertEqual(card['value'], D('440000'))
        self.assertEqual(period_totals()['profit'], D('1080000'))
        self.assertEqual(period_totals()['difference'], 0)
        for j in JournalEntry.objects.all():
            self.assertEqual(j.total_debit, j.total_credit)

    def test_inventory_can_be_posted_directly_from_general_journal_form(self):
        response = self.client.post(reverse('journal_new'), data={
            'date': '2026-10-02', 'description': 'Penjualan hasil panen Lahan 1',
            'inventory_kind': 'OUT', 'inventory_product': str(self.product.pk),
            'inventory_quantity': '2', 'settlement_account': str(self.cash.pk),
            'inventory_unit_price': '', 'inventory_party': '', 'amount': '',
            'term_text': '', 'discount_percent': '', 'discount_rp': '',
        })
        self.assertEqual(response.status_code, 302)
        movement = self.product.movements.get(kind='OUT')
        self.assertEqual(movement.quantity, D('2'))
        self.assertTrue(JournalLine.objects.filter(
            entry=movement.entry, account=self.cogs, debit=D('40000'), credit=D('0')
        ).exists())
        self.assertTrue(JournalLine.objects.filter(
            entry=movement.entry, account=self.stock, debit=D('0'), credit=D('40000')
        ).exists())
        self.assertEqual(movement.entry.total_debit, movement.entry.total_credit)

    def test_negative_stock_and_backdate_rejected_without_partial_journal(self):
        before = JournalEntry.objects.count()
        with self.assertRaises(ValidationError):
            create_stock_movement(product=self.product, date_value=date(2026, 10, 2),
                reference='JUAL-BESAR', kind='OUT', quantity=D('101'), unit_price=D('30000'),
                settlement_account=self.cash, description='Melebihi stok')
        self.assertEqual(JournalEntry.objects.count(), before)
        create_stock_movement(product=self.product, date_value=date(2026, 10, 3),
            reference='BELI-02', kind='IN', quantity=D('2'), unit_price=D('21000'),
            settlement_account=self.cash, description='Pembelian kedua')
        with self.assertRaises(ValidationError):
            create_stock_movement(product=self.product, date_value=date(2026, 10, 2),
                reference='BELI-BACKDATE', kind='IN', quantity=D('1'), unit_price=D('21000'),
                settlement_account=self.cash, description='Terlarang')

    def test_party_validation(self):
        supplier = Party.objects.create(code='S001', name='Pemasok', kind='SUPPLIER')
        entry = JournalEntry.objects.create(date=date(2026, 10, 1), reference='MAN-001', description='Cek')
        line = JournalLine(entry=entry, account=self.receivable, party=supplier,
                           debit=D('100'), credit=D('0'))
        with self.assertRaises(ValidationError):
            line.full_clean()

    def test_income_and_stock_excel_export(self):
        for kind in ('all','accounts','parties','journal','ledger','subsidiary','trial',
                     'worksheet','income','balance','products','inventory'):
            response = self.client.get(reverse('export', args=[kind]))
            self.assertEqual(response.status_code, 200, kind)
            self.assertEqual(response.content[:2], b'PK')
            self.assertIn('.xlsx', response['Content-Disposition'])

    def test_login_required_and_settings(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 302)
        self.client.force_login(self.user)
        res = self.client.post(reverse('settings'), data={
            'name': 'Kebun Ceria', 'subtitle': 'Pertanian', 'address': '', 'phone': '',
            'email': '', 'owner': '', 'inventory_account_code': '1150',
            'cogs_account_code': '5110', 'sales_account_code': '4110',
        })
        self.assertEqual(res.status_code, 302)
        self.assertEqual(BusinessProfile.current().name, 'Kebun Ceria')

    def test_adjustment_is_separate_and_cannot_be_forged_as_general(self):
        response = self.client.post(reverse('adjustment_new'), data={
            'date': '2026-10-10', 'reference': 'AJP-01',
            'description': 'Jurnal penyesuaian akhir periode', 'kind': 'GENERAL',
            'pairs-TOTAL_FORMS': '1', 'pairs-INITIAL_FORMS': '0',
            'pairs-MIN_NUM_FORMS': '1', 'pairs-MAX_NUM_FORMS': '15',
            'pairs-0-debit_account': str(self.cash.pk), 'pairs-0-credit_account': str(self.equity.pk),
            'pairs-0-amount': '12000',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(JournalEntry.objects.get(reference='AJP-01').kind, JournalEntry.Kind.ADJUSTMENT)
        self.assertContains(self.client.get(reverse('adjustment_list')), 'AJP-01')
        self.assertNotContains(self.client.get(reverse('journal_list')), 'AJP-01')
        self.assertNotContains(self.client.get(reverse('dashboard')), 'Daftar Akun')
        self.assertNotContains(self.client.get(reverse('dashboard')), 'Kode Pembantu')

    def test_excel_has_live_cross_sheet_formulas(self):
        from io import BytesIO
        from openpyxl import load_workbook
        entry = JournalEntry.objects.create(date=date(2026, 10, 10), reference='JV-42', description='Penjualan jasa')
        JournalLine.objects.create(entry=entry, account=self.cash, debit=D('250000'))
        JournalLine.objects.create(entry=entry, account=self.sales, credit=D('250000'))
        self.client.post(reverse('adjustment_new'), data={
            'date': '2026-10-11', 'reference': 'AJP-42', 'description': 'Penyesuaian',
            'pairs-TOTAL_FORMS': '1', 'pairs-INITIAL_FORMS': '0',
            'pairs-MIN_NUM_FORMS': '1', 'pairs-MAX_NUM_FORMS': '15',
            'pairs-0-debit_account': str(self.cash.pk), 'pairs-0-credit_account': str(self.equity.pk),
            'pairs-0-amount': '1000',
        })
        create_stock_movement(product=self.product, date_value=date(2026, 10, 12),
            reference='BELI-XLSX', kind='IN', quantity=D('30'), unit_price=D('22000'),
            settlement_account=self.cash, description='Pembelian FIFO untuk Excel')
        create_stock_movement(product=self.product, date_value=date(2026, 10, 13),
            reference='JUAL-XLSX', kind='OUT', quantity=D('110'), unit_price=D('30000'),
            settlement_account=self.cash, description='Penjualan FIFO untuk Excel')
        response = self.client.get(reverse('export', args=['all']) + '?to=2026-11-01')
        self.assertEqual(response.status_code, 200)
        wb = load_workbook(BytesIO(response.content), data_only=False)
        self.assertEqual(len(wb.worksheets), 12)
        for tab in ('AKUN', 'KODE', 'JURNAL UMUM', 'BUKU BESAR',
                    'JURNAL PENYESUAIAN', 'BB PEMBANTU', 'NERACA SALDO',
                    'NERACA LAJUR', 'LABA RUGI', 'NERACA', 'PRODUK', 'INVENTORY CARD'):
            self.assertIn(tab, wb.sheetnames)
        self.assertEqual(wb.sheetnames[3:5], ['BUKU BESAR','JURNAL PENYESUAIAN'])
        self.assertIn('SUMIF', wb['NERACA SALDO']['D5'].value)
        self.assertIn('JURNAL PENYESUAIAN', wb['NERACA SALDO']['D5'].value)
        self.assertIn('NERACA LAJUR', wb['LABA RUGI']['D5'].value)
        self.assertIn('LABA RUGI', next(
            c.value for row in wb['NERACA'].iter_rows() for c in row
            if c.data_type == 'f' and 'LABA RUGI' in c.value))
        self.assertIn('MIN', wb['INVENTORY CARD']['U7'].value)
        self.assertIn('SUM', wb['INVENTORY CARD']['N7'].value)
        self.assertEqual(wb.calculation.calcMode, 'auto')


class AutoJournalTests(TestCase):
    """Jurnal umum otomatis, termin/diskon/retur, hapus jurnal."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='kasir', password='strong-test-pass-123')
        rows = [('1110', 'Kas di Bank', 'ASSET', 'D'), ('1120', 'Kas Tunai', 'ASSET', 'D'),
                ('1140', 'Piutang Usaha', 'ASSET', 'D'), ('1150', 'Persediaan', 'ASSET', 'D'),
                ('2110', 'Utang Usaha', 'LIABILITY', 'C'), ('3100', 'Modal', 'EQUITY', 'C'),
                ('4110', 'Penjualan', 'REVENUE', 'C'), ('4130', 'Potongan Penjualan', 'REVENUE', 'D'),
                ('4140', 'Retur Penjualan', 'REVENUE', 'D'), ('5110', 'HPP', 'COGS', 'D'),
                ('5120', 'Potongan Pembelian', 'COGS', 'C'), ('5130', 'Retur Pembelian', 'COGS', 'C'),
                ('6210', 'Beban Pupuk', 'EXPENSE', 'D'),
                ('1160', 'Perlengkapan', 'ASSET', 'D'), ('1170', 'Sewa Dibayar di Muka', 'ASSET', 'D'),
                ('1230', 'Mesin', 'ASSET', 'D'), ('1240', 'Kendaraan', 'ASSET', 'D'),
                ('2120', 'Utang Pinjaman', 'LIABILITY', 'C'), ('4120', 'Pendapatan Jasa', 'REVENUE', 'C'),
                ('6120', 'Beban Listrik', 'EXPENSE', 'D'), ('6190', 'Beban Gaji', 'EXPENSE', 'D'),
                ('8110', 'Pendapatan Bunga', 'OTHER_INCOME', 'C'),
                ('2130', 'Utang Pajak', 'LIABILITY', 'C'), ('2140', 'Utang Gaji', 'LIABILITY', 'C')]
        for code, name, cat, normal in rows:
            Account.objects.create(code=code, name=name, category=cat, normal_balance=normal)
        cls.customer = Party.objects.create(code='C001', name='Pak Budi', kind='CUSTOMER')
        cls.supplier = Party.objects.create(code='S001', name='CV Tani Jaya', kind='SUPPLIER')
        BusinessProfile.current()

    def setUp(self):
        self.client.force_login(self.user)

    def post(self, description, amount, **extra):
        data = {'date': '2026-11-11', 'description': description, 'amount': str(amount)}
        data.update(extra)
        return self.client.post(reverse('journal_new'), data)

    def lines(self, entry):
        return sorted((l.account.code, l.debit, l.credit) for l in entry.lines.all())

    def test_form_renders_each_field_once(self):
        html = self.client.get(reverse('journal_new')).content.decode()
        for name in ('date', 'description', 'amount', 'term_text', 'discount_percent', 'discount_rp', 'use_term_discount'):
            self.assertEqual(html.count(f'name="{name}"'), 1, name)
        self.assertIn('value="2026-', html)  # tanggal default terisi

    def test_basic_purchase_cash(self):
        self.assertEqual(self.post('Pembelian pupuk tunai', 1000000).status_code, 302)
        e = JournalEntry.objects.get()
        self.assertEqual(self.lines(e), [('1120', 0, D('1000000')), ('6210', D('1000000'), 0)])
        self.assertTrue(e.reference.startswith('JU-20261111-'))

    def test_registered_farm_input_without_quantity_is_not_forced_into_stock(self):
        Product.objects.create(code='PPK-KOM', name='Pupuk Kompos', unit='kg')
        response = self.post('Pembelian pupuk kompos tunai', 100000)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.lines(JournalEntry.objects.get()),
                         [('1120', 0, D('100000')), ('6210', D('100000'), 0)])

    def test_credit_sale_with_term_sets_due_date_and_party(self):
        self.post('Penjualan hasil tani kredit Pak Budi', 5000000, term_text='2/10,n/30')
        e = JournalEntry.objects.get()
        self.assertEqual(e.term, '2/10,n/30')
        self.assertEqual(e.due_date, date(2026, 12, 11))
        line = e.lines.get(account__code='1140')
        self.assertEqual(line.party, self.customer)
        self.assertEqual(e.total_debit, e.total_credit)

    def test_term_rejected_for_cash_sale(self):
        r = self.post('Penjualan hasil tani tunai', 100000, term_text='n/30')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_sale_with_percent_discount(self):
        self.post('Penjualan hasil tani tunai', 1000000, discount_percent='5')
        e = JournalEntry.objects.get()
        self.assertEqual(self.lines(e), [('1120', D('950000'), 0), ('4110', 0, D('1000000')), ('4130', D('50000'), 0)])
        self.assertEqual(e.discount_amount, D('50000'))

    def test_purchase_with_rupiah_discount(self):
        self.post('Pembelian persediaan kredit CV Tani Jaya', 2000000, discount_rp='100000')
        e = JournalEntry.objects.get()
        self.assertEqual(self.lines(e), [('1150', D('1900000'), 0), ('2110', 0, D('1900000'))])
        self.assertEqual(e.lines.get(account__code='2110').party, self.supplier)

    def test_common_non_inventory_transactions_are_classified_separately(self):
        cases = [
            ('Pembelian mesin tunai', '1230', '1120'),
            ('Pembelian perlengkapan kantor tunai', '1160', '1120'),
            ('Pembelian pupuk untuk tanam tunai', '6210', '1120'),
            ('Pembayaran listrik tunai', '6120', '1120'),
            ('Pembayaran gaji tunai', '6190', '1120'),
            ('Pembelian sewa dibayar di muka tunai', '1170', '1120'),
            ('Setoran modal tunai', '1120', '3100'),
            ('Penerimaan pinjaman bank transfer', '1110', '2120'),
            ('Pembayaran pokok pinjaman transfer', '2120', '1110'),
            ('Pendapatan jasa tunai', '1120', '4120'),
            ('Pendapatan bunga transfer', '1110', '8110'),
            ('Pembayaran utang pajak tunai', '2130', '1120'),
            ('Pembayaran utang gaji tunai', '2140', '1120'),
        ]
        for description, debit_code, credit_code in cases:
            with self.subTest(description=description):
                response = self.post(description, 125000)
                self.assertEqual(response.status_code, 302, description)
                entry = JournalEntry.objects.order_by('-id').first()
                self.assertEqual(
                    self.lines(entry),
                    sorted([(debit_code, D('125000'), 0), (credit_code, 0, D('125000'))]),
                    description,
                )

    def test_asset_purchase_is_not_recorded_as_inventory(self):
        self.post('Pembelian traktor tunai', 25000000)
        entry = JournalEntry.objects.get()
        self.assertEqual(self.lines(entry), [('1120', 0, D('25000000')), ('1240', D('25000000'), 0)])
        self.assertFalse(hasattr(entry, 'stock_movement'))

    def test_purchase_discount_reduces_asset_cost_under_perpetual_method(self):
        self.post('Pembelian mesin tunai', 2000000, discount_rp='100000')
        entry = JournalEntry.objects.get()
        self.assertEqual(self.lines(entry), [('1120', 0, D('1900000')), ('1230', D('1900000'), 0)])
        self.assertFalse(entry.lines.filter(account__code='5120').exists())

    def test_receipt_with_term_discount(self):
        self.post('Penerimaan piutang Pak Budi transfer', 5000000, term_text='2/10,n/30', use_term_discount='on')
        e = JournalEntry.objects.get()
        self.assertEqual(self.lines(e), [('1110', D('4900000'), 0), ('1140', 0, D('5000000')), ('4130', D('100000'), 0)])
        self.assertEqual(e.total_debit, e.total_credit)

    def test_supplier_settlement_discount_is_rejected_without_fifo_layer_adjustment(self):
        response = self.post('Pembayaran utang persediaan CV Tani Jaya transfer', 2000000,
                             discount_percent='5')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_term_discount_rejected_on_credit_sale(self):
        r = self.post('Penjualan hasil tani kredit Pak Budi', 1000, term_text='2/10,n/30', use_term_discount='on')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_discount_validation(self):
        for extra in ({'discount_percent': '5', 'discount_rp': '100'}, {'discount_rp': '2000000'}):
            self.assertEqual(self.post('Penjualan hasil tani tunai', 1000000, **extra).status_code, 200)
        self.assertEqual(self.post('Pembayaran listrik tunai', 100000, discount_percent='5').status_code, 200)
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_inventory_returns_are_rejected_until_fifo_subledger_workflow_exists(self):
        for description in (
            'Retur penjualan hasil tani kredit Pak Budi',
            'Retur pembelian persediaan tunai',
        ):
            with self.subTest(description=description):
                response = self.post(description, 200000)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(JournalEntry.objects.count(), 0)


    def test_unknown_description_creates_nothing(self):
        self.assertEqual(self.post('sesuatu yang aneh', 1000).status_code, 200)
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_preview_matches_posting(self):
        r = self.client.get(reverse('journal_preview'), {
            'description': 'Penjualan hasil tani tunai', 'amount': '1000000', 'discount_percent': '5', 'date': '2026-11-11'})
        data = r.json()
        self.assertTrue(data['ok'])
        self.assertEqual([x['code'] for x in data['lines']], ['1120', '4140', '4110'])
        self.assertEqual(data['total_debit'], data['total_credit'])
        empty = self.client.get(reverse('journal_preview'), {'description': ''}).json()
        self.assertFalse(empty['ok'])
        no_amount = self.client.get(reverse('journal_preview'), {'description': 'Pembelian pupuk tunai'}).json()
        self.assertTrue(no_amount['ok'])
        self.assertEqual(no_amount['total_debit'], '0.00')

    def test_reference_stays_unique_after_delete(self):
        self.post('Pembelian pupuk tunai', 1000)
        self.post('Pembelian pupuk tunai', 2000)
        first = JournalEntry.objects.order_by('id').first()
        self.client.post(reverse('journal_delete', args=[first.pk]))
        self.assertEqual(self.post('Pembelian pupuk tunai', 3000).status_code, 302)
        refs = list(JournalEntry.objects.values_list('reference', flat=True))
        self.assertEqual(len(refs), len(set(refs)))

    def test_delete_requires_post_and_updates_reports(self):
        self.post('Pembelian pupuk tunai', 1000000)
        e = JournalEntry.objects.get()
        self.assertContains(self.client.get(reverse('journal_delete', args=[e.pk])), 'Ya, hapus')
        self.assertEqual(JournalEntry.objects.count(), 1)  # GET tidak menghapus
        self.assertEqual(self.client.post(reverse('journal_delete', args=[e.pk])).status_code, 302)
        self.assertEqual(JournalEntry.objects.count(), 0)
        self.assertEqual(JournalLine.objects.count(), 0)
        self.assertEqual(period_totals()['costs'], 0)

    def test_delete_adjustment_redirects_to_adjustments(self):
        e = JournalEntry.objects.create(date=date(2026, 11, 11), reference='AJP-9', description='x', kind='ADJUSTMENT')
        r = self.client.post(reverse('journal_delete', args=[e.pk]))
        self.assertRedirects(r, reverse('adjustment_list'))

    def test_delete_stock_journal_restores_fifo(self):
        cash = Account.objects.get(code='1120')
        product = Product.objects.create(code='P1', name='Beras', unit='kg', sale_price=D('30000'))
        create_opening_stock(product, D('100'), D('20000'), date(2026, 11, 1))
        buy = create_stock_movement(product=product, date_value=date(2026, 11, 2), reference='B1', kind='IN',
            quantity=D('30'), unit_price=D('22000'), settlement_account=cash, description='beli')
        sell = create_stock_movement(product=product, date_value=date(2026, 11, 3), reference='J1', kind='OUT',
            quantity=D('110'), unit_price=D('30000'), settlement_account=cash, description='jual')
        # pembelian sudah terpakai & ada mutasi lebih baru -> ditolak
        r = self.client.post(reverse('journal_delete', args=[buy.entry_id]), follow=True)
        self.assertTrue(JournalEntry.objects.filter(pk=buy.entry_id).exists())
        self.assertContains(r, 'lebih baru')
        # hapus penjualan terakhir -> stok dan lapisan FIFO pulih
        self.client.post(reverse('journal_delete', args=[sell.entry_id]))
        self.assertFalse(JournalEntry.objects.filter(pk=sell.entry_id).exists())
        card = stock_card(product)
        self.assertEqual((card['qty'], card['value']), (D('130'), D('2660000')))
        self.assertEqual([float(l.remaining_quantity) for l in product.layers.all()], [100.0, 30.0])
        # sekarang pembelian menjadi mutasi terakhir -> boleh dihapus
        self.client.post(reverse('journal_delete', args=[buy.entry_id]))
        self.assertEqual(stock_card(product)['qty'], D('100'))
        self.assertEqual(period_totals()['difference'], 0)

    def test_seed_products_and_accounts(self):
        from django.core.management import call_command
        from io import StringIO
        Account.objects.filter(code='3100').update(opening_balance=0)
        call_command('seed_produk_tani', stdout=StringIO())
        call_command('seed_produk_tani', stdout=StringIO())  # idempoten
        self.assertEqual(Product.objects.count(), 3)
        values = {p.code: stock_card(p) for p in Product.objects.all()}
        self.assertEqual((values['BIBIT-01']['qty'], values['BIBIT-01']['value']), (D('2000'), D('500000')))
        self.assertEqual(values['PPK-KOM']['value'], D('1000000'))
        self.assertEqual(values['SPR-HAMA']['value'], D('2250000'))
        self.assertEqual(Account.objects.get(code='1150').opening_balance, D('3750000'))
        self.assertEqual(period_totals()['difference'], 0)
