from decimal import Decimal
import re
from django import forms
from django.core.exceptions import ValidationError
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone
from .models import Account, BusinessProfile, InventoryMovement, JournalEntry, JournalLine, Party, Product


class StyledMixin:
    def style_fields(self):
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault('class', 'check-input')
            else:
                field.widget.attrs.setdefault('class', 'form-control')


class AccountForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = Account
        fields = ('code', 'name', 'category', 'normal_balance', 'opening_balance', 'is_header', 'is_active')
        widgets = {'code': forms.TextInput(attrs={'placeholder': 'Contoh: 1110'}),
                   'name': forms.TextInput(attrs={'placeholder': 'Contoh: Kas di Bank'})}
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()
    def clean(self):
        data = super().clean()
        if self.instance.pk and self.instance.journalline_set.exists():
            for key in ('code', 'category', 'normal_balance', 'opening_balance', 'is_header'):
                if key in data and data[key] != getattr(self.instance, key):
                    self.add_error(key, 'Tidak dapat mengubah struktur/saldo awal akun yang telah mempunyai jurnal.')
        if data.get('is_header') and data.get('opening_balance'):
            self.add_error('opening_balance', 'Akun header tidak memiliki saldo.')
        return data


class PartyForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = Party
        fields = ('name', 'kind', 'opening_balance', 'phone')
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()
    def clean(self):
        data = super().clean()
        if self.instance.pk and self.instance.journalline_set.exists():
            for key in ('code', 'kind', 'opening_balance'):
                if key in data and data[key] != getattr(self.instance, key):
                    self.add_error(key, 'Relasi yang memiliki transaksi tidak dapat diubah kode, jenis, atau saldo awalnya.')
        return data


class JournalEntryForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = JournalEntry
        fields = ('date', 'description', 'kind')
        widgets = {'date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
                   'description': forms.Textarea(attrs={'rows': 2})}
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.initial.get('date'):
            self.initial['date'] = timezone.localdate()
        self.fields['kind'].choices = [(k, n) for k, n in JournalEntry.Kind.choices if k != 'INVENTORY']
        self.style_fields()


class JournalLineForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = JournalLine
        fields = ('account', 'party', 'description', 'debit', 'credit')
        widgets = {'description': forms.TextInput(attrs={'placeholder': 'Opsional'}),
            'debit': forms.NumberInput(attrs={'step': '0.01', 'min': 0, 'placeholder': '0'}),
            'credit': forms.NumberInput(attrs={'step': '0.01', 'min': 0, 'placeholder': '0'})}
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['account'].queryset = Account.objects.filter(is_active=True, is_header=False)
        self.fields['party'].required = False
        self.fields['debit'].required = False
        self.fields['credit'].required = False
        self.style_fields()
    def clean(self):
        data = super().clean()
        if data.get('DELETE'):
            return data
        account, party = data.get('account'), data.get('party')
        d, c = data.get('debit') or Decimal('0'), data.get('credit') or Decimal('0')
        data['debit'], data['credit'] = d, c
        if account and (d > 0) == (c > 0):
            raise ValidationError('Isi debet ATAU kredit, bukan keduanya atau kosong.')
        if account and account.code == '1140' and (not party or party.kind != 'CUSTOMER'):
            self.add_error('party', 'Harus memilih pelanggan untuk akun 1140.')
        elif account and account.code == '2110' and (not party or party.kind != 'SUPPLIER'):
            self.add_error('party', 'Harus memilih pemasok untuk akun 2110.')
        elif account and account.code not in ('1140', '2110') and party:
            self.add_error('party', 'Akun selain 1140/2110 tidak memerlukan kode relasi.')
        return data


class BalancedJournalFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        rows = [f.cleaned_data for f in self.forms
                if f.cleaned_data and not f.cleaned_data.get('DELETE') and f.cleaned_data.get('account')]
        if len(rows) < 2:
            raise ValidationError('Minimal 2 baris akun untuk jurnal berpasangan.')
        debit = sum((r.get('debit') or Decimal('0') for r in rows), Decimal('0'))
        credit = sum((r.get('credit') or Decimal('0') for r in rows), Decimal('0'))
        if debit <= 0 or debit != credit:
            raise ValidationError(f'Jurnal tidak seimbang. Debet {debit:,.2f} dan kredit {credit:,.2f}.')


JournalLineFormSet = inlineformset_factory(
    JournalEntry, JournalLine, form=JournalLineForm, formset=BalancedJournalFormSet,
    extra=2, min_num=2, validate_min=True, can_delete=True, max_num=30, validate_max=True,
)


class ProductForm(StyledMixin, forms.ModelForm):
    opening_quantity = forms.DecimalField(label='Stok awal', required=False, initial=0,
        max_digits=18, decimal_places=3, min_value=Decimal('0'))
    opening_unit_cost = forms.DecimalField(label='Harga pokok awal per unit', required=False, initial=0,
        max_digits=20, decimal_places=2, min_value=Decimal('0'))
    opening_date = forms.DateField(label='Tanggal saldo awal', required=False,
        widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))

    class Meta:
        model = Product
        fields = ('code', 'name', 'unit', 'sale_price', 'is_active')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            for key in ('opening_quantity', 'opening_unit_cost', 'opening_date'):
                self.fields.pop(key, None)
        else:
            self.fields['opening_date'].initial = timezone.localdate()
        self.style_fields()

    def clean(self):
        data = super().clean()
        if not self.instance.pk and (data.get('opening_quantity') or Decimal('0')) > 0:
            if not data.get('opening_date'):
                self.add_error('opening_date', 'Tanggal saldo awal wajib diisi.')
            if (data.get('opening_unit_cost') or Decimal('0')) <= 0:
                self.add_error('opening_unit_cost', 'Harga pokok awal wajib lebih dari nol.')
        return data


class MovementForm(StyledMixin, forms.Form):
    product = forms.ModelChoiceField(label='Barang', queryset=Product.objects.none())
    date = forms.DateField(label='Tanggal', widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
    kind = forms.ChoiceField(label='Jenis transaksi', choices=[('IN', 'Barang masuk / pembelian'), ('OUT', 'Barang keluar / penjualan')])
    quantity = forms.DecimalField(label='Jumlah unit', max_digits=18, decimal_places=3, min_value=Decimal('0.001'))
    unit_price = forms.DecimalField(label='Harga beli per unit (barang masuk; harga jual otomatis dari master produk saat keluar)',
        required=False, max_digits=20, decimal_places=2, min_value=Decimal('0.01'))
    settlement_account = forms.ModelChoiceField(label='Akun pembayaran atau utang/piutang', queryset=Account.objects.none())
    party = forms.ModelChoiceField(label='Pelanggan / pemasok (pilih nama, kode dibuat otomatis)',
                                  queryset=Party.objects.all(), required=False)
    description = forms.CharField(label='Keterangan', max_length=250)
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product'].queryset = Product.objects.filter(is_active=True)
        self.fields['settlement_account'].queryset = Account.objects.filter(is_active=True, is_header=False)
        self.fields['date'].initial = timezone.localdate()
        self.style_fields()


class ProfileForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = BusinessProfile
        fields = ('name', 'subtitle', 'address', 'phone', 'email', 'owner',
                  'inventory_account_code', 'cogs_account_code', 'sales_account_code')
        widgets = {'address': forms.Textarea(attrs={'rows': 3})}
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()
    def clean(self):
        data = super().clean()
        codes = [data.get('inventory_account_code'), data.get('cogs_account_code'),
                 data.get('sales_account_code')]
        if len(set(codes)) != 3:
            raise ValidationError('Kode akun Persediaan, HPP, dan Penjualan harus berbeda.')
        return data


class JournalPairForm(StyledMixin, forms.Form):
    """Satu baris input mewakili pasangan jurnal debet-kredit."""
    debit_account = forms.ModelChoiceField(label='Akun debet', queryset=Account.objects.none())
    credit_account = forms.ModelChoiceField(label='Akun kredit', queryset=Account.objects.none())
    amount = forms.DecimalField(label='Nominal transaksi (Rp)', max_digits=20, decimal_places=2,
                                min_value=Decimal('0.01'), widget=forms.NumberInput(attrs={
                                    'step': '0.01', 'min': '0.01', 'placeholder': 'Masukkan nominal'}))
    description = forms.CharField(label='Uraian', max_length=250, required=False,
                                  widget=forms.TextInput(attrs={'placeholder': 'Opsional'}))
    debit_party = forms.ModelChoiceField(label='Pelanggan (jika akun 1140)', queryset=Party.objects.none(), required=False)
    credit_party = forms.ModelChoiceField(label='Pemasok (jika akun 2110)', queryset=Party.objects.none(), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        accounts = Account.objects.filter(is_active=True, is_header=False)
        self.fields['debit_account'].queryset = accounts
        self.fields['credit_account'].queryset = accounts
        self.fields['debit_party'].queryset = Party.objects.filter(kind=Party.Kind.CUSTOMER)
        self.fields['credit_party'].queryset = Party.objects.filter(kind=Party.Kind.SUPPLIER)
        self.style_fields()

    def clean(self):
        data = super().clean()
        debit_account, credit_account = data.get('debit_account'), data.get('credit_account')
        debit_party, credit_party = data.get('debit_party'), data.get('credit_party')
        if debit_account and debit_account.code == '1140' and not debit_party:
            self.add_error('debit_party', 'Pilih pelanggan untuk akun 1140.')
        elif debit_account and debit_account.code != '1140' and debit_party:
            self.add_error('debit_party', 'Relasi pelanggan hanya untuk akun 1140.')
        if credit_account and credit_account.code == '2110' and not credit_party:
            self.add_error('credit_party', 'Pilih pemasok untuk akun 2110.')
        elif credit_account and credit_account.code != '2110' and credit_party:
            self.add_error('credit_party', 'Relasi pemasok hanya untuk akun 2110.')
        return data


from django.forms import formset_factory
JournalPairFormSet = formset_factory(
    JournalPairForm, extra=1, min_num=1, validate_min=True,
    can_delete=True, max_num=15, validate_max=True,
)


class AutoJournalForm(StyledMixin, forms.ModelForm):
    """Form jurnal umum: admin hanya mengisi keterangan dan satu nominal.

    Termin dan diskon bersifat opsional; akun debet/kredit tetap ditentukan otomatis.
    """
    inventory_kind = forms.ChoiceField(label='Transaksi persediaan', required=False, choices=[('', 'Bukan transaksi persediaan'), ('IN', 'Pembelian / barang masuk'), ('OUT', 'Penjualan / barang keluar')])
    inventory_product = forms.ModelChoiceField(label='Produk / lahan', queryset=Product.objects.none(), required=False)
    inventory_quantity = forms.DecimalField(label='Kuantitas (kg)', required=False, max_digits=18, decimal_places=3, min_value=Decimal('0.001'))
    inventory_unit_price = forms.DecimalField(label='Harga beli per kg (untuk pembelian)', required=False, max_digits=20, decimal_places=2, min_value=Decimal('0.01'))
    settlement_account = forms.ModelChoiceField(label='Kas / Bank / Piutang / Utang', queryset=Account.objects.none(), required=False)
    inventory_party = forms.ModelChoiceField(label='Pelanggan (penjualan kredit)', queryset=Party.objects.none(), required=False)
    amount = forms.DecimalField(
        label='Nominal transaksi (Rp)', max_digits=20, decimal_places=2, required=False,
        min_value=Decimal('0.01'),
        widget=forms.NumberInput(attrs={
            'step': '0.01', 'min': '0.01', 'placeholder': 'Masukkan nominal transaksi'
        })
    )
    term_text = forms.CharField(
        label='Termin pembayaran', required=False, max_length=30,
        widget=forms.TextInput(attrs={'list': 'termSuggestions', 'placeholder': 'Contoh: 2/10,n/30',
                                      'autocomplete': 'off'}),
        help_text='n/30 = jatuh tempo 30 hari. 2/10,n/30 = diskon 2% bila lunas ≤10 hari, jatuh tempo 30 hari.')
    discount_percent = forms.DecimalField(
        label='Diskon (%)', required=False, min_value=Decimal('0'), max_value=Decimal('99.99'),
        max_digits=5, decimal_places=2,
        widget=forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'placeholder': '0'}))
    use_term_discount = forms.BooleanField(
        label='Lunas dalam masa diskon termin (pakai potongan dari termin)', required=False)

    class Meta:
        model = JournalEntry
        fields = ('date', 'description')
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'description': forms.Textarea(attrs={
                'rows': 3,
                'placeholder': 'Contoh: Pembelian pupuk tunai / Penjualan hasil tani kredit'
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.initial.get('date'):
            self.initial['date'] = timezone.localdate()
        self.fields['inventory_product'].queryset = Product.objects.filter(is_active=True)
        self.fields['settlement_account'].queryset = Account.objects.filter(is_active=True, is_header=False)
        self.fields['inventory_party'].queryset = Party.objects.filter(kind=Party.Kind.CUSTOMER)
        self.style_fields()

    def clean(self):
        data = super().clean()
        # Otomatis masuk alur persediaan HANYA jika produk master dan kuantitas
        # disebutkan. Kata "pembelian/penjualan" saja tidak cukup: bisa berupa
        # beban, aset tetap, jasa, perlengkapan, dan transaksi nonpersediaan lain.
        description = (data.get('description') or '').casefold()
        sale_words = ('penjualan', 'jual ', 'menjual')
        purchase_words = ('pembelian', 'beli ', 'membeli')
        detected_kind = ('OUT' if any(w in description for w in sale_words) else
                         'IN' if any(w in description for w in purchase_words) else '')
        if detected_kind and not data.get('inventory_kind'):
            products = list(Product.objects.filter(is_active=True))
            matched = []
            for product in products:
                aliases = [product.name.casefold(), product.code.casefold()]
                m = re.search(r'lahan\s*(\d+)', product.name.casefold())
                if m:
                    aliases.append('lahan ' + m.group(1))
                if any(re.search(r'(?<![a-z0-9])' + re.escape(a) + r'(?![a-z0-9])', description)
                       for a in aliases):
                    matched.append(product)
            qty_match = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:kg|kilogram|gram|gr|ton|unit|pcs|karung|liter|ltr|dus|pack|pak|sak|batang|ekor|ikat|box|buah)\b', description)
            # Nama produk saja tidak cukup untuk memaksa mutasi stok. Misalnya
            # pupuk/bibit pada master produk dapat dibeli untuk dipakai sendiri.
            # Mutasi FIFO otomatis hanya dibuat bila produk DAN kuantitas disebut.
            stock_intent = any(w in description for w in ('persediaan', 'stok', 'barang dagang', 'untuk dijual', 'hasil tani', 'hasil panen'))
            own_use_input = any(w in description for w in ('pupuk', 'pestisida', 'herbisida', 'insektisida', 'bibit untuk tanam', 'benih untuk tanam'))
            if len(matched) > 1 and qty_match and (stock_intent or not own_use_input):
                self.add_error('description', 'Nama produk pada keterangan tidak unik. Gunakan kode produk yang tepat.')
            elif len(matched) == 1 and qty_match and (stock_intent or not own_use_input):
                product = matched[0]
                quantity = Decimal(qty_match.group(1).replace(',', '.'))
                data['inventory_kind'] = detected_kind
                data['inventory_product'] = product
                data['inventory_quantity'] = quantity
                if any(w in description for w in ('kredit', 'tempo', 'utang', 'hutang')):
                    settle_code = '1140' if detected_kind == 'OUT' else '2110'
                elif any(w in description for w in ('transfer', 'bank', 'rekening', 'qris', 'giro', 'mobile banking')):
                    settle_code = '1110'
                elif any(w in description for w in ('tunai', 'cash', 'kas', 'kontan', 'uang tunai')):
                    settle_code = '1120'
                else:
                    self.add_error('description', 'Tambahkan cara pembayaran: tunai, transfer/bank, atau kredit.')
                    settle_code = None
                if settle_code:
                    data['settlement_account'] = Account.objects.filter(
                        code=settle_code, is_active=True, is_header=False).first()
                    if not data['settlement_account']:
                        self.add_error('description', f'Akun pembayaran {settle_code} belum tersedia.')
                if detected_kind == 'OUT':
                    data['inventory_unit_price'] = product.sale_price
                else:
                    total = data.get('amount')
                    if total and quantity > 0:
                        data['inventory_unit_price'] = (total / quantity).quantize(Decimal('0.01'))
                    else:
                        self.add_error('amount', 'Untuk pembelian persediaan, isi total nilai pembelian agar harga per unit dapat dihitung.')
                if settle_code in ('1140', '2110'):
                    party_kind = Party.Kind.CUSTOMER if settle_code == '1140' else Party.Kind.SUPPLIER
                    try:
                        from .journal_rules import _party_for
                        data['inventory_party'] = _party_for(party_kind, description)
                    except ValidationError as exc:
                        self.add_error('description', exc)
        stock_kind = data.get('inventory_kind')
        if stock_kind:
            if not data.get('inventory_product'):
                self.add_error('inventory_product', 'Pilih produk/lahan.')
            if not data.get('inventory_quantity'):
                self.add_error('inventory_quantity', 'Masukkan kuantitas dalam kg.')
            if not data.get('settlement_account'):
                self.add_error('settlement_account', 'Pilih akun kas/bank atau piutang/utang.')
            if stock_kind == 'IN' and not data.get('inventory_unit_price'):
                self.add_error('inventory_unit_price', 'Harga beli per kg wajib diisi untuk pembelian.')
            if stock_kind == 'OUT' and data.get('inventory_product') and data['inventory_product'].sale_price <= 0:
                self.add_error('inventory_product', 'Harga jual produk belum diatur pada Master Produk.')
            account = data.get('settlement_account')
            party = data.get('inventory_party')
            if account and account.code == '1140' and (not party or party.kind != Party.Kind.CUSTOMER):
                self.add_error('inventory_party', 'Pilih pelanggan untuk penjualan kredit (Piutang).')
            elif account and account.code == '2110' and (not party or party.kind != Party.Kind.SUPPLIER):
                self.add_error('inventory_party', 'Pilih pemasok untuk pembelian kredit (Utang).')
            elif account and account.code not in ('1140', '2110') and party:
                self.add_error('inventory_party', 'Relasi hanya diisi untuk transaksi piutang/utang.')
        elif not data.get('amount'):
            self.add_error('amount', 'Nominal transaksi wajib diisi.')
        if data.get('inventory_kind') == 'OUT' and any(w in description for w in ('kredit', 'tempo', 'piutang')) and not data.get('inventory_party'):
            self.add_error('inventory_party', 'Pilih pelanggan yang membeli secara kredit agar piutang tercatat pada buku pembantu pelanggan.')
        return data
